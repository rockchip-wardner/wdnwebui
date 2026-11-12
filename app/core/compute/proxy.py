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

from app.core.thinking import effective_thinking

LOG = logging.getLogger("compute_proxy")

# Generation budget for one respond(). Generous on purpose: a VLM looking
# at a 768px letterboxed frame runs ~35s per call, and the point of the
# timeout is to catch a *wedged* worker, not to race a slow one. The
# transport retries a structural wedge once with a fresh requester.
#
# This is the **default**; a model that takes longer declares its own
# budget in its manifest (``infer_timeout_s``) and gets
# ``that + _INFER_TIMEOUT_MARGIN_S`` instead — see
# :func:`_infer_timeout_ms_for`.
_INFER_TIMEOUT_MS = 120_000

#: How much **longer** than the worker's own bound the client's RPC budget
#: is. The client must outlast the worker: a worker that gives up replies
#: "inference exceeded Ns" *with a reason*, while a client that gives up
#: first produces an opaque timeout with no diagnosis at all.
_INFER_TIMEOUT_MARGIN_S = 30.0

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

# --------------------------------------------------------------------------- #
# Inference-timeout recovery
# --------------------------------------------------------------------------- #
# A worker that blows its inference budget does **not** interrupt the
# inference: the inference thread is a daemon and is simply abandoned, so
# the NPU slot stays busy until the driver returns on its own. The worker
# says so itself — see the trade-off note in
# ``app/core/compute/workers/_base.py`` (``_serve_one``): "a timeout
# therefore means 'this worker needs restarting', not 'retry this call'".
#
# That sentence described the intended recovery, but nothing implemented
# it, so one runaway generation wedged the card **for good**: every later
# request queued behind the abandoned thread and died with the same
# timeout, and the user saw "推理错误" over and over until they restarted
# the instance by hand. This is the missing half — restart the worker so
# the feature heals by itself.
#
# The restart runs in a background thread: unloading and reloading a model
# takes ~50 s on this board, and the caller that just got the timeout has
# to return *now*.
_RECOVERY_LOCKS: dict[str, threading.Lock] = {}
_RECOVERY_LOCKS_GUARD = threading.Lock()


def _recovery_lock(model_id: str) -> threading.Lock:
    with _RECOVERY_LOCKS_GUARD:
        lock = _RECOVERY_LOCKS.get(model_id)
        if lock is None:
            lock = threading.Lock()
            _RECOVERY_LOCKS[model_id] = lock
        return lock


def _is_infer_timeout(exc: BaseException) -> bool:
    """Is this the worker's own "inference exceeded Ns" reply?

    The worker replies with a *reason* (``TimeoutError: inference exceeded
    60.0s``) instead of letting the client time out blind, so the string
    is the only marker available on this side of the RPC boundary.
    """
    return "inference exceeded" in str(exc)


def _schedule_recovery(model_id: str) -> None:
    """Restart the wedged worker — once per model, in the background.

    Several callers can hit the same wedged worker at once (the UI polls,
    the agent retries); without the lock every one of them would start a
    restart, and the NPU would be fought over by several spawns.
    """
    lock = _recovery_lock(model_id)
    if not lock.acquire(blocking=False):
        return          # a recovery for this model is already on its way
    try:
        def _run() -> None:
            try:
                from app.core.compute.registry import get_registry
                LOG.warning(
                    "compute: inference timeout on %s — restarting the worker",
                    model_id)
                get_registry().recover_wedged_instance(model_id)
                LOG.warning(
                    "compute: worker %s restarted after an inference timeout",
                    model_id)
            except Exception:  # noqa: BLE001 — a failed recovery must not
                # kill the caller's thread; the next request tries again.
                LOG.exception(
                    "compute: worker restart after an inference timeout failed")
            finally:
                lock.release()

        threading.Thread(target=_run, name=f"wdn-recover-{model_id}",
                         daemon=True).start()
    except Exception:  # noqa: BLE001
        lock.release()
        raise


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
        # ⚠️ **思考模式在这块板子上全局关闭**（2026-11-12 的决定）。
        #
        # Qwen3.5（4B 和 9B 都一样）开思考有概率陷入**死循环**：贪心解码
        # 一路复读、不吐 EOS，把 worker 的推理预算烧光；更糟的是 C 侧
        # thinking 是「开了就锁住」的（见本文件上面那段说明），掉进去
        # 很难自己出来。一次这样的跑飞会把 NPU 占住，后面每个请求都跟着
        # 超时 —— 现场表现就是「电脑控制老是推理错误」。
        #
        # 所以这里**忽略调用方要什么**，一律按非思考模式跑。这是应用里
        # 所有推理请求的**唯一咽喉**（文本对话、电脑控制、音视频描述、
        # 微信桥接都要从这里过），总闸放在这里，才不会有漏网的调用方。
        #
        # 保留 ``set_thinking_mode`` 这套机制本身：它是「C 会话一旦开启
        # 就锁死」的唯一解药 —— worker 起来时 9B 的 C 会话**默认就是
        # thinking 模式**，总闸的第一次调用会把它切回非思考（reset +
        # 重设模板）。以后要重新开放思考，把 ``app/core/thinking.py`` 的
        # ``THINKING_ENABLED`` 改回 True 即可，别的文件都不用动。
        desired = effective_thinking(kwargs.get("enable_thinking", False))
        # ⚠️ **per-call 覆盖值也必须一起压掉** —— 它是总闸的**旁路**。
        #
        # ``enable_thinking`` 随 RPC payload 的 ``options`` 一路透传到后端
        # （options → run_prompt(**options) → backend.run_images(…,
        # **overrides) → _build_prompt(enable_thinking=…)），后果有两处：
        #
        #   1. prompt 尾巴会挂上 ``<think>\n``（非思考模式应该是
        #      ``<think>\n\n</think>\n\n``）—— 模型被重新拉回思考模式；
        #   2. C 层 session_run 的输入标志也被置上。
        #
        # 只挡 ``set_thinking_mode`` 这个**控制调用**是不够的：调用方只要
        # 传了 ``enable_thinking=True``，总闸就被从后门打开了。这正是
        # 「worker 日志显示 thinking mode -> False、但回答里仍然有思考
        # 内容」的原因 —— 两处标志不是一回事，必须一起压。
        kwargs["enable_thinking"] = desired
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
                    else:
                        # ⚠️ **不能静默**。思考模式的切换失败 = C 会话保持
                        # 原模式（9B 起来时就是 thinking 模式），而 thinking
                        # 正是会死循环的那个模式 —— 现场表现是「模型随机卡住」，
                        # 没有任何线索指向这里。这条曾经是 DEBUG 级、什么痕
                        # 迹都不留，排查了很久。
                        LOG.warning(
                            "set_thinking_mode failed (%s) — the worker keeps "
                            "its current C-side mode", e)
            return await orig_respond(prompt, *args, **kwargs)

    sess.respond = respond


def _wrap_infer_timeout_recovery(sess: Any, model_id: str) -> None:
    """Restart the worker when an inference blows its budget.

    Wrapped **around** the thinking-mode wrapper, so it also sees the
    failures that the mode switch itself produces. The error is re-raised
    unchanged — the caller still gets the worker's explanation ("inference
    exceeded 60.0s"), the restart is a side effect, not a replacement.
    """
    orig_respond = sess.respond

    async def respond(prompt: Any = None, *args: Any, **kwargs: Any) -> Any:
        try:
            return await orig_respond(prompt, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001 — re-raised below
            if _is_infer_timeout(exc):
                _schedule_recovery(model_id)
            raise

    sess.respond = respond


def _infer_timeout_ms_for(model_id: str) -> int:
    """Client-side inference budget for ``model_id``, in milliseconds.

    The worker publishes its own bound in the model manifest
    (``infer_timeout_s``); the client budget is that plus
    :data:`_INFER_TIMEOUT_MARGIN_S`. Both processes read the **same
    manifest file**, so the worker's bound and the client's budget can
    never drift apart — a drift would show up as an opaque client-side
    timeout instead of the worker's explanatory reply.

    Models without the field keep the global default.
    """
    worker_s = 0.0
    try:
        from app.core.compute.workers import manifest_infer_timeout_s
        worker_s = manifest_infer_timeout_s(model_id)
    except Exception:  # noqa: BLE001 — a missing manifest must not break RPC
        worker_s = 0.0
    if worker_s <= 0:
        return _INFER_TIMEOUT_MS
    return int(round((worker_s + _INFER_TIMEOUT_MARGIN_S) * 1000))


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
        infer_timeout_ms=_infer_timeout_ms_for(model_id),
        timeout_ms=_CONTROL_TIMEOUT_MS,
    )
    # The registry addresses instances by model_id; the session only knows
    # its spec. Attach it so diagnostics and logs can name the model
    # without threading the id through every call site.
    sess.model_id = model_id
    # Make ``enable_thinking`` behave as a per-call toggle even though the C
    # session latches it on. See the block above.
    _wrap_thinking_mode(sess, service_name)
    # A worker that blows its inference budget keeps the NPU busy (the
    # inference thread is abandoned, not interrupted) — restart it so one
    # runaway generation cannot wedge the card for good.
    _wrap_infer_timeout_recovery(sess, model_id)
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