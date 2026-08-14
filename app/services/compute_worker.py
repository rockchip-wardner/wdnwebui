"""compute_worker — wdndds cshim RPC server, one process per model.

Spawned by ``ComputeRegistry._spawn_compute_worker`` via
``subprocess.Popen``. Each worker holds:

  * its own DomainParticipant (via wdndds Hub)
  * one RPC service endpoint (request topic + reply topic)

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
import json
import logging
import os
import signal
import socket
import sys
import threading
import time
from typing import Any

LOG = logging.getLogger("compute_worker")


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
    """Resolve ``device_name`` from ``config.json["base"]``.

    Used as the FastDDS participant name suffix so LAN peers can
    identify which board each worker is running on. Falls back to the
    host's ``socket.gethostname()`` when the field is missing or
    ``config.json`` is unreachable (workers running before the main
    process, or in a stripped-down test environment).
    """
    try:
        from app.services.config import get_config
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


# --------------------------------------------------------------------------- #
# wdnai lazy-import + session builder                                          #
# --------------------------------------------------------------------------- #
_WDNAI = None


def _load_wdnai() -> dict:
    global _WDNAI
    if _WDNAI is not None:
        return _WDNAI
    try:
        from wdnai import (
            Attachment,
            LLMSession,
            SystemLanguageModel,
        )
    except ImportError as e:
        raise RuntimeError(f"wdnai import failed: {e}")
    _WDNAI = {
        "Attachment": Attachment,
        "LLMSession": LLMSession,
        "SystemLanguageModel": SystemLanguageModel,
    }
    return _WDNAI


def _build_session(model_id: str, npu_index: int):
    """Construct an LLMSession. May raise (or call terminate())."""
    mod = _load_wdnai()
    if model_id == "qwen3-vl-4b":
        spec = mod["SystemLanguageModel"].qwen3_vl_4b()
    elif model_id == "qwen3-tts":
        spec = mod["SystemLanguageModel"].qwen3_tts()
    elif model_id == "qwen3-8b":
        from dataclasses import replace as _dc_replace
        spec = mod["SystemLanguageModel"].qwen3_8b()
        spec = _dc_replace(spec, max_context_len=32768).with_options(
            max_new_tokens=2048, keep_history=1, enable_thinking=True,
        )
    elif model_id == "qwen3-vl-embedding-2b":
        # Embedding 模型：RKNN3Lite 普通模型路径输出 2048 维归一化向量；
        # 不做 token 生成，wdnai 内部用 EmbeddingBackend 跑 embed_text /
        # embed_video_chunk；模型文件布局与 Qwen3-VL-4B 同款。
        spec = mod["SystemLanguageModel"].qwen3_vl_embedding()
    elif model_id == "qwen2.5-omni-3b":
        # Qwen2.5-Omni-3B（kind="omni"）。单次 run_audio 上限 6s，调用方负责切片。
        spec = mod["SystemLanguageModel"].qwen_2_5_omni_3b()
    else:
        raise ValueError(f"unknown model_id: {model_id!r}")
    LOG.info("building LLMSession(model=%s, npu=%d) ...", model_id, npu_index)
    t0 = time.time()
    sess = mod["LLMSession"](model=spec, npu=npu_index)
    LOG.info("LLMSession ready (%.1fs)", time.time() - t0)
    return sess


# --------------------------------------------------------------------------- #
# Per-call handlers                                                          #
# --------------------------------------------------------------------------- #
def info_handler(state: dict, model_id: str,
                 startup_npu_index: int | None,
                 model_dir: str | None) -> dict:
    npu_index = state.get("npu_index")
    return {
        "status":             state.get("status", "not_loaded"),
        "model":              model_id,
        "model_dir":          model_dir,
        "npu_index":          npu_index,
        "npu_index_startup":  startup_npu_index,
        "load_time_sec":      state.get("load_time_sec"),
        "init_error":         state.get("init_error", ""),
        "wdnai_available":     state.get("wdnai_available", False),
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
    return {"status": "unloaded"}


def embed_text_handler(state: dict, payload: dict) -> dict:
    """Embedding text query (Qwen3-VL-Embedding-2B only).

    Payload: ``{"text": str}``
    Reply:   ``{"vec": List[float], "dim": int}`` — already-normalized
             2048-dim vector for cosine-similarity ranking.
    """
    sess = state.get("session")
    if sess is None:
        return {"status": "error",
                "error": "session not loaded; send action='load' first"}
    text = payload.get("text") or ""
    if not isinstance(text, str) or not text:
        return {"status": "error", "error": "text 必须是非空字符串"}
    try:
        vec = sess.embed_text(text)
    except Exception as e:  # noqa: BLE001
        return {"status": "error",
                "error": f"{type(e).__name__}: {e}"}
    # numpy array → JSON-friendly list. C extension returns either
    # np.ndarray or sequence-like; normalize via numpy when available.
    try:
        import numpy as _np
        arr = _np.asarray(vec, dtype=_np.float32).reshape(-1)
        return {"vec": arr.tolist(), "dim": int(arr.size)}
    except Exception:
        flat = list(vec) if hasattr(vec, "__iter__") else [float(x) for x in vec]
        return {"vec": flat, "dim": len(flat)}


def embed_video_handler(state: dict, payload: dict) -> dict:
    """Embedding video chunk (Qwen3-VL-Embedding-2B only).

    Payload: ``{"frames": [bgr-ndarray, ...], "prompt": str (optional)}``
    Reply:   ``{"vec": List[float], "dim": int, "n_frames": int}``

    Frames are encoded as base64-encoded JPEG bytes (decoded to BGR
    here). Wire size matters: a typical 16-frame chunk at JPEG q=85
    fits comfortably; we don't compress further server-side.
    """
    sess = state.get("session")
    if sess is None:
        return {"status": "error",
                "error": "session not loaded; send action='load' first"}
    import base64 as _b64
    import numpy as _np
    import cv2 as _cv2
    frames_meta = payload.get("frames", [])
    if not isinstance(frames_meta, list) or not frames_meta:
        return {"status": "error", "error": "frames 必须是非空 list"}
    frames: list = []
    for item in frames_meta:
        if isinstance(item, dict) and "data_b64" in item:
            data_b64 = item["data_b64"]
        elif isinstance(item, str):
            data_b64 = item
        else:
            return {"status": "error",
                    "error": f"frame 项必须是 b64 string 或 {{data_b64}} dict: {type(item).__name__}"}
        raw = _b64.b64decode(data_b64)
        arr = _np.frombuffer(raw, dtype=_np.uint8)
        img = _cv2.imdecode(arr, _cv2.IMREAD_COLOR)
        if img is None:
            return {"status": "error", "error": "cv2.imdecode returned None"}
        frames.append(img)
    prompt = payload.get("prompt", "")
    try:
        vec = sess.embed_video(frames, prompt=prompt or "")
    except Exception as e:  # noqa: BLE001
        return {"status": "error",
                "error": f"{type(e).__name__}: {e}"}
    try:
        arr = _np.asarray(vec, dtype=_np.float32).reshape(-1)
        return {"vec": arr.tolist(),
                "dim": int(arr.size),
                "n_frames": len(frames)}
    except Exception:
        flat = list(vec) if hasattr(vec, "__iter__") else [float(x) for x in vec]
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


def infer_handler(state: dict, payload: dict) -> dict:
    sess = state.get("session")
    if sess is None:
        return {"status": "error", "error": "session not loaded; send action='load' first"}

    try:
        mod = _load_wdnai()
        Attachment = mod["Attachment"]
        prompt = payload.get("prompt")
        atts_meta = payload.get("attachment", [])
        # audio 通道参数（omni run_audio 需要）。未传则用 omni 默认
        # 契约（16 kHz mono s16le），兜底靠 session.py 内部 default。
        options = payload.get("options") or {}
        audio_kwargs: dict = {}
        if "sample_rate" in options:
            audio_kwargs["sample_rate"] = options["sample_rate"]
        if "n_channels" in options:
            audio_kwargs["n_channels"] = options["n_channels"]
        if "sample_width" in options:
            audio_kwargs["sample_width"] = options["sample_width"]

        attachments = []
        for item in atts_meta:
            if isinstance(item, dict) and "data_b64" in item:
                # audio 走 tag dict（``kind=="audio"``）；图片走裸 base64 字符串。
                import base64 as _b64
                raw = _b64.b64decode(item["data_b64"])
                if item.get("kind") == "audio":
                    attachments.append(Attachment(raw, kind="audio"))
                    continue
                import numpy as _np
                arr = _np.frombuffer(raw, dtype=_np.uint8)
                import cv2 as _cv2
                img = _cv2.imdecode(arr, _cv2.IMREAD_COLOR)
                if img is not None:
                    attachments.append(Attachment(img))
                continue
            if isinstance(item, str):
                try:
                    import base64 as _b64
                    import numpy as _np
                    raw = _b64.b64decode(item)
                    arr = _np.frombuffer(raw, dtype=_np.uint8)
                    import cv2 as _cv2
                    img = _cv2.imdecode(arr, _cv2.IMREAD_COLOR)
                    if img is not None:
                        attachments.append(Attachment(img))
                        continue
                except Exception:
                    pass
                attachments.append(Attachment(item, kind="text"))
                continue
            if hasattr(item, "shape"):
                attachments.append(Attachment(item))

        import asyncio
        generating_cls = None
        gen_name = payload.get("generating")
        if gen_name == "VLMDoc":
            try:
                from app.services.detector import _get_vlm_doc_cls
                generating_cls = _get_vlm_doc_cls()
            except Exception as e:  # noqa: BLE001
                LOG.warning("VLMDoc schema load failed in worker: %s", e)
        elif gen_name == "TargetDoc":
            try:
                from app.services.detector import _get_target_doc_cls
                generating_cls = _get_target_doc_cls()
            except Exception as e:  # noqa: BLE001
                LOG.warning("TargetDoc schema load failed in worker: %s", e)

        async def _call():
            try:
                sess.reset()
            except Exception:
                pass
            kwargs = dict(audio_kwargs)
            if generating_cls is not None:
                kwargs["generating"] = generating_cls
            return await sess.respond(
                prompt=prompt,
                attachment=attachments if attachments else None,
                **kwargs,
            )

        result = asyncio.run(_call())
        text = getattr(result, "text", None) or (
            result if isinstance(result, str) else None)
        return {
            "text": text,
            "level": getattr(result, "level", None),
            "tool_call": getattr(result, "tool_call", None),
            "bbox": getattr(result, "bbox", None),
            "crop_b64": getattr(result, "crop_b64", None),
        }
    except Exception as e:  # noqa: BLE001
        LOG.warning("infer_handler exception: %s", e)
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


# --------------------------------------------------------------------------- #
# Router                                                                    #
# --------------------------------------------------------------------------- #
def router(service_name: str, payload: Any, state: dict,
           model_id: str, startup_npu_index: int | None,
           model_dir: str | None) -> dict:
    # The cshim v0.5 replier (``Replier._on_request_c``) already
    # ``json.loads`` the inbound payload before dispatching it here, so
    # ``payload`` is a dict. The cshim then json.dumps the dict this
    # function returns and embeds it as a raw JSON object under the
    # wire ``data`` field — consumers access handler results directly
    # via ``r["data"]`` without an extra unwrap.
    if not isinstance(payload, dict):
        return {"ok": False, "error": f"payload must be dict, got {type(payload).__name__}"}

    # The cshim v0.5 client stuffs the entire original JSON into the
    # typed ``prompt`` field (because the IDL only carries action /
    # every_n / request_id + a prompt string). The cshim worker side
    # re-quotes that prompt field as a JSON string value, so we see
    # ``payload["prompt"]`` as a JSON-encoded string here. Re-parse
    # it so downstream handlers see the original fields
    # (attachment / generating / tools / options / npu_index / ...).
    prompt_field = payload.get("prompt", "")
    if isinstance(prompt_field, str) and prompt_field.startswith("{"):
        try:
            inner = json.loads(prompt_field)
            if isinstance(inner, dict):
                payload = inner
        except Exception:
            pass

    action = payload.get("action", "info")
    if action == "info":
        return info_handler(state, model_id, startup_npu_index, model_dir)
    if action == "load":
        return load_handler(state, model_id, payload.get("npu_index"))
    if action == "infer":
        return infer_handler(state, payload)
    if action == "reset":
        return reset_handler(state)
    if action == "unload":
        return unload_handler(state)
    if action == "embed_text":
        return embed_text_handler(state, payload)
    if action == "embed_video":
        return embed_video_handler(state, payload)
    if action == "shutdown":
        # Trigger graceful exit of the worker main loop. The caller
        # (ComputeRegistry.stop_instance_local) waits up to 3 s, then
        # escalates to SIGKILL.
        LOG.info("shutdown action received — exiting main loop")
        _WORKER_STOP_EVENT.set()
        return {"status": "shutting_down"}
    return {"ok": False, "error": f"unknown action: {action!r}"}


# --------------------------------------------------------------------------- #
# Main — wdndds cshim Hub as RPC server                                      #
# --------------------------------------------------------------------------- #
def _config_path() -> str:
    """Resolve config.json path from WDNWEBUI_CONFIG env var, defaulting
    to the repo root config.json."""
    import os
    return os.environ.get('WDNWEBUI_CONFIG') or '/userdata/wdnwebui/config.json'


def main() -> int:
    ap = argparse.ArgumentParser(prog="compute_worker")
    ap.add_argument("--model", required=True,
                    help="model id (key in MODEL_PRESETS)")
    ap.add_argument("--npu", type=int, required=False, default=None,
                    help="DEPRECATED: npu_index 现在通过 RPC load action 传入。"
                         "保留只为兼容旧 systemd / 调试用。worker 启动后只注册 "
                         "DDS endpoint 不建 LLMSession；npu_index 由用户在算力"
                         "配置页选择后通过 RPC ``action=load npu_index=N`` 传"
                         "入。如果这里也传，worker 启动后会自动 build LLMSession"
                         "（仅用于 e2e 测试方便）。")
    ap.add_argument("--debug", action="store_true",
                    default=os.environ.get("WDNDDS_DEBUG") == "1")
    args = ap.parse_args()

    _setup_logging(args.debug)

    model_id = args.model
    npu_index = args.npu

    # 设置本进程 comm（"worker:<alias>"），让主进程 _find_workers_by_name()
    # 能通过 /proc/<pid>/comm 找到我们做 dedup。必须在 _setup_logging 后
    # 调，让 LOG.warning 报错时 comm 已经设上。
    try:
        from app.services.compute_registry import _worker_comm_name
        comm_name = _worker_comm_name(model_id)
        _set_comm_name(comm_name)
    except Exception as e:  # noqa: BLE001
        LOG.warning("could not set comm name for %s: %s", model_id, e)

    # Resolve model_dir for the info reply (don't fail if missing).
    model_dir = None
    try:
        base_dir = os.environ.get("WDNAI_MODELS_DIR", "/userdata/models/")
        from app.services.compute_registry import MODEL_PRESETS
        preset = MODEL_PRESETS.get(model_id)
        if preset:
            model_dir = os.path.join(base_dir, preset["model_dir_name"])
    except Exception:
        pass

    LOG.info("compute_worker start: model=%s service=wdndds/rpc/%s domain=0 "
             "(npu_index=%s, deferred-to-RPC=%s)",
             model_id, model_id, npu_index, npu_index is None)

    # Bring up wdndds Hub with the single service. The cshim Hub has a
    # background pump thread that calls our ``router`` for each
    # inbound request.
    from wdndds.dds._rpc import Hub
    # Discovery: point at the LAN's wdndds_daemon (one per LAN). If
    # config.json doesn't list any discovery_servers, the worker
    # falls back to SIMPLE discovery (single-host, multicast).
    discovery_servers = []
    try:
        import json as _json
        with open(_config_path()) as f:
            cfg = _json.load(f).get("compute", {})
        servers = cfg.get("discovery_servers") or []
        if isinstance(servers, str):
            servers = [s.strip() for s in servers.split(",") if s.strip()]
        discovery_servers = [s for s in servers if s]
    except (OSError, ValueError):
        pass

    LOG.info("compute_worker discovery_servers=%r (config=%s)",
             discovery_servers, _config_path())

    # State dict shared with the router callback. Initial status is
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
    }

    # Reset module-level stop event in case main() is re-entered under
    # the same interpreter (unusual but defensive).
    global _WORKER_STOP_EVENT
    _WORKER_STOP_EVENT = threading.Event()
    stop_event = _WORKER_STOP_EVENT

    device_name = _read_device_name()
    hub = Hub(
        participant_name=f"compute_worker-{device_name}-{model_id}",
        domain_id=0,
        services=[f"wdndds/rpc/{model_id}"],
        callback=lambda svc, pl: router(
            svc, pl, _worker_state, model_id, npu_index, model_dir),
        discovery_servers=discovery_servers,
    )
    hub.start()

    # Legacy compat: if --npu was passed on the command line, build the
    # session eagerly so old systemd / debug workflows keep working.
    # Production paths omit --npu and rely on the RPC load action.
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
        try:
            hub.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())