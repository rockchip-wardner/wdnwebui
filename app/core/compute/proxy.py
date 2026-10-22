"""RPC session assembly for the main process.

The transport itself is **not** here. Inference happens in a
``compute_worker`` subprocess; both halves of that conversation live in
``wdndds.ai_session``:

    worker side    serve_local_session(session, service_name=…, extra_ops=…)
    client side    LLMRPCSession(model=spec, service_name=…)

``LLMRPCSession`` is a ``wdnai.LLMSession`` subclass, so every consumer
in this repo gets a real session object: ``respond()`` returns a parsed
``generating`` instance (or the raw ``str`` when validation fails), and
``embed_text`` / ``embed_video`` / ``reset`` / ``close`` behave the same
whether the model is in this process or on another board.

What used to live in this file and is deliberately *not* here:

* **Schema parsing and tool dispatch.** The worker returns the model's raw
  text and the caller parses it into ``generating``. There is no
  ``result_data`` wire field, no "reconstruct the class by name on the far
  side" step, and therefore no drift between what the worker believes a
  schema looks like and what the caller does with it.
* **Reply-side ``/dev/shm`` routing.** ``serve_local_session`` already
  ships a long reply through tmpfs when it would otherwise hit the cshim
  1 MiB ceiling, and reads it back here in the transport. This also
  removed the old LAN-peer degradation, where inline fields were dropped
  outright because a remote worker cannot read our ``/dev/shm``.
* **Request-side attachment encoding.** Same story: ``encode_attachments``
  picks path-list vs JPEG+base64 from ``is_remote``.

What *is* still here is the wdnwebui policy that the transport has no
opinion about: choosing timeouts, and deciding what to do when a worker
cannot answer a question we need.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, List, Optional

from wdndds import LLMRPCSession

LOG = logging.getLogger("compute_proxy")

# Generation budget for one respond(). Generous on purpose: a VLM looking
# at a 768px letterboxed frame runs ~35s per call, and the point of the
# timeout is to catch a *wedged* worker, not to race a slow one. The
# transport retries a structural wedge once with a fresh requester.
_INFER_TIMEOUT_MS = 120_000

# Control-plane verbs (``info`` / ``load`` / ``unload`` / ``count_tokens``)
# answer in milliseconds and must not inherit the inference budget, or a
# wedged inference would make the UI's poll loop look broken too.
_CONTROL_TIMEOUT_MS = 15_000


# --------------------------------------------------------------------------- #
# Thinking-mode arbitration (shared worker, per-call toggle)
# --------------------------------------------------------------------------- #
# ``enable_thinking`` is a per-call field on ``session_run``, but the RKNN
# session latches thinking **on** once it has been enabled: a later
# ``enable_thinking=False`` is ignored until the KV cache is cleared *and*
# the chat template is re-applied (measured on Qwen3.5-4B; ``reset()``
# alone is a no-op). The worker's ``set_thinking_mode`` op owns that state
# so every caller — text chat, video description, USB-HID agent, WeChat —
# runs in the mode it asked for, even though they share one session.
#
# The lock is per worker, not global: different models are independent and
# must not serialise against each other. It surrounds both the mode switch
# and the ``respond`` call, so within this process no other caller can flip
# the mode in between. (A LAN peer adopting the same worker is outside this
# lock; see AGENTS.md — single-board deployments are unaffected.)
_THINKING_LOCKS: dict[str, threading.RLock] = {}
_THINKING_LOCKS_GUARD = threading.Lock()
_THINKING_SUPPORTED: dict[str, bool] = {}


def _thinking_lock(service_name: str) -> threading.RLock:
    with _THINKING_LOCKS_GUARD:
        lock = _THINKING_LOCKS.get(service_name)
        if lock is None:
            lock = threading.RLock()
            _THINKING_LOCKS[service_name] = lock
        return lock


def _wrap_thinking_mode(sess: Any, service_name: str) -> None:
    """Tell the worker the mode the current caller expects before ``respond``.

    ``set_thinking_mode`` is idempotent: worker-side state is the single
    source of truth, and it only does the reset + template reapply when the
    requested mode actually differs. That is what makes the toggle "only
    acts on change" while still being correct if another board flipped the
    session in the meantime.
    """
    orig_respond = sess.respond
    lock = _thinking_lock(service_name)

    async def respond(prompt: Any = None, *args: Any, **kwargs: Any) -> Any:
        desired = bool(kwargs.get("enable_thinking", False))
        with lock:
            # A worker that predates the op answers "unknown op". Remember
            # that (unless the caller needs ON, where a retry may succeed
            # after an upgrade) so non-thinking models pay at most one
            # failed control call per process.
            if _THINKING_SUPPORTED.get(service_name) is not False or desired:
                try:
                    sess.call_op(
                        "set_thinking_mode",
                        {"enable_thinking": desired},
                        timeout_ms=_CONTROL_TIMEOUT_MS,
                    )
                    _THINKING_SUPPORTED[service_name] = True
                except Exception as e:  # noqa: BLE001
                    if "unknown op" in str(e).lower():
                        _THINKING_SUPPORTED[service_name] = False
                    LOG.debug("set_thinking_mode failed (%s) — continuing", e)
            return await orig_respond(prompt, *args, **kwargs)

    sess.respond = respond


def make_rpc_session(
    *,
    model: Any,
    model_id: str,
    service_name: str,
    hub: Any,
    requester: Any = None,
    side_hub: Any = None,
    _svc: Any = None,
    is_remote: bool = False,
) -> LLMRPCSession:
    """Build the main-process handle for one compute worker.

    Reuses the registry's existing ``Hub`` / ``Service`` / ``Requester``
    rather than standing up its own participant: the registry already
    owns them for boot probes and lifecycle, and a second participant
    would double the cshim payload-pool RSS for no benefit.

    ``side_hub`` is the one participant every session in this process
    shares — see ``ComputeRegistry._side_hub`` for why it must not be
    per-instance.

    ``LLMRPCSession`` adds one side channel so a 35 s inference on the
    main pump cannot delay or block stream deltas or stop flags.
    """
    sess = LLMRPCSession(
        model,
        service_name=service_name,
        hub=hub,
        requester=requester,
        side_hub=side_hub,
        _svc=_svc,
        is_remote=is_remote,
        infer_timeout_ms=_INFER_TIMEOUT_MS,
        timeout_ms=_CONTROL_TIMEOUT_MS,
    )
    # The registry addresses instances by model_id; the session only knows
    # its spec. Attach it so diagnostics and logs can name the model
    # without threading the id through every call site.
    sess.model_id = model_id
    # Make ``enable_thinking`` behave as a per-call toggle even though the C
    # session latches it on. See the block above.
    _wrap_thinking_mode(sess, service_name)
    return sess


def count_tokens(sess: Optional[Any], texts: List[str]) -> List[int]:
    """Ask the worker for exact token counts, degrading rather than failing.

    Chat history trimming needs the model's own tokenizer, and the model
    is in another process. The worker exposes ``count_tokens`` as an extra
    op — a round trip of microseconds, no inference, no NPU.

    If the worker is unreachable or predates the op, fall back to
    ``len(text) // 2``. That estimate is bad for Chinese (it under-counts
    by ~3x), so callers treat the fallback as "trim harder than strictly
    necessary" rather than as a hard budget.
    """
    if not texts:
        return []
    fallback = [max(1, len(t) // 2) if t else 0 for t in texts]
    if sess is None:
        return fallback
    try:
        out = sess.call_op("count_tokens", {"texts": texts},
                           timeout_ms=_CONTROL_TIMEOUT_MS)
    except Exception as e:  # noqa: BLE001 — a bad count must not fail the turn
        LOG.debug("count_tokens unavailable (%s) — using length estimate", e)
        return fallback
    counts = (out or {}).get("counts")
    if not isinstance(counts, list) or len(counts) != len(texts):
        LOG.debug("count_tokens returned %r — using length estimate", out)
        return fallback
    out_int: List[int] = []
    for c in counts:
        try:
            out_int.append(int(c))
        except (TypeError, ValueError):
            out_int.append(0)
    return out_int