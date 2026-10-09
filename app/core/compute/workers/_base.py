"""Shared worker runtime — wdndds cshim RPC server, one process per model.

Spawned by ``ComputeRegistry._spawn_compute_worker`` via
``subprocess.Popen``. Each worker holds:

  * its own DomainParticipant (via wdndds Hub)
  * one RPC service endpoint (request topic + reply topic)

This module is the model-agnostic half: process bring-up, comm naming,
logging, SHM helpers, the router and every RPC handler. Everything that
is specific to *one* model lives in its manifest — ``workers/<id>.json``,
which names both what to build and the ``spec_factory`` that builds it.
There is exactly one entry script for all of them.

Model-specific *behaviour* belongs in wdnai's ``LLMLocalSession`` / backend
layer, not in a manifest and not here. A manifest says which model; it
never says how to behave.

Nothing in here may import ``app.apps.*`` / ``app.system.*`` (zero-coupling
rule, same as cards), and it deliberately does **not** import
``app.core.compute.registry`` either: the registry spawns workers, so an
import back into it would close the cycle. A worker learns everything it
needs about itself from its own manifest.

Wire protocol
-------------
A request is one JSON object published to the request topic; a
reply is one JSON object published to the reply topic. The wdndds
cshim layer wraps this in a ``{wdndds_reply_suffix, data}`` envelope
so the client can match replies to in-flight calls.

Lifecycle (v0.7):
  1. Worker boots, brings up Hub + Service + Replier, registers
     discovery on the LAN daemon. Does **not** instantiate LLMSession.
  2. State = ``not_loaded``. ``info`` RPC reports this so the main
     process's compute-config page can show "未启用".
  3. User selects NPU on the compute-config page → main process sends
     RPC ``{"action":"load","npu_index":N}`` → worker instantiates
     ``LLMSession(model=spec, npu=N)`` → state ``loading`` → ``running``.
  4. ``{"action":"unload"}`` releases the LLMSession handle but keeps
     the worker process alive (returns to ``not_loaded``).
  5. ``{"action":"shutdown"}`` sets ``_WORKER_STOP_EVENT``; main loop
     exits; process self-terminates. After a 3 s grace period the
     caller may ``kill -9`` as a fallback.
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import importlib
import json
import logging
import os
import re
import signal
import socket
import sys
import threading
import time
import uuid
from typing import Any, Optional, Tuple

LOG = logging.getLogger("compute_worker")


# --------------------------------------------------------------------------- #
# Response-side /dev/shm path-list: GONE, and no replacement needed here
# --------------------------------------------------------------------------- #
# The worker used to route heavy reply payloads (crop JPEG, result_data
# JSON, long text, structured SceneFrame) through /dev/shm and ship only a
# ~50-byte path, because the ComputeReply IDL's string fields get truncated
# by FastCDR around 255 bytes.
#
# That is now wdndds's problem, solved once: ``serve_local_session`` /
# ``LLMRPCSession`` ship attachments *and* long replies as /dev/shm
# descriptors on both sides of the wire. This worker only ever hands the
# transport a raw model string; the transport decides whether that string
# rides inline or via tmpfs.
#
# It is also strictly better than what it replaces: the old scheme clipped
# every field at 255 bytes for LAN peers (``is_remote=True``) and dropped
# the large ones entirely, which is why VFA had a "parse failure"
# placeholder path at all. There is no LAN-peer downgrade here.


# Orphan-worker SIGPIPE pre-arm. compute_worker is spawned with
# stdout/stderr=PIPE so the main process can tail + forward logs via
# _drain_worker_stderr; when the original parent dies (uvicorn reload,
# kill-stale reaping, unclean shutdown) the kernel closes the parent's
# read-end of the pipes but our write-end stays open — every subsequent
# stderr write then raises BrokenPipeError on the Python side AND
# triggers the default SIGPIPE termination on the C side. Either one
# flips state to {"status":"error","init_error":"BrokenPipeError: ..."},
# which the new run.py's adoption probe then surfaces as a warning.
# Two pieces of pre-arming so we stay cleanly adoptable across restarts.
try:
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)
except (AttributeError, ValueError):
    pass


class _BrokenPipeSafeStderr:
    """Forward to the real sys.stderr but swallow BrokenPipeError on write/flush."""

    __slots__ = ("_real",)

    def __init__(self, real):
        self._real = real

    def write(self, data):
        try:
            return self._real.write(data)
        except BrokenPipeError:
            return len(data) if data else 0

    def flush(self):
        try:
            self._real.flush()
        except BrokenPipeError:
            pass

    def __getattr__(self, name):
        return getattr(self._real, name)


if not isinstance(sys.stderr, _BrokenPipeSafeStderr):
    sys.stderr = _BrokenPipeSafeStderr(sys.stderr)


_EMBED_TEXT_TIMEOUT = 5.0
_EMBED_VIDEO_TIMEOUT = 5.0

# How long a single ``respond`` may hold the replier pump before the
# worker gives up and answers with an error. Must stay comfortably below
# the main process's inference budget (``proxy._INFER_TIMEOUT_MS``) so
# the failure surfaces as a worker reply with a reason attached rather
# than as an opaque client-side timeout.
_INFER_TIMEOUT_SEC = 60.0


_PR_SET_NAME = 15
_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
_libc.prctl.restype = ctypes.c_int
_libc.prctl.argtypes = [
    ctypes.c_int,                       # option
    ctypes.c_char_p,                    # arg2
    ctypes.c_ulong, ctypes.c_ulong,     # arg3, arg4
    ctypes.c_ulong,                     # arg5
]


def _set_comm_name(name: str) -> None:
    """Set this process's comm (visible in ``ps -c`` / ``top`` /
    ``/proc/<pid>/comm``) via ``prctl(PR_SET_NAME)``.

    The name is truncated to 15 bytes (TASK_COMM_LEN - 1). The main
    process's :mod:`app.services.compute_registry` dedup walks
    ``/proc/*/comm`` looking for ``worker:<alias>`` to find us, so
    setting the name early (before we open DDS endpoints) is what
    makes "no duplicate workers after reload" actually work.

    Why not preexec_fn? ``prctl(PR_SET_NAME)`` in preexec_fn is
    immediately overwritten by the subsequent ``execvp()`` (the new
    argv[0] basename becomes the new comm). Setting it here, after
    Python has already started, sticks.
    """
    name_bytes = name.encode("utf-8")[:15]
    try:
        _libc.prctl(_PR_SET_NAME, name_bytes, 0, 0, 0)
    except Exception as e:  # noqa: BLE001
        LOG.warning("set comm name to %r failed: %s", name, e)


_WORKER_PID: int = os.getpid()


def _read_local_ip() -> str:
    """Best-effort: return the primary outbound IPv4 (for the discovery
    layer to display alongside device_name). Opens a UDP socket to a
    public IP without sending any packet, then reads the local endpoint.
    Returns ``""`` on any failure (no IPv4 default route, etc.) so the
    field is never the cause of a probe failure."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        return ""


_WORKER_IP: str = _read_local_ip()


def _read_device_name() -> str:
    """Resolve ``device_name`` from ``config.jsonc["base"]``.

    Used as the FastDDS participant name suffix so LAN peers can
    identify which board each worker is running on. Falls back to the
    host's ``socket.gethostname()`` when the field is missing or
    ``config.jsonc`` is unreachable (workers running before the main
    process, or in a stripped-down test environment).
    """
    try:
        from app.core.config import get_config
        base = get_config().get_section("base") or {}
        v = (base.get("device_name") or "").strip()
        if v:
            return v
    except Exception:  # noqa: BLE001
        pass
    return socket.gethostname()


def _setup_logging(debug: bool) -> None:
    h = logging.StreamHandler(stream=sys.stderr)
    h.setFormatter(
        logging.Formatter("[compute_worker:%(process)d] %(asctime)s %(levelname)s %(message)s")
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(h)
    root.setLevel(logging.DEBUG if debug else logging.INFO)


# Module-level stop event. ``main()`` initialises it and ``router``
# (specifically the ``shutdown`` action) sets it. The main thread
# waits on it and exits when set.
_WORKER_STOP_EVENT: threading.Event = threading.Event()


# LAN TTS 音频分片 DDS topic writer（main() 里 init，finally 里 close）。
# None = DDS init 失败，TTS 链路退回 WAV-only。
_AUDIO_CHUNK_WRITER: Any = None


# --------------------------------------------------------------------------- #
# wdnai lazy-import + session builder                                          #
# --------------------------------------------------------------------------- #
_WDNAI = None

#: Absolute path of ``workers/`` — every manifest sits next to this module
#: as ``<model_id>.json``.
HERE = os.path.dirname(os.path.abspath(__file__))


def load_manifest(model_id: str) -> dict:
    """Read ``workers/<model_id>.json``.

    Resolved from the model id rather than a caller-supplied folder:
    every model now has exactly one manifest file, so the id *is* the
    path. Read from this package rather than imported from the registry —
    the registry spawns workers, so importing back into it would close
    the cycle.
    """
    import json as _json
    path = os.path.join(HERE, f"{model_id}.json")
    try:
        with open(path, encoding="utf-8") as f:
            return _json.load(f)
    except FileNotFoundError:
        raise RuntimeError(
            f"compute_worker: no manifest for model {model_id!r} "
            f"(expected {path})") from None


def worker_comm_name(model_id: str) -> str:
    """Process comm name (``worker:<alias>``) for this worker.

    Computed from the manifest's ``worker_name`` rather than imported
    from the registry, for the same reason as :func:`load_manifest`.

    Linux truncates ``comm`` to 15 chars, so a longer alias gets a short
    numeric suffix instead of being silently cut (which would make two
    workers indistinguishable in ``/proc``).
    """
    alias = ""
    try:
        alias = (load_manifest(model_id).get("worker_name")
                 or model_id[:8]).strip()
    except Exception:  # noqa: BLE001 — manifest unreadable is not fatal here
        alias = model_id[:8]
    name = f"worker:{alias}"
    if len(name) > 15:
        name = f"{name[:12]}{int(time.time()) % 1000:03d}"
    return name


def _load_wdnai() -> dict:
    global _WDNAI
    if _WDNAI is not None:
        return _WDNAI
    try:
        from wdnai import (
            Attachment,
            AudioAttachment,
            ImageAttachment,
            LLMLocalSession,
            SystemLanguageModel,
            TextAttachment,
        )
    except ImportError as e:
        raise RuntimeError(f"wdnai import failed: {e}")
    # NOTE: no monkey-patching of the SDK's streaming internals here.
    # The old ``app.core.chat.stream.install_patch`` reached into wdnai's
    # result-callback chain to publish chat deltas over a side topic.
    # Streaming is now a first-class parameter of the session interface
    # (``respond(on_delta=…)``), so the worker needs no private knowledge
    # of how the backend produces tokens.
    _WDNAI = {
        "Attachment": Attachment,
        "AudioAttachment": AudioAttachment,
        "ImageAttachment": ImageAttachment,
        "TextAttachment": TextAttachment,
        "LLMLocalSession": LLMLocalSession,
        "SystemLanguageModel": SystemLanguageModel,
    }
    return _WDNAI


def _build_session(model_id: str, npu_index: int):
    """Construct the session for this worker using its manifest.

    ``spec_factory`` in ``workers/<model_id>.json`` names a method on
    ``wdnai.SystemLanguageModel``; that is the whole per-model knowledge
    the worker needs. Resolving it by name keeps model-specific behaviour
    out of this module entirely — and out of per-model Python files that
    would have to be kept in sync. Anything beyond "which model" belongs
    in wdnai's session/backend layer, where every caller gets it.

    A missing or misspelled factory surfaces here as a clear
    ``RuntimeError`` (the instance flips to ``error`` with this text)
    rather than a bare ``AttributeError`` from deep inside a loader.
    """
    manifest = load_manifest(model_id)
    factory_name = (manifest.get("spec_factory") or "").strip()
    if not factory_name:
        raise RuntimeError(
            f"worker {model_id!r}: manifest has no 'spec_factory' "
            f"(expected a wdnai.SystemLanguageModel method name)")
    mod = _load_wdnai()
    factory = getattr(mod["SystemLanguageModel"], factory_name, None)
    if factory is None:
        raise RuntimeError(
            f"worker {model_id!r}: wdnai.SystemLanguageModel has no "
            f"{factory_name!r} — check 'spec_factory' in "
            f"workers/{model_id}.json against the installed wdnai")
    spec = factory()
    LOG.info("building LLMLocalSession(model=%s, npu=%d) via %s) ...",
             model_id, npu_index, factory_name)
    t0 = time.time()
    # Not the deprecated ``LLMSession(model=…)`` shim: the worker is the
    # one place that genuinely means "inference runs right here".
    sess = mod["LLMLocalSession"](model=spec, npu=npu_index)
    LOG.info("LLMLocalSession ready (%.1fs)", time.time() - t0)
    return sess


def _resolve_available_image_sizes(sess) -> list:
    """Read vision-model native resolutions from the loaded session.

    Returns a sorted list of integers (square pixel sizes like
    ``[384]`` or ``[384, 768]``). Empty list when the model isn't
    vision-bearing (text-only / TTS) or when the session has no
    backend yet.

    The main process's probe thread calls this on every info tick so
    that wdnwebui's 音视频描述 page can populate its ``#vd-detect-size``
    dropdown dynamically — never hardcoded.
    """
    if sess is None:
        return []
    backend = getattr(sess, "_backend", None)
    if backend is None:
        return []
    fn = getattr(backend, "available_image_sizes", None)
    if not callable(fn):
        return []
    try:
        sizes = fn()
    except Exception as e:  # noqa: BLE001
        LOG.warning("available_image_sizes probe failed: %s", e)
        return []
    out: list = []
    src = sizes if isinstance(sizes, (list, tuple)) else []
    for s in src:
        try:
            out.append(int(s))
        except (TypeError, ValueError):
            continue
    return sorted(set(out))


# --------------------------------------------------------------------------- #
# Per-call handlers                                                          #
# --------------------------------------------------------------------------- #
def info_handler(state: dict, model_id: str,
                 startup_npu_index: int | None,
                 model_dir: str | None) -> dict:
    npu_index = state.get("npu_index")

    # Resource self-heal: recheck model_dir on every RPC. The
    # startup check at line 1076 only fires once; if the user
    # populates the directory later (or deletes it out from under
    # us) we want the next probe to converge without a worker
    # restart. Skip ``running`` / ``loading`` / ``error`` —
    # those are owned by the load path.
    cur_model_dir = state.get("model_dir")
    if cur_model_dir is not None:
        cur_status = state.get("status", "not_loaded")
        if os.path.isdir(cur_model_dir):
            if cur_status == "missing_model_dir":
                state["status"] = "not_loaded"
        else:
            if cur_status == "not_loaded":
                state["status"] = "missing_model_dir"

    return {
        "status":             state.get("status", "not_loaded"),
        "model":              model_id,
        "model_dir":          model_dir,
        "npu_index":          npu_index,
        "npu_index_startup":  startup_npu_index,
        "load_time_sec":      state.get("load_time_sec"),
        "init_error":         state.get("init_error", ""),
        "wdnai_available":     state.get("wdnai_available", False),
        # Native vision-model resolutions loaded into the session.
        # Empty until the worker reaches ``running`` for a vision
        # model (text-only / TTS models report ``[]``). The main
        # process caches this per ``ModelInstance`` and consumes it
        # in ``WdnaiDetector`` for backend ``_pick_letterbox_size``
        # (auto-pick when ``image_size is None``) and strict
        # validation when the caller pins a specific size. See
        # ``_resolve_available_image_sizes`` for the resolution
        # rules (single-element for legacy single-file installs;
        # ``[384, 768]`` when both variants are present).
        "available_image_sizes": _resolve_available_image_sizes(
            state.get("session")),
        # v0.8: provenance for the main process's boot probe — used
        # to distinguish a local-orphan worker (same hostname as the
        # main process) from a real LAN peer. Hostname is the
        # authoritative signal; ``device_name`` is the user-facing
        # label that shows up in the compute-config page's 网络发现
        # column as ``<model> @ <device_name>``.
        "pid":                _WORKER_PID,
        "hostname":           socket.gethostname(),
        "device_name":        _read_device_name(),
        # Best-effort outbound IPv4 — shown as ``<device> @ <ip>`` in
        # the network compute group header. Empty if the host has no
        # default IPv4 route (offline / init failure).
        "peer_ip":            _WORKER_IP,
    }


def load_handler(state: dict, model_id: str,
                 payload_npu_index: Any) -> dict:
    npu_index = payload_npu_index
    if npu_index is None:
        npu_index = state.get("npu_index")
    if not isinstance(npu_index, int) or npu_index < 0:
        return {
            "status": "error",
            "error": "npu_index 缺失：load action 必须传 npu_index",
        }
    # 资源预检：model_dir 不存在就直接报 missing_model_dir，不再去
    # 撞 rknn（撞了也只是把 FileNotFoundError 当 init_error 报上去，
    # 误诊为"启动失败"）。恢复路径：用户补完目录后再调一次 load，
    # 这里的预检会重新走通。
    model_dir = state.get("model_dir")
    if model_dir is not None and not os.path.isdir(model_dir):
        return {
            "status": "missing_model_dir",
            "npu_index": npu_index,
            "model_dir": model_dir,
            "error": f"模型目录不存在: {model_dir}",
        }
    # If already running on the same NPU, idempotent.
    if (state.get("status") == "running"
            and state.get("npu_index") == npu_index
            and state.get("session") is not None):
        return {
            "status": "running",
            "npu_index": npu_index,
            "load_time_sec": state.get("load_time_sec"),
            "loaded_at": state.get("loaded_at"),
        }
    # If previously bound to a different NPU, drop the old session first.
    if state.get("session") is not None:
        try:
            state["session"].close()
        except Exception:  # noqa: BLE001
            pass
        state["session"] = None
    state["status"] = "loading"
    state["npu_index"] = npu_index
    state["init_error"] = ""
    state["loaded_at"] = None
    state["load_time_sec"] = None

    # Init 在**独立 Python 线程**里跑，不在 cshim Replier pump 线程上。
    #
    # Why: cshim 的 C++ pump 线程（Replier._on_request_c 触发）跑同步
    # Python 调用栈 router → load_handler → _build_session。实测 NPU
    # #1 上 qwen3-vl-4b init 需要 28.6s。在这 28.6s 内 pump 线程持
    # cshim 内部的 endpoint / payload pool / FastDDS 线程同步资源，
    # rknn3lite / wdnai C 端的 init 想抢同一组 fd / driver handle →
    # 死锁，进程卡在 futex_wait_queue 永远不出。stderr 也写不出
    # "building LLMSession" 那条 log（C 端卡住前 Python logging 还没
    # flush 到 pipe）。
    #
    # Fix: pump 线程立即返回 {"status": "loading"} + npu_index，
    # 后台线程跑 _build_session；完成时把 state["status"] 翻 running
    # 或 error（info RPC 会自动看到）。主进程 _probe_instance 1Hz 探测
    # info RPC，按 loading → running / error 状态机收敛。
    #
    # 重新 bind 不同 NPU 时：上一轮 init 线程可能还在跑 —— 用
    # state["_init_token"] 标记当前轮次，旧线程完成时发现 token 不匹配
    # 就丢弃结果（session 不写 state），避免覆盖新轮的 state。
    import threading as _threading
    token = state.get("_init_token", 0) + 1
    state["_init_token"] = token
    state["_init_started_at"] = time.time()

    def _do_init():
        try:
            t0 = time.time()
            LOG.info("building LLMSession(model=%s, npu=%d) ...",
                     model_id, npu_index)
            sess = _build_session(model_id, npu_index)
            load_sec = round(time.time() - t0, 2)
            # 轮次校验：旧轮的 init 完成后才到，看到 token 不一致
            # 就丢弃结果（新的 load 已经在用 state 了）。
            if state.get("_init_token") != token:
                LOG.info("load %s @ npu=%d init done but token stale "
                         "(got %s, current %s); discarding",
                         model_id, npu_index, token, state.get("_init_token"))
                try:
                    sess.close()
                except Exception:  # noqa: BLE001
                    pass
                return
            state["session"] = sess
            state["status"] = "running"
            state["load_time_sec"] = load_sec
            state["loaded_at"] = time.time()
            state["init_error"] = ""
            LOG.info("LLMSession ready (%.1fs) for %s @ npu=%d",
                     load_sec, model_id, npu_index)
        except Exception as e:  # noqa: BLE001
            err_msg = f"{type(e).__name__}: {e}"
            if state.get("_init_token") != token:
                LOG.info("load %s @ npu=%d init failed but token stale; "
                         "discarding (%s)", model_id, npu_index, err_msg)
                return
            state["status"] = "error"
            state["init_error"] = err_msg
            LOG.warning("LLMSession init failed for %s @ npu=%d: %s",
                        model_id, npu_index, err_msg)

    _threading.Thread(target=_do_init, name=f"init-{model_id}",
                      daemon=True).start()
    return {
        "status": "loading",
        "npu_index": npu_index,
    }


def unload_handler(state: dict) -> dict:
    sess = state.get("session")
    if sess is None:
        return {"status": "noop", "npu_index": state.get("npu_index")}
    try:
        sess.close()
    except Exception as e:  # noqa: BLE001
        LOG.warning("session.close raised: %s", e)
    state["session"] = None
    state["status"] = "not_loaded"
    state["npu_index"] = None
    state["load_time_sec"] = None
    state["loaded_at"] = None
    # Force-release any large numpy buffers (embed tables, tokenizer
    # vocab, hidden-state caches) that ``sess.close()`` doesn't explicitly
    # drop. backend.__dict__ references drop naturally once ``sess._backend``
    # is None, but glibc arena holds the freed memory until malloc_trim.
    # TTS's 3 custom embed tables (text_embed/input_embed/codec_embed
    # ~750 MB) bypass ``load_embed_table`` and are the dominant leak;
    # the 6 other backends are fixed at the SDK side via mmap but this
    # is a defense-in-depth net for any future backend.
    try:
        import gc
        gc.collect()
        import ctypes
        import ctypes.util
        ctypes.CDLL(
            ctypes.util.find_library("c") or "libc.so.6"
        ).malloc_trim(0)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("post-unload arena trim failed: %s", exc)
    return {"status": "unloaded"}


def _as_json_vector(vec) -> list:
    """Coerce whatever a backend returned into a list of Python floats.

    Necessary because the replier JSON-encodes this reply: a numpy
    ``float32`` element raises ``TypeError: Object of type float32 is not
    JSON serializable``, and the whole embed call fails at the transport
    with an error that says nothing about embeddings.

    The old fallback here was ``list(vec)``, which is exactly that — the
    elements stay numpy scalars. Going through ``float()`` per element
    also absorbs torch tensors and nested sequences, so a backend that
    returns either shape still produces a usable vector.
    """
    import numpy as _np

    try:
        return _np.asarray(vec, dtype=_np.float32).reshape(-1).tolist()
    except Exception:  # noqa: BLE001
        pass
    if hasattr(vec, "tolist"):          # torch.Tensor and friends
        try:
            return [float(x) for x in vec.tolist()]
        except Exception:  # noqa: BLE001
            pass
    try:
        return [float(x) for x in vec]
    except TypeError:
        return []


def embed_text_handler(state: dict, payload: dict) -> dict:
    """Embedding text query (Qwen3-VL-Embedding-2B only).

    Payload: ``{"text": str}``
    Reply:   ``{"vec": List[float], "dim": int}`` — already-normalized
             2048-dim vector for cosine-similarity ranking.

    Like ``embed_video_handler`` this runs in a separate thread with
    a Python-level timeout so a wedged RKNN session can't pin the
    wdndds cshim pump thread.
    """
    import time as _time
    import threading as _threading
    sess = state.get("session")
    if sess is None:
        LOG.warning("embed_text: session not loaded — rejecting request")
        return {"status": "error",
                "error": "session not loaded; send action='load' first"}
    text = payload.get("text") or ""
    if not isinstance(text, str) or not text:
        return {"status": "error", "error": "text 必须是非空字符串"}
    LOG.info("embed_text: starting (text_len=%d)", len(text))
    t0 = _time.time()
    holder: list = []

    def _run():
        try:
            holder.append(sess.embed_text(text))
        except BaseException as e:  # noqa: BLE001
            holder.append(e)

    th = _threading.Thread(target=_run, name="embed_text_run", daemon=True)
    th.start()
    th.join(timeout=_EMBED_TEXT_TIMEOUT)

    if th.is_alive():
        LOG.warning("embed_text: timed out after %.1fs — calling session_stop()",
                    _EMBED_TEXT_TIMEOUT)
        try:
            backend = getattr(sess, "_backend", None)
            if backend is not None and hasattr(backend, "rknn_llm"):
                backend.rknn_llm.session_stop()
        except Exception:  # noqa: BLE001
            pass
        th.join(timeout=3.0)
        return {"status": "error",
                "error": f"embed_text timeout after {_EMBED_TEXT_TIMEOUT:.0f}s "
                        f"(NPU may be wedged; restart the instance)"}

    if not holder:
        return {"status": "error",
                "error": "embed_text returned no result"}

    result = holder[0]
    if isinstance(result, BaseException):
        LOG.warning("embed_text: failed after %.2fs: %s",
                    _time.time() - t0, result)
        return {"status": "error",
                "error": f"{type(result).__name__}: {result}"}
    # numpy array → JSON-friendly list. C extension returns either
    # np.ndarray or sequence-like; normalize via numpy when available.
    flat = _as_json_vector(result)
    LOG.info("embed_text: done in %.2fs, dim=%d", _time.time() - t0, len(flat))
    return {"vec": flat, "dim": len(flat)}


def embed_video_handler(state: dict, payload: dict) -> dict:
    """Embedding video chunk (Qwen3-VL-Embedding-2B only).

    Payload (two equivalent forms):
      - ``{"frame_paths": ["/dev/shm/xxx_c0000_f00.jpg", ...], "prompt": str}``
      - ``{"frames": [b64-jpeg-str, ...], "prompt": str}`` (legacy)

    The path-list form is the **preferred** contract: the VCS service
    writes each chunk's JPEG frames to ``/dev/shm`` (tmpfs, RAM-backed)
    and ships a list of *paths* (~50 bytes per chunk). The base64
    form is kept for any other caller (e.g. ad-hoc scripts) that
    doesn't have /dev/shm access.

    Wire-size note: a 4-frame chunk at JPEG q=85 is ~500 KiB base64 —
    well above FastDDS 3.6.2's effective per-message limit on the SHM
    transport, so the path-list form is what makes the cshim RPC
    actually work for the VCS page.

    Runs the underlying ``sess.embed_video()`` in a **separate
    thread** with a Python-level timeout. The wdndds cshim pump
    thread that calls this handler is single-threaded; if the C
    library's ``rknn_session.run()`` blocks indefinitely (e.g. RKNN
    driver wedged from a previous overload), the pump thread is
    pinned and every subsequent RPC (load / info / embed_text) queues
    up behind it. A normal chunk takes ~3 s on RK3588, so 60 s is a
    generous ceiling. On timeout, ``session_stop()`` is invoked
    asynchronously — if the C library honours the stop, the worker
    thread exits within a few hundred ms.
    """
    import time as _time
    import threading as _threading
    sess = state.get("session")
    if sess is None:
        LOG.warning("embed_video: session not loaded — rejecting request "
                    "(n_frames=%s)", len(payload.get("frames") or []))
        return {"status": "error",
                "error": "session not loaded; send action='load' first"}
    # One decoder, not two. This handler used to branch on
    # ``frame_paths`` (raw BGR via ``cv2.imread``, which silently
    # returns None for uncompressed bytes) versus ``frames`` (base64
    # JPEG via ``imdecode``), and the two never agreed about which shape
    # the caller was sending — so a correct client got None frames and
    # the call failed with an opaque OpenCV message. wdndds already has
    # one correct implementation that also handles the LAN-peer JPEG
    # fallback; reuse it rather than growing a third variant.
    from wdndds.ai_session import decode_attachments
    from wdnai import ImageAttachment

    entries = payload.get("frames")
    if not isinstance(entries, list) or not entries:
        return {"status": "error", "error": "frames 必须是非空 list"}
    frames = [a.images[0] for a in decode_attachments(entries)
              if isinstance(a, ImageAttachment)]
    if not frames:
        return {"status": "error",
                "error": "no decodable frame in payload"}
    LOG.info("embed_video: starting (n_frames=%d)", len(frames))
    t0 = _time.time()
    prompt = payload.get("prompt", "")
    holder: list = []

    def _run():
        try:
            holder.append(sess.embed_video(frames, prompt=prompt or ""))
        except BaseException as e:  # noqa: BLE001
            holder.append(e)

    th = _threading.Thread(target=_run, name="embed_video_run", daemon=True)
    th.start()
    th.join(timeout=_EMBED_VIDEO_TIMEOUT)

    if th.is_alive():
        LOG.warning("embed_video: timed out after %.1fs (n_frames=%d) — "
                    "calling session_stop() to interrupt RKNN inference",
                    _EMBED_VIDEO_TIMEOUT, len(frames))
        try:
            backend = getattr(sess, "_backend", None)
            if backend is not None and hasattr(backend, "rknn_llm"):
                backend.rknn_llm.session_stop()
        except Exception as e:  # noqa: BLE001
            LOG.warning("session_stop() raised (continuing): %s", e)
        th.join(timeout=3.0)
        if th.is_alive():
            LOG.error("embed_video: thread still alive 3s after stop — "
                      "RKNN driver likely wedged; recommend POST "
                      "/api/compute/restart for model_id=%r",
                      state.get("model_id", "?"))
        return {"status": "error",
                "error": f"embed_video timeout after {_EMBED_VIDEO_TIMEOUT:.0f}s "
                        f"(NPU may be wedged; restart the instance)"}

    if not holder:
        return {"status": "error",
                "error": "embed_video returned no result (thread exited without output)"}

    result = holder[0]
    if isinstance(result, BaseException):
        LOG.warning("embed_video: failed after %.2fs (n_frames=%d): %s",
                    _time.time() - t0, len(frames), result)
        return {"status": "error",
                "error": f"{type(result).__name__}: {result}"}
    elapsed = _time.time() - t0
    flat = _as_json_vector(result)
    LOG.info("embed_video: done in %.2fs (n_frames=%d, dim=%d)",
             elapsed, len(frames), len(flat))
    return {"vec": flat, "dim": len(flat), "n_frames": len(frames)}


def reset_handler(state: dict) -> dict:
    sess = state.get("session")
    if sess is None:
        return {"status": "noop (not loaded)"}
    try:
        sess.reset()
        return {"status": "reset"}
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "error": f"reset failed: {e}"}


def _pcm_chunk_forwarder_factory(sock):
    """Build the SDK's ``on_audio_chunk`` for a Unix-domain PCM sink.

    The caller passes only a socket **path** — it cannot pass this
    callable, because callables don't survive JSON serialisation, and
    constructing the forwarder in the caller's process would make the
    SDK call into a main-process object it cannot reach. So the worker
    builds its own, exactly as it always has.
    """
    import struct as _struct

    import numpy as _np

    def _forward(audio, sr, _c=sock):
        try:
            arr = _np.asarray(audio, dtype="<f4").reshape(-1)
            header = _struct.pack("<III", int(sr), 1, int(arr.size))
            _c.sendall(header + arr.tobytes())
        except Exception:  # noqa: BLE001 — a dropped chunk must not kill synthesis
            pass

    return _forward


def _pcm_chunk_dds_forwarder_factory(writer):
    """``on_audio_chunk`` variant for the LAN fallback path."""
    import struct as _struct

    import numpy as _np

    def _forward(audio, sr, _w=writer):
        try:
            arr = _np.asarray(audio, dtype="<f4").reshape(-1)
            header = _struct.pack("<III", int(sr), 1, int(arr.size))
            _w.write(header + arr.tobytes())
        except Exception:  # noqa: BLE001
            pass

    return _forward


def _tts_channel_options(options: dict) -> dict:
    """Turn ``tts_pcm_socket_path`` into the SDK callbacks TTS needs.

    ``respond(…, on_audio_chunk=…)`` only ever ran in-process. Over RPC
    the caller sends a **path**, and the worker opens the connection and
    installs the forwarder itself. Three fallbacks, in order: the Unix
    socket (same board, no DDS round trip), the per-worker DDS audio
    topic (LAN peer), then nothing — in which case synthesis still
    produces a WAV the caller can play after the fact.
    """
    out: dict = {}
    sock_path = options.get("tts_pcm_socket_path")
    if not isinstance(sock_path, str) or not sock_path:
        return out
    try:
        import socket as _socket

        sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        sock.settimeout(2.0)
        sock.connect(sock_path)
    except Exception as e:  # noqa: BLE001
        LOG.warning("tts_pcm_socket: connect to %s failed (%s)", sock_path, e)
        try:
            sock.close()
        except Exception:  # noqa: BLE001
            pass
        if _AUDIO_CHUNK_WRITER is None:
            LOG.info("tts_pcm: no DDS writer available; falling back to WAV-only")
            return out
        out["on_audio_chunk"] = _pcm_chunk_dds_forwarder_factory(
            _AUDIO_CHUNK_WRITER)
        LOG.info("tts_pcm: using DDS topic writer (LAN fallback)")
        return out
    out["_tts_pcm_sock"] = sock
    out["on_audio_chunk"] = _pcm_chunk_forwarder_factory(sock)
    LOG.info("tts_pcm: using Unix domain socket (local fast path)")
    return out


def count_tokens_handler(state: dict, payload: dict) -> dict:
    """Exact token counts for a batch of strings.

    Trimming a chat history against the model's context window is the
    caller's job now (history reaches the model as ChatML text, and the
    decision of what to drop is a caller-side policy), but only the
    process holding the tokenizer can count accurately. So this is the
    one piece of session knowledge the worker still lends out — over a
    round trip costing microseconds, with no inference and no NPU.

    Approximating instead (``len(text) // 2``, which the caller's
    fallback does when this fails) is a real quality regression on
    Chinese text, where it under-counts by ~3x and lets a history
    overflow the window.
    """
    sess = state.get("session")
    if sess is None:
        return {"status": "error",
                "error": "session not loaded; send action='load' first"}
    texts = payload.get("texts")
    if not isinstance(texts, list) or not all(isinstance(t, str) for t in texts):
        return {"status": "error", "error": "texts 必须是 list[str]"}
    tokenizer = _session_tokenizer(sess)
    if tokenizer is None:
        return {"status": "error", "error": "session has no tokenizer"}
    counts = []
    for t in texts:
        try:
            counts.append(len(tokenizer.encode(t, add_special_tokens=False)))
        except Exception:  # noqa: BLE001 — one bad string must not sink the batch
            counts.append(len(t) // 2)
    return {"status": "ok", "counts": counts}


def _session_tokenizer(sess):
    """Find the tokenizer a session's backend is using.

    It lives on the **backend**, not the session: every wdnai backend
    does ``self.tokenizer = load_tokenizer(spec.tokenizer_path)`` and
    hands it to its ``StreamSink``. ``LLMSession`` exposes no accessor
    for it, so reach through one hop and check the two shapes that exist
    (``backend.tokenizer``, and ``backend.sink.tokenizer`` for backends
    that only kept a reference).
    """
    backend = getattr(sess, "_backend", None)
    if backend is None:
        return None
    tok = getattr(backend, "tokenizer", None)
    if tok is None:
        tok = getattr(getattr(backend, "sink", None), "tokenizer", None)
    return tok if hasattr(tok, "encode") else None


# --------------------------------------------------------------------------- #
# Extra ops — the verbs ``serve_local_session`` doesn't know about            #
# --------------------------------------------------------------------------- #
def _build_extra_ops(state: dict, model_id: str,
                     startup_npu_index: int | None,
                     model_dir: str | None) -> dict:
    """Everything this worker offers on top of plain ``respond``.

    ``serve_local_session`` speaks one inference verb plus the generic
    session verbs (``info`` / ``reset`` / ``embed_text`` /
    ``embed_video``). wdnwebui adds a lifecycle of its own:

    * ``info`` / ``embed_text`` / ``embed_video`` are **overridden**, not
      added. The generic ``info`` doesn't know about ``not_loaded`` vs
      ``missing_model_dir`` vs ``error``; the generic embed verbs run on
      the replier pump thread, where a wedged RKNN session would block
      every other request for its full duration. Both keep their
      run-off-the-pump-thread + timeout treatment below.
    * ``load`` / ``unload`` / ``shutdown`` / ``count_tokens`` are ours
      outright.

    ``respond`` itself cannot be overridden — see
    ``wdndds.ai_session._handle_request``.
    """
    def _shutdown(_payload: dict) -> dict:
        # Graceful exit of the worker main loop. The caller
        # (ComputeRegistry.stop_instance_local) waits up to 3 s, then
        # escalates to SIGKILL.
        LOG.info("shutdown action received — exiting main loop")
        _WORKER_STOP_EVENT.set()
        return {"status": "shutting_down"}

    return {
        "info": lambda p: info_handler(
            state, model_id, startup_npu_index, model_dir),
        "load": lambda p: load_handler(state, model_id, p.get("npu_index")),
        "unload": lambda p: unload_handler(state),
        "shutdown": _shutdown,
        "reset": lambda p: reset_handler(state),
        "count_tokens": lambda p: count_tokens_handler(state, p),
        # Off-pump-thread + timeout, per the note above.
        "embed_text": lambda p: embed_text_handler(state, p),
        "embed_video": lambda p: embed_video_handler(state, p),
    }


def _serve_one(hub, side_hub, service_name: str, session, extra_ops: dict):
    """Attach ``serve_local_session`` to one already-registered service.

    The worker advertises **two** names for itself — the per-PID one the
    local main process dials directly, and the standard one LAN peers
    discover — and both need a full replier, so this is called once per
    name against a single shared ``session``.

    Two consequences of sharing one session across both repliers, both
    fine:

    * ``LLMLocalSession`` serializes inference on its own single-worker
      thread pool, so concurrent requests over the two names queue
      instead of racing on the NPU.
    * Each replier gets its own delta/stop side-channel, derived from
      its own service name — which is exactly what the client uses, so a
      request and its ``stop()`` always meet on the same channel.

    Both share **one** ``side_hub``. Letting ``serve_local_session``
    build its own per call would register two DomainParticipants with
    the identical default name (``wdnai-serve-<pid>``); duplicate
    participant names in one domain break discovery, and the symptom is
    a worker whose replier quietly stops replying — every RPC, including
    ``info``, times out while the process looks perfectly alive.
    """
    from wdndds import serve_local_session
    return serve_local_session(
        session,
        service_name=service_name,
        hub=hub,
        side_hub=side_hub,
        extra_ops=extra_ops,
        # Bound the replier pump. A hung rknn3 call (wedged NPU driver,
        # long prefill, a blocking clear_kvcache) would otherwise pin the
        # serial pump forever and leave the worker deaf to *every* later
        # request, not just the slow one. Well under the client's 120 s
        # infer budget so the error comes back as a clean worker reply
        # rather than as a client-side timeout with no diagnosis.
        #
        # Trade-off, stated plainly: the inference thread is a daemon and
        # is abandoned on timeout, so the NPU slot stays occupied until
        # the driver returns on its own. A timeout therefore means "this
        # worker needs restarting", not "retry this call".
        infer_timeout_s=_INFER_TIMEOUT_SEC,
    )


# --------------------------------------------------------------------------- #
# Main — wdndds cshim Hub as RPC server                                      #
# --------------------------------------------------------------------------- #
def _config_path() -> str:
    """Resolve config.jsonc path.

    Priority mirrors :func:`app.core.config._resolve_config_path`:

    1. ``WDNWEBUI_CONFIG`` env var (the main process always sets this
       when spawning a worker, so this is the common case).
    2. The main-process default — same computation, so the worker
       and the spawning parent agree even if the env was dropped.

    Note: we deliberately do NOT keep a hardcoded ``/userdata/...``
    fallback here; on off-device hosts ``/userdata`` is not mounted,
    and the worker should agree with the main process rather than
    silently diverging to a non-existent path.
    """
    from app.core.config import _resolve_config_path as _main_resolve
    return os.environ.get('WDNWEBUI_CONFIG') or str(_main_resolve())


def run_worker(argv=None) -> int:
    """Boot this worker: bring up the Hub, register the replier, serve.

    Called by the shared ``compute_worker.py``, which knows nothing about
    any particular model — everything comes from ``workers/<id>.json``.
    Kept as a function (not a ``__main__`` block) so the entry point stays
    a thin shim.
    """
    # fd 保险：把 soft RLIMIT_NOFILE 提到 hard 上限。worker 是长寿命进程，
    # RKNN init（每 context/卡一堆 fd）+ mmap 权重 + DDS endpoint 的 fd
    # 占用远超交互 shell 默认的 1024 soft limit —— 一旦逼近上限，
    # cv2.imread 的 fopen 会**间歇性** EMFILE 失败，OpenCV 只报一句
    # "can't open/read file: check file path/integrity"，表象与文件缺失
    # 无法区分。失败不致命 —— 低 hard 环境下保持原值继续跑。
    try:
        import resource as _resource
        _soft, _hard = _resource.getrlimit(_resource.RLIMIT_NOFILE)
        if _soft < _hard:
            _resource.setrlimit(_resource.RLIMIT_NOFILE, (_hard, _hard))
            print(f"[compute_worker] RLIMIT_NOFILE soft {_soft} -> {_hard}",
                  flush=True)
    except Exception as _e:  # noqa: BLE001
        print(f"[compute_worker] RLIMIT_NOFILE raise skipped: {_e}",
              flush=True)

    ap = argparse.ArgumentParser(prog="compute_worker")
    ap.add_argument("--model", required=True,
                    help="model id (key in MODEL_PRESETS)")
    ap.add_argument("--npu", type=int, required=False, default=None,
                    help="开机自动启动的 worker 在 spawn 时由主进程传入"
                         "（config.jsonc 的 compute.autostart[].npu_index）。"
                         "传了就等于注册完 replier 后立刻在后台线程 load，"
                         "所以 worker 一起来 info probe 就有应答，不会 boot 超时。"
                         "不传则只注册 DDS endpoint，等用户在算力页选卡后通过 "
                         "RPC ``action=load npu_index=N`` 加载。")
    ap.add_argument("--local-only", action="store_true",
                    help="主进程 spawn 本机 worker 时传:worker 只注册 per-PID "
                         "service (wdndds/rpc/<model_id>/worker-<pid>)，不注册 "
                         "标准 service。避免与 LAN peer 共用 standard topic 造成 "
                         "FastDDS 路由冲突(否则本机 probe 的 info RPC 会被路由到 "
                         "本机 spawned worker，adopted (NETWORK) 卡 status 翻转)。"
                         "仅 main process spawn 时使用;手动启动 / debug 路径不传。")
    ap.add_argument("--debug", action="store_true",
                    default=os.environ.get("WDNDDS_DEBUG") == "1")
    args = ap.parse_args(argv)

    _setup_logging(args.debug)

    model_id = args.model
    npu_index = args.npu

    # 设置本进程 comm（"worker:<alias>"），让主进程 _find_workers_by_name()
    # 能通过 /proc/<pid>/comm 找到我们做 dedup。必须在 _setup_logging 后
    # 调，让 LOG.warning 报错时 comm 已经设上。
    try:
        comm_name = worker_comm_name(model_id)
        _set_comm_name(comm_name)
    except Exception as e:  # noqa: BLE001
        LOG.warning("could not set comm name for %s: %s", model_id, e)

    # Resolve model_dir for the info reply (don't fail if missing).
    model_dir = None
    try:
        # Match the main process's ``default_models_root()`` priority so
        # off-device workers don't pin to a non-existent ``/userdata/models``.
        from app.core.config import default_models_root
        base_dir = os.environ.get("WDNAI_MODELS_DIR", f"{default_models_root()}/")
        manifest = load_manifest(model_id)
        dir_name = manifest.get("model_dir_name")
        if dir_name:
            model_dir = os.path.join(base_dir, dir_name)
    except Exception:
        pass

    LOG.info("compute_worker start: model=%s service=wdndds/rpc/%s domain=0 "
             "(npu_index=%s, deferred-to-RPC=%s)",
             model_id, model_id, npu_index, npu_index is None)

    from wdndds.dds._rpc import Hub

    # State dict shared with the extra-op handlers. Initial status is
    # ``not_loaded`` — worker is alive and reachable, but has not
    # built an LLMSession yet (npu_index deferred to RPC load).
    #
    # 特殊状态 ``missing_model_dir``：worker 启动时就发现 model_dir 不
    # 存在 —— 不是 init 失败，是"压根没料"。load_handler 看到这状态
    # 直接返回，不会去碰 rknn；UI 那边在算力卡上显示"模型目录不存在"
    # 而不是"启动失败"。这样把"环境/资源缺失"和"实际启动出错"区分
    # 干净，banner / 错误归类也对得上。
    initial_status = "not_loaded"
    if model_dir is not None and not os.path.isdir(model_dir):
        initial_status = "missing_model_dir"
        LOG.warning(
            "model_dir %s does not exist; worker %s will report "
            "missing_model_dir instead of attempting init",
            model_dir, model_id)
    _worker_state: dict = {
        "status": initial_status,
        "started_at": time.time(),
        "npu_index": None,
        "load_time_sec": None,
        "loaded_at": None,
        "init_error": "",
        "session": None,
        "wdnai_available": False,
        "model_dir": model_dir,
        "model_id": model_id,
    }

    # Reset module-level stop event in case main() is re-entered under
    # the same interpreter (unusual but defensive).
    global _WORKER_STOP_EVENT
    _WORKER_STOP_EVENT = threading.Event()
    stop_event = _WORKER_STOP_EVENT

    device_name = _read_device_name()
    # v0.8.16: 必须注册 BOTH services。仅注册 unique (v0.8.15) 会让 LAN
    # peer 的 boot probe 走 standard service 找不到本机 worker,NETWORK 栏
    # 为空。LOCAL main 用 unique 直连本机 (避免 routing 冲突),adopted main
    # 走 standard 找 LAN peer — 同时也会路由到本机,但本地 loading 完成后
    # 状态与 peer 一致,不再翻转。Do NOT 改回 only-unique。
    #
    # ``Hub`` is created without a global callback: the RPC surface is
    # built below by ``serve_local_session``, which owns the request
    # dispatch. Left here purely so both service names exist on the
    # participant before anything can route to them.
    services = [
        f"wdndds/rpc/{model_id}/worker-{_WORKER_PID}",
        f"wdndds/rpc/{model_id}",
    ]
    hub = Hub(
        participant_name=f"compute_worker-{device_name}-{model_id}",
        domain_id=0,
        services=services,
        discovery_servers=[],
    )
    hub.start()

    # 内置 @Callable 工具注册：与主进程共用同一份 registry 真源
    # (详见 app/core/tools/_builtins.py)。text_chat / im_bridge 在主进程
    # 里查 registry 拿真实 tool class 传给 ``respond(tools=…)``，由调用
    # 方拼 prompt；worker 侧不再需要查表，但主进程没起 uvicorn 前
    # registry 可能是空的，仍按原样注册一次保持一致。
    try:
        from app.core.tools.registry import get_tool_registry
        from app.core.tools._builtins import register_builtin_tools
        register_builtin_tools(get_tool_registry())
    except Exception as e:  # noqa: BLE001
        LOG.warning("worker: register_builtin_tools failed: %s", e)

    # Deferred model build: the session does not exist until the first
    # ``load`` arrives, so hand ``serve_local_session`` a shim that
    # forwards to whatever ``state["session"]`` holds at call time.
    # ``load`` runs on the same pump thread as ``respond``, so there is
    # no window where one can observe the other half-built.
    class _LazySession:
        """Forwards the session interface to ``state['session']``.

        Exists because the worker's model is built lazily (an unloaded
        worker must answer ``info`` without touching rknn), while the
        transport wants one stable object to hold on to.

        ``run_prompt`` is overridden rather than forwarded because it is
        the one place a worker can still add something the wire cannot
        carry: **callables**. The caller sends ``tts_pcm_socket_path``
        (a string); the SDK needs an ``on_audio_chunk`` **function**.
        Building it here is what lets the TTS page stream PCM to the
        browser without the caller shipping a function across a process
        boundary.
        """

        def __getattr__(self, name):
            sess = _worker_state.get("session")
            if sess is None:
                raise RuntimeError(
                    "session not loaded; send action='load' first")
            return getattr(sess, name)

        async def run_prompt(self, prompt, attachment=None, *,
                             on_delta=None, **overrides):
            extra = _tts_channel_options(overrides)
            sock = extra.pop("_tts_pcm_sock", None)
            overrides.update(extra)
            try:
                return await self._delegate().run_prompt(
                    prompt, attachment, on_delta=on_delta, **overrides)
            finally:
                if sock is not None:
                    try:
                        sock.close()
                    except Exception:  # noqa: BLE001
                        pass

        def _delegate(self):
            sess = _worker_state.get("session")
            if sess is None:
                raise RuntimeError(
                    "session not loaded; send action='load' first")
            return sess

    # One side channel for both service names — see the note on
    # ``_serve_one``. It carries ``<service_name>/delta`` and
    # ``<service_name>/stop``, so the two still never share a topic.
    from wdndds.dds._rpc import Hub as _SideHub
    side_hub = _SideHub(
        participant_name=f"compute_worker-side-{device_name}-{model_id}",
        domain_id=0,
        discovery_servers=[],
    )
    side_hub.start()

    extra_ops = _build_extra_ops(_worker_state, model_id, npu_index, model_dir)
    served = []
    for svc in services:
        served.append(
            _serve_one(hub, side_hub, svc, _LazySession(), extra_ops))

    # 内置 @Callable 工具注册：与主进程共用同一份 registry 真源
    # (详见 app/core/tools/_builtins.py)。
    #
    # 现在主进程才需要查表（``chat_agent._tool_classes`` 要拿真实 class
    # 传给 ``respond(tools=…)``），worker 侧其实用不上了 —— 但注册成本
    # 是一次性的，且让两个进程的 registry 保持一致仍然有价值（比如
    # im_bridge 直接问 worker 要 capability 可用性时）。保留。
    try:
        from app.core.tools.registry import get_tool_registry
        from app.core.tools._builtins import register_builtin_tools
        register_builtin_tools(get_tool_registry())
    except Exception as e:  # noqa: BLE001
        LOG.warning("worker: register_builtin_tools failed: %s", e)

# Per-worker LAN 流式 fallback：Writer 跑在独立 DomainParticipant 上，
    # 让 cshim publisher 线程不与 RPC replier 的 reply publish 共享——
    # 共享 pump 在长文本 reply 上仍会卡（前一个 commit 解决了一类），
    # 我们不想让 BEST_EFFORT 音频写入与 RELIABLE reply publish 抢队。
    # 不挂 service / peer_listener：本 Hub 纯 DataWriter host。
    audio_hub = None
    global _AUDIO_CHUNK_WRITER
    try:
        from wdndds.dds._rpc import Hub as _RpcHub
        from wdndds.dds import create_topic_writer
        audio_hub = _RpcHub(
            participant_name=f"compute_worker-audio-{device_name}-{model_id}",
            domain_id=0,
            discovery_servers=[],
        )
        audio_hub.start()
        _AUDIO_CHUNK_WRITER = create_topic_writer(
            audio_hub, f"wdndds/audio/tts_chunk/{model_id}",
            qos_kind="best_effort")
        LOG.info("audio_chunk_writer: ready on "
                 "wdndds/audio/tts_chunk/%s (BEST_EFFORT, separate_hub=True)",
                 model_id)
    except Exception as e:  # noqa: BLE001
        LOG.warning("audio_chunk_writer: DDS init failed (%s); "
                    "LAN TTS streaming will fall back to WAV-only", e)
        _AUDIO_CHUNK_WRITER = None
        if audio_hub is not None:
            try:
                audio_hub.close()
            except Exception:
                pass
            audio_hub = None

    # --npu passed (config.jsonc compute.autostart): load on a background
    # thread *after* the replier is registered, so the boot-time info probe
    # gets a real answer instead of blocking until the model is resident.
    # No --npu: only the DDS endpoint, and load arrives via the RPC action
    # once the user picks a card on the compute page.
    if npu_index is not None:
        def _eager_load():
            try:
                LOG.info("eager load starting (--npu=%d) for %s",
                         npu_index, model_id)
                load_handler(_worker_state, model_id, npu_index)
            except Exception as e:  # noqa: BLE001
                LOG.warning("eager load raised: %s", e)
        threading.Thread(target=_eager_load, name="eager-load",
                         daemon=True).start()

    # Graceful shutdown on SIGTERM / SIGINT — same effect as RPC shutdown.
    def _signal_handler(signum, frame):
        LOG.info("received signal %d — shutting down", signum)
        stop_event.set()
        hub.stop()
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    # Print ready marker for the parent to detect.
    print(json.dumps({
        "type": "ready",
        "service": f"wdndds/rpc/{model_id}",
        "model": model_id,
        "npu_index": npu_index,
        "process_id": os.getpid(),
    }), file=sys.stderr, flush=True)

    try:
        while not stop_event.is_set():
            stop_event.wait(timeout=1.0)
    finally:
        LOG.info("compute_worker exit")
        # Close the DDS audio-chunk writer before the Hub tears down the
        # participant so any in-flight BEST_EFFORT chunks can flush.
        # ``close()`` is idempotent so a double-close is a no-op.
        try:
            if _AUDIO_CHUNK_WRITER is not None:
                _AUDIO_CHUNK_WRITER.close()
        except Exception:
            pass
        # ``ServedSession.close()`` won't touch a side hub it doesn't own,
        # so it has to be closed explicitly — after the served sessions,
        # which publish their final stream-end marker through it.
        for sv in served:
            try:
                sv.close()
            except Exception:
                pass
        try:
            side_hub.close()
        except Exception:
            pass
        try:
            hub.close()
        except Exception:
            pass
        try:
            if audio_hub is not None:
                audio_hub.close()
        except Exception:
            pass
    return 0
