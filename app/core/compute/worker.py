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
# Response-side /dev/shm path-list (mirror of main's _bgr_ndarray_to_path)
# --------------------------------------------------------------------------- #
# ComputeReply IDL has 11 unbounded string fields, each capped at the
# FastCDR default 255-byte assumption (ComputeReply_max_cdr_typesize =
# 2653 bytes total). Stuffing ``{"structured": ..., "result_data": ...,
# "crop_b64": "<50KB base64>"}`` into the single ``text`` field would
# silently truncate when desc/crop grew, breaking JSON parse downstream
# ("malformed reply: '...{...一辆停'" — text cut mid-Chinese-character).
#
# Fix: route heavy payloads through /dev/shm path-list, just like the
# request side does for BGR ndarray attachments (``data_path``/``shape``/
# ``dtype``). The wire only carries a ~50-byte path string per payload,
# keeping the entire reply dict inside the 255-byte IDL field cap. The
# main process reads the file (in ``RpcSessionProxy._sync_call``) and
# inlines the result back into the ``_ProxyResult`` so the rest of the
# detector / VFA code path stays unchanged.
_REPLY_SHM_DIR = "/dev/shm"
_REPLY_SHM_PREFIX = "wdnwebui_cr_"  # cr = compute reply (crop / result_data)

# v0.15 (LAN peer 降级):
# 主进程 proxy 在 is_remote=True (LAN peer worker) 时透传该 flag,
# worker 端跳过所有 /dev/shm path-list 路由, 防止 reply 字段超过
# FastCDR 255 字节硬截断 → 主进程 JSON parse 失败。后果:
# - text / result_data / structured / crop_b64 大字段一律 drop
# - 主进程拿到 None / 空 _ProxyResult._raw, VFA Path 1 garbage
#   检标"本帧分析失败"占位, 不崩
# - crop_b64 走主进程 frame_bgr 重新裁切兜底 (现有行为)
# 一次性 WARNING 标志: per-worker 进程内只 log 一次, 避免每帧
# repeat 刷屏 (VFA 每秒 1 帧, 1 小时 = 3600 行 log 噪声)。
_LAN_PEER_WARNED: bool = False


def _write_reply_payload_to_shm(payload: bytes, suffix: str) -> Optional[str]:
    # Failure → caller drops the field rather than crashing the worker.
    try:
        os.makedirs(_REPLY_SHM_DIR, exist_ok=True)
    except OSError:
        return None
    name = f"{_REPLY_SHM_PREFIX}{uuid.uuid4().hex}.{suffix}"
    path = os.path.join(_REPLY_SHM_DIR, name)
    try:
        with open(path, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
    except OSError as e:
        LOG.warning("reply shm write failed for %s: %s", suffix, e)
        return None
    return path


def _write_crop_b64_to_shm(b64_str: str) -> Optional[str]:
    import base64 as _b64
    try:
        raw = _b64.b64decode(b64_str)
    except Exception:
        return None
    return _write_reply_payload_to_shm(raw, "jpg")


def _write_result_data_to_shm(result_data: dict) -> Optional[str]:
    import json as _json
    try:
        encoded = _json.dumps(result_data, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError):
        return None
    return _write_reply_payload_to_shm(encoded, "json")


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
# None = DDS init 失败，infer_handler 在 UDS 也失败时退回 WAV-only。
_AUDIO_CHUNK_WRITER: Any = None


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
            AudioAttachment,
            ImageAttachment,
            LLMSession,
            SystemLanguageModel,
            TextAttachment,
        )
    except ImportError as e:
        raise RuntimeError(f"wdnai import failed: {e}")
    try:
        from app.core.chat.stream import install_patch as _install_chat_stream_patch
        _install_chat_stream_patch()
    except Exception as e:  # noqa: BLE001 — patch 失败只损失 chat 流式，不阻断加载
        LOG.warning("chat_stream patch 安装失败（chat 将无流式）：%s", e)
    _WDNAI = {
        "Attachment": Attachment,
        "AudioAttachment": AudioAttachment,
        "ImageAttachment": ImageAttachment,
        "TextAttachment": TextAttachment,
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
            from app.core.tools.registry import get_tool_registry
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


# qwen3-4b 输出工具调用时使用 ``<tool_call>{...}</tool_call>`` XML
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
    1. ``<tool_call>...</tool_call>`` 标签定位（qwen3-4b 在 System 强调
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
    elif model_id == "qwen3-4b":
        from dataclasses import replace as _dc_replace
        spec = mod["SystemLanguageModel"].qwen3_4b()
        spec = _dc_replace(spec, max_context_len=16384).with_options(
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
    elif model_id == "qwen3.5-vl-4b":
        # Qwen3.5-VL-4B 多模态（kind="vlm"，MRoPE 烙印）。spec.options
        # 里 presence_penalty=1.5（来自 examples/Qwen3_5_VL/cpp/main.cc
        # 默认值，避免长 multimodal 输出的重复）。
        spec = mod["SystemLanguageModel"].qwen3_5_vl_4b()
    elif model_id == "LocateAnything-3b":
        spec = mod["SystemLanguageModel"].locate_anything()
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
        LOG.warning("embed_video: session not loaded — rejecting request "
                    "(n_paths=%s)", len(payload.get("frame_paths") or
                                        payload.get("frames") or []))
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
                # 读失败必须可见 —— OpenCV 只往 stderr 打一行 WARN，
                # handler 层再记一条带存在性/大小的，事后排查才有据。
                import os as _os
                LOG.warning(
                    "embed_video: cv2.imread failed for %s (exists=%s "
                    "size=%s)", p, _os.path.exists(p),
                    _os.path.getsize(p) if _os.path.exists(p) else -1)
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


def _clear_backend_kv(backend) -> None:
    """显式清理 backend 的 C 侧 KV cache（chat 轮次之间）。

    wdnai ``LLMSession.reset()`` 取 ``self._backend.rknn_llm``，但
    TextBackend（chat 链路的唯一 backend）的实例属性名是 ``self.rknn``
    —— reset 对 text 恒为 no-op，导致 agent 循环 / 多轮对话之间 KV
    历史累积（真机实测：第二轮把第一轮的输出一起续写出来，并生成
    ``<|im_start|>user`` / ``<|im_end|>`` 等 chatml 标记，经 GGUF
    tokenizer 的字面 decode 泄漏到最终回复）。

    这里按 backend 的真实属性名兜底清一次（``rknn`` 优先 —— 见
    ``wdnai/backends/text.py``；``rknn_llm`` 兜底 —— 其它 backend）。
    SDK 侧 ``reset()`` 修好后两者并存不冲突。
    """
    for attr in ("rknn", "rknn_llm"):
        obj = getattr(backend, attr, None)
        if obj is not None and hasattr(obj, "clear_kvcache"):
            try:
                obj.clear_kvcache()
            except Exception as e:  # noqa: BLE001
                LOG.warning(
                    "chat_run: clear_kvcache via backend.%s failed: %s",
                    attr, e)
            return


def _chat_run_loop(state: dict) -> None:
    sess = state.get("session")
    chat = state.get("chat") or {}
    try:
        from app.core.chat.runner import (
            build_chatml_prompt, count_tokens, sanitize_chat_output,
            split_thinking, trim_history,
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
            from app.core.tools.registry import get_tool_registry
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

        from app.core.chat.stream import register_observer, unregister_observer

        # ---- SHM 快照发布（配 rpc_session_proxy.chat_run 的 via_shm
        # 双通道协议）----
        # proxy 每 150ms 读 /dev/shm/wdnwebui_chat_<model>.json 拿
        # status/deltas；RPC chat_poll 仅作回退。不发布快照时 poll RPC
        # 会撞 worker 推理期间 pump 线程 35-60s 的 GIL 聋哑窗口
        # （实测中发现，iter2 poll 重试 51s 耗尽 raise
        # "chat_poll failed: timeout"，agent 循环因 error 残留会把上一轮
        # tool_call 原文当最终回答发给微信）。
        _chat_shm_path = f"/dev/shm/wdnwebui_chat_{state.get('model_id') or 'chat'}.json"
        _chat_start_ts = time.time()

        def _publish_chat_snapshot() -> None:
            """原子发布 chat 状态快照。失败静默 —— 快照是数据面加速
            通道，proxy 在读不到时自动回退 chat_poll RPC。"""
            try:
                snap = {
                    "start_ts": _chat_start_ts,
                    "status": chat.get("status") or "running",
                    "deltas": list(chat.get("deltas") or []),
                }
                if chat.get("status") == "done":
                    snap["result"] = dict(chat.get("result") or {})
                elif chat.get("status") == "error":
                    snap["error"] = chat.get("error") or ""
                tmp = f"{_chat_shm_path}.{os.getpid()}.tmp"
                with open(tmp, "w") as f:
                    json.dump(snap, f)
                os.replace(tmp, _chat_shm_path)
            except Exception:  # noqa: BLE001
                pass

        def _on_delta(d: str) -> None:
            if d:
                chat["deltas"].append(d)
                _publish_chat_snapshot()

        sess.reset()
        # LLMSession.reset() 对 TextBackend 是 no-op（属性名不匹配）——
        # 显式兜底清 C 侧 KV，避免 agent 循环 / 多轮之间历史累积。
        _clear_backend_kv(backend)
        _publish_chat_snapshot()
        register_observer(sink, _on_delta)
        t0 = time.time()
        # VLMBackend（Qwen3-VL / Qwen3.5-VL）只暴露 ``run_text`` 而非
        # ``run``；TextBackend 反之。chat_run 不带图像附件，所以两
        # 种 backend 都该走文本推理路径 —— 镜像 ``wdnai.session``
        # 的 dispatch 顺序（``getattr(run_text) or run``）。
        # 历史 bug：chat_run 直接 ``backend.run(prompt, ...)``，碰到
        # qwen3.5-vl-4b 等 VLM preset 时 ``AttributeError: 'VLMBackend'
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

        # 诊断日志（截断）：raw output 与 prompt 对照定位 chatml 标记
        # 泄漏 / 多轮续写类问题 —— 只靠 prompt_head 无法判断模型到底
        # 生成了什么。
        LOG.info("chat_run: prompt_len=%d prompt=%r", len(prompt), prompt[:1200])
        LOG.info("chat_run: raw_output_len=%d raw_output=%r",
                 len(full_text), full_text[:1200])
        # 清理模型越界生成的 chatml 标记 / 截断自造的下一回合
        # （KV 未清时的续写产物；详见 chat_runner.sanitize_chat_output）。
        full_text = sanitize_chat_output(full_text)

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
        _publish_chat_snapshot()
    except Exception as e:  # noqa: BLE001
        chat["status"] = "error"
        chat["error"] = f"{type(e).__name__}: {e}"
        try:
            _publish_chat_snapshot()
        except Exception:  # noqa: BLE001
            pass


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

    # ---- 诊断日志 (768 切档后无返回) -------------------------------------
    # 4 类故障定位：
    #   1) worker 没收到 call        → 主进程 log 看 RPC fail / timeout
    #   2) worker 收到但 sess.respond 抛错/挂死 → 看下面 "infer: exception" / "infer: started but no 'done'"
    #   3) worker 算出结果但 reply 没回主进程 → 看 "infer: done" 后是否接 "infer_handler: returning reply"
    #   4) reply 格式坏掉主进程解析失败  → 看 reply 的 keys / 序列化大小
    import time as _t_inf
    _t_req_in = _t_inf.time()
    _req_id = payload.get("request_id") or f"local-{_t_inf.time_ns()}"
    _diag_image_size = payload.get("image_size")
    _diag_atts_meta_count = len(payload.get("attachment", []) or [])
    _diag_generating = payload.get("generating")
    _diag_tools = payload.get("tools") or []
    try:
        _diag_prompt_preview = (payload.get("prompt") or "")[:50]
    except Exception:
        _diag_prompt_preview = "<unreadable>"
    LOG.info(
        "infer_handler[%s]: ENTER image_size=%r attachments=%d "
        "generating=%r tools=%r prompt=%r",
        _req_id, _diag_image_size, _diag_atts_meta_count,
        _diag_generating, _diag_tools, _diag_prompt_preview,
    )

    try:
        mod = _load_wdnai()
        Attachment = mod["Attachment"]
        AudioAttachment = mod["AudioAttachment"]
        ImageAttachment = mod["ImageAttachment"]
        TextAttachment = mod["TextAttachment"]
        prompt = payload.get("prompt")
        atts_meta = payload.get("attachment", [])
        # v0.15 LAN peer 降级标记: 主进程 proxy 在 ``is_remote=True`` 时
        # (跨机 worker, 主机 hostname 不同) 透传该 flag。worker 端跳过
        # 全部 /dev/shm path-list 路由 (4 个块: crop_b64 / result_data /
        # structured / text) → 大字段一律 drop, 避免 FastCDR 255 字节
        # 截断崩 wire。同机 (``is_remote=False``, 默认) 行为不变。
        is_remote = bool(payload.get("is_remote", False))
        if is_remote and not _LAN_PEER_WARNED:
            globals()["_LAN_PEER_WARNED"] = True
            LOG.warning(
                "compute_worker[%s]: LAN peer mode (is_remote=True) — "
                "skipping /dev/shm path-list for all reply payloads. "
                "Large text / structured / result_data / crop_b64 will be "
                "dropped (FastCDR 255-byte cap on inline fields). "
                "Consumer fallback: VFA Path 1 garbage → 解析失败 "
                "placeholder (no crash); crop_b64 re-crop from frame_bgr. "
                "Long-term fix: IDL change to sequence<octet> or "
                "per-call heavy payload topic — see AGENTS.md TODO.",
                state.get("model_id", "?"),
            )
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
        # TTS PCM 分片通过 UDS 回传主进程：bound method 无法跨 wdndds RPC
        # pickle，缺这条路径 SDK 的 on_audio_chunk 就只能为 None，每片
        # 解码窗口静默丢弃。线缆格式: struct.pack("<III", sr, ch, n)
        # + float32 LE PCM —— 与 TtsSession._on_audio_chunk 一致。
        tts_pcm_socket_path = options.get("tts_pcm_socket_path")
        tts_pcm_client = None
        if isinstance(tts_pcm_socket_path, str) and tts_pcm_socket_path:
            try:
                tts_pcm_client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                tts_pcm_client.settimeout(2.0)
                tts_pcm_client.connect(tts_pcm_socket_path)
            except Exception as e:  # noqa: BLE001
                # UDS 连失败，两条非致命路径：① LAN DDS topic writer
                # (主进程订阅 wdndds/audio/tts_chunk/<model_id>，BEST_EFFORT
                # 丢片留静默 gap 但不阻塞 SDK decoder)；② 只剩 WAV。
                # writer boot 时 init 成功走 ①，否则走 ②。
                LOG.warning(
                    "tts_pcm_socket: connect to %s failed (%s)",
                    tts_pcm_socket_path, e)
                if tts_pcm_client is not None:
                    try:
                        tts_pcm_client.close()
                    except Exception:
                        pass
                    tts_pcm_client = None
                if _AUDIO_CHUNK_WRITER is not None:
                    def _pcm_chunk_dds_forwarder(
                            audio, sr, _w=_AUDIO_CHUNK_WRITER):
                        try:
                            import struct as _struct
                            import numpy as _np
                            arr = _np.asarray(audio, dtype="<f4").reshape(-1)
                            header = _struct.pack(
                                "<III", int(sr), 1, int(arr.size))
                            # write() best-effort：丢片返 0（静默），
                            # 硬错（writer 关闭 / cshim）返非零；外层
                            # try/except 兜 numpy copy / pack 异常，
                            # 防止坏分片毒死 SDK decoder worker。
                            _w.write(header + arr.tobytes())
                        except Exception:
                            pass
                    tts_kwargs["on_audio_chunk"] = _pcm_chunk_dds_forwarder
                    LOG.info("tts_pcm: using DDS topic writer (LAN fallback)")
                else:
                    LOG.info("tts_pcm: no DDS writer available; "
                             "falling back to WAV-only path")
            else:
                # 本机快路径：AF_UNIX 直连主进程 reader，线缆格式与
                # DDS 路径一致，主进程 _on_audio_chunk 透明兼容两者。
                def _pcm_chunk_forwarder(audio, sr, _c=tts_pcm_client):
                    try:
                        import struct as _struct
                        import numpy as _np
                        arr = _np.asarray(audio, dtype="<f4").reshape(-1)
                        header = _struct.pack("<III", int(sr), 1, int(arr.size))
                        _c.sendall(header + arr.tobytes())
                    except Exception:
                        pass
                tts_kwargs["on_audio_chunk"] = _pcm_chunk_forwarder
                LOG.info("tts_pcm: using Unix domain socket (local fast path)")

        attachments = []
        for item in atts_meta:
            if isinstance(item, dict) and "data_path" in item:
                # 主进程写到 /dev/shm 的原始 BGR ndarray (无 JPEG 损失)。
                # 这是音视频描述 / 视频目标检测页的默认路径。
                # 配套说明见 ``rpc_session_proxy._bgr_ndarray_to_path``
                # 顶部的 4 点 rationale —— 简言之：wire 只传 ~150 字节
                # path 描述符，worker ``np.fromfile().reshape()`` 还原
                # byte-exact 帧，零 DDS serialize / 反序列化开销。
                # 跨机时 LAN peer 拿不到 caller 的 /dev/shm → FileNotFoundError
                # （proxy 侧 is_remote=True 已经切回 base64 JPEG 兜底，
                #  不会进这一分支）。
                path = item["data_path"]
                kind = item.get("kind") or "image"
                if kind == "audio":
                    # Audio path-list (mirror of image above): raw PCM
                    # bytes on tmpfs; open().read() rebuilds byte-exact.
                    try:
                        with open(path, "rb") as _f:
                            raw_audio = _f.read()
                        attachments.append(AudioAttachment(
                            raw_audio,
                            mime_type=item.get("mime_type", "audio/wav"),
                        ))
                    except FileNotFoundError:
                        LOG.warning("infer: data_path %s missing (skipping)", path)
                    except Exception as _e:
                        LOG.warning("infer: data_path %s read failed (%s)", path, _e)
                    finally:
                        try:
                            import os as _os
                            if _os.path.exists(path):
                                _os.unlink(path)
                        except OSError:
                            pass
                    continue
                shape = item.get("shape") or [0, 0, 3]
                dtype = item.get("dtype", "uint8")
                img = None
                try:
                    import numpy as _np
                    raw = _np.fromfile(path, dtype=dtype)
                    if dtype == "uint8" and len(shape) == 3 and shape[2] == 3 \
                            and raw.size == shape[0] * shape[1] * shape[2]:
                        img = raw.reshape(shape)
                    else:
                        # Fallback: try cv2.imread (PNG fallback) if raw reshape fails
                        # （主进程写 raw bytes 时只有 uint8 BGR HxWx3 路径命中，
                        # 其他 dtype / shape 通过这条 fallback 走 PNG/JPEG 解码）。
                        import cv2 as _cv2
                        img = _cv2.imread(path, _cv2.IMREAD_COLOR)
                except FileNotFoundError:
                    LOG.warning("infer: data_path %s missing (skipping)", path)
                except Exception as e:
                    LOG.warning("infer: data_path %s failed (%s)", path, e)
                finally:
                    # 立即回收 /dev/shm 临时文件，避免每秒 1 帧堆积。
                    try:
                        import os as _os
                        if _os.path.exists(path):
                            _os.unlink(path)
                    except OSError:
                        pass
                if img is not None:
                    attachments.append(ImageAttachment(img))
                continue
            if isinstance(item, dict) and "data_b64" in item:
                # audio 走 tag dict（``kind=="audio"``）；图片走裸 base64 字符串。
                import base64 as _b64
                raw = _b64.b64decode(item["data_b64"])
                if item.get("kind") == "audio":
                    attachments.append(AudioAttachment(
                        raw, mime_type=item.get("mime_type", "audio/wav"),
                    ))
                    continue
                import numpy as _np
                arr = _np.frombuffer(raw, dtype=_np.uint8)
                import cv2 as _cv2
                img = _cv2.imdecode(arr, _cv2.IMREAD_COLOR)
                if img is not None:
                    attachments.append(ImageAttachment(img))
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
                        attachments.append(ImageAttachment(img))
                        continue
                except Exception:
                    pass
                attachments.append(TextAttachment(item))
                continue
            if hasattr(item, "shape"):
                attachments.append(ImageAttachment(item))

        import asyncio
        generating_cls = None
        gen_name = payload.get("generating")
        if gen_name == "VLMDoc":
            try:
                from app.core.llm.detector_base import _get_vlm_doc_cls
                generating_cls = _get_vlm_doc_cls()
            except Exception as e:  # noqa: BLE001
                LOG.warning("VLMDoc schema load failed in worker: %s", e)
        elif gen_name == "TargetDoc":
            try:
                from app.core.llm.detector_base import _get_target_doc_cls
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
                from app.apps.tts.service import _get_tts_result_cls
                generating_cls = _get_tts_result_cls()
            except Exception as e:  # noqa: BLE001
                LOG.warning("TTSResult schema load failed in worker: %s", e)
        elif gen_name is not None:
            # 诊断：未识别的 gen_name。RPC payload 引用了一种 worker 没
            # 听过的 schema 类 —— 可能主进程 / worker 版本错位、或
            # 有人在 schema 列表里加了新条目忘了同步 worker。
            LOG.warning(
                "infer_handler[%s]: unrecognized gen_name=%r (no schema "
                "lookup) — sess.respond() will fall through to raw-text "
                "parse path",
                _req_id, gen_name,
            )

        async def _call():
            try:
                sess.reset()
            except Exception:
                pass

            # ---------------------------------------------------------------- #
            # 诊断 (v0.13): monkey-patch SDK 让 worker 在 raw model output 进    #
            # ``_parse_output`` 之前先 dump 一次。这才是模型**真正**的输出(没有 #
            # 经过 SDK 的 JSON 提取 / strip fence / model_validate)。              #
            # 这步让"模型吐了啥"和"SDK 把它变成啥"分开看 —— 用户怀疑              #
            # "reply 是 cache" 用这条日志直接证实。                                       #
            # ---------------------------------------------------------------- #
            _orig_parse_output = None
            try:
                if hasattr(sess, "_parse_output"):
                    _orig_parse_output = sess._parse_output

                    def _diag_parse_output(raw_text, generating,
                                            attachments_out=None):
                        try:
                            LOG.debug(
                                "diag_raw_model_output[%s]: type=%s "
                                "len=%d content=%r",
                                _req_id,
                                type(raw_text).__name__,
                                len(str(raw_text)),
                                str(raw_text)[:600],
                            )
                        except Exception:
                            pass
                        return _orig_parse_output(
                            raw_text, generating, attachments_out)

                    sess._parse_output = _diag_parse_output
            except Exception as e:  # noqa: BLE001
                LOG.debug("diag[%s]: parse_output monkey-patch failed: %s",
                          _req_id, e)
            # 合并 channel kwargs：audio 通道（omni run_audio）+ TTS 通道。
            # 顺序无关（key 不重叠），但 audio 优先覆盖以防未来 TTS 也
            # 引入 sample_rate 类参数时单边胜出（当前无重叠）。
            kwargs = {**audio_kwargs, **tts_kwargs}
            # ---------------------------------------------------------------- #
            # worker 是 thin RPC server:直接把 client 传的 ``generating`` /
            # ``tools`` 透传给 wdnai SDK,prompt 构造 / schema hint 注入由
            # SDK 的 ``_maybe_inject_json_hint`` + ``@Generable`` 装饰器
            # 自动完成。worker 不干预 SDK 内部行为 (sdk 行为由
            # ``@Generable`` 定义,client 应用 (detector) 用 ``generating=``
            # 触发 schema 注入 + 用 ``tools=`` 注入 function-call schema)。
            # 如果模型输出格式异常, 由 client 端 (detector) 应用层 rescue
            # 解析,见 ``app/services/detector.py``。
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
            # ---------------------------------------------------------------- #
            # 诊断 (v0.13): 核对 worker 实际传给 SDK 的 prompt 与 image 是不是  #
            # 我们想要的。如果 RPC 链路中间 prompt 被改动 (stale cache / 序列化  #
            # 丢字段) 或 image bytes 损坏,模型会输出非预期格式 —— 用户怀疑    #
            # "reply 是 cache",这条日志直接证实。                                     #
            # ---------------------------------------------------------------- #
            # 1) RPC payload user_prompt (worker 收到的)
            LOG.debug(
                "diag_infer_input[%s]: prompt=%r attachments=%d kwargs_keys=%s",
                _req_id, prompt, len(attachments),
                sorted(kwargs.keys()),
            )
            # 2) attachment 元信息 (验 /dev/shm 路径读出来的 ndarray 是否完整)
            for _ai, _att in enumerate(attachments):
                try:
                    _img = getattr(_att, "images", None) or getattr(_att, "frames", None)
                    if _img:
                        _arr = _img[0]
                        LOG.info(
                            "diag_infer_input[%s]: att[%d] shape=%s "
                            "dtype=%s size=%d bytes",
                            _req_id, _ai,
                            getattr(_arr, "shape", None),
                            getattr(_arr, "dtype", None),
                            getattr(_arr, "nbytes", 0),
                        )
                    else:
                        LOG.info(
                            "diag_infer_input[%s]: att[%d] non-image "
                            "kind=%s mime=%s data_len=%d",
                            _req_id, _ai,
                            getattr(_att, "kind", "?"),
                            getattr(_att, "mime_type", "?"),
                            len(getattr(_att, "data", b"") or b""),
                        )
                except Exception as e:
                    LOG.debug("diag_infer_input[%s]: att[%d] inspect failed: %s",
                              _req_id, _ai, e)
            # 3) kwargs 细节
            LOG.debug(
                "diag_infer_input[%s]: generating=%s tools=%s image_size=%s",
                _req_id,
                getattr(kwargs.get("generating"), "__name__", None),
                [t.__name__ for t in kwargs.get("tools", []) or []],
                kwargs.get("image_size"),
            )
            # 4) SDK 最终 prompt (用 ``_compose_prompt`` 模拟)
            try:
                mod_session = importlib.import_module("wdnai.session")
                LLMSession_cls = getattr(mod_session, "LLMSession", None)
                if LLMSession_cls is not None:
                    _tool_specs = []
                    try:
                        from wdnai._callable import get_tool_specs as _gts
                        _tool_specs = _gts(kwargs.get("tools") or [])
                    except Exception:
                        pass
                    _final_prompt = LLMSession_cls._compose_prompt(
                        prompt, kwargs.get("generating"), _tool_specs,
                    )
                    LOG.debug(
                        "diag_infer_input[%s]: FINAL prompt to model "
                        "(%d chars):\n%s",
                        _req_id, len(_final_prompt), _final_prompt,
                    )
            except Exception as e:
                LOG.debug("diag_infer_input[%s]: _compose_prompt dump "
                          "failed: %s", _req_id, e)

            # 调 sess.respond 后立刻还原 _parse_output patch,防止同 sess 上
            # 下次 call (chat_run / chat_poll) 累积包装。
            try:
                _resp = await sess.respond(
                    prompt=prompt,
                    attachment=attachments if attachments else None,
                    **kwargs,
                )
            finally:
                try:
                    if _orig_parse_output is not None and hasattr(sess, "_parse_output"):
                        sess._parse_output = _orig_parse_output
                except Exception:
                    pass
            return _resp

        LOG.info("infer: starting (prompt_len=%d, attachments=%d) image_size=%r",
                 len(prompt or ""), len(attachments), _diag_image_size)
        import time as _time_inf
        t0_inf = _time_inf.time()
        try:
            result = asyncio.run(_call())
        except Exception as _infer_exc:
            LOG.warning(
                "infer_handler[%s]: sess.respond raised in %.2fs: %s: %s",
                _req_id, _time_inf.time() - t0_inf,
                type(_infer_exc).__name__, _infer_exc,
            )
            raise
        finally:
            if tts_pcm_client is not None:
                try:
                    tts_pcm_client.close()
                except Exception:
                    pass
        # 诊断：拿到 result 之后的实际属性
        try:
            _rtext_len = len(getattr(result, "text", "") or "")
        except Exception:
            _rtext_len = -1
        try:
            _ratts_count = len(list(getattr(result, "attachment", []) or []))
        except Exception:
            _ratts_count = -1
        LOG.info(
            "infer: done in %.2fs (result_type=%s, text_len=%d, attachment_count=%d) "
            "image_size=%r",
            _time_inf.time() - t0_inf, type(result).__name__,
            _rtext_len, _ratts_count, _diag_image_size,
        )
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
            and tool_call_name not in {
                "str", "TTSResult", "VLMDoc", "TargetDoc",
                "LocatorDoc", "GroundingResult",
            }
            and hasattr(result, "model_dump")
            and not isinstance(result, (str, bytes))
        )
        tool_call_args: Optional[dict] = None
        if is_tool_instance:
            try:
                tool_call_args = result.model_dump()
            except Exception as e:  # noqa: BLE001
                LOG.warning("tool instance dump failed: %s", e)

        # 把 result.attachment 序列化为 base64 让 wire RPC 能携带二进制
        # payload（VLM 解码图、embed 表…）。TTS 音频字节故意跳过 ——
        # 走 DDS / UDS audio chunks live 流回主进程，TtsSession 在
        # main 侧从 chunk 重组 WAV。把 WAV 再塞回 reply 会 (a) 重复占
        # 两条传输、(b) 长文 reply 超过 1 MB 后 cshim RPC replier 的
        # publisher 线程（与 audio chunk writer 共享）卡到 120 s 超时
        # —— 而音频早已流完。解耦后 reply 恒 ~200 B。
        is_tts_result = (
            type(result).__name__ == "TTSResult"
            or (state.get("model_id") == "qwen3-tts")
        )
        attachment_b64: Optional[str] = None
        if is_tts_result:
            LOG.info(
                "tts: skipping attachment_b64 (DDS chunks carry PCM); "
                "text_len=%d, attachment_count=%d",
                len(getattr(result, "text", "") or ""),
                len(list(getattr(result, "attachment", []) or [])),
            )
        else:
            try:
                atts = list(getattr(result, "attachment", []) or [])
                if atts and atts[0].data:
                    import base64 as _b64
                    attachment_b64 = _b64.b64encode(bytes(atts[0].data)).decode("ascii")
            except Exception as e:  # noqa: BLE001
                LOG.warning("attachment serialize failed: %s", e)

        # v0.12 修复 ("malformed reply" 视频目标检测截断):
        # ComputeReply IDL 把整个 reply JSON dict 塞进单 string 字段
        # ``text``，单字段默认 255 字节上限。TargetDoc/VLMDoc/TTSResult
        # 的 reply 同时塞了 ``structured`` 和 ``result_data``（两份相同
        # model_dump），desc 一长就超 255 → FastCDR 静默截断 → 主进程
        # JSON parse 失败报 "malformed reply: '...{...一辆停'"。
        # 修法 (a) schema-instance 不再写 ``structured`` —— ``result_data``
        # 已覆盖；SceneFrame 这种没 ``result_data`` 的才写 ``structured``。
        # (b) ``crop_b64``（JPEG base64，50 KB+）和超过 200 字节的
        # ``result_data`` 走 /dev/shm path-list，wire 只传 ~50 字节 path。
        # 主进程 ``RpcSessionProxy._sync_call`` 读文件后填回 ``crop_b64`` /
        # ``result_data``，detector / VFA 看到的是原 shape。
        #
        # v0.14 修复 (VFA SceneFrame Path 5 兜底截断):
        # ``text`` 字段同样受 255 字节限制。VFA SceneFrame 路径下 SDK 解析
        # 失败 (extract_json 返 None) 时, wdnai/session.py:
        # _construct_text_instance 构造 SceneFrame(text=<raw_text>,
        # scene="", actors=[], action="", objects=[], note="") 把整段
        # 模型原始输出塞进 .text。ab93f37 删了 VFA prompt 示例引导后,
        # 5 字段 SceneFrame JSON 经常超过 200 字节 JSON-encoded,加上
        # 模型可能逼近 max_new_tokens=250 上限 → reply.text 整段序列化进
        # ComputeReply.text 时触发 FastCDR 255 字节静默截断 → 主进程
        # json.loads 失败报 "malformed reply: '...{...厨房内灶上...橙红'"。
        # 修法: ``text`` 字段长度 (JSON-encoded) 超过 200 字节时走
        # /dev/shm path-list,与 ``result_data`` / ``structured`` 路径对称。
        # 主进程 RpcSessionProxy._decode_reply_shm 读回填 ``data["text"]``,
        # VFA _call_llm → _split_scene_frame_result Path 2 拿到完整
        # ``_raw["text"]`` 后, regex 抽 JSON 走智能归一化兜底。
        # SHM 写失败: 整段 text drop (与 ``crop_b64`` 失败同款),
        # 主进程拿到 text=None → VFA Path 1 garbage check 标"本帧分析失败"
        # 占位事件,不崩。
        text_inline: Optional[str] = text
        text_path: Optional[str] = None
        if isinstance(text_inline, str) and text_inline:
            try:
                _text_json_bytes = len(json.dumps(
                    text_inline, ensure_ascii=False).encode("utf-8"))
            except (TypeError, ValueError):
                _text_json_bytes = 0
            if _text_json_bytes > 200:
                if is_remote:
                    LOG.warning(
                        "infer[%s]: LAN peer — dropping %d-byte text "
                        "(no SHM route, FastCDR 255-byte inline cap)",
                        _req_id, _text_json_bytes,
                    )
                    text_inline = None
                else:
                    text_path = _write_reply_payload_to_shm(
                        text_inline.encode("utf-8"), "txt")
                    if text_path:
                        text_inline = None  # proxy 端从 SHM 读回填 data["text"]
                    else:
                        LOG.warning(
                            "infer: failed to route reply.text via /dev/shm — "
                            "dropping (%d bytes JSON-encoded). main process "
                            "will see text=None; VFA Path 1 garbage check "
                            "marks frame as 解析失败 (placeholder, not crash).",
                            _text_json_bytes,
                        )
                        text_inline = None
        reply = {
            "text": text_inline,
            "level": getattr(result, "level", None),
            "tool_call": tool_call_name if is_tool_instance else getattr(result, "tool_call", None),
            "bbox": getattr(result, "bbox", None),
        }
        if text_path:
            reply["text_path"] = text_path
        crop_b64_inline = getattr(result, "crop_b64", None)
        if crop_b64_inline:
            if is_remote:
                LOG.warning(
                    "infer[%s]: LAN peer — dropping %d-byte crop_b64 "
                    "(no SHM route, inline > 255B FastCDR cap)",
                    _req_id, len(crop_b64_inline),
                )
            else:
                crop_path = _write_crop_b64_to_shm(crop_b64_inline)
                if crop_path:
                    reply["crop_path"] = crop_path
                else:
                    LOG.warning(
                        "infer: failed to route crop_b64 via /dev/shm — "
                        "dropping (%d chars). main process will re-crop "
                        "from frame_bgr as fallback.",
                        len(crop_b64_inline),
                    )
        # ------------------------------------------------------------------ #
        # 诊断 (v0.13): dump worker 给主进程的实际 reply.text (前 800 字符),  #
        # 确认模型真实 raw output 是什么。reply 构造完后插在 result_data/  #
        # structured 路由之前,这样 dump 的是 SDK 处理后的 reply.text (等同   #
        # 主进程拿到的 _ProxyResult.text),不是用户期望的 model output (那是  #
        # diag_raw_model_output)。                                                  #
        # ------------------------------------------------------------------ #
        if text is not None:
            LOG.debug(
                "diag_reply[%s]: reply.text (%d chars, type=%s):\n%s%s",
                _req_id, len(text), type(result).__name__,
                text[:800],
                "..." if len(text) > 800 else "",
            )
        if attachment_b64 is not None:
            reply["attachment_b64"] = attachment_b64
        if is_tool_instance and tool_call_args is not None:
            reply["tool_call_args"] = tool_call_args

        # Schema instances (TargetDoc / VLMDoc / TTSResult) — forward the
        # full ``model_dump()`` so the proxy can reconstruct the actual
        # class. ``text / level / bbox`` slots above only cover VLMDoc's
        # surface; for TargetDoc (``x1 / y1 / x2 / y2 / desc``) every
        # structured field would otherwise be lost at the worker boundary
        # and the detector's ``isinstance(result, TargetDoc)`` branch
        # never matches on the main side. Mirrors the ``tool_call_args``
        # pattern for ``@Callable`` reconstruction. strip ``attachment``
        # so reply stays JSON-serializable.
        result_data_dict: Optional[dict] = None
        if (not is_tool_instance
                and type(result).__name__ in {
                    "VLMDoc", "TargetDoc", "TTSResult",
                    "LocatorDoc", "GroundingResult",
                }):
            try:
                _dump = result.model_dump()
                if isinstance(_dump, dict):
                    _dump.pop("attachment", None)
                    result_data_dict = _dump
            except Exception as e:
                LOG.warning(
                    "schema instance dump failed (%s): %s",
                    type(result).__name__, e,
                )
        # ------------------------------------------------------------------ #
        # 诊断 (v0.13): schema parse 失败 silent 路径补日志 + marker         #
        # ------------------------------------------------------------------ #
        # SDK ``_safe_validate`` 对没 ``text`` 字段的 schema (TargetDoc)  #
        # 在 ValidationError 时 fallback 到 raw text string → result 是   #
        # ``str``,主进程拿不到 TargetDoc 实例 → silent fallback 到       #
        # _ProxyResult,bbox/crop_b64 全无,目标检测失败。现在把异常状态提 #
        # 到 ERROR 级别,并通过 ``schema_parse_failed: True`` marker 写到   #
        # reply 让 proxy / detector 都能识别,事件历史里也能看到带            #
        # ``[TargetDoc 解析失败]`` 前缀的标记。                              #
        # ------------------------------------------------------------------ #
        if (not is_tool_instance
                and _diag_generating is not None
                and type(result).__name__ not in {
                    "VLMDoc", "TargetDoc", "TTSResult",
                    "LocatorDoc", "GroundingResult",
                }):
            _raw_text_for_log = (
                text if isinstance(text, str) else str(result)[:500]
            )
            LOG.error(
                "infer_handler[%s]: SCHEMA PARSE FAILED — gen=%r but "
                "result_type=%s (expected VLMDoc/TargetDoc/TTSResult/"
                "LocatorDoc/GroundingResult).",
                _req_id, _diag_generating, type(result).__name__,
            )
            # raw_text 独立多行 LOG：%r 会把 \n 转成 \\n，多行 JSON 全挤
            # 一行没法看。这里尝试 json.dumps(indent=2) 美化；非 JSON 原文
            # 输出；超过 1500 字符截断,避免 log 爆炸。
            try:
                _obj = json.loads(_raw_text_for_log)
                _formatted = json.dumps(_obj, indent=2, ensure_ascii=False)
            except Exception:
                _formatted = _raw_text_for_log
            if len(_formatted) > 1500:
                _formatted = _formatted[:1500] + f"\n...[truncated {len(_formatted) - 1500} chars]"
            LOG.error("raw_text (formatted):\n%s", _formatted)
            reply["schema_parse_failed"] = True
            reply["actual_result_type"] = type(result).__name__
            # raw_text 仅在 text_path 没设时 inline (短文本诊断路径):
            # text_path 已设说明长文本走 SHM path-list,wire 上再 inline
            # 一份 = FastCDR ComputeReply.text 255B 静默截断 → 主进程
            # json.loads 失败报 "malformed reply: '...{...scene 小区入口}'"
            # (VFA SceneFrame 解析失败时必现)。
            if isinstance(text, str) and "text_path" not in reply:
                reply["raw_text"] = text

        if result_data_dict is not None:
            import json as _json_rd
            try:
                rd_size = len(_json_rd.dumps(result_data_dict, ensure_ascii=False).encode("utf-8"))
            except (TypeError, ValueError):
                rd_size = 0
            if is_remote:
                LOG.warning(
                    "infer[%s]: LAN peer — dropping %d-byte result_data "
                    "(no SHM route, inline > 255B FastCDR cap; "
                    "TargetDoc falls back to _ProxyResult._raw defaults)",
                    _req_id, rd_size,
                )
            elif rd_size > 200:
                rd_path = _write_result_data_to_shm(result_data_dict)
                if rd_path:
                    reply["result_data_path"] = rd_path
                else:
                    LOG.warning(
                        "infer: failed to route result_data via /dev/shm — "
                        "dropping (%d bytes). TargetDoc reconstruction will "
                        "fall back to _ProxyResult._raw default fields.",
                        rd_size,
                    )
            else:
                reply["result_data"] = result_data_dict

        # SceneFrame 等非 schema-instance（VFA 路径）：proxy 端没有对应
        # model_validate，所以 ``result_data`` 留空；``structured`` 是 VFA
        # ``_split_scene_frame_result`` Path 2 拿完整字段的唯一来源。
        structured_dict: Optional[dict] = None
        if (not is_tool_instance
                and not isinstance(result, (str, bytes, dict))
                and result_data_dict is None):
            try:
                if hasattr(result, "model_dump") and callable(getattr(result, "model_dump", None)):
                    _dump = result.model_dump()
                    if isinstance(_dump, dict) and _dump:
                        _dump.pop("attachment", None)
                        if _dump:
                            structured_dict = _dump
            except Exception as _dump_ex:
                LOG.warning("model_dump failed: %s", _dump_ex)
        if structured_dict is not None:
            import json as _json_s
            try:
                sd_size = len(_json_s.dumps(structured_dict, ensure_ascii=False).encode("utf-8"))
            except (TypeError, ValueError):
                sd_size = 0
            if is_remote:
                LOG.warning(
                    "infer[%s]: LAN peer — dropping %d-byte structured "
                    "(no SHM route, inline > 255B FastCDR cap; "
                    "VFA Path 2 will see empty _raw.structured)",
                    _req_id, sd_size,
                )
            elif sd_size > 100:
                sd_path = _write_result_data_to_shm(structured_dict)
                if sd_path:
                    reply["structured_path"] = sd_path
                else:
                    LOG.warning(
                        "infer: failed to route structured via /dev/shm — "
                        "dropping (%d bytes). VFA Path 2 will see empty "
                        "_raw.structured.",
                        sd_size,
                    )
            else:
                reply["structured"] = structured_dict
        # 诊断：reply 即将被 cshim 序列化回主进程。记下 keys / 体积
        # 以区分 "worker 算出来了但 reply 格式坏" vs "cshim 回不去"。
        try:
            import json as _json_diag
            _reply_str = _json_diag.dumps(reply, ensure_ascii=False)
            _reply_bytes = len(_reply_str.encode("utf-8"))
        except Exception as _enc_exc:
            _reply_bytes = -1
            _reply_str = ""
            LOG.warning(
                "infer_handler[%s]: reply NOT JSON-serializable: %s — "
                "this is the most likely cause of 'no result returned' "
                "(cshim json.dumps will fail at the worker boundary)",
                _req_id, _enc_exc,
            )
        LOG.info(
            "infer_handler[%s]: returning reply keys=%s bytes=%d "
            "image_size=%r total=%.2fs request_id=%s",
            _req_id, sorted(reply.keys()), _reply_bytes,
            _diag_image_size, _t_inf.time() - _t_req_in,
            _req_id,  # 诊断 (v0.13): reply.request_id 由 cshim 从 req.request_id 拿
            # 用于排查主进程 cshim discovery race 时核对主进程收到的
            # request_id 与 worker 发出的 request_id 是否一致。
        )
        return reply
    except Exception as e:  # noqa: BLE001
        LOG.warning(
            "infer_handler[%s]: exception (image_size=%r): %s: %s",
            _req_id, _diag_image_size, type(e).__name__, e,
        )
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
        from app.core.compute.registry import _worker_comm_name
        comm_name = _worker_comm_name(model_id)
        _set_comm_name(comm_name)
    except Exception as e:  # noqa: BLE001
        LOG.warning("could not set comm name for %s: %s", model_id, e)

    # Resolve model_dir for the info reply (don't fail if missing).
    model_dir = None
    try:
        base_dir = os.environ.get("WDNAI_MODELS_DIR", "/userdata/models/")
        from app.core.compute.registry import MODEL_PRESETS
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

    # 内置 @Callable 工具注册:与主进程共用同一份 registry 真源
    # (详见 app/core/tools/_builtins.py)。chat_run / infer 路径的
    # _resolve_tool_classes / parse_classes 都靠这个 registry 查工具
    # class —— 主进程只在 uvicorn 里注册,worker 子进程必须自己注册
    # 否则 chat_run(tools=['VideoSearch']) 走到 worker 端查不到类,
    # prompt 缺工具 schema → 模型 hallucinate "模拟调用"。
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
        # Close the DDS audio-chunk writer before the Hub tears down the
        # participant so any in-flight BEST_EFFORT chunks can flush.
        # ``close()`` is idempotent so a double-close is a no-op.
        try:
            if _AUDIO_CHUNK_WRITER is not None:
                _AUDIO_CHUNK_WRITER.close()
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


if __name__ == "__main__":
    sys.exit(main())
