"""算力注册中心：算力卡 + 模型实例的统一管理（per-model-subprocess RPC）。

设计目标（v0.4）：

* **每个 (npu_index, model_id) 实例 = 一个独立 compute_worker 子进程**。
  父子进程通过 **wdndds cshim RPC**（标准 Pub-Sub on
  ``wdndds/rpc/<model_id>``）通信，**支持跨机器 LAN 互联**
  —— 这是 v0.4 设计的核心目标。
* **算力配置页 toggle click → ``start_one`` 立即 spawn 子进程**。
  worker argparse 默认 ``--lazy=False``（eager load），子进程起
  后自动启后台线程跑 ``load_handler``，状态机走
  ``not_loaded → loading → running`` (or ``error`` on NPU setup
  failure)；主进程通过 cshim ``RpcClient.call("info")`` poll 镜像。
* **跨进程崩溃隔离**：wdnai 的 ``LLMSession()`` 在 RKNN 失败时会
  ``terminate()`` 整个进程（不可被 Python 捕获）。子进程死了主进程
  不受牵连 —— 探针线程读 worker stderr，标记实例为 ``error``。
* **FastDDS 2.9 在本机有 DataWriter::enable SIGSEGV bug**。
  wdndds cshim 的修补方案：建 writer 之前**先建一个 no-op reader**
  on the same topic，让 FastDDS 初始化共享 payload pool。

时间字段：

* ``queued_at``  —— start_one 那一刻
* ``started_at`` —— 子进程 spawn 那一刻
* ``loaded_at``  —— worker 通过 ``info`` RPC 回报的模型就绪时刻

snapshot 字段：

* ``status``     — ``queued`` (spawn 还没回包) / ``spawning`` / ``loading`` /
                  ``running`` / ``error`` / ``stopped`` / ``ready_lazy``
                  (boot 时设的)
* ``pid``        —— 子进程 PID；用于诊断
"""
from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional, Tuple

from app.services.config import get_config


# --------------------------------------------------------------------------- #
# Compute mode: only ``subprocess`` (v0.4)                                  #
# --------------------------------------------------------------------------- #
# One compute_worker subprocess per (npu, model) instance; the worker
# exposes a wdndds cshim RPC service (``wdndds/rpc/<model_id>``). The
# main process talks to it via ``wdndds.RpcClient``. The subprocess
# boundary isolates wdnai's uncatchable ``terminate()`` on a broken NPU
# setup. ``WDNWEBUI_COMPUTE_MODE=inprocess`` is reserved for the case
# where the cshim DDS layer can't run (e.g. kernel blocklists UDP
# multicast); the v0.4 inprocess mode is otherwise a no-go on hosts
# where wdnai RKNN init can call ``terminate()``.
import os as _os
import contextlib as _contextlib


def _resolve_compute_mode() -> str:
    env = _os.environ.get("WDNWEBUI_COMPUTE_MODE", "").strip().lower()
    if env in ("subprocess", "inprocess"):
        return env
    return "subprocess"


# --------------------------------------------------------------------------- #
# 硬件能力（HW）目录                                                       #
# --------------------------------------------------------------------------- #
HARDWARE_CAPABILITIES: list[dict] = [
    {
        "capability_type": "hwcap_video_input",
        "class_name":      "HWVideoInput",
        "category":        "video",
        "label":           "视频输入（摄像头）",
        "description":     "采集视频帧，供其它设备订阅。",
    },
    {
        "capability_type": "hwcap_audio_input",
        "class_name":      "HWAudioInput",
        "category":        "audio",
        "label":           "音频输入（麦克风）",
        "description":     "采集音频数据，供其它设备订阅。",
    },
    {
        "capability_type": "hwcap_audio_output",
        "class_name":      "HWAudioOutput",
        "category":        "audio",
        "label":           "音频输出（扬声器）",
        "description":     "对外播放音频，配合 swcap_audio_tts 一起使用。",
    },
    {
        "capability_type": "hwcap_hdmi_display",
        "class_name":      "HWHDMIDisplay",
        "category":        "video",
        "label":           "HDMI 显示输出",
        "description":     "把视频 / 画面推送到 HDMI 显示设备。",
    },
]


def _hardware_capability_default_state() -> dict[str, bool]:
    return {entry["capability_type"]: True for entry in HARDWARE_CAPABILITIES}


def _load_hardware_capability_state() -> dict[str, bool]:
    cfg = get_config().get_section("compute", {})
    stored = cfg.get("hardware_capabilities") if isinstance(cfg, dict) else None
    state = _hardware_capability_default_state()
    if isinstance(stored, dict):
        for k, v in stored.items():
            if k in state and isinstance(v, bool):
                state[k] = v
    return state


LOG = logging.getLogger("compute_registry")

DEFAULT_ESTIMATED_INIT_SECONDS = 10


# adopted peer "失联" 判定阈值：连续失败次数。
# 失败间隔固定 ``time.sleep(2.0)``（见 _probe_instance），所以
# ``>= _PEER_LOST_CONSECUTIVE_FAILURES`` 等价于大约
# ``_PEER_LOST_CONSECUTIVE_FAILURES * 2`` 秒未收到 reply。
# 当前 5 ≈ 10 秒，足够吃掉 LAN 上偶发的丢包 / participant 重协商，
# 又能在真断电 / kill 场景下及时把卡片翻成 error。
_PEER_LOST_CONSECUTIVE_FAILURES = 5


def _read_device_name() -> str:
    """Read ``device_name`` from ``config.json["base"]``.

    Used both for the main process's ``Hub(participant_name=...)`` and
    to compare against the ``hostname`` field on the worker's ``info``
    response when deciding whether a probe hit is a local orphan vs
    a real LAN peer. Falls back to ``socket.gethostname()`` when the
    field is unset (so the participant_name is never empty).
    """
    try:
        base = get_config().get_section("base") or {}
        v = (base.get("device_name") or "").strip()
        if v:
            return v
    except Exception:  # noqa: BLE001
        pass
    return socket.gethostname()


def _find_orphan_worker_pid(model_id: str) -> Optional[int]:
    """Back-compat shim — returns the first match from
    :func:`_find_workers_by_name`. New callers should use the list
    version directly.
    """
    pids = _find_workers_by_name(model_id)
    return pids[0] if pids else None


def _worker_comm_name(model_id: str) -> str:
    """Compute the comm (process name) we set for this model's worker.

    Format: ``worker:<alias>`` where ``<alias>`` is
    ``MODEL_PRESETS[model_id]["worker_name"]``. The full string must
    fit in 15 bytes (TASK_COMM_LEN - 1) for ``prctl(PR_SET_NAME)``.
    The dedup walk reads ``/proc/<pid>/comm`` and matches on this
    prefix.
    """
    preset = MODEL_PRESETS.get(model_id)
    if preset is None:
        alias = model_id[:8]
    else:
        alias = preset.get("worker_name") or model_id[:8]
    full = f"worker:{alias}"
    if len(full.encode("utf-8")) > 15:
        # 兜底截断：保留 ``worker:`` 前缀 + alias 前缀
        return full[:15]
    return full


def _set_comm_name_preexec(name: str):
    """Return a ``preexec_fn`` for ``subprocess.Popen`` that calls
    ``prctl(PR_SET_NAME, name)`` in the child between fork and exec.

    The name is truncated to 15 bytes (TASK_COMM_LEN - 1) to match the
    kernel limit. Failure is swallowed: a misnamed worker still works,
    it just doesn't dedup by name.

    Implementation note: ``subprocess.Popen`` pickles the preexec_fn
    to send it across the fork boundary, so the inner function must
    not close over any non-picklable object. A ``ctypes.CDLL`` handle
    is NOT picklable, so we acquire libc inside the inner function
    on every call (cheap — it's a no-op after the first mmap, and
    preexec_fn runs once per spawn).
    """
    PR_SET_NAME = 15
    name_bytes = name.encode("utf-8")[:15]

    def _set():
        try:
            libc = ctypes.CDLL(ctypes.util.find_library("c"),
                               use_errno=True)
            libc.prctl(PR_SET_NAME, name_bytes, 0, 0, 0)
        except Exception:  # noqa: BLE001
            pass

    return _set


def _find_workers_by_name(model_id: str) -> List[int]:
    """Walk ``/proc/*/comm`` looking for processes whose comm equals
    the canonical name for this model's worker (``worker:<alias>``).

    PR_SET_NAME only writes the comm field (visible in ``ps -c`` /
    ``top`` / ``/proc/<pid>/comm``); ``/proc/<pid>/cmdline`` is
    unchanged and still shows the original ``python -u ...`` argv.
    Walking ``comm`` is both faster and unambiguous: two workers
    spawned from different sources but serving the same model will
    both have the same comm, so the dedup will catch them.

    For backward compat with workers spawned by the *old* code path
    (before the preexec_fn change), we also fall back to a cmdline
    substring match on
    ``compute_worker.py\x00--model\x00<model_id>\x00`` —
    those workers have ``comm="python"`` (the default) and would
    otherwise slip through the dedup and keep consuming NPU memory.
    Once every old worker has been replaced by a new one (one
    reload after deploy), the cmdline branch becomes a no-op.

    Returns all matching PIDs (potentially many — a reload storm can
    leave several orphans). The caller is expected to adopt one and
    clean the rest.
    """
    own_pid = os.getpid()
    comm_needle = _worker_comm_name(model_id).encode("utf-8")
    # cmdline 里 argv 各元素以 \x00 分隔，不是空格：
    #   b'/.../python\x00-u\x00/.../compute_worker.py\x00--model\x00qwen3-vl-4b\x00'
    cmdline_needle = (
        f"compute_worker.py\x00--model\x00{model_id}\x00".encode("utf-8"))
    pids: List[int] = []
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            if pid == own_pid:
                continue
            # 先查 comm（新 worker）
            try:
                with open(f"/proc/{pid}/comm", "rb") as f:
                    comm = f.read().rstrip(b"\x00").rstrip()
            except (OSError, FileNotFoundError):
                continue
            if comm == comm_needle:
                pids.append(pid)
                continue
            # fallback：老 worker（comm 没设），靠 cmdline 认
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    cmdline = f.read()
            except (OSError, FileNotFoundError):
                continue
            if cmdline_needle in cmdline:
                pids.append(pid)
    except OSError:
        pass
    return pids


def _pid_alive(pid: int) -> bool:
    """``kill(pid, 0)`` returns 0 if the process exists and we have
    permission, raises ``ProcessLookupError`` if it's gone, or
    ``PermissionError`` if it exists but isn't ours. Both of the
    errors mean "not killable by us" — treat as not alive for the
    purposes of the orphan-cleanup wait loop.
    """
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def _drain_worker_stderr(proc, max_bytes: int = 2000) -> str:
    """Best-effort non-blocking drain of a compute_worker's stderr pipe.

    Used by :meth:`ComputeRegistry.bind_instance` when the RPC ``load``
    call returns ``ok=False`` (typically ``"timeout"``) or raises — at
    that point we want to surface the worker's actual failure (NPU
    abort, native panic, weight load error, FileNotFoundError on a
    missing .1828.rknn, …) instead of leaving the user with a cryptic
    one-word error.

    Implementation notes:

    * The pipe is **always** read non-blocking. A blocking read here
      would defeat the whole point (we're already past the RPC timeout
      and don't want to wait longer for stderr to arrive).
    * If the worker is still alive and hasn't written anything yet,
      returns ``""`` — the probe thread will pick up the next round of
      stderr on its next pass.
    * If the worker has exited, ``poll()`` is non-None and read drains
      whatever is buffered (Linux default pipe buffer is 64 KB; we
      cap at ``max_bytes`` and let the front get truncated).
    * Never raises — the caller is in an error path already.
    """
    if proc is None or proc.stderr is None:
        return ""
    import fcntl
    fd = -1
    try:
        fd = proc.stderr.fileno()
    except (OSError, ValueError):
        return ""
    saved_flags = -1
    try:
        # Toggle O_NONBLOCK for the duration of the read; restore the
        # original flags on exit so other consumers (the probe thread)
        # aren't surprised by a non-blocking pipe.
        try:
            saved_flags = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, saved_flags | os.O_NONBLOCK)
        except OSError:
            saved_flags = -1
        try:
            chunk = os.read(fd, max_bytes)
        except (BlockingIOError, OSError):
            return ""
        return chunk.decode("utf-8", errors="replace")
    finally:
        if saved_flags != -1:
            try:
                fcntl.fcntl(fd, fcntl.F_SETFL, saved_flags)
            except OSError:
                pass


def _terminate_orphan_pids(pids: List[int], grace_sec: int = 5) -> int:
    """SIGTERM the given PIDs, wait up to ``grace_sec`` for graceful
    exit, then SIGKILL the stragglers. Returns the count of PIDs we
    managed to send SIGTERM to.

    PIDs that we lack permission to signal (PermissionError) are
    skipped silently — that's another process's worker, not ours to
    reap (e.g. ``adopted`` peer workers, which are the other
    board's responsibility). The dedup walk in this module only ever
    looks at local workers so in practice this shouldn't fire, but
    the defensive check keeps the function safe to call on
    "what if it's a peer" inputs.
    """
    if not pids:
        return 0
    sent = 0
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
            LOG.info("sent SIGTERM to orphan worker pid=%d", pid)
            sent += 1
        except ProcessLookupError:
            pass
        except PermissionError:
            LOG.warning("permission denied SIGTERM pid=%d (likely peer-owned)", pid)
    deadline = time.time() + grace_sec
    survivors = list(pids)
    while survivors and time.time() < deadline:
        time.sleep(0.2)
        survivors = [p for p in survivors if _pid_alive(p)]
    for pid in survivors:
        try:
            os.kill(pid, signal.SIGKILL)
            LOG.warning("SIGKILL stubborn orphan worker pid=%d", pid)
        except ProcessLookupError:
            pass
        except PermissionError:
            LOG.error("permission denied SIGKILL pid=%d", pid)
    return sent


def _resolve_worker_model_id(comm: str) -> Optional[str]:
    """Reverse-lookup: given a comm string (e.g. ``worker:llm-8b``),
    return the ``model_id`` whose ``MODEL_PRESETS[model_id].worker_name``
    matches the suffix. Returns ``None`` for unknown comms (e.g.
    ``worker:debug``, future aliases not in the preset table).
    """
    prefix = "worker:"
    if not comm.startswith(prefix):
        return None
    alias = comm[len(prefix):]
    for mid, preset in MODEL_PRESETS.items():
        if preset.get("worker_name") == alias:
            return mid
    return None


# --------------------------------------------------------------------------- #
# preset 映射表                                                              #
# --------------------------------------------------------------------------- #
def _qwen3_vl_4b_spec():
    from wdnai import SystemLanguageModel  # noqa: F401  lazy
    return SystemLanguageModel.qwen3_vl_4b()


def _qwen3_tts_spec():
    from wdnai import SystemLanguageModel
    return SystemLanguageModel.qwen3_tts()


def _qwen3_8b_spec():
    from dataclasses import replace as _dc_replace
    from wdnai import SystemLanguageModel
    spec = SystemLanguageModel.qwen3_8b()
    return _dc_replace(spec, max_context_len=32768).with_options(
        max_new_tokens=2048,
        keep_history=1,
        enable_thinking=True,
    )


def _qwen3_vl_embedding_2b_spec():
    """Qwen3-VL-Embedding-2B 检索 embedding preset（kind="embedding"）。

    不做 token 生成，LLM 走 RKNN3Lite 普通模型路径输出 2048 维归一化
    向量；wdnai 侧 ``EmbeddingBackend`` 暴露 ``embed_text`` /
    ``embed_video``。模型路径走 ``WDNAI_MODELS_DIR``（默认
    ``/userdata/models/``），与 Qwen3-VL-4B 同款文件布局。
    """
    from wdnai import SystemLanguageModel
    return SystemLanguageModel.qwen3_vl_embedding()


def _qwen_2_5_omni_3b_spec():
    """Qwen2.5-Omni-3B 音频理解 preset（kind="omni"）。

    单次 ``run_audio`` 输入上限 6.0 秒（omni.py 硬限制），调用方负责切片。
    """
    from wdnai import SystemLanguageModel
    return SystemLanguageModel.qwen_2_5_omni_3b()


# 每个 model 的 ``estimated_init_seconds`` 是经验值，用来渲染"加载中"倒计时
# 的初始起点；loading 段倒计时按 (estimate - (now - started_at)) 实算，estimate
# 只影响倒计时的**起点**。真机冷启动实测（page cache dropped 之后）：
#   * Qwen3-VL-4B: 57s → +3s buffer → 60s
#   * Qwen3-TTS:   改 wdnai 加上 ensure_loaded() 后变成 eager init：
#                  - load_resources:  ~4s
#                  - init_text_projection: ~0.4s
#                  - init_speech_decoder:  ~2s
#                  - init_code_predictor:  ~1.6s
#                  - init_talker (785MB weight + 24MB rknn, 8-core AR): dev box
#                    init_talker 直接 MODEL_SETUP fail，没法在 dev box 测真机
#                    时延；真机用户体感"首次等非常久"，按 VL-4B 同档拍 60s
#                    + buffer → 60s（无真机实测，等你测完再调）
#   * Qwen3-ASR:   未实测（模型文件不在板上），按编码器+LLM 推算 ~15s + 10s buffer → 25s
#   * Qwen3-VL-Embedding-2B：~30s（加载 2 个 rknn + embed 表）
# 取"最冷"加载时间（即用户首次加载，不是文件页缓存命中后的二次加载）+ 小 buffer，
# 保证倒计时结束时实际加载大概率已就绪，避免 UI 卡很久才更新。
MODEL_PRESETS: dict[str, dict] = {
    "qwen3-vl-4b": {
        "label":            "Qwen3-VL-4B",
        "preset_factory":   _qwen3_vl_4b_spec,
        "model_dir_name":   "Qwen3-VL-4B",
        "memory_estimate_mb": 3500,
        "estimated_init_seconds": 60,
        "capability_types": ["swcap_video_describe"],
        "instance_name":    "llm-vl",
        "worker_name":      "vlm-4b",          # comm = "worker:vlm-4b"（15 chars）
        "description":      "视频描述模型",
        "available_targets": ["182x"],   # .1828.rknn 权重 — RK182x 协卡
    },
    "qwen3-tts": {
        "label":            "Qwen3-TTS",
        "preset_factory":   _qwen3_tts_spec,
        "model_dir_name":   "Qwen3-TTS",
        "memory_estimate_mb": 1900,
        "estimated_init_seconds": 30,
        "capability_types": ["swcap_audio_tts"],
        "instance_name":    "llm-tts",
        "worker_name":      "tts",             # comm = "worker:tts"（10 chars）
        "description":      "文字转语音模型",
        "available_targets": ["182x"],
    },
    "qwen3-8b": {
        "label":            "Qwen3-8B",
        "preset_factory":   _qwen3_8b_spec,
        "model_dir_name":   "Qwen3-8B",
        "memory_estimate_mb": 4950,
        "estimated_init_seconds": 60,
        "capability_types": ["swcap_text_chat"],
        "instance_name":    "llm-chat",
        "worker_name":      "llm-8b",          # comm = "worker:llm-8b"（12 chars）
        "description":      "文字对话模型",
        "available_targets": ["182x"],
    },
    "qwen3-vl-embedding-2b": {
        # v0.9 第一次缩成 "Qwen3-VL-Embed-2B"（16 字符），还是嫌长。
        # 再缩成 "Qwen3-VLEmb-2B"（14 字符）—— "VL" + "Emb" 当连续
        # 缩写，符合 wdnai 圈子常见写法（"VLE" / "Emb" 已是行业缩略）。
        # 文件名 / model_dir_name 不动，只改 UI label。
        "label":            "Qwen3-VLEmb-2B",
        "preset_factory":   _qwen3_vl_embedding_2b_spec,
        "model_dir_name":   "Qwen3-VL-Embedding-2B",
        "memory_estimate_mb": 2000,        # ~1GB 权重 + 622MB embed 表 + vision
        "estimated_init_seconds": 30,      # 加载两个 rknn + embed 表
        "capability_types": ["swcap_video_retrieval"],
        "instance_name":    "llm-embed",
        "worker_name":      "emb-2b",          # comm = "worker:emb-2b"（12 chars）
        "description":      "视觉信息编码模型",
        "available_targets": ["182x"],
    },
    "qwen2.5-omni-3b": {
        # Qwen2.5-Omni-3B（kind="omni"，wav-in/text-out）。单次推理上限
        # 6.0 秒（omni.py 硬限制），调用方负责切片。memory_estimate_mb /
        # estimated_init_seconds 是预估值，真机测后回填。
        "label":            "Qwen2.5-Omni-3B",
        "preset_factory":   _qwen_2_5_omni_3b_spec,
        "model_dir_name":   "Qwen2.5-Omni-3B",
        "memory_estimate_mb": 2500,
        "estimated_init_seconds": 50,
        "capability_types": ["swcap_audio_understand"],
        "instance_name":    "llm-audio",
        "worker_name":      "audio-3b",     # comm = "worker:audio-3b" (13 chars)
        "description":      "音频理解模型",
        "available_targets": ["182x"],
    },
}


# --------------------------------------------------------------------------- #
# Legacy capability_type migration (kept for backward compat with old configs) #
# --------------------------------------------------------------------------- #
_LEGACY_CAPABILITY_TYPE_RENAMES: dict[str, str] = {
    "video_input":     "hwcap_video_input",
    "video_query":     "swcap_video_query",
    "video_describe":  "swcap_video_describe",
    "audio_input":     "hwcap_audio_input",
    "audio_output":    "hwcap_audio_output",
    "hdmi_display":    "hwcap_hdmi_display",
    "audio_tts":       "swcap_audio_tts",
    "audio_understand":"swcap_audio_understand",
    "video_analyse":   "swcap_video_analyse",
    "text_chat":       "swcap_text_chat",
}


# --------------------------------------------------------------------------- #
# ModelInstance                                                             #
# --------------------------------------------------------------------------- #
@dataclass
class ModelInstance:
    """One compute service instance, either local (spawned) or remote (adopted).

    Lifecycle (v0.7):
      1. ``_spawn_compute_worker(model)`` or ``_adopt_existing_worker(model)``
         creates the record; key is ``(-1, model_id)`` until the user binds
         an NPU via RPC ``load``.
      2. ``bind_instance(model, npu_index)`` sends RPC ``load`` and the
         worker instantiates ``LLMSession``. key becomes
         ``(npu_index, model_id)``.
      3. ``unload_instance(model)`` sends RPC ``unload``; worker drops the
         session, status returns to ``not_loaded``, npu_index resets to -1.
      4. ``stop_instance_local(model)`` is spawned-only: RPC unload →
         RPC shutdown → wait → kill. Adopted peers are not affected.
    """
    key: Tuple[int, str]                        # (-1, model_id) → (npu, model_id)
    npu_index: int                             # -1 until bound
    model_id: str
    device_id: Optional[bytes] = None
    llm_session: Any = None
    capability_types: list[str] = field(default_factory=list)
    instance_name: str = ""
    label: str = ""
    memory_estimate_mb: int = 0
    status: str = "not_loaded"                  # not_loaded | loading | running | error
    error: str = ""
    stderr_full: str = ""
    queued_at: float = 0.0
    started_at: float = 0.0
    finished_at: float = 0.0
    load_time_sec: Optional[float] = None
    loaded_at: Optional[float] = None
    # 模型目录（worker probe 解析出来；missing_model_dir 状态时主进程
    # 据此决定 UI 怎么显示）。
    model_dir: str = ""
    # RPC subprocess state
    proc: Optional[subprocess.Popen] = None
    rpc_client: Any = None
    rpc_session_proxy: Any = None
    _probe_thread: Optional[threading.Thread] = None
    # Provenance / discovery
    _source: str = "spawned"                    # "spawned" | "adopted_local" | "adopted" | "unavailable"
    _peer_guid: str = ""                       # adopted: peer participant GUID
    _peer_name: str = ""                       # adopted: peer participant name (or "<device> @ <host>")
    _peer_ip: str = ""                         # adopted: peer worker's outbound IPv4 (best-effort)
    _peer_pid: int = 0                          # adopted: responder's PID (for cross-check / kill)

    def to_dict(self) -> dict:
        # spawned: PID 来自自己 Popen 的子进程；adopted_local: 来自 probe
        # info（孤儿 worker 的真实 PID）；adopted: 同上（远端 peer）；
        # unavailable: 0.
        if self.proc is not None:
            pid = self.proc.pid
        elif self._source in ("adopted_local", "adopted") and self._peer_pid:
            pid = self._peer_pid
        else:
            pid = 0
        preset = MODEL_PRESETS.get(self.model_id, {})
        return {
            "npu_index":          self.npu_index,
            "model_id":           self.model_id,
            "label":              self.label,
            "instance_name":      self.instance_name,
            "status":             self.status,
            "error":              self.error,
            "stderr_full":        self.stderr_full,
            "capability_types":   list(self.capability_types),
            "memory_estimate_mb": self.memory_estimate_mb,
            "description":        preset.get("description", ""),
            "available_targets":  list(preset.get("available_targets", ["182x"])),
            "model_dir":          self.model_dir,
            "device_id":          self.device_id.decode("utf-8", errors="replace")
                                    if self.device_id else None,
            "queued_at":          self.queued_at,
            "started_at":         self.started_at,
            "finished_at":         self.finished_at,
            "load_time_sec":      self.load_time_sec,
            "loaded_at":          self.loaded_at,
            "pid":                pid,
            "_source":            self._source,
            "_peer_guid":         self._peer_guid,
            "_peer_name":         self._peer_name,
            "_peer_ip":           self._peer_ip,
            "_peer_pid":          self._peer_pid,
        }


# --------------------------------------------------------------------------- #
# ComputeRegistry                                                           #
# --------------------------------------------------------------------------- #
class ComputeRegistry:
    """算力注册中心：管理 subprocess 实例 + capability 路由。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._instances: dict[Tuple[int, str], ModelInstance] = {}
        self._cards: list[dict] = []            # not used by RPC path
        self._cards_failed: str = ""
        self._hub: Any = None                   # wdndds Hub for client side
        self._hub_lock = threading.Lock()
        self._mode: str = _resolve_compute_mode()
        self._migrate_legacy_capability_types()
        if self._mode == "subprocess":
            self._ensure_hub()

    @property
    def compute_mode(self) -> str:
        return self._mode

    # ------------------------------------------------------------------ #
    # Hub bring-up (client side)                                         #
    # ------------------------------------------------------------------ #
    def _ensure_hub(self) -> None:
        """Bring up the wdndds Hub that the main process uses to talk
        to all compute_worker subprocesses. The Hub points at the LAN's
        wdndds discovery daemon (one per LAN, configured in
        config.json["compute"]["discovery_server"]). All wdnwebui
        processes on the LAN share this daemon for participant +
        endpoint discovery, replacing the unreliable SIMPLE discovery
        that broke on embedded boards (Raspberry Pi, RK3588)."""
        with self._hub_lock:
            if self._hub is not None:
                return
        try:
            from wdndds.dds._rpc import Hub as _RpcHub
            # Register a fixed list of services (one per model_id)
            # upfront so the cshim Hub has them at start time.
            service_names = [
                f"wdndds/rpc/{mid}" for mid in MODEL_PRESETS.keys()
            ]
            # Read the discovery server list from config; default
            # is empty (SIMPLE discovery, single-host) when no
            # daemon is configured.
            discovery_servers = self._read_discovery_servers()
            device_name = _read_device_name()
            self._hub = _RpcHub(
                participant_name=f"wdnwebui-main-{device_name}",
                domain_id=0,
                discovery_servers=discovery_servers,
            )
            self._hub.start()
            LOG.info(
                "ComputeRegistry Hub started: services=%s "
                "discovery_servers=%s participant_name=wdnwebui-main-%s",
                service_names, discovery_servers, device_name)
        except Exception as e:  # noqa: BLE001
            LOG.exception("ComputeRegistry Hub bring-up failed")
            self._cards_failed = str(e)
            self._hub = None

    def _read_discovery_servers(self) -> list[str]:
        """Read the discovery server list from config.json.

        Returns ["host:port", ...] or [] for default SIMPLE discovery.
        """
        cfg = get_config().get_section("compute")
        if not cfg:
            return []
        servers = cfg.get("discovery_servers") or []
        if isinstance(servers, str):
            servers = [s.strip() for s in servers.split(",") if s.strip()]
        return [s for s in servers if s]

    def _hub_or_raise(self):
        if self._hub is None:
            raise RuntimeError(
                "wdndds Hub not available (fastdds init failed). "
                f"Reason: {self._cards_failed}"
            )
        return self._hub

    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    # Legacy migration                                                  #
    # ------------------------------------------------------------------ #
    def _migrate_legacy_capability_types(self) -> None:
        cfg = get_config().get_section("compute", {})
        instances = cfg.get("instances") if isinstance(cfg, dict) else None
        if not isinstance(instances, list):
            return
        renamed_total = 0
        for inst in instances:
            if not isinstance(inst, dict):
                continue
            caps = inst.get("capability_types")
            if not isinstance(caps, list):
                continue
            new_caps = [_LEGACY_CAPABILITY_TYPE_RENAMES.get(c, c) for c in caps]
            if new_caps != caps:
                inst["capability_types"] = new_caps
                renamed_total += sum(
                    1 for old, new in zip(caps, new_caps) if old != new
                )
        if renamed_total:
            cfg["instances"] = instances
            try:
                get_config().set_section("compute", cfg)
            except Exception as e:  # noqa: BLE001
                LOG.warning("failed to persist migrated capability_types: %s", e)

    # ------------------------------------------------------------------ #
    # 元数据查询                                                          #
    # ------------------------------------------------------------------ #
    def list_cards(self, force_refresh: bool = False) -> list[dict]:
        """NPU enumeration (wdnai path still needed for non-RPC metadata)."""
        with self._lock:
            if self._cards and not force_refresh:
                return list(self._cards)
        try:
            from rknn3lite.api import RKNN3Lite  # lazy
            ids = RKNN3Lite().get_devices_id()
        except Exception as e:
            with self._lock:
                self._cards = []
                self._cards_failed = str(e)
            return []
        cards = []
        for i, x in enumerate(ids):
            raw = x if isinstance(x, (bytes, bytearray)) else str(x).encode("utf-8")
            cards.append({
                "npu_index": i,
                "device_id": raw.decode("utf-8", errors="replace"),
                "_raw_device_id": raw,
            })
        with self._lock:
            self._cards = cards
            self._cards_failed = ""
        return list(cards)

    def cards_failed_reason(self) -> str:
        with self._lock:
            return self._cards_failed

    def list_supported_models(self) -> list[dict]:
        base = get_config().get_section("base", {})
        model_path = (
            base.get("model_path")
            or os.environ.get("WDNAI_MODELS_DIR")
            or "/userdata/models/"
        )
        root = Path(model_path)
        out = []
        for model_id, preset in MODEL_PRESETS.items():
            dir_name = preset["model_dir_name"]
            d = root / dir_name if dir_name else None
            out.append({
                "id":                 model_id,
                "label":              preset["label"],
                "description":        preset.get("description", ""),
                "exists":             d.is_dir() if d else None,
                "memory_estimate_mb": preset["memory_estimate_mb"],
                "capability_types":   list(preset["capability_types"]),
                "available_targets":  list(preset.get("available_targets", ["182x"])),
            })
        return out

    # ------------------------------------------------------------------ #
    # Error state management                                            #
    # ------------------------------------------------------------------ #
    def clear_error(self, model_id: Optional[str] = None) -> int:
        """清空一个（或全部）错误实例的 ``error`` 字段并把状态翻回
        ``not_loaded``，让 UI 上的红色 banner 消失、用户可以从 toggle
        重试启用。

        不会重启 / 卸载 worker —— 仅清错误显示 + 允许重试。Worker
        进程本身仍然在跑（如果还活着），下一次走 RPC load 即可。

        Returns the number of instances cleared.
        """
        cleared = 0
        with self._lock:
            for inst in self._instances.values():
                if model_id is not None and inst.model_id != model_id:
                    continue
                if inst.status != "error" and not inst.error:
                    continue
                inst.error = ""
                inst.status = "not_loaded"
                inst.stderr_full = ""
                cleared += 1
        return cleared

    # ------------------------------------------------------------------ #
    # Snapshot                                                           #
    # ------------------------------------------------------------------ #
    def snapshot(self) -> dict:
        with self._lock:
            instances = [inst.to_dict() for inst in self._instances.values()]

        cards = [
            {"npu_index": c["npu_index"], "device_id": c["device_id"]}
            for c in self.list_cards()
        ]
        supported = self.list_supported_models()
        per_card_mb: dict[int, int] = {}
        total_mb = 0
        for inst in instances:
            if inst["status"] != "running":
                continue
            per_card_mb[inst["npu_index"]] = per_card_mb.get(
                inst["npu_index"], 0) + inst["memory_estimate_mb"]
            total_mb += inst["memory_estimate_mb"]
        running_count = sum(1 for i in instances if i["status"] == "running")
        error_count   = sum(1 for i in instances if i["status"] == "error")
        loaded_count = sum(1 for i in instances
                           if i["status"] in {"running", "loading"})
        capabilities = self.list_active_capabilities()
        services = self.list_with_source()

        from app.services.tools import get_tool_registry
        tool_reg = get_tool_registry()
        tools = []
        for spec in tool_reg.list_specs():
            available = True
            reason = ""
            if spec.requires_capability:
                try:
                    self.get_session(spec.requires_capability)
                except RuntimeError as e:
                    available = False
                    reason = str(e)
            tools.append({
                "key":                 spec.key,
                "name":                spec.name,
                "description":         spec.description,
                "requires_capability": spec.requires_capability,
                "available":           available,
                "reason":              reason,
            })

        estimated_per_model = {
            mid: preset.get("estimated_init_seconds", DEFAULT_ESTIMATED_INIT_SECONDS)
            for mid, preset in MODEL_PRESETS.items()
        }
        return {
            "cards":             cards,
            "cards_failed":      self.cards_failed_reason(),
            "supported_models":  supported,
            "services":          services,
            "instances":         instances,
            "total_memory_mb":   total_mb,
            "per_card_memory_mb": per_card_mb,
            "queue_length":      sum(1 for s in services
                                     if s["status"] in {"not_loaded", "loading"}),
            "running_count":     running_count,
            "queued_count":      sum(1 for s in services
                                     if s["status"] == "not_loaded"),
            "loaded_count":      loaded_count,
            "error_count":       error_count,
            "estimated_init_seconds_per_model": estimated_per_model,
            "default_estimated_init_seconds":  DEFAULT_ESTIMATED_INIT_SECONDS,
            "worker_alive":      True,
            "last_updated":      time.time(),
            "capabilities":      capabilities,
            "tools":             tools,
        }

    def list_active_capabilities(self) -> list[dict]:
        """Capabilities advertised to consumer APIs (detector / tts / chat / vfa).

        A capability is "active" when its host instance has at least
        *some* live worker (not_loaded / loading / running all qualify).
        Only consumers calling ``get_session(capability)`` need the
        worker to be in ``running`` state; UI listings can show
        "available, not loaded" cards too.
        """
        with self._lock:
            out = []
            for inst in self._instances.values():
                if inst.status not in {"running", "loading", "not_loaded"}:
                    continue
                for cap_type in inst.capability_types:
                    out.append({
                        "type":               cap_type,
                        "name":               inst.instance_name,
                        "model":              inst.model_id,
                        "npu_index":          inst.npu_index,
                        "memory_estimate_mb": inst.memory_estimate_mb,
                    })
        return out

    def list_hardware_capabilities(self) -> list[dict]:
        state = _load_hardware_capability_state()
        out = []
        for entry in HARDWARE_CAPABILITIES:
            item = dict(entry)
            item["enabled"] = bool(state.get(entry["capability_type"], True))
            out.append(item)
        return out

    def set_hardware_capability_state(self, updates: dict[str, bool]) -> dict[str, bool]:
        if not isinstance(updates, dict):
            raise ValueError("updates 必须是 dict[str, bool]")
        valid_keys = {entry["capability_type"] for entry in HARDWARE_CAPABILITIES}
        new_state = _load_hardware_capability_state()
        applied: list[str] = []
        rejected: list[str] = []
        for k, v in updates.items():
            if k not in valid_keys:
                rejected.append(k)
                continue
            if not isinstance(v, bool):
                raise ValueError(f"{k} 必须是 bool, got {type(v).__name__}")
            new_state[k] = v
            applied.append(k)
        if applied:
            with self._lock:
                cfg = get_config().get_section("compute", {})
                if not isinstance(cfg, dict):
                    cfg = {}
                cfg["hardware_capabilities"] = new_state
                try:
                    get_config().set_section("compute", cfg)
                except Exception as e:  # noqa: BLE001
                    LOG.warning("failed to persist hardware_capabilities: %s", e)
        return {"applied": applied, "rejected": rejected, "state": new_state}

    # ------------------------------------------------------------------ #
    # 启停                                                              #
    # ------------------------------------------------------------------ #
    # v0.7: 启停拆成 spawn / adopt / bind / unload / stop 五步。
    # boot 由 ``boot_compute_services`` 触发；用户在算力配置页的"启用 /
    # 禁用 / 重启"对应 ensure_worker_for_model / unload_instance /
    # restart_instance_local。npu_index 通过 RPC load action 传给 worker，
    # 而不是命令行；worker 启动后只注册 DDS endpoint，不建 LLMSession。
    # 禁用 = unload（worker 进程保留，可秒级重启用）；stop_instance_local
    # 仅 restart 路径会走到，不再由 UI "禁用"按钮触发。

    def _register_errored_instance(self, inst: ModelInstance) -> None:
        """把 spawn / RPC setup 失败的 errored inst 加进 _instances，让
        算力卡能渲染红色 "启动失败" banner + 错误文案，而不是被
        list_with_source 的 fallback 当成 "unavailable" 隐藏起来。

        Key 用 ``(-1, model_id)``（_spawn_compute_worker 创建 inst 时
        的初始 key）。如果同一 model 已经在 _instances 里就覆盖（理论上
        不会 —— stop_instance_local 会先 pop 干净，但万一有 stale 条目
        也清掉）。
        """
        with self._lock:
            old_key = inst.key
            inst.key = (-1, inst.model_id)
            if old_key != inst.key:
                self._instances.pop(old_key, None)
            self._instances[inst.key] = inst
        LOG.info("registered errored instance: %s status=error err=%r",
                 inst.model_id, inst.error[:200])

    def _spawn_compute_worker(self, model_id: str) -> ModelInstance:
        """Spawn a fresh ``compute_worker.py --model <id>`` subprocess.

        The worker boots up with no ``--npu``: it brings its Hub +
        Service + Replier online and waits in ``not_loaded`` state
        until the main process sends ``{"action":"load","npu_index":N}``.

        v0.8 dedup: before spawning, scan ``/proc`` for any pre-existing
        ``compute_worker.py --model <id>`` process. If found, refuse to
        spawn a duplicate and instead adopt the existing one (handles
        the uvicorn-reload orphan case where a previous main process
        spawned a worker that survived SIGTERM).

        Returns the new ``ModelInstance`` keyed ``(-1, model_id)``, or
        the adopted one if dedup kicks in.
        """
        if model_id not in MODEL_PRESETS:
            raise ValueError(f"未知 model: {model_id!r}")
        # 1. 进程级 dedup：扫 /proc 同 comm 名的 worker（取代旧 cmdline substring 匹配）
        orphan_pids = _find_workers_by_name(model_id)
        if orphan_pids:
            LOG.warning(
                "spawn skipped: %d existing %s worker(s) found (pids=%s); "
                "adopting one and cleaning the rest",
                len(orphan_pids), model_id, orphan_pids)
            adopted = self._adopt_existing_worker(model_id)
            if adopted is not None:
                return adopted
            # adopt 失败（probe 超时等）降级到 spawn，避免完全阻塞启用流程
        # 2. 让 ensure_worker_for_model 阶段不重复进入
        with self._lock:
            for inst in self._instances.values():
                if inst.model_id == model_id:
                    return inst
        preset = MODEL_PRESETS[model_id]
        inst = ModelInstance(
            key=(-1, model_id),
            npu_index=-1,
            model_id=model_id,
            capability_types=list(preset["capability_types"]),
            instance_name=preset["instance_name"],
            label=preset["label"],
            memory_estimate_mb=preset["memory_estimate_mb"],
            status="not_loaded",
            queued_at=time.time(),
            started_at=time.time(),
            _source="spawned",
        )

        env = os.environ.copy()
        env.setdefault("PYTHONPATH", str(Path(__file__).resolve().parents[2]))
        env.setdefault("WDNAI_MODELS_DIR",
                       get_config().get_section("base", {}).get(
                           "model_path", "/userdata/models/"))

        cmd = [
            sys.executable, "-u",
            str(Path(__file__).resolve().parent / "compute_worker.py"),
            "--model", model_id,
            # 不传 --npu：npu_index 由用户在算力配置页选 NPU 后通过 RPC 传入
        ]
        try:
            proc = subprocess.Popen(
                cmd, env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                # 注意：进程 comm 是在 worker 端 main() 里
                # _set_comm_name(...) 调的，不是在 preexec_fn 里
                # —— preexec_fn 设的 PR_SET_NAME 会被紧随的 exec() 覆盖
                # （新 argv[0] basename 变成 comm）。所以这里不传
                # preexec_fn，命名逻辑下沉到 compute_worker.py。
            )
            inst.proc = proc
        except Exception as e:
            inst.status = "error"
            inst.error = f"spawn failed: {e}"
            # errored inst 也得进 _instances，否则 UI 走 list_with_source
            # fallback 把它当 "unavailable" 渲染（看不到红色 banner + 错误
            # 文案）。下面是 Popen 失败，inst.proc 还是 None —— probe
            # 跑不起来也没关系，UI 直接读 inst.error 显示。
            self._register_errored_instance(inst)
            return inst

        try:
            self._setup_rpc_client(inst)
        except Exception as e:  # noqa: BLE001
            LOG.exception("RpcClient create failed")
            inst.status = "error"
            inst.error = f"RpcClient: {e}"
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                pass
            inst.proc = None
            self._register_errored_instance(inst)
            return inst

        # Probe thread keeps inst.status in sync with worker's actual
        # not_loaded / loading / running state.
        probe = threading.Thread(
            target=self._probe_instance,
            args=(inst,),
            name=f"compute-probe-{model_id}",
            daemon=True,
        )
        inst._probe_thread = probe
        probe.start()

        with self._lock:
            self._instances[inst.key] = inst
        LOG.info("spawned compute_worker for %s (pid=%d)",
                 model_id, proc.pid)
        return inst

    def _adopt_existing_worker(self, model_id: str) -> Optional[ModelInstance]:
        """Probe the LAN daemon for a peer worker providing
        ``wdndds/rpc/<model_id>``. If found, register a ``ModelInstance``
        pointing at it (proc=None).

        v0.8 distinguishes two flavours of probed worker:

        * ``_source="adopted_local"`` — the responder's ``hostname``
          matches ``socket.gethostname()`` of the main process. This
          is a leftover worker from a previous uvicorn reload (Popen
          ``start_new_session=True`` workers detach from the parent
          process group and survive SIGTERM). The UI shows it in the
          本地算力 column but disables 禁用/重启 (only bind/load
          operations make sense against a worker we don't own).
        * ``_source="adopted"`` — the responder is on a different host.
          Shown in the 网络发现 column as ``<model> @ <device_name>``
          (read-only, no bind).

        Returns the adopted instance or ``None`` if no peer / orphan
        responds within ~1.5 s.
        """
        if model_id not in MODEL_PRESETS:
            return None
        info = self._probe_service(model_id)
        if not info:
            return None
        preset = MODEL_PRESETS[model_id]
        worker_npu = info.get("npu_index")
        worker_pid = info.get("pid")
        worker_host = (info.get("hostname") or "").strip()
        worker_device = (info.get("device_name") or "").strip()
        local_host = socket.gethostname()
        is_local = bool(worker_host) and worker_host == local_host
        if is_local:
            source = "adopted_local"
            label_host = worker_device or local_host
            if isinstance(worker_pid, int):
                # 本机孤儿：device_name（"本机"标注 + PID 排查用）
                peer_name = f"{label_host}（本机 pid={worker_pid}）"
            else:
                peer_name = f"{label_host}（本机）"
        else:
            source = "adopted"
            # 远端 peer：优先返回 device_name（用户配的易记名），UI 拼接
            # ``@<device_name>``；hostname 兜底显示在 _makePeerCard 的 peer-id 行
            if worker_device:
                peer_name = worker_device
            elif worker_host:
                peer_name = worker_host
            else:
                peer_name = "远端"
        inst = ModelInstance(
            key=(-1, model_id),
            npu_index=worker_npu if isinstance(worker_npu, int) else -1,
            model_id=model_id,
            capability_types=list(preset["capability_types"]),
            instance_name=preset["instance_name"],
            label=preset["label"],
            memory_estimate_mb=preset["memory_estimate_mb"],
            status=info.get("status", "not_loaded"),
            started_at=time.time(),
            finished_at=info.get("loaded_at"),
            load_time_sec=info.get("load_time_sec"),
            loaded_at=info.get("loaded_at"),
            _source=source,
            _peer_guid=info.get("_peer_guid", ""),
            _peer_name=peer_name,
            _peer_ip=info.get("peer_ip", ""),
        )
        # 把远端进程的真实 PID 暴露给 UI，让用户能 cross-check
        # ps -ef pkill（避免 reload 留下的同 model 孤儿进程被误判）。
        if isinstance(worker_pid, int):
            inst._peer_pid = worker_pid
        try:
            self._setup_rpc_client(inst)
        except Exception as e:  # noqa: BLE001
            LOG.warning("adopt: rpc setup failed for %s: %s",
                        model_id, e)
            return None
        with self._lock:
            existing = next((v for v in self._instances.values()
                             if v.model_id == model_id), None)
            if existing is not None:
                return existing
            if inst.npu_index >= 0:
                inst.key = (inst.npu_index, model_id)
            self._instances[inst.key] = inst
        probe = threading.Thread(
            target=self._probe_instance,
            args=(inst,),
            name=f"compute-probe-adopted-{model_id}",
            daemon=True,
        )
        inst._probe_thread = probe
        probe.start()
        LOG.info("adopted %s worker for %s (source=%s name=%s npu=%s status=%s)",
                 source, model_id, source, inst._peer_name,
                 inst.npu_index, inst.status)

        # 接管完后清理同一 model 的多余孤儿（同 comm 名但 probe 没接上的 PID）。
        # 这是修 uvicorn-reload 留下的 NPU 内存泄漏的关键：
        # 旧实现 _find_orphan_worker_pid 只返第一个 PID，剩下的孤儿无人收；
        # 现在按 comm 名扫出全部，probe 接走一个，剩下的 SIGTERM → 等 5s → SIGKILL。
        if isinstance(worker_pid, int):
            existing_pids = _find_workers_by_name(model_id)
            extras = [p for p in existing_pids if p != worker_pid]
            if extras:
                LOG.warning(
                    "adopted %s from pid=%d; cleaning %d extra orphan(s): %s",
                    model_id, worker_pid, len(extras), extras)
                self._terminate_orphan_pids(extras, grace_sec=5)

        return inst

    # Legacy single-step entry point kept for back-compat with code
    # written before the v0.7 split (UI now drives ensure + bind).
    def start_one(self, npu_index: int, model_id: str) -> ModelInstance:
        """Deprecated: use ``ensure_worker_for_model`` + ``bind_instance``.

        For back-compat: spawns the worker (no --npu), then issues an
        RPC ``load`` with the supplied ``npu_index`` so behaviour looks
        identical to callers expecting ``status='running'`` on return.
        """
        inst = self.ensure_worker_for_model(model_id)
        if inst._source != "spawned":
            raise RuntimeError(
                f"{model_id} 是 peer 提供的 service，本机无权 spawn")
        return self.bind_instance(model_id, npu_index)

    def ensure_worker_for_model(self, model_id: str) -> ModelInstance:
        """User-facing: ensure *some* worker exists for this model.

        Order:
          1. already in ``_instances``? return it
          2. probe the LAN daemon — if a peer responds, adopt
          3. otherwise spawn a fresh local subprocess

        Status returned may be ``not_loaded`` if the worker is up but
        the user has not yet bound an NPU (call ``bind_instance`` for
        that).
        """
        with self._lock:
            for inst in self._instances.values():
                if inst.model_id != model_id:
                    continue
                # spawned workers: 如果 subprocess 死了（probe 还没
                # 来得及翻 error，或 probe 已 return），不要复用 stale
                # inst —— 后面的 RPC 会发到死进程，等 120s timeout。
                # 杀掉 stale inst，重新走 adopt / spawn。
                if (inst._source == "spawned"
                        and inst.proc is not None
                        and inst.proc.poll() is not None):
                    LOG.warning(
                        "ensure_worker_for_model: %s inst=%s stale "
                        "(proc rc=%s); respawning",
                        model_id, id(inst), inst.proc.returncode)
                    try:
                        if inst.rpc_session_proxy is not None:
                            inst.rpc_session_proxy.close()
                    except Exception:
                        pass
                    try:
                        if inst.rpc_client is not None:
                            inst.rpc_client.destroy()
                    except Exception:
                        pass
                    self._instances.pop(inst.key, None)
                    # fall through to adopt / spawn
                    break
                return inst
        adopted = self._adopt_existing_worker(model_id)
        if adopted is not None:
            return adopted
        return self._spawn_compute_worker(model_id)

    def bind_instance(self, model_id: str, npu_index: int) -> ModelInstance:
        """v0.8 fire-and-forget：立即把 ``inst.status="loading"`` 返回，
        让前端 1Hz tick 看到 loading 状态 + 倒计时；后台线程跑 RPC
        ``load``，完成后翻 status=running / error，失败时 npu_index
        回退 -1。

        v0.7 之前是同步阻塞等 RPC load（120s timeout）—— 前端
        ``await fetch POST`` 卡 60s 才看到状态变化，用户体验
        "点了就变蓝，不知道什么时候加载完"。fire-and-forget 后
        用户能看到 is-load 脉动 + 倒计时（"约 30s"），加载完成
        toggle 切到 running 绿色 + 显示加载耗时。

        Raises ``RuntimeError`` for pre-flight checks (peer source,
        no RPC client, bad npu_index). The RPC call itself is async
        — its failures land on ``inst.status = "error"`` + ``inst.error``.
        """
        import threading

        inst = self._find_by_model(model_id)
        if inst is None:
            raise RuntimeError(f"本地无 {model_id} 的 worker")
        if inst._source == "adopted":
            raise RuntimeError(
                f"{model_id} 是 peer 提供的 service，本机无权 bind")
        if inst.rpc_client is None:
            raise RuntimeError(f"{model_id} worker RPC client 未就绪")
        if not isinstance(npu_index, int) or npu_index < 0:
            raise RuntimeError(f"npu_index 非法: {npu_index!r}")

        # 乐观更新：前端 1Hz tick 立即看到 status="loading" + npu_index。
        # key 不变 —— 同一个 model 在加载期间应仍是 (-1, model_id)；
        # 加载成功后异步改成 (npu_index, model_id)。
        with self._lock:
            inst.npu_index = npu_index
            inst.status = "loading"
            inst.started_at = time.time()
            inst.error = ""
        inst._bind_inflight = True

        def _run_load():
            try:
                r = inst.rpc_client.call(
                    {"action": "load", "npu_index": npu_index},
                    timeout_ms=120_000,
                )
                data = (r or {}).get("data") or {}
                if not (r or {}).get("ok"):
                    err_msg = (r or {}).get("error") or "RPC failed"
                    rc = inst.proc.poll() if inst.proc is not None else None
                    # 如果 worker 已经死了，stderr 留给 _probe_instance 读
                    # + 写错误（它有完整 worker exit 处理路径，且会 return）。
                    # 这里只更新状态，避免覆盖 probe 写的 stderr。
                    stderr_tail = (
                        "" if rc is not None
                        else _drain_worker_stderr(inst.proc))
                    LOG.warning(
                        "bind %s RPC fail: err=%r rc=%s stderr_len=%d "
                        "stderr_tail=%r",
                        model_id, err_msg, rc, len(stderr_tail),
                        stderr_tail[:200] if stderr_tail else "",
                    )
                    with self._lock:
                        inst.status = "error"
                        inst.npu_index = -1
                        if rc is not None:
                            if not inst.error:
                                inst.error = f"compute_worker exited rc={rc}"
                        else:
                            parts = [str(err_msg)]
                            if stderr_tail:
                                parts.append(
                                    f"worker stderr (last "
                                    f"{len(stderr_tail)} chars):\n"
                                    f"{stderr_tail.rstrip()}")
                            inst.error = "\n".join(parts)
                    LOG.warning("bind %s load RPC failed: %s%s",
                                model_id, err_msg,
                                f" (rc={rc})" if rc is not None else "")
                    return
                if data.get("status") == "error":
                    err_msg = data.get("init_error", "load worker error")
                    with self._lock:
                        inst.status = "error"
                        inst.error = str(err_msg)
                        inst.npu_index = -1
                    LOG.warning("bind %s load worker error: %s",
                                model_id, err_msg)
                    return
                if data.get("status") == "missing_model_dir":
                    err_msg = data.get("error", "模型目录不存在")
                    with self._lock:
                        inst.status = "missing_model_dir"
                        inst.error = err_msg
                        inst.npu_index = -1
                    LOG.warning("bind %s missing_model_dir: %s",
                                model_id, err_msg)
                    return
                if data.get("status") == "loading":
                    # Worker 把 LLMSession init 挪到独立线程跑（避免
                    # cshim Replier pump 线程持 rknn 资源跟 init 死锁），
                    # RPC 立即返 loading。_probe_instance 1Hz 探测 info
                    # RPC，看到 worker 的 state["status"] 翻 running /
                    # error 自动同步到 inst。这里 fire-and-forget 直接
                    # 退场：probe 接管。
                    new_key = (npu_index, model_id)
                    with self._lock:
                        if inst.key != new_key:
                            self._instances.pop(inst.key, None)
                            inst.key = new_key
                            self._instances[new_key] = inst
                        # inst.status 已经是 loading（bind_instance 入口
                        # 乐观设置）；保留不要改 running。
                    LOG.info("bind %s @ npu=%d: async load started, "
                             "deferring to probe", model_id, npu_index)
                    return
                # 翻 running + 更新 key + 加载耗时
                new_key = (npu_index, model_id)
                with self._lock:
                    if inst.key != new_key:
                        self._instances.pop(inst.key, None)
                        inst.key = new_key
                        self._instances[new_key] = inst
                    inst.status = data.get("status", "running")
                    inst.load_time_sec = data.get("load_time_sec")
                    inst.loaded_at = data.get("loaded_at")
                    inst.finished_at = time.time()
                LOG.info("bound %s @ npu=%d (load=%.1fs)",
                         model_id, npu_index, inst.load_time_sec or 0.0)
            except Exception as e:  # noqa: BLE001
                # RPC call itself raised (transport-level error before
                # we even got a JSON reply). Best-effort drain worker
                # stderr so the user sees the real reason (NPU abort,
                # native panic, weight load error) instead of just the
                # exception name.
                rc = inst.proc.poll() if inst.proc is not None else None
                stderr_tail = (
                    "" if rc is not None
                    else _drain_worker_stderr(inst.proc))
                with self._lock:
                    inst.status = "error"
                    inst.npu_index = -1
                    if rc is not None:
                        if not inst.error:
                            inst.error = f"compute_worker exited rc={rc}"
                    else:
                        parts = [f"{type(e).__name__}: {e}"]
                        if stderr_tail:
                            parts.append(
                                f"worker stderr (last "
                                f"{len(stderr_tail)} chars):\n"
                                f"{stderr_tail.rstrip()}")
                        inst.error = "\n".join(parts)
                LOG.exception("bind %s load raised%s",
                              model_id,
                              f" (rc={rc})" if rc is not None else "")
            finally:
                inst._bind_inflight = False

        threading.Thread(target=_run_load, daemon=True,
                         name=f"compute-bind-{model_id}").start()
        return inst

    def unload_instance(self, model_id: str) -> None:
        """RPC ``unload``: worker drops its LLMSession, status returns
        to ``not_loaded``, npu_index resets to -1.

        Works for adopted peers too — we just ask the peer to release
        the resource; we don't terminate anything we don't own.
        """
        inst = self._find_by_model(model_id)
        if inst is None or inst.rpc_client is None:
            return
        try:
            inst.rpc_client.call(
                {"action": "unload"}, timeout_ms=30_000)
        except Exception as e:  # noqa: BLE001
            LOG.warning("unload %s RPC failed: %s", model_id, e)
        with self._lock:
            old_key = inst.key
            inst.npu_index = -1
            inst.key = (-1, model_id)
            inst.status = "not_loaded"
            inst.load_time_sec = None
            inst.loaded_at = None
            self._instances.pop(old_key, None)
            self._instances[inst.key] = inst

    def stop_instance_local(self, model_id: str) -> None:
        """Spawned-only: unload → RPC shutdown → wait 3 s → kill -9.

        Raises ``RuntimeError`` if the instance is adopted (proc is
        None) — the caller (API layer) turns this into HTTP 403.
        """
        inst = self._find_by_model(model_id)
        if inst is None:
            raise RuntimeError(f"{model_id} 不在 registry")
        if inst._source == "adopted":
            raise RuntimeError(
                f"{model_id} 是远端 peer 提供的 service，本机无权停止")
        try:
            self.unload_instance(model_id)
        except Exception as e:  # noqa: BLE001
            LOG.warning("stop: unload %s raised: %s", model_id, e)
        try:
            if inst.rpc_client is not None:
                inst.rpc_client.call(
                    {"action": "shutdown"}, timeout_ms=5_000)
        except Exception as e:  # noqa: BLE001
            LOG.info("stop: shutdown RPC %s raised: %s", model_id, e)
        proc = inst.proc
        if proc is not None and proc.poll() is None:
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                    proc.wait(timeout=2)
                except Exception as e:  # noqa: BLE001
                    LOG.warning("kill %s failed: %s", model_id, e)
        try:
            if inst.rpc_session_proxy is not None:
                inst.rpc_session_proxy.close()
        except Exception:
            pass
        try:
            if inst.rpc_client is not None:
                inst.rpc_client.destroy()
        except Exception:
            pass
        with self._lock:
            self._instances.pop(inst.key, None)
        LOG.info("stopped local worker for %s", model_id)

    def restart_instance_local(self, model_id: str) -> None:
        """Spawned-only 4-step restart (Q3 spec):
          1. RPC ``unload`` (graceful model release)
          2. RPC ``shutdown`` (worker self-terminates)
          3. wait / kill -9 if it doesn't exit
          4. run ``ensure_worker_for_model`` (probe → adopt-or-spawn).

        Per "走 run.py 启动流程" semantics: we do NOT auto-bind after
        respawn. The worker comes back up in ``not_loaded`` and the
        user must re-select an NPU on the compute-config page.
        """
        try:
            self.stop_instance_local(model_id)
        except RuntimeError:
            raise
        except Exception as e:  # noqa: BLE001
            LOG.warning("restart: stop %s raised: %s", model_id, e)
        self.ensure_worker_for_model(model_id)

    def stop_all_local(self) -> None:
        """Stop every spawned worker (proc != None). Adopted peers
        untouched."""
        models = []
        with self._lock:
            for inst in self._instances.values():
                if inst.proc is not None:
                    models.append(inst.model_id)
        for m in models:
            try:
                self.stop_instance_local(m)
            except Exception:  # noqa: BLE001
                LOG.exception("stop_all_local: %s failed", m)

    def restart_all_local(self) -> tuple[int, list[str]]:
        """Restart every spawned worker (unload → shutdown → spawn).
        Adopted peers untouched — they're owned by other machines.

        Each model goes through the same Q3 4-step flow as a per-card
        "重启" button click; after this call, every spawned worker is
        back in ``not_loaded`` state and the user must re-select NPU on
        the compute-config page to re-bind.

        Returns ``(exit_code, log_lines)``:
        * ``exit_code = 0`` — all spawned workers restarted OK, OR
          there were no spawned workers to restart.
        * ``exit_code = 1`` — at least one worker's restart failed
          (others may still have succeeded; log lines carry details).
        """
        models: list[str] = []
        with self._lock:
            for inst in self._instances.values():
                if inst.proc is not None:
                    models.append(inst.model_id)
        if not models:
            return 0, ["无 spawned worker，跳过"]
        lines: list[str] = []
        any_fail = False
        for m in models:
            try:
                self.restart_instance_local(m)
                # 重启后强制归零：race window 期间（OLD worker 收到 RPC
                # shutdown → Python main loop 检测到 stop_event 并 sys.exit
                # 之间 ~1s，cshim pump 还没退、info RPC 还能 reply），
                # _adopt_existing_worker 可能把 OLD worker 临死前的
                # status（"running" / "loading" / "error"）继承进新 inst。
                # OLD worker 死掉之后 _probe_instance 对 adopted_local
                # 不翻 error（那个分支只对 adopted 远端 peer 生效），算
                # 力卡就永久停在 stale 颜色。这里直接 normalize 成
                # not_loaded —— 新 spawn 出来的 worker 没绑 NPU，本就应
                # 该是这个状态；只是兜底。跳过 adopted（远端 peer），它们
                # 的 status 应该如实反映。
                new_inst = self._find_by_model(m)
                if new_inst is not None and new_inst._source != "adopted":
                    with self._lock:
                        new_inst.status = "not_loaded"
                        new_inst.npu_index = -1
                        new_inst.error = ""
                        new_inst.finished_at = 0.0
                        new_inst.load_time_sec = None
                        new_inst.loaded_at = None
                lines.append(f"{m}: 重启 OK")
            except Exception as e:  # noqa: BLE001
                LOG.exception("restart_all_local: %s failed", m)
                lines.append(f"{m}: 失败 — {e}")
                any_fail = True
        return (1 if any_fail else 0), lines

    # ------------------------------------------------------------------ #
    # subprocess-mode spawn (wdndds cshim RPC)                           #
    # ------------------------------------------------------------------ #
    def _setup_rpc_client(self, inst: ModelInstance) -> None:
        """Create the RpcClient + RpcSessionProxy for the given instance.

        Used by both ``_start_one_subprocess`` (fresh spawn) and
        ``_adopt_existing_worker`` (boot-time adoption of an already
        responding peer). Both paths go through the same wdndds
        service name ``wdndds/rpc/<model_id>`` so FastDDS discovery
        routes calls to whichever process is actually hosting the
        service — local subprocess OR a peer on the LAN.
        """
        hub = self._hub_or_raise()
        svc = hub.create_service(f"wdndds/rpc/{inst.model_id}")
        rpc_client = svc.create_requester(timeout_ms=10_000)
        inst.rpc_client = rpc_client
        try:
            from app.services.rpc_session_proxy import make_rpc_session_proxy
            inst.rpc_session_proxy = make_rpc_session_proxy(
                rpc_client, inst.model_id)
            inst.llm_session = inst.rpc_session_proxy
        except Exception as e:  # noqa: BLE001
            LOG.warning("RpcSessionProxy create failed for %s: %s",
                        inst.model_id, e)

    def _probe_service(self, model_id: str) -> Optional[dict]:
        """Short-timeout RPC ``info`` probe on ``wdndds/rpc/<model_id>``.

        Returns the worker's info dict if a worker responds within
        ~1.5s, else ``None``. Used by ``_adopt_existing_worker`` to
        avoid spawning duplicate processes when a peer (or a stale
        orphan from a previous boot) is already providing the
        service on the LAN.
        """
        if self._hub is None:
            return None
        try:
            svc = self._hub.create_service(f"wdndds/rpc/{model_id}")
            rpc = svc.create_requester(timeout_ms=1500)
        except Exception as e:  # noqa: BLE001
            LOG.debug("probe: create requester failed for %s: %s",
                      model_id, e)
            return None
        try:
            r = rpc.call({"action": "info"}, timeout_ms=1500)
        except Exception as e:  # noqa: BLE001
            LOG.debug("probe: call failed for %s: %s", model_id, e)
            return None
        finally:
            try:
                rpc.destroy()
            except Exception:
                pass
        if not r or not r.get("ok"):
            return None
        return r.get("data") or {}

    def _find_by_model(self, model_id: str) -> Optional[ModelInstance]:
        """Look up a registered instance by model_id (key changes after
        bind so we can't just key-index)."""
        with self._lock:
            for inst in self._instances.values():
                if inst.model_id == model_id:
                    return inst
        return None

    def list_with_source(self) -> list[dict]:
        """UI-friendly snapshot of every catalog model's current state.

        Returns one entry per ``MODEL_PRESETS`` key (so the page can
        always render the full catalog), with a ``source`` field:

          * ``"spawned"``  — this machine spawned the worker (proc set)
          * ``"adopted"``  — peer on the LAN is providing the service
          * ``"unavailable"`` — daemon probed, no peer responded

        Ordered: spawned first, adopted second, unavailable last.
        """
        out: list[dict] = []
        with self._lock:
            by_model: dict[str, ModelInstance] = {
                inst.model_id: inst for inst in self._instances.values()
            }
        for model_id, preset in MODEL_PRESETS.items():
            inst = by_model.get(model_id)
            if inst is None:
                out.append({
                    "model_id":           model_id,
                    "label":              preset["label"],
                    "instance_name":      preset["instance_name"],
                    "capability_types":   list(preset["capability_types"]),
                    "memory_estimate_mb": preset["memory_estimate_mb"],
                    "description":        preset.get("description", ""),
                    "available_targets":  list(preset.get("available_targets", ["182x"])),
                    "status":             "unavailable",
                    "error":              "",
                    "npu_index":          -1,
                    "pid":                0,
                    "load_time_sec":      None,
                    "loaded_at":          None,
                    "_source":            "unavailable",
                    "_peer_guid":         "",
                    "_peer_name":         "",
                    "_peer_ip":           "",
                })
                continue
            d = inst.to_dict()
            out.append(d)
        # Stable ordering: spawned → adopted → unavailable.
        order = {"spawned": 0, "adopted": 1, "unavailable": 2}
        out.sort(key=lambda x: (order.get(x.get("_source", ""), 9),
                                x.get("model_id", "")))
        return out

    def boot_compute_services(self) -> None:
        """App boot: ensure every model in ``config.compute.services``
        has *some* live worker reachable on the LAN.

        For each model_id in the list:
          1. if we already have a record, skip
          2. probe the LAN daemon — adopt if a peer responds
          3. otherwise spawn a fresh local subprocess

        Workers are NOT bound to an NPU at boot; they wait in
        ``not_loaded`` until the user clicks 启用 on the compute-config
        page (which triggers ``bind_instance``).

        末尾跑一次 :meth:`_cleanup_unregistered_workers` 兜底扫一遍
        ``/proc/*/comm``，杀掉 registry 没接管、但 comm 以 ``worker:``
        开头的进程（理论上 ``_adopt_existing_worker`` 内已经清过一遍，
        这里只是防 probe 漏掉、或用户在 boot 期间手动启了 worker 的
        极端情况）。
        """
        try:
            self.list_cards()
        except Exception:
            pass
        try:
            stored = get_config().get_section("compute", {})
        except Exception:
            stored = {}
        services = stored.get("services") if isinstance(stored, dict) else None
        if not isinstance(services, list):
            return
        for entry in services:
            if not isinstance(entry, str):
                continue
            model_id = entry
            if model_id not in MODEL_PRESETS:
                LOG.warning("config.compute.services 跳过未知 model: %s",
                            model_id)
                continue
            with self._lock:
                if any(i.model_id == model_id for i in self._instances.values()):
                    continue
            adopted = None
            try:
                adopted = self._adopt_existing_worker(model_id)
            except Exception as e:  # noqa: BLE001
                LOG.warning("boot probe failed for %s: %s", model_id, e)
            if adopted is not None:
                LOG.info("boot: adopted %s (peer guid=%s status=%s)",
                         model_id, adopted._peer_guid, adopted.status)
                continue
            try:
                self._spawn_compute_worker(model_id)
            except Exception as e:  # noqa: BLE001
                LOG.warning("boot spawn %s failed: %s", model_id, e)
        # boot 末尾总清扫：处理 config 移除某 service 后残留的孤儿、
        # 以及 _adopt 阶段 probe 漏掉的边界情况。
        try:
            self._cleanup_unregistered_workers()
        except Exception as e:  # noqa: BLE001
            LOG.warning("boot cleanup sweep failed: %s", e)

    def _cleanup_unregistered_workers(self) -> int:
        """Boot-time defensive sweep.

        对每个在 ``config.compute.services`` 列出的 model 调
        :func:`_find_workers_by_name`（先按 comm、再 fallback cmdline
        兼容老 worker），扫出所有同名进程：
          * 已经在 registry 里的（spawned 的 proc.pid / adopted_local
            的 _peer_pid）→ 保留；
          * 剩下的（不在 registry 但 comm/cmdline 匹配）→ 视为孤儿，
            SIGTERM → 5s grace → SIGKILL。

        对**不在** config 里的 model_id 的孤儿 worker 也会扫（用
        ``MODEL_PRESETS`` 全集）—— 用户从 config 删除某个 service
        后，那个孤儿进程应该被回收，不能继续吃 NPU 内存。

        Returns the count of PIDs killed.
        """
        try:
            configured = set(
                get_config().get_section("compute", {}).get("services", []))
        except Exception:
            configured = set()

        tracked_pids = set()
        with self._lock:
            for inst in self._instances.values():
                if inst.proc is not None and inst.proc.pid:
                    tracked_pids.add(inst.proc.pid)
                if inst._source == "adopted_local":
                    pid = getattr(inst, "_peer_pid", None)
                    if isinstance(pid, int):
                        tracked_pids.add(pid)

        # 扫的范围：config 里的 + preset 全集（处理 user 从 config 删
        # 掉的孤儿）。preset 里没的 model_id 不会被 _find_workers_by_name
        # 误识（needle 不匹配），所以多扫也安全。
        scan_targets = set(configured) | set(MODEL_PRESETS.keys())
        orphans: List[int] = []
        for model_id in scan_targets:
            for pid in _find_workers_by_name(model_id):
                if pid in tracked_pids:
                    continue
                orphans.append(pid)
                if model_id in configured:
                    LOG.warning(
                        "cleanup: %s pid=%d not tracked; killing",
                        model_id, pid)
                else:
                    LOG.warning(
                        "cleanup: %s pid=%d no longer in "
                        "config.compute.services; killing",
                        model_id, pid)

        if not orphans:
            return 0
        return _terminate_orphan_pids(orphans, grace_sec=5)

    def close_all(self) -> None:
        """Web shutdown: close the main process Hub ONLY.

        Workers are independent processes; we must NOT terminate them
        from here — they may outlive this web process, be restarted
        by systemd, or be required by a peer. Adopted peers obviously
        can't be killed by us anyway.

        ``stop_all_local`` is intentionally NOT called.
        """
        try:
            if self._hub is not None:
                self._hub.close()
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # Boot                                                              #
    # ------------------------------------------------------------------ #
    # Note: boot 不再自动 spawn worker。实例 (npu, model) 由用户在算力
    # 配置页点击启动时通过 ``POST /api/compute/instances`` 指定，npu_index
    # 作为命令行 ``--npu <idx>`` 透传给子进程，不写 config.json；主进程
    # 重启后用户需重新点击启用。registry 单例本身仍会被构造（_ensure_hub
    # 起主进程 Hub 做 RPC client），但 _instances 一律为空起步。

    def _probe_instance(self, inst: ModelInstance) -> None:
        """Background: keep polling the worker's ``info`` RPC.

        Loop exits when the worker reaches a **terminal** state:

          * ``running``  — LLMSession is up; probe no longer needed
          * ``error``    — worker reported init_error / probe gave up
          * spawned worker subprocess exits (proc.poll() != None)
          * adopted peer disappears (>= 10s of consecutive RPC timeouts,
            controlled by ``_PEER_LOST_CONSECUTIVE_FAILURES``)

        For ``not_loaded`` / ``loading`` we keep polling indefinitely.
        Previously this loop had a 10-min deadline that incorrectly
        flipped long-idle workers to ``error`` — the new design lets
        a worker sit in ``not_loaded`` until the user binds an NPU
        via the compute-config page (which can be arbitrarily late).
        """
        consecutive_failures = 0
        try:
            while True:
                # Check subprocess liveness first (spawned only).
                if inst.proc is not None:
                    rc = inst.proc.poll()
                    if rc is not None:
                        # Once the worker has exited, ``proc.stderr.read()``
                        # drains everything that's still buffered (the pipe
                        # stays open until the parent closes its fd).
                        # Coordinate with :meth:`bind_instance._run_load`
                        # which also drains stderr on the RPC-fail path:
                        # whichever thread reads first wins, the other
                        # defers. ``inst.error`` containing ``"stderr"``
                        # is the marker that someone already populated
                        # the error field — in that case we only update
                        # status / finished_at so the message stays.
                        err = ""
                        try:
                            if inst.proc.stderr:
                                err = inst.proc.stderr.read().decode(
                                    "utf-8", errors="replace")
                        except Exception:
                            pass
                        with self._lock:
                            inst.status = "error"
                            inst.finished_at = time.time()
                            if "stderr" not in inst.error:
                                parts = [f"compute_worker exited rc={rc}"]
                                if err:
                                    parts.append(f"stderr: {err[:1500]}")
                                inst.error = "\n".join(parts)
                        LOG.warning(
                            "compute_worker for %s @ npu=%d exited rc=%d; "
                            "stderr (last 800 chars):\n%s",
                            inst.model_id, inst.npu_index, rc, err[-800:],
                        )
                        return

                # Probe worker.
                if inst.rpc_client is not None:
                    try:
                        r = inst.rpc_client.call(
                            {"action": "info"}, timeout_ms=5000)
                    except Exception as e:
                        consecutive_failures += 1
                        LOG.warning("info call failed for %s (%d): %s",
                                   inst.model_id, consecutive_failures, e)
                        # For adopted (远端 peer): after several consecutive
                        # failures we conclude the peer is gone. adopted_local
                        # 跟 spawned 一样不算（它们是本机孤儿，不存在 "peer
                        # 失联" 的概念）。
                        if (inst._source == "adopted"
                                and consecutive_failures >= _PEER_LOST_CONSECUTIVE_FAILURES
                                and inst.status != "error"):
                            with self._lock:
                                inst.status = "error"
                                inst.error = "peer 已失联"
                            LOG.warning(
                                "adopted peer %s lost (consecutive "
                                "RPC failures=%d)",
                                inst.model_id, consecutive_failures)
                        time.sleep(2.0)
                        continue
                    if not r.get("ok"):
                        consecutive_failures += 1
                        time.sleep(1.0)
                        continue
                    consecutive_failures = 0
                    data = r.get("data") or {}
                    worker_status = data.get("status", "not_loaded")
                    # 每轮都把 worker 报告的 model_dir 缓存到实例上——
                    # missing_model_dir 状态时 UI 直接拿这个字段渲染路径。
                    if isinstance(data.get("model_dir"), str) and data["model_dir"]:
                        inst.model_dir = data["model_dir"]
                    if worker_status == "running":
                        with self._lock:
                            inst.status = "running"
                            worker_npu = data.get("npu_index")
                            if (isinstance(worker_npu, int)
                                    and worker_npu >= 0
                                    and inst.npu_index != worker_npu):
                                self._instances.pop(inst.key, None)
                                inst.npu_index = worker_npu
                                inst.key = (worker_npu, inst.model_id)
                                self._instances[inst.key] = inst
                            inst.finished_at = data.get(
                                "loaded_at", time.time())
                            inst.load_time_sec = data.get("load_time_sec", 0.0)
                            inst.loaded_at = inst.finished_at
                            inst.error = ""
                        LOG.info(
                            "instance %s @ npu=%d → running (load=%.1fs)",
                            inst.model_id, inst.npu_index,
                            inst.load_time_sec)
                        return
                    if worker_status == "error":
                        with self._lock:
                            inst.status = "error"
                            inst.error = data.get("init_error", "")
                            inst.finished_at = time.time()
                        LOG.warning(
                            "instance %s @ npu=%d → error: %s",
                            inst.model_id, inst.npu_index, inst.error)
                        return
                    if worker_status == "loading":
                        with self._lock:
                            if inst.status != "loading":
                                inst.status = "loading"
                                LOG.info("instance %s @ npu=%d → loading",
                                          inst.model_id, inst.npu_index)
                    elif worker_status == "not_loaded":
                        with self._lock:
                            if inst.status not in {"not_loaded", "loading",
                                                    "running", "error",
                                                    "missing_model_dir"}:
                                inst.status = "not_loaded"
                    elif worker_status == "missing_model_dir":
                        # 资源缺失：worker 启动时就发现 model_dir 不存在，
                        # 或 load 时仍不存在。**不** exit probe —— 用户补
                        # 完目录后可以再 toggle 重发 RPC load，probe 跑下
                        # 一轮就能收敛到 running。
                        with self._lock:
                            inst.status = "missing_model_dir"
                            inst.error = ""
                            inst.finished_at = time.time()

                time.sleep(1.0)
        except Exception as e:
            LOG.exception("probe_instance crashed")
            with self._lock:
                inst.status = "error"
                inst.error = f"probe crashed: {type(e).__name__}: {e}"

    def _persist(self) -> None:
        """No-op stub kept for backward compat with older call sites.

        ``config.json["compute"]["instances"]`` 不再被 registry 维护——
        实例 (npu, model) 由用户在算力配置页点击启动时通过
        ``POST /api/compute/instances`` 指定并作为子进程命令行
        （``--npu <idx>``）传入，不持久化。
        """
        return

    # ------------------------------------------------------------------ #
    # 消费者 API                                                         #
    # ------------------------------------------------------------------ #
    def get_session(self, capability_type: str) -> Any:
        """Return the RpcSessionProxy for the first running instance
        exposing ``capability_type``. Raises RuntimeError if none."""
        with self._lock:
            for inst in self._instances.values():
                if (inst.status == "running"
                        and capability_type in inst.capability_types):
                    if inst.rpc_session_proxy is None:
                        raise RuntimeError(
                            f"功能未启用: {capability_type} (instance has no proxy)")
                    return inst.rpc_session_proxy
        raise RuntimeError(f"功能未启用: {capability_type}")


# --------------------------------------------------------------------------- #
# Singleton                                                                  #
# --------------------------------------------------------------------------- #
_registry: Optional[ComputeRegistry] = None
_registry_lock = threading.Lock()


def get_registry() -> ComputeRegistry:
    global _registry
    with _registry_lock:
        if _registry is None:
            _registry = ComputeRegistry()
        return _registry


# --------------------------------------------------------------------------- #
# Backward-compat: legacy ``get_session_call_lock()`` API.                       #
# In the v0.4 per-subprocess RPC refactor the call lock is no longer needed     #
# in the main process (server-side lock in compute_worker serializes per       #
# instance). We keep this function returning a no-op context manager so        #
# existing call sites (video_file_analysis.py) keep working.                  #
# --------------------------------------------------------------------------- #
import contextlib


@contextlib.contextmanager
def get_session_call_lock():
    """Legacy no-op call lock. Kept for backward compat with code
    written before the per-subprocess RPC refactor."""
    yield
