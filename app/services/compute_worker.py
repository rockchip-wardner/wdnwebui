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
import re
import signal
import socket
import sys
import threading
import time
from typing import Any, Optional, Tuple

LOG = logging.getLogger("compute_worker")


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


_EMBED_TEXT_TIMEOUT = 30.0
_EMBED_VIDEO_TIMEOUT = 60.0


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
    from app.services.chat_stream import install_patch as _install_chat_stream_patch
    _install_chat_stream_patch()
    _WDNAI = {
        "Attachment": Attachment,
        "LLMSession": LLMSession,
        "SystemLanguageModel": SystemLanguageModel,
    }
    return _WDNAI


# RPC only carries ``@Callable`` class NAMES (the class objects can't be
# pickled across the wdndds cshim boundary). Each worker holds this
# lookup table so it can resolve the name back to the actual class
# before passing ``tools=[…]`` to the SDK. Add new entries here when a
# new ``@Callable`` tool is registered.
def _resolve_tool_classes(names):
    """Map ``@Callable`` class names (strings) to actual class objects.

    真源是主进程与 worker 共享的 ``app.services.tools`` registry ——
    新增工具只需在该包内 register 一行，无需再维护手工映射表。
    Unknown names are silently skipped (and logged) so the SDK
    doesn't error out — the model simply can't call a tool it never
    sees a schema for.

    Lazy imports keep the worker boot path clean: a model that's
    never invoked with ``tools=[...]`` never imports
    ``app.services.tools.*``.
    """
    if not names:
        return []
    out = []
    for n in names:
        cls = None
        try:
            from app.services.tools import get_tool_registry
            for spec in get_tool_registry().list_specs():
                if spec.tool_class.__name__ == n:
                    cls = spec.tool_class
                    break
        except Exception as e:  # noqa: BLE001
            LOG.warning("infer_handler: tool registry lookup failed: %s", e)
        if cls is None:
            LOG.warning("infer_handler: unknown tool name %r — skipping", n)
            continue
        out.append(cls)
    return out


# qwen3-8b 输出工具调用时使用 ``<tool_call>{...}</tool_call>`` XML
# 标签包裹的 JSON 格式（与 wdnai ``try_parse_tool_call`` 期望的裸 JSON
# / markdown 围栏不同）。先按标签定位再剥离出 JSON。
_TOOL_CALL_TAG_RE = re.compile(r"<tool_call>(.*?)</tool_call>", re.DOTALL)


def _find_json_objects(text: str) -> list:
    """扫描 text 中所有顶层 JSON 对象。

    处理嵌套 ``{}`` 与字符串内的引号（状态机维护 ``in_str`` / 转义
    ``esc``）。每个候选独立 ``json.loads``，失败的跳过。结果按文本中
    出现顺序排列。
    """
    out: list = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth = 0
        in_str = False
        esc = False
        end_j = -1
        for j in range(i, n):
            ch = text[j]
            if esc:
                esc = False
                continue
            if in_str:
                if ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end_j = j
                    break
        if end_j < 0:
            # 未闭合的 ``{``（LLM 输出截断 / 编码错误）—— 强制推进游标
            # 避免外层 while 死循环。
            i += 1
            break
        try:
            data = json.loads(text[i:end_j + 1])
            if isinstance(data, dict):
                out.append(data)
        except json.JSONDecodeError:
            pass
        i = end_j + 1
    return out


def _extract_name_args(data: dict):
    """从 JSON 字典提取 (name, arguments) 元组。

    name 取 ``name`` / ``tool`` / ``function`` 任一别名；
    arguments 取 ``arguments`` / ``args`` / ``parameters`` 任一别名。
    返回 ``(None, None)`` 表示该 dict 不像 tool_call 候选。
    """
    name = data.get("name") or data.get("tool") or data.get("function")
    if not isinstance(name, str):
        return None, None
    args = (data.get("arguments") or data.get("args")
            or data.get("parameters") or {})
    return name, args


def _parse_qwen_tool_call(
    answer_text: str, tool_classes: list,
) -> Optional[Tuple[str, Any]]:
    """从模型 answer 部分解析 tool_call 并做参数校验。

    策略（双重保险）：
    1. ``<tool_call>...</tool_call>`` 标签定位（qwen3-8b 在 System 强调
       "exactly one JSON" 后通常用此格式；标签天然给出唯一边界）。
    2. fallback：扫描 answer 中所有顶层 JSON 对象，**倒序**尝试
       ``dispatch_tool_call``，最后一个 dispatch 成功的为准。

       倒序依据：LLM 在 answer 段经常先写"描述 / 示例 JSON"（如
       解释工具 schema），再写真 tool_call —— 最后一个 JSON 通常是
       最终决策。**与 prompt 强化指令（chat_runner._TOOL_CALL_INSTRUCTION）
       互为冗余**：c 让模型少输出多 JSON，b 兜底模型不守规矩。

    返回 ``(name, validated_args_dict)``，**args 已通过
    ``cls.model_validate`` 校验**（即下游主进程可直接用 args 重建
    ``@Callable`` 实例）。阶段1：调用方只透传到前端，不执行；阶段2
    改造时改用 ``args`` 重建 + 执行。
    """
    from wdnai.utils import extract_json
    from wdnai._callable import dispatch_tool_call

    # 路径 1：标签定位
    m = _TOOL_CALL_TAG_RE.search(answer_text)
    if m:
        payload = m.group(1).strip()
        data = extract_json(payload)
        if isinstance(data, dict):
            name, args = _extract_name_args(data)
            if name:
                try:
                    inst = dispatch_tool_call(tool_classes, name, args)
                    if inst is not None:
                        return name, inst.model_dump()
                except Exception:
                    pass  # 路径 1 校验失败，回退到路径 2

    # 路径 2：扫描所有顶层 JSON，倒序尝试 dispatch
    candidates = _find_json_objects(answer_text)
    for data in reversed(candidates):
        name, args = _extract_name_args(data)
        if not name:
            continue
        try:
            inst = dispatch_tool_call(tool_classes, name, args)
            if inst is not None:
                return name, inst.model_dump()
        except Exception:
            # pydantic ValidationError 等 —— 跳过此候选
            continue
    return None


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
    elif model_id == "qwen3-5-vl-4b":
        # Qwen3.5-VL-4B 多模态（kind="vlm"，MRoPE 烙印）。spec.options
        # 里 presence_penalty=1.5（来自 examples/Qwen3_5_VL/cpp/main.cc
        # 默认值，避免长 multimodal 输出的重复）。
        spec = mod["SystemLanguageModel"].qwen3_5_vl_4b()
    else:
        raise ValueError(f"unknown model_id: {model_id!r}")
    LOG.info("building LLMSession(model=%s, npu=%d) ...", model_id, npu_index)
    t0 = time.time()
    sess = mod["LLMSession"](model=spec, npu=npu_index)
    LOG.info("LLMSession ready (%.1fs)", time.time() - t0)
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
        # process caches this per ``ModelInstance`` and surfaces it
        # via ``GET /api/video-detection/available-vision-sizes`` so
        # the 音视频描述 page dropdown is data-driven, not
        # hardcoded. See ``_resolve_available_image_sizes`` for the
        # resolution rules (single-element for legacy single-file
        # installs; ``[384, 768]`` when both variants are present).
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
    return {"status": "unloaded"}


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
    vec = result
    # numpy array → JSON-friendly list. C extension returns either
    # np.ndarray or sequence-like; normalize via numpy when available.
    try:
        import numpy as _np
        arr = _np.asarray(vec, dtype=_np.float32).reshape(-1)
        LOG.info("embed_text: done in %.2fs, dim=%d",
                 _time.time() - t0, arr.size)
        return {"vec": arr.tolist(), "dim": int(arr.size)}
    except Exception:
        flat = list(vec) if hasattr(vec, "__iter__") else [float(x) for x in vec]
        LOG.info("embed_text: done in %.2fs, dim=%d (fallback)",
                 _time.time() - t0, len(flat))
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
        return {"status": "error",
                "error": "session not loaded; send action='load' first"}
    import numpy as _np
    import cv2 as _cv2

    frames: list = []
    if "frame_paths" in payload:
        paths = payload["frame_paths"]
        if not isinstance(paths, list) or not paths:
            return {"status": "error", "error": "frame_paths 必须是非空 list"}
        LOG.info("embed_video: starting (n_paths=%d, /dev/shm path-list form)",
                 len(paths))
        t0 = _time.time()
        for p in paths:
            img = _cv2.imread(p, _cv2.IMREAD_COLOR)
            if img is None:
                return {"status": "error",
                        "error": f"cv2.imread returned None for {p}"}
            frames.append(img)
    else:
        # Legacy base64 form
        import base64 as _b64
        frames_meta = payload.get("frames", [])
        if not isinstance(frames_meta, list) or not frames_meta:
            return {"status": "error", "error": "frames 必须是非空 list"}
        LOG.info("embed_video: starting (n_frames=%d, legacy base64 form)",
                 len(frames_meta))
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
    vec = result
    elapsed = _time.time() - t0
    try:
        arr = _np.asarray(vec, dtype=_np.float32).reshape(-1)
        LOG.info("embed_video: done in %.2fs (n_frames=%d, dim=%d)",
                 elapsed, len(frames), arr.size)
        return {"vec": arr.tolist(),
                "dim": int(arr.size),
                "n_frames": len(frames)}
    except Exception:
        flat = list(vec) if hasattr(vec, "__iter__") else [float(x) for x in vec]
        LOG.info("embed_video: done in %.2fs (n_frames=%d, dim=%d, fallback)",
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


def chat_run_handler(state: dict, payload: dict) -> dict:
    sess = state.get("session")
    if sess is None:
        return {"status": "error", "error": "session not loaded; send action='load' first"}
    chat = state.get("chat") or {}
    if chat.get("status") == "running":
        return {"status": "error", "error": "another chat run is already in progress"}
    history = payload.get("history")
    text = payload.get("text")
    if not isinstance(history, list):
        return {"status": "error", "error": "history 必须是 list"}
    if not isinstance(text, str) or not text.strip():
        return {"status": "error", "error": "text 必须是非空字符串"}
    try:
        context_limit = int(payload.get("context_limit") or 0)
        max_new_tokens = int(payload.get("max_new_tokens") or 2048)
    except Exception:
        context_limit, max_new_tokens = 0, 2048
    # RPC 仅透传 class name 字符串（与 infer 路径对齐）；worker 端按名
    # 查工具 registry 拿真实 class 注入到 prompt schema，并解析模型输出。
    tool_names = payload.get("tools") or []
    if not isinstance(tool_names, list):
        tool_names = []
    chat.update({
        "status": "running",
        "deltas": [],
        "offset": 0,
        "stop": False,
        "history": history,
        "text": text,
        "context_limit": context_limit,
        "max_new_tokens": max_new_tokens,
        "tool_names": tool_names,
        "result": None,
        "error": "",
    })
    state["chat"] = chat
    threading.Thread(target=_chat_run_loop, args=(state,),
                     daemon=True, name="chat-run").start()
    return {"status": "chat_started"}


def _chat_run_loop(state: dict) -> None:
    sess = state.get("session")
    chat = state.get("chat") or {}
    try:
        from app.services.chat_runner import (
            build_chatml_prompt, count_tokens, split_thinking, trim_history,
        )
        backend = getattr(sess, "_backend", None)
        if backend is None:
            raise RuntimeError("session._backend missing")
        tokenizer = getattr(backend, "tokenizer", None)
        if tokenizer is None:
            raise RuntimeError("session._backend.tokenizer missing")
        history = chat.get("history") or []
        text = chat.get("text") or ""
        context_limit = int(chat.get("context_limit") or 0)
        max_new_tokens = int(chat.get("max_new_tokens") or 2048)
        selected, err = trim_history(
            history, text, tokenizer, context_limit, max_new_tokens)
        if err:
            chat["status"] = "error"
            chat["error"] = err
            return

        # ---- 工具 schema 注入 + tool_call 解析 ----
        # 注入按 capability 过滤后的 tool_names 渲染 schema；解析则
        # **始终按 registry 全量**进行（两者解耦）：工具模型卸载后新
        # prompt 无该 schema，但模型凭历史记忆自发输出的 tool_call 仍
        # 能被识别并返回主进程 —— 主进程执行时 capability 前置校验必然
        # 抛错，错误 transcript 回喂让模型自然语言告知用户。完全编造的
        # 工具名 dispatch 校验失败，当纯文本处理。
        inject_classes = _resolve_tool_classes(chat.get("tool_names") or [])
        tool_specs = None
        if inject_classes:
            from wdnai._callable import get_tool_specs
            tool_specs = get_tool_specs(inject_classes)

        parse_classes: list = []
        try:
            from app.services.tools import get_tool_registry
            parse_classes = [
                spec.tool_class
                for spec in get_tool_registry().list_specs()]
        except Exception as e:  # noqa: BLE001
            LOG.warning("infer_handler: tool registry listing failed: %s", e)

        prompt = build_chatml_prompt(selected, text, tool_specs=tool_specs)
        chat["prompt_tokens"] = count_tokens(tokenizer, prompt)

        sink = getattr(backend, "sink", None)
        if sink is None:
            raise RuntimeError("session._backend.sink missing")

        from app.services.chat_stream import register_observer, unregister_observer

        def _on_delta(d: str) -> None:
            if d:
                chat["deltas"].append(d)

        sess.reset()
        register_observer(sink, _on_delta)
        t0 = time.time()
        # VLMBackend（Qwen3-VL / Qwen3.5-VL）只暴露 ``run_text`` 而非
        # ``run``；TextBackend 反之。chat_run 不带图像附件，所以两
        # 种 backend 都该走文本推理路径 —— 镜像 ``wdnai.session``
        # 的 dispatch 顺序（``getattr(run_text) or run``）。
        # 历史 bug：chat_run 直接 ``backend.run(prompt, ...)``，碰到
        # qwen3-5-vl-4b 等 VLM preset 时 ``AttributeError: 'VLMBackend'
        # object has no attribute 'run'`` 直接挂在 worker，main 进
        # 程 ``_chat_run_loop`` 把 RuntimeError 透传回前端的 ``error``
        # 帧，文本对话页一直停在"未启用"状态。
        run_text_fn = getattr(backend, "run_text", None) or backend.run
        try:
            full_text = run_text_fn(
                prompt, keep_history=0, enable_thinking=True)
        finally:
            unregister_observer(sink)
        elapsed = round(time.time() - t0, 2)

        # ---- 切 thinking / answer + 解析 tool_call（仅 answer 段）----
        # 先切 thinking：避免模型思考过程中输出 ``{"name":...}`` 等 JSON
        # 被误识别为 tool_call。thinking 段里也可能出现 JSON，因此 tool
        # call 解析只对 answer 部分执行。
        #
        # _parse_qwen_tool_call 内部已完成 ``dispatch_tool_call`` 校验 +
        # ``model_dump``（args 已 validated），外层不再二次 dispatch —
        # 直接取 ``args`` 作为 ``tool_call_args``。
        thinking, answer_text = split_thinking(full_text)
        tool_call_name = None
        tool_call_args = None
        if parse_classes:
            parsed = _parse_qwen_tool_call(answer_text, parse_classes)
            if parsed is not None:
                tool_call_name, tool_call_args = parsed

        chat["status"] = "done"
        chat["result"] = {
            "text": full_text,
            "thinking": thinking,
            "answer": answer_text,
            "elapsed_sec": elapsed,
            "prompt_tokens": chat.get("prompt_tokens", 0),
            "answer_tokens": count_tokens(tokenizer, answer_text),
            "tool_call": tool_call_name,
            "tool_call_args": tool_call_args,
        }
    except Exception as e:  # noqa: BLE001
        chat["status"] = "error"
        chat["error"] = f"{type(e).__name__}: {e}"


def chat_poll_handler(state: dict, payload: dict) -> dict:
    chat = state.get("chat")
    if not chat:
        return {"status": "idle", "deltas": [], "offset": 0}
    try:
        offset = max(0, int(payload.get("offset", 0)))
    except Exception:
        offset = 0
    deltas = chat.get("deltas") or []
    reply = {
        "status": chat.get("status", "idle"),
        "deltas": deltas[offset:],
        "offset": len(deltas),
    }
    if chat.get("result"):
        reply["result"] = chat["result"]
    if chat.get("error"):
        reply["error"] = chat["error"]
    return reply


def chat_stop_handler(state: dict) -> dict:
    chat = state.get("chat")
    if chat:
        chat["stop"] = True
    return {"status": "stopped"}


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
        # audio 通道 kwargs（omni run_audio）
        audio_kwargs: dict = {}
        for k in ("sample_rate", "n_channels", "sample_width"):
            if k in options:
                audio_kwargs[k] = options[k]
        # TTS 通道 kwargs（qwen3-tts TTSBackend.run_text 读这些）。
        # 不传时 SDK 用默认值：ref_speaker="girl_base", language="auto",
        # volume=0.5, no_play=False, non_streaming_mode=False — 但用户
        # 在音视频描述页的 tts_alert 工具传的 ref_speaker/volume
        # 必须流到这里才会真正生效。
        tts_kwargs: dict = {}
        for k in ("language", "speaker", "ref_speaker", "instruct",
                  "volume", "no_play", "non_streaming_mode"):
            if k in options:
                tts_kwargs[k] = options[k]

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
        elif gen_name == "TTSResult":
            # TTS：worker 必须知道 TTSResult schema，否则 SDK 走
            # ``if generating is None: return raw_text`` 分支，
            # result.attachment 永远是空 → 主进程收不到 WAV bytes
            # → 无声。tts_session.py 定义的 TTSResult 继承 LMOutput，
            # SDK 检测到 bool(attachments_out) AND issubclass(generating,
            # LMOutput) 时会把 audio bytes 挂到 result.attachment。
            try:
                from app.services.tts_session import _get_tts_result_cls
                generating_cls = _get_tts_result_cls()
            except Exception as e:  # noqa: BLE001
                LOG.warning("TTSResult schema load failed in worker: %s", e)

        async def _call():
            try:
                sess.reset()
            except Exception:
                pass
            # 合并 channel kwargs：audio 通道（omni run_audio）+ TTS 通道。
            # 顺序无关（key 不重叠），但 audio 优先覆盖以防未来 TTS 也
            # 引入 sample_rate 类参数时单边胜出（当前无重叠）。
            kwargs = {**audio_kwargs, **tts_kwargs}
            if generating_cls is not None:
                kwargs["generating"] = generating_cls
            # ``tools`` 是 wdnai 的 ``@Callable`` 类列表。RPC 只透传
            # class name 字符串（类对象不可 pickle），worker 端按名查
            # 表拿到真实 class 再传给 SDK。当前唯一已注册的工具是
            # 音视频描述页的 ``tts_alert``；后续加新工具时在
            # ``app.services/tools/`` 内 register 一行即可，无需改此处。
            # 即可。
            tool_names = payload.get("tools") or []
            tool_classes = _resolve_tool_classes(tool_names)
            if tool_classes:
                kwargs["tools"] = tool_classes
            # 视觉模型目标尺寸（音视频描述页 ``#vd-detect-size``）——
            # 主进程 detector 透传到 RPC payload，worker 直接转发给
            # ``sess.respond(image_size=...)``，backend ``run_images``
            # 据此选择对应的 vision 变体；``None``（缺省）→ 走 legacy
            # auto-pick（按帧最长边最近匹配）。
            image_size = payload.get("image_size")
            if isinstance(image_size, int) and not isinstance(image_size, bool):
                kwargs["image_size"] = image_size
            return await sess.respond(
                prompt=prompt,
                attachment=attachments if attachments else None,
                **kwargs,
            )

        LOG.info("infer: starting (prompt_len=%d, attachments=%d)",
                 len(prompt or ""), len(attachments))
        import time as _time_inf
        t0_inf = _time_inf.time()
        result = asyncio.run(_call())
        LOG.info("infer: done in %.2fs (result_type=%s)",
                 _time_inf.time() - t0_inf, type(result).__name__)
        text = getattr(result, "text", None) or (
            result if isinstance(result, str) else None)

        # Detect tool instance. SDK 在 worker 内 ``dispatch_tool_call``
        # 把模型返回的 JSON 还原成 ``@Callable`` 实例并直接 return
        # ``sess.respond()``——也就是说 ``result`` 可能是 ``TtsAlert``
        # 等实例，不是 ``VLMDoc`` / ``TargetDoc`` 结构化输出。detector
        # 端的 ``isinstance(result, TtsAlert)`` 分支需要拿到真正的实例
        # 才能 ``result(frame_bgr, ctx)``；下面把实例 ``model_dump``
        # 出来供 proxy 端 ``cls.model_validate(args)`` 重建。
        tool_call_name = type(result).__name__ if not isinstance(result, str) else None
        is_tool_instance = (
            tool_call_name is not None
            and tool_call_name not in {"str", "TTSResult", "VLMDoc", "TargetDoc"}
            and hasattr(result, "model_dump")
            and not isinstance(result, (str, bytes))
        )
        tool_call_args: Optional[dict] = None
        if is_tool_instance:
            try:
                tool_call_args = result.model_dump()
            except Exception as e:  # noqa: BLE001
                LOG.warning("tool instance dump failed: %s", e)

        # Serialize ``result.attachment`` as base64 so the wire RPC can
        # carry binary payloads (TTS WAV bytes, VLM decoded images,
        # …). Earlier this dict only had text/level/tool_call/bbox/
        # crop_b64, which meant TTS audio bytes silently disappeared
        # at the worker boundary.
        attachment_b64: Optional[str] = None
        try:
            atts = list(getattr(result, "attachment", []) or [])
            if atts and atts[0].data:
                import base64 as _b64
                attachment_b64 = _b64.b64encode(bytes(atts[0].data)).decode("ascii")
        except Exception as e:  # noqa: BLE001
            LOG.warning("attachment serialize failed: %s", e)

        reply = {
            "text": text,
            "level": getattr(result, "level", None),
            "tool_call": tool_call_name if is_tool_instance else getattr(result, "tool_call", None),
            "bbox": getattr(result, "bbox", None),
            "crop_b64": getattr(result, "crop_b64", None),
        }
        # v0.10 修复：当 ``generating=SceneFrame`` / TargetDoc 等 pydantic
        # 类被 SDK 成功解析时，result 是 BaseModel 实例，但其字段（scene /
        # actors / action / objects / text / note 等）并不在 ``reply`` 里
        # —— 历史 reply 只搬 text/level/tool_call/bbox/crop_b64，把结构化
        # 字段全部丢在 worker 边界。主进程 ``_ProxyResult._raw`` 收到这份
        # 精简 reply，导致 VFA 的 ``_split_scene_frame_result`` 看到空
        # structured。这里 model_dump 把结构化字段塞进 reply["structured"]，
        # 主进程 ``rpc_session_proxy`` 那边解析后填进 ``_ProxyResult._raw``，
        # VFA Path 2 就能拿到完整字段。
        #
        # 弹掉 ``attachment``：LMOutput 的 attachment 字段是
        # ``List[Attachment]``，Attachment 是 plain class（不是
        # pydantic model）— model_dump 要么把 Attachment 实例原样塞进
        # dict，要么直接抛 PydanticUserError，两种情况都会让 reply
        # 无法 JSON 序列化（cshim 在 worker 边界 json.dumps 整个
        # reply）。audio bytes 已经走 ``attachment_b64`` 单独走线，主进程
        # proxy 从那里重建 ``_AttachmentProxy``（rpc_session_proxy.py:
        # 287-299）— pop 掉 result_data.attachment 对 TTS / VLM
        # 重构无影响。
        if not is_tool_instance and not isinstance(result, (str, bytes, dict)):
            try:
                if hasattr(result, "model_dump") and callable(getattr(result, "model_dump", None)):
                    _dump = result.model_dump()
                    if isinstance(_dump, dict) and _dump:
                        _dump.pop("attachment", None)
                        if _dump:
                            reply["structured"] = _dump
            except Exception as _dump_ex:
                LOG.warning("model_dump failed: %s", _dump_ex)
        if attachment_b64 is not None:
            reply["attachment_b64"] = attachment_b64
        if is_tool_instance and tool_call_args is not None:
            reply["tool_call_args"] = tool_call_args
        # Schema instances (TargetDoc / VLMDoc / TTSResult) — forward the
        # full ``model_dump()`` so the proxy can reconstruct the actual
        # class. The ``text / level / bbox / crop_b64`` slots above only
        # cover VLMDoc's surface; for TargetDoc (``x1 / y1 / x2 / y2 /
        # desc``) every structured field would otherwise be lost at the
        # worker boundary and the detector's ``isinstance(result,
        # TargetDoc)`` branch never matches on the main side. Mirrors the
        # ``tool_call_args`` pattern for ``@Callable`` reconstruction
        # (see ``_run_tool_side_effect`` docstring). 同上 strip
        # ``attachment`` 让 reply 可 JSON 序列化。
        if (not is_tool_instance
                and type(result).__name__ in {"VLMDoc", "TargetDoc", "TTSResult"}):
            try:
                _dump = result.model_dump()
                if isinstance(_dump, dict):
                    _dump.pop("attachment", None)
                    reply["result_data"] = _dump
            except Exception as e:
                LOG.warning(
                    "schema instance dump failed (%s): %s",
                    type(result).__name__, e,
                )
        return reply
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
    if action == "chat_run":
        return chat_run_handler(state, payload)
    if action == "chat_poll":
        return chat_poll_handler(state, payload)
    if action == "chat_stop":
        return chat_stop_handler(state)
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
    """Resolve config.jsonc path from WDNWEBUI_CONFIG env var, defaulting
    to the repo root config.jsonc."""
    import os
    return os.environ.get('WDNWEBUI_CONFIG') or '/userdata/wdnwebui/config.jsonc'


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
    ap.add_argument("--local-only", action="store_true",
                    help="主进程 spawn 本机 worker 时传:worker 只注册 per-PID "
                         "service (wdndds/rpc/<model_id>/worker-<pid>)，不注册 "
                         "标准 service。避免与 LAN peer 共用 standard topic 造成 "
                         "FastDDS 路由冲突(否则本机 probe 的 info RPC 会被路由到 "
                         "本机 spawned worker，adopted (NETWORK) 卡 status 翻转)。"
                         "仅 main process spawn 时使用;手动启动 / debug 路径不传。")
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
    services = [
        f"wdndds/rpc/{model_id}/worker-{_WORKER_PID}",
        f"wdndds/rpc/{model_id}",
    ]
    hub = Hub(
        participant_name=f"compute_worker-{device_name}-{model_id}",
        domain_id=0,
        services=services,
        callback=lambda svc, pl: router(
            svc, pl, _worker_state, model_id, npu_index, model_dir),
        discovery_servers=[],
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