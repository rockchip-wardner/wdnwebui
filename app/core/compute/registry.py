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
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional, Tuple

from wdnai import RetiredCapabilityError, migrate_legacy

from app.core.config import default_models_root, get_config
from app.core.compute.workers import (
    WorkerManifest,
    build_model_spec,
    discover as discover_workers,
    load_one as load_worker_manifest,
    model_presets as _model_presets,
)


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


def _resolve_compute_mode() -> str:
    env = _os.environ.get("WDNWEBUI_COMPUTE_MODE", "").strip().lower()
    if env in ("subprocess", "inprocess"):
        return env
    return "subprocess"


# --------------------------------------------------------------------------- #
# 模型卡位（model slot）                                                        #
# --------------------------------------------------------------------------- #
# **枚举到的卡 ≠ 能跑模型的卡**。RKNN3 枚举（``get_devices_id()``）里混着
# 两类东西：PCI BDF 形态（``0003:31:00.0``）是真正的 RK1828 协卡，能加载
# rknn 模型；USB 序列号形态（``180B6D6C…``）只在 RGA / VPU 侧可见，在它
# 上面加载 rknn 模型必然 ``vision init_runtime failed``（AGENTS.md v0.20
# 实测：4 卡板子上 npu2 / npu3 就是这个形态）。
#
# 所以「模型卡位数」= 探测到的 PCI 形态卡数。**每张卡只能放一个模型**
# （AGENTS.md v0.17 实测：同卡叠两个模型 rc=-6），这个数量就是本机
# 能同时跑几个模型的硬上限。
_PCI_BDF_RE = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.\d+$")


def card_count_override() -> Optional[int]:
    """``config.jsonc["compute"]["card_count"]`` —— 手工指定模型卡位数。

    语义是**对探测结果的上限**，不是强制值：双卡板写 1 → 模拟单卡板；
    单卡板写 2 → 仍然是 1 张（不能凭空变出卡）。值非法（非整数 / < 1 /
    bool）时忽略，按探测结果走 —— 配置写错不该让整机没有算力。
    """
    try:
        section = get_config().get_section("compute", {})
    except Exception:  # noqa: BLE001
        return None
    raw = section.get("card_count") if isinstance(section, dict) else None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        return None
    return raw


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
        "description":     "对外播放音频，配合 swcap_text_to_speech 一起使用。",
    },
    {
        "capability_type": "hwcap_hdmi_display",
        "class_name":      "HWHDMIDisplay",
        "category":        "video",
        "label":           "HDMI 显示输出",
        "description":     "把视频 / 画面推送到 HDMI 显示设备。",
    },
    {
        # RK3588 的 HDMI-RX 控制器（驱动 rk_hdmirx，对应 /dev/video0），
        # 通过 wdnav.v4l2.V4L2Capture 抓 PC 输出画面。该 capability 是
        # "AI 操作电脑" 卡片（app/apps/usb_hid/）的硬性依赖：没启用
        # 时卡片显示不可用。PCIe 板卡上不一定有这个节点，所以放在
        # capability 后面挂开关，比硬编码更通用。
        "capability_type": "hwcap_hdmiin",
        "class_name":      "HWHDMIInput",
        "category":        "video",
        "label":           "HDMI 输入 (rk_hdmirx)",
        "description":     "通过 RK3588 HDMI-RX 控制器采集 PC 端画面。",
    },
    {
        # USB Gadget HID (configfs /sys/kernel/config/usb_gadget/g1
        # + /dev/hidg{0,1})。与 hwcap_hdmiin 解耦: hdmiin 是视频
        # 输入源, gadget 是输出通道; 任一缺失都会让卡片显示不可用
        # 但 wizard / page 应分别提示。板子无 OTG controller 时
        # 把这个关掉即可彻底隐藏 HID 链路。
        "capability_type": "hwcap_hid_gadget",
        "class_name":      "HWHIDGadget",
        "category":        "usb",
        "label":           "USB Gadget HID (OTG)",
        "description":     "通过 configfs + OTG UDC 暴露 boot 键盘 + digitizer。",
    },
]


def _hardware_capability_default_state() -> dict[str, bool]:
    return {entry["capability_type"]: True for entry in HARDWARE_CAPABILITIES}


def _load_hardware_capability_state() -> dict[str, bool]:
    """读 ``base.hardware_capabilities`` 的 enabled 状态。

    ``hardware_capabilities`` 已从 ``compute`` 挪到 ``base``（跟设备
    自身的硬件能力绑定，不属于 RKNN 实例配置）。老 config.jsonc 里
    仍写在 ``compute.hardware_capabilities`` 也能读 —— 这是过渡期
    兜底，等下次任意 save 触发落盘后老路径自然被替换。

    返回每个 ``HARDWARE_CAPABILITIES`` key 对应 bool；缺省全 True。
    """
    cfg = get_config()
    state = _hardware_capability_default_state()
    # New path: base.hardware_capabilities
    base_cfg = cfg.get_section("base", {})
    stored = (base_cfg.get("hardware_capabilities")
              if isinstance(base_cfg, dict) else None)
    # Backward-compat fallback: compute.hardware_capabilities (legacy)
    if not isinstance(stored, dict):
        compute_cfg = cfg.get_section("compute", {})
        stored = (compute_cfg.get("hardware_capabilities")
                  if isinstance(compute_cfg, dict) else None)
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

_RKNN_PROBE_TIMEOUT = 5.0


def _read_device_name() -> str:
    """Read ``device_name`` from ``config.jsonc["base"]``.

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


def _get_local_ip() -> str:
    """Outbound LAN IP via UDP-connect trick (no actual send). Used to
    tell local orphan from true LAN peer — clone-image boxes share
    the same hostname but have different IPs, so hostname() alone
    is ambiguous."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))   # public IP, no actual send
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return ""


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
    """Reverse-lookup: given a comm string (e.g. ``worker:vlm-4b``),
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
# Preset table — derived from the worker manifests                          #
# --------------------------------------------------------------------------- #
# 每个模型的「身份」现在由 ``app/core/compute/workers/<id>.json`` 声明，
# 规格构造则由该 manifest 的 ``spec_factory`` 字段指名 ``wdnai`` 上的方法
# （只有 worker 进程才需要真正 build spec）。这里从扫描结果生成兼容视图，
# 让 registry / config / compute / manifest 四处共 54 处既有调用点零改动。
#
# 旧的 ``preset_factory`` 字段连同 7 个 ``_*_spec()`` 构造函数已随之删除：
# 那个字段定义过 7 次却从未被任何代码读取（只有当时的 worker.py 自己的
# if/elif 在干同样的事，而那份已下沉为 manifest 的一个字符串字段）。
#
# ``estimated_init_seconds`` 是经验值，用来渲染「加载中」倒计时的**起点**
# （loading 段按 estimate - (now - started_at) 实算）。真机冷启动实测
# （page cache dropped 之后）：
#   * Qwen3-VL-4B: 57s → +3s buffer → 60s
#   * Qwen3-TTS:   加 ensure_loaded() 后变 eager init：load_resources ~4s +
#                  init_text_projection ~0.4s + init_speech_decoder ~2s +
#                  init_code_predictor ~1.6s + init_talker（785MB weight，
#                  8-core AR）在 dev box 上直接 MODEL_SETUP fail 没法实测，
#                  按 VL-4B 同档拍 60s（无真机实测，等你测完再调）
#   * Qwen3-ASR:   未实测（模型文件不在板上），按编码器+LLM 推算 ~15s →
#                  +10s buffer → 25s
#   * Qwen3-VL-Embedding-2B：~30s（加载 2 个 rknn + embed 表）
# 取「最冷」加载时间（用户首次加载，不是文件页缓存命中后的二次加载）+ 小
# buffer，保证倒计时结束时实际加载大概率已就绪，避免 UI 卡很久才更新。
MODEL_PRESETS: dict[str, dict] = _model_presets()


# --------------------------------------------------------------------------- #
# Legacy capability_type migration (kept for backward compat with old configs) #
# --------------------------------------------------------------------------- #
# Software capabilities are **not** listed here any more: their whole rename
# table lives in ``wdnai.LEGACY_ALIASES`` / ``wdnai.RETIRED_CAPABILITIES``,
# next to the enum that closes the vocabulary. What stays below are the
# *hardware* names, which have no SDK-side vocabulary to live in.
#
# The old table also carried two software entries that were dead on arrival —
# ``video_query`` → ``swcap_video_query`` and ``video_analyse`` →
# ``swcap_video_analyse`` — for capabilities **no worker ever declared**. They
# migrated silently into a string that could never resolve, so an old config
# became a permanent "功能未启用" with no trace of where it came from. Those
# now raise :class:`wdnai.RetiredCapabilityError` instead.
_LEGACY_CAPABILITY_TYPE_RENAMES: dict[str, str] = {
    "video_input":     "hwcap_video_input",
    "audio_input":     "hwcap_audio_input",
    "audio_output":    "hwcap_audio_output",
    "hdmi_display":    "hwcap_hdmi_display",
}


def _normalize_capability_groups(
    required_capabilities: list[str],
    required_groups: Optional[List[List[str]]] = None,
) -> list[list[str]]:
    """Normalise a card's capability requirements into groups.

    A **group** is a set of capabilities that one *single* model must provide
    together. Entries in ``swcap_to_model`` sharing a ``group`` name form one;
    an entry without one forms its own singleton group. That makes the common
    case ("one capability, one model") need no declaration at all.

    ``required_groups`` is accepted as explicit lists of capabilities — the
    shape the browser sends, derived from the wizard config it already fetched.
    Capabilities present in ``required_capabilities`` but absent from every
    group are appended as singletons, so a partial (or absent) group list
    degrades to the old per-capability behaviour instead of silently dropping
    a requirement.
    """
    caps = [c for c in (required_capabilities or []) if isinstance(c, str)]
    grouped: set[str] = set()
    out: list[list[str]] = []
    for group in (required_groups or []):
        if not isinstance(group, (list, tuple)):
            continue
        members = [c for c in group if isinstance(c, str)]
        if not members:
            continue
        out.append(members)
        grouped.update(members)
    out.extend([[c] for c in caps if c not in grouped])
    return out


# --------------------------------------------------------------------------- #
# Legacy preset-name migration (kept for backward compat with old configs)   #
# --------------------------------------------------------------------------- #
_LEGACY_PRESET_NAME_RENAMES: dict[str, str] = {
    # NOTE: the old ``qwen3-8b`` → ``qwen3-4b`` rename is **gone**. The
    # qwen3-4b worker was deleted (no weights on the target board, and
    # qwen3.5-vl-4b already declares ``swcap_text_chat``). Redirecting a
    # stale config to a different model than the one its author named
    # would be worse than rejecting it, so such a config now fails
    # validation with an explicit "unknown model" instead of silently
    # loading something else.
    "locate-anything": "LocateAnything-3b",
}


# --------------------------------------------------------------------------- #
# compute.autostart schema helpers                                            #
# --------------------------------------------------------------------------- #
# An entry has 2 fields:
#   model      : workers/<model_id>.json stem (a MODEL_PRESETS key)
#   npu_index  : physical card index, straight from ``list_cards()``
#
# Only models listed here are started at boot. Everything else is inert:
# no spawn, no bind, no NPU memory — the user enables it from the compute
# page when they want it. This replaces the old ``compute.services[]`` table,
# which had to enumerate every model to say "don't start this one", and
# mixed a label override and a device number into the same record.
#
# ``npu_index`` is passed to the worker as ``--npu``, so the model is
# already loading by the time the first ``info`` probe lands. The worker
# does that on a background thread *after* its replier is registered, so
# the probe gets a real answer instead of a boot-timeout.

_VALID_AUTOSTART_KEYS: frozenset[str] = frozenset({"model", "npu_index"})


def _parse_autostart_config(raw) -> list[dict]:
    """Validate the ``compute.autostart`` config payload.

    Returns ``[{"model": str, "npu_index": int}, ...]`` in file order.

    * non-list (None / dict / scalar) → ``[]`` (nothing autostarts)
    * list entry not a dict → **raise** ``ValueError``
    * ``model`` not in ``MODEL_PRESETS`` → **raise** ``ValueError``
    * ``npu_index`` not a non-negative int → **raise** ``ValueError``
    * duplicate ``(model, npu_index)`` → **raise** ``ValueError``. 同一模型
      跑多张卡是并行推理要的形态；同一张卡上重复才是真冲突（同卡叠两个模型
      会 rc=-6）。

    Rejecting loudly matters here: a typo in this file means a model the
    operator expects to be up at boot is silently not there.
    """
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    seen: set[tuple[str, int]] = set()
    for idx, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ValueError(
                f"compute.autostart[{idx}] 必须是 object "
                "{{model, npu_index}}；例如 "
                '{"model": "qwen3-vl-4b", "npu_index": 0}'
            )
        unknown = set(entry) - _VALID_AUTOSTART_KEYS
        if unknown:
            raise ValueError(
                f"compute.autostart[{idx}] 含未知字段 {sorted(unknown)}；"
                f"只接受 {sorted(_VALID_AUTOSTART_KEYS)}（label / device 等"
                "旧 services 字段已删除，label 见 workers/<model_id>.json）"
            )
        model = entry.get("model")
        if not isinstance(model, str) or not model:
            raise ValueError(
                f"compute.autostart[{idx}].model 缺失或非字符串")
        if model not in MODEL_PRESETS:
            raise ValueError(
                f"compute.autostart[{idx}].model={model!r} 不在 "
                f"MODEL_PRESETS {sorted(MODEL_PRESETS.keys())} 中"
            )
        npu_index = entry.get("npu_index")
        if not isinstance(npu_index, int) or isinstance(npu_index, bool):
            raise ValueError(
                f"compute.autostart[{idx}].npu_index 缺失或不是整数 "
                f"(got {npu_index!r})；可选 0..N 的实际卡号")
        if npu_index < 0:
            raise ValueError(
                f"compute.autostart[{idx}].npu_index={npu_index} 非法；"
                "必须是 0 起的实际卡号（主控 NPU 用 0）")
        key = (model, int(npu_index))
        if key in seen:
            raise ValueError(
                f"compute.autostart 含重复条目 model={model!r} "
                f"npu_index={npu_index}；同一张卡上不能挂两份同一个模型"
                "（同卡叠模型会加载失败），换卡请写不同的 npu_index")
        seen.add(key)
        out.append({"model": model, "npu_index": int(npu_index)})
    return out


def _autostart_npu_map() -> dict[str, int]:
    """config 的 autostart 列表 → ``{model_id: npu_index}``。

    徽章 / 算力页 radio 默认卡位的**真源**；实例上缓存的同名字段只是 boot
    快照，运行期改配置就过期。详见 AGENTS.md「开机自启徽章」。
    """
    try:
        section = get_config().get_section("compute", {}) or {}
    except Exception:
        return {}
    try:
        entries = _parse_autostart_config(
            section.get("autostart") if isinstance(section, dict) else None)
    except ValueError as e:
        LOG.error("compute.autostart 配置非法（默认卡信息按未配置处理）: %s", e)
        return {}
    return {e["model"]: e["npu_index"] for e in entries}


def _with_autostart_markers(d: dict, autostart_npus: dict[str, int]) -> dict:
    """把 config 派生的「开机自启」标记写进实例 dict。

    覆盖实例上的 boot 快照。peer（adopted）卡不显示徽章 —— 开机自启是
    **本板**的声明，对端板子的卡跟它无关。
    """
    cfg_npu = autostart_npus.get(d.get("model_id", ""), -1)
    d["_default_npu_index"] = cfg_npu
    d["_autostart"] = ("enabled" if cfg_npu >= 0 and
                       d.get("_source") != "adopted" else "")
    return d


def _wants_eager_load(eager_load: bool | None) -> bool:
    """要不要让 worker **自己在命令行上带** ``--npu`` 加载模型？

    ``None`` = 默认要（autostart 路径），``False`` = 不要（算力页走 bind）。

    **别拿卡号本身当布尔用** —— 卡号 0 是 falsy，早期就是这么写错的：
    0 号卡的 autostart 条目静默变成「不带 --npu」，worker 起来了却永远
    不加载模型。
    """
    return True if eager_load is None else bool(eager_load)


def _worker_npu_arg(npu_index: int | None,
                    eager_load: bool | None) -> list[str]:
    """worker 命令行上的 ``--npu`` 参数；不该由 worker 自己加载时是空列表。"""
    if npu_index is None or not _wants_eager_load(eager_load):
        return []
    return ["--npu", str(int(npu_index))]


def _instance_reusable(inst_npu_index: int, inst_source: str,
                       want_npu: int | None,
                       eager_load: bool | None) -> bool:
    """已有实例能不能被本次请求复用？

    * 不指定卡 / 非本机来源（peer）→ 都能复用。
    * 已绑在**同一张**卡 → 复用。
    * 未绑卡（-1）→ 只有不要求 eager-load 时才复用（算力页 spawn→bind）。
      autostart 要的是「worker 带着 --npu 自己加载」，复用空实例达不到，
      且那条路径不再补 bind（补了就是两条 load 抢同一个 session slot）。
    * 绑在**别的**卡 → 不复用（「同模型跑多张卡」要的正是这个）。
    """
    if want_npu is None:
        return True
    if inst_source not in ("spawned", "adopted_local"):
        return True
    if inst_npu_index == want_npu:
        return True
    if inst_npu_index == -1 and not _wants_eager_load(eager_load):
        return True
    return False


def _crop_autostart_for_cards(entries: list[dict],
                              model_cards: list[int]) -> tuple[list[dict], list[dict]]:
    """按模型卡位裁剪 autostart：非模型卡丢弃，**每张卡只留第一条**。

    返回 ``(保留的, 丢弃的)``，丢弃的由调用方记日志。保留哪条按 config
    的先后顺序（第一条优先）—— 那是用户写入时的意图顺序。
    """
    if not model_cards:
        return list(entries), []
    keep_cards = set(model_cards)
    kept: list[dict] = []
    dropped: list[dict] = []
    seen_cards: set[int] = set()
    for e in entries:
        card = e.get("npu_index")
        if card not in keep_cards or card in seen_cards:
            dropped.append(e)
            continue
        seen_cards.add(card)
        kept.append(e)
    return kept, dropped


def _merge_autostart_entry(prev: list[dict], model_id: str,
                            npu_index: int) -> tuple[list[dict], bool, list[dict]]:
    """把 ``(model_id, npu_index)`` 并入 autostart，保持「一卡一模型」。

    返回 ``(新列表, 是否新增, 被替换掉的同卡旧条目)``。同卡已有**别的**
    模型时那条旧记录必然陈旧（运行期同卡只可能有一个 running 实例），留着
    boot 时同卡就起两个 worker（rc=-6）。替换**原地**进行，保持 config
    的先后顺序。
    """
    def _is_stale(e: dict) -> bool:
        return e.get("npu_index") == npu_index and e.get("model") != model_id

    stale = [e for e in prev if _is_stale(e)]
    has_self = any(e.get("model") == model_id
                   and e.get("npu_index") == npu_index for e in prev)
    out: list[dict] = []
    placed = False
    for e in prev:
        if _is_stale(e):
            # 陈旧条目：第一个位置给本条目（保持 config 的先后顺序），
            # 其余直接丢弃 —— 同卡只留一条。
            if not placed:
                if not has_self:
                    out.append({"model": model_id, "npu_index": npu_index})
                placed = True
            continue
        out.append(dict(e))
    if not has_self and not placed:
        out.append({"model": model_id, "npu_index": npu_index})
    return out, not has_self, stale


def _autostart_plan(instances: list[ModelInstance], model_id: str,
                    npu_index: int) -> str:
    """boot autostart 的判定：**本板**这张卡上有没有在跑的 worker。

    ``"skip"`` = 本板这张卡已在跑；``"spawn_local"`` = 本板负责起一个。

    判据：本机实例（spawned / adopted_local）+ 状态 running / loading。
    「进程在但 not_loaded」不算跑。**peer 不参与判定** —— autostart 声明的
    是本板这张卡，借 peer 的算力会静默失效（对方一 unload 就没人跑）。
    """
    serving_here = [i for i in instances
                    if i.model_id == model_id
                    and i._source in ("spawned", "adopted_local")
                    and i.npu_index == npu_index
                    and i.status in ("running", "loading")]
    return "skip" if serving_here else "spawn_local"


def _autostart_needs_load(argv: list[str], worker_status: str | None) -> bool:
    """复用的 worker 要不要补一次 ``load`` RPC？

    带 ``--npu`` → 它自己加载，别插手（两条 load 抢同一个 session slot →
    worker rc=-6）；已在 loading/running → 别插手；问不到状态 → 不发。
    """
    if "--npu" in argv:
        return False
    if worker_status in ("running", "loading"):
        return False
    return worker_status is not None


def _sorted(it):
    return sorted(it) if not isinstance(it, (set, frozenset)) else sorted(it)


# --------------------------------------------------------------------------- #
# ModelInstance                                                             #
# --------------------------------------------------------------------------- #
def _coerce_image_sizes(raw) -> list:
    """Normalise a worker ``available_image_sizes`` payload into a
    sorted list of ints; drop anything that doesn't parse.

    Used both by ``_adopt_existing_worker`` (one-shot, from the
    probe's info dict) and by ``_probe_instance`` (every info tick)
    so a future worker upgrade that adds new resolutions surfaces
    them automatically without a registry restart.
    """
    if not isinstance(raw, list):
        return []
    out = []
    for s in raw:
        try:
            out.append(int(s))
        except (TypeError, ValueError):
            continue
    return sorted(set(out))


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
    key: Tuple[int, str, str]                    # (npu, model_id, source) — source tag 防 spawned/adopted dict key 互撞
    npu_index: int                             # -1 until bound
    model_id: str
    device_id: Optional[bytes] = None
    llm_session: Any = None
    capability_types: list[str] = field(default_factory=list)
    instance_name: str = ""
    label: str = ""
    memory_estimate_mb: int = 0
    # Config-driven overrides (from the compute.autostart[] entry).
    # ``_config_label`` overrides preset label when present (non-empty) —
    # kept for API clients; the autostart schema itself has no label field.
    # ``_autostart`` is "enabled" for models declared in compute.autostart,
    # "" for every other preset. ``_default_npu_index`` is the card index
    # the entry pins, used as the compute-page radio default and as the
    # boot-bind target (``--npu``). All three are empty/-1 when the model
    # isn't in compute.autostart (those presets fall through to legacy
    # behaviour: shown as unavailable, user enables manually).
    #
    # 这两个字段是 **boot 时的快照**；API 出参一律改用 config 现算的值
    # （_with_autostart_markers）—— 运行期改配置后快照会过期。
    _config_label: str = ""
    _autostart: str = ""
    _default_npu_index: int = -1
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
    # Vision-model native image sizes (e.g. ``[384]`` 或 ``[384, 768]``)
    # loaded into the worker's session — populated from worker's info
    # ``available_image_sizes`` field by ``_probe_instance`` after the
    # session reaches ``running``. Empty until then, and empty
    # permanently for non-vision backends (TTS / text-only). UI
    # dropdowns (e.g. 音视频描述页 ``#vd-detect-size``) query this
    # list — it is **never** hardcoded on the client side.
    available_image_sizes: list = field(default_factory=list)
    # RPC subprocess state
    proc: Optional[subprocess.Popen] = None
    rpc_client: Any = None
    rpc_session_proxy: Any = None
    _svc: Any = None                            # wdndds Service handle; needed for full cleanup in stop_instance_local
    _probe_thread: Optional[threading.Thread] = None
    # 串行化 probe 的 .call() 与 teardown 的 destroy；否则 cshim Requester
    # 在 C 层 UAF → SIGSEGV。AGENTS "destroy 拿 requester._lock"。
    _probe_lock: threading.RLock = field(default_factory=threading.RLock)
    # probe 的 in-flight ``r`` 失效计数器。unload/stop 自增；probe 调用
    # 前快照、调用后比对，变了就跳过 status 写回。修算力配置页 disable
    # 按钮 flicker（probe 陈旧 r 覆盖 unload 乐观写的 "not_loaded"）。
    _probe_gen: int = 0
    _logged_running: bool = False
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
            "finished_at":        self.finished_at,
            "load_time_sec":      self.load_time_sec,
            "loaded_at":          self.loaded_at,
            "pid":                pid,
            # Vision-model native sizes loaded into this instance.
            # Empty for non-vision backends; vision backends report at
            # least ``[384]`` (legacy single-resolution) or
            # ``[384, 768]`` (full multi-resolution install). Consumed
            # by ``WdnaiDetector`` for backend ``_pick_letterbox_size``
            # (auto-pick when ``image_size is None``) and strict
            # validation when the caller pins a specific size.
            "available_image_sizes": list(self.available_image_sizes),
            "_source":            self._source,
            "_peer_guid":         self._peer_guid,
            "_peer_name":         self._peer_name,
            "_peer_ip":           self._peer_ip,
            "_peer_pid":          self._peer_pid,
            # Config-driven overrides from the compute.autostart[] entry.
            # Empty when the model isn't declared there.
            "_autostart":         self._autostart,
            "_default_npu_index": self._default_npu_index,
            "_config_label":      self._config_label,
            # Enumeration order for the compute page's 本地算力 card list,
            # straight from the worker manifest. The UI sorts on this so
            # the card order lives in workers/<id>.json, not in JS.
            "display_order":      preset.get("display_order", 9999),
        }


@dataclass(frozen=True)
class SessionHandle:
    """One addressable inference endpoint behind a capability.

    The same model can be running on several cards at once (the compute
    page's 运行在 is multi-select), and a LAN peer can be serving the same
    model too. A consumer therefore has to be able to *name* the endpoint
    it wants — ``LLMRPCSession`` is bound to one worker's per-PID DDS
    service (``wdndds/rpc/<model>/worker-<pid>``), so there is no
    transparent round-robin: picking a card means picking a handle.

    ``npu_index`` is the card the worker is loaded on (-1 when unknown,
    e.g. a worker that came up without a bind). ``source`` mirrors
    ``ModelInstance._source``; ``is_remote`` is the one bit callers
    actually branch on.
    """

    model_id: str
    npu_index: int
    source: str
    pid: int
    is_remote: bool
    session: Any

    @property
    def worker_key(self) -> str:
        """Stable identity for logs / UI labels: ``model@card#pid``."""
        return f"{self.model_id}@{self.npu_index}#{self.pid}"

    def to_dict(self) -> dict:
        return {
            "model_id":   self.model_id,
            "npu_index":  self.npu_index,
            "source":     self.source,
            "pid":        self.pid,
            "is_remote":  self.is_remote,
            "worker_key": self.worker_key,
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
        # Cache for list_cards() — see that method for the full rationale.
        self._rknn_probe = None
        self._rknn_device_ids: Optional[list] = None
        self._hub: Any = None                   # wdndds Hub for client side
        self._hub_lock = threading.Lock()
        # Shared side-channel participant for all LLMRPCSessions — see
        # ``_side_hub``. Owned and closed here, never per instance.
        self._side_channel_hub: Any = None
        self._peers: dict[str, dict] = {}       # guid → {name, discovered_at}
        self._peers_lock = threading.Lock()
        # 防 peer listener 重入 adopt:同 model_id SPDP lease 重发时,
        # 第一次触发 adopt 在跑 (16s probe 上限),后续事件直接跳过。
        self._adopt_inflight: set[str] = set()
        self._mode: str = _resolve_compute_mode()
        self._migrate_legacy_capability_types()
        self._migrate_legacy_preset_names()
        if self._mode == "subprocess":
            self._ensure_hub()

    # peer listener 触发 adopt 后,失败按 2/4/8/16/32s exponential backoff
    # 重试(累计 62s 上限),覆盖 SIMPLE discovery / cshim endpoint 启动期
    # race window。v0.8.3 之前只跑一次就退出,FastDDS SPDP 不会重发
    # discovered=True,导致重启 race 失败的 model 永久 lost。
    _ADOPT_RETRY_DELAYS_SEC: tuple[float, ...] = (2.0, 4.0, 8.0, 16.0, 32.0)

    @property
    def compute_mode(self) -> str:
        return self._mode

# ------------------------------------------------------------------ #
    # Hub bring-up (SIMPLE discovery)                                    #
    # ------------------------------------------------------------------ #
    def _ensure_hub(self) -> None:
        """Bring up the wdndds Hub the main process uses to talk to
        compute_worker subprocesses. v0.6+ uses FastDDS SIMPLE
        discovery (multicast 239.255.0.1:7400); set
        ``WDNDDS_INITIAL_PEERS`` for unicast fallback when LAN
        multicast is blocked."""
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
            device_name = _read_device_name()
            from wdndds.dds import _cshim
            # 等等 NIC ready,避免 boot 时 SPDP 走错 interface → NETWORK 空。
            # wdndds 0.2/0.4 有 wait_for_network_ready,0.5+ 删了 → socket fallback。
            try:
                _cshim.wait_for_network_ready(timeout_sec=30.0)
            except AttributeError:
                import socket as _sock
                _deadline = time.monotonic() + 30.0
                while time.monotonic() < _deadline:
                    try:
                        _s = _sock.socket(_sock.AF_INET, _sock.SOCK_DGRAM)
                        _s.settimeout(0.5)
                        _s.connect(("8.8.8.8", 80))  # 不真发包,只触发 kernel 选源接口
                        _s.close()
                        break
                    except OSError:
                        time.sleep(0.5)
                else:
                    LOG.warning("no IPv4 NIC ready after 30s; Hub may end up on lo")
            self._hub = _RpcHub(
                participant_name=f"wdnwebui-main-{device_name}",
                domain_id=0,
                discovery_servers=[],
            )
            self._hub.start()
            def _on_peer(guid: str, name: str, ip: str, discovered: bool) -> None:
                with self._peers_lock:
                    if discovered:
                        self._peers[guid] = {
                            "name": name,
                            "ip": ip,
                            "discovered_at": time.time(),
                        }
                    else:
                        self._peers.pop(guid, None)
                # discovered: peer listener 触发异步 adopt,让对端提供的
                # service 进 ``self._instances``,网络算力栏 / capability
                # 路由才看得到。
                #
                # v0.8.2: 不再用 peer_name 解析 model_id —— FastDDS SIMPLE
                # 第一次 announce 时 participant_name 可能为空 (submessage
                # 后续才带 name;listener 不会再触发 discovered=True),
                # ``compute_worker-test-box-qwen3-tts`` 这种名字可能在
                # probe 这一刻还是空字符串。改用 RPC probe 反向验证:
                # 对每个 MODEL_PRESETS 探一次 ``wdndds/rpc/<model_id>``,
                # 能拿到 info 的就是该 peer 提供的服务。
                if discovered:
                    self._maybe_adopt_from_peer()
            self._hub.set_peer_listener(_on_peer)
            LOG.info(
                "ComputeRegistry Hub started (SIMPLE discovery): "
                "services=%s participant_name=wdnwebui-main-%s "
                "WDNDDS_INITIAL_PEERS=%s",
                service_names, device_name,
                os.environ.get("WDNDDS_INITIAL_PEERS", "<unset>"))
        except Exception as e:  # noqa: BLE001
            LOG.exception("ComputeRegistry Hub bring-up failed")
            self._cards_failed = str(e)
            self._hub = None

    def _maybe_adopt_from_peer(self) -> None:
        """peer listener 触发:对每个 model_id 没 adopted 时起 daemon adopt。

        防重入:_adopt_inflight 集合防止 SPDP lease 重发期间重复启动。
        只 skip 已 adopted(peer),不 skip adopted_local——本机 orphan 跟
        LAN peer 是两台机器,NETWORK 卡要各自显示。
        """
        for model_id in MODEL_PRESETS:
            with self._lock:
                if any(inst.model_id == model_id
                       and inst._source == "adopted"
                       for inst in self._instances.values()):
                    continue
                if model_id in self._adopt_inflight:
                    continue
                self._adopt_inflight.add(model_id)
            threading.Thread(
                target=self._safe_adopt_for_peer,
                args=(model_id,),
                name=f"compute-adopt-{model_id}",
                daemon=True,
            ).start()

    def _safe_adopt_for_peer(self, model_id: str) -> None:
        """daemon 线程:跑 adopt,失败按 exponential backoff 重试(累计 62s)。

        peer listener 只在 participant 加入/消失时触发,lease 重发不重触;
        重启期 race window 一次 probe 5s timeout 失败 → 不重试就永久 lost。
        """
        attempts = len(self._ADOPT_RETRY_DELAYS_SEC) + 1   # 第 1 次 + N 次 retry
        try:
            for attempt in range(attempts):
                if attempt > 0:
                    delay = self._ADOPT_RETRY_DELAYS_SEC[attempt - 1]
                    LOG.info(
                        "auto-adopt peer %s: retry %d/%d in %.1fs "
                        "(waiting for SIMPLE discovery / cshim endpoint)",
                        model_id, attempt, len(self._ADOPT_RETRY_DELAYS_SEC),
                        delay)
                    time.sleep(delay)
                inst = self._adopt_existing_worker(model_id)
                if inst is not None:
                    return
            LOG.warning(
                "auto-adopt peer %s: giving up after %d attempts "
                "(total waited %.0fs); peer may not be providing this "
                "service, or endpoint unreachable",
                model_id, attempts,
                sum(self._ADOPT_RETRY_DELAYS_SEC))
        except Exception as e:  # noqa: BLE001
            LOG.warning("auto-adopt peer %s raised: %s", model_id, e)
        finally:
            with self._lock:
                self._adopt_inflight.discard(model_id)

    def list_peers(self) -> list[dict]:
        """Return the currently visible FastDDS SIMPLE peers as
        ``[{guid, name, discovered_at}, ...]``. ``name`` may be empty
        if the peer hasn't sent its participant name yet (FastDDS
        announces name in a later submessage). Empty list when the
        Hub isn't up yet.
        """
        with self._peers_lock:
            return [
                {"guid": g, **info}
                for g, info in self._peers.items()
            ]

    def own_guid(self) -> str:
        """Return this participant's own GUID, or ``""`` when the
        Hub isn't up. Useful for the system-monitor UI to distinguish
        ``self`` from peers.
        """
        hub = self._hub
        if hub is None:
            return ""
        try:
            return hub.guid()
        except Exception:
            return ""

    def _hub_or_raise(self):
        if self._hub is None:
            raise RuntimeError(
                "wdndds Hub not available (fastdds init failed). "
                f"Reason: {self._cards_failed}"
            )
        return self._hub

    def _side_hub(self):
        """One shared side-channel participant for every session.

        ``LLMRPCSession`` carries stream deltas and stop flags on its own
        DomainParticipant so they are never queued behind the replier's
        reply path. Letting each session build its own breaks as soon as
        there are two: participant names must be unique within a domain,
        and a duplicate name corrupts discovery **silently** — the second
        worker's RPCs stop routing while the process still answers, so the
        symptom is "this model never finishes loading" rather than anything
        that points at DDS.

        One participant serves every instance because the topics are
        namespaced per service (``<service_name>/delta``), so they cannot
        collide. Teardown belongs to the registry, not to any one session.
        """
        with self._lock:
            if self._side_channel_hub is None:
                from wdndds.dds._rpc import Hub as _RpcHub

                hub = _RpcHub(
                    participant_name=f"wdnwebui-side-{os.getpid()}",
                    domain_id=0,
                    discovery_servers=[],
                )
                hub.start()
                self._side_channel_hub = hub
            return self._side_channel_hub

    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    # Legacy migration                                                  #
    # ------------------------------------------------------------------ #
    def _migrate_legacy_capability_types(self) -> None:
        """Rewrite historical capability names stored in ``compute.instances``.

        Two vocabularies live side by side here. Hardware names go through
        :data:`_LEGACY_CAPABILITY_TYPE_RENAMES`; software names go through
        ``wdnai.migrate_legacy``, which knows both the bare legacy names
        (``video_describe``) and the previous ``swcap_*`` spelling
        (``swcap_vision_chat``).

        **This runs at boot, so it never raises.** A config naming a retired
        software capability (``video_query`` / ``video_analyse``) used to be
        migrated into a string no worker declares, leaving a permanent
        "功能未启用" with no way to trace it. Now the entry is dropped with a
        loud warning instead — the instance still starts, the stale claim does
        not survive, and the log says exactly which entry went away.
        """
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
            new_caps: list[str] = []
            for cap in caps:
                if not isinstance(cap, str):
                    continue
                # Hardware first: only the HW rename table knows these.
                hw = _LEGACY_CAPABILITY_TYPE_RENAMES.get(cap)
                if hw is not None:
                    new_caps.append(hw)
                    renamed_total += 1
                    continue
                try:
                    migrated, renamed = migrate_legacy(cap)
                except RetiredCapabilityError as exc:
                    LOG.warning(
                        "实例 %s 声明的能力已下线，本次丢弃该能力声明: %s",
                        inst.get("model") or inst.get("name") or "?",
                        exc,
                    )
                    continue
                except ValueError:
                    # Unknown string (hand-edited config). Keep it verbatim:
                    # dropping it would silently change what the user asked
                    # for, and the registry validates on use anyway.
                    new_caps.append(cap)
                    continue
                new_caps.append(migrated.value)
                if renamed:
                    renamed_total += 1
            if new_caps != caps:
                inst["capability_types"] = new_caps
        if renamed_total:
            LOG.info("已迁移 %d 处历史软件能力名", renamed_total)
            cfg["instances"] = instances
            try:
                get_config().set_section("compute", cfg)
            except Exception as e:  # noqa: BLE001
                LOG.warning("failed to persist migrated capability_types: %s", e)

    def _migrate_legacy_preset_names(self) -> None:
        """Rewrite renamed model ids inside ``compute.autostart``.

        Runs once at boot so an old config keeps working instead of
        silently autostarting nothing (the parser raises on an unknown
        model, which would take the whole list down).
        """
        cfg = get_config().get_section("compute", {})
        autostart = cfg.get("autostart") if isinstance(cfg, dict) else None
        if not isinstance(autostart, list):
            return
        renamed_total = 0
        for idx, entry in enumerate(autostart):
            if not isinstance(entry, dict):
                continue
            name = entry.get("model")
            new_name = _LEGACY_PRESET_NAME_RENAMES.get(name)
            if new_name and new_name in MODEL_PRESETS:
                entry["model"] = new_name
                renamed_total += 1
                LOG.info("compute.autostart[%d].model=%r 已自动迁移到 %r "
                         "（如有疑问参见 git log / AGENTS.md）",
                         idx, name, new_name)
        if renamed_total:
            cfg["autostart"] = autostart
            try:
                get_config().set_section("compute", cfg)
            except Exception as e:  # noqa: BLE001
                LOG.warning("failed to persist migrated preset names: %s", e)

    # ------------------------------------------------------------------ #
    # 元数据查询                                                          #
    # ------------------------------------------------------------------ #
    def list_cards(self, force_refresh: bool = False) -> list[dict]:
        """NPU enumeration (wdnai path still needed for non-RK metadata).

        Caches the ``RKNN3Lite`` probe handle and the raw
        ``get_devices_id()`` result so polling endpoints (``/api/compute``)
        don't re-open ``/dev/rknn3`` on every request. The UI typically
        polls this 1×/s while the worker is running inference; without
        the cache, every poll logs ``rknn3-toolkit-lite version: 1.0.4``
        and contends with the worker on the RKNN driver — which has
        been observed to wedge ``embed_video`` mid-extract on RK3588.

        First call (no cache) runs ``RKNN3Lite()`` + ``get_devices_id()``
        in a worker thread bounded by ``_RKNN_PROBE_TIMEOUT`` so a
        contended ``/dev/rknn3`` can't hang the entire snapshot call
        (which would block the 1Hz UI poll forever). On timeout we
        return ``[]`` + set ``cards_failed`` so the UI can surface the
        driver busy state without taking the API offline.
        """
        with self._lock:
            if self._cards and not force_refresh:
                return list(self._cards)

        if self._rknn_probe is None or force_refresh:
            import threading
            holder: dict = {}

            def _probe():
                try:
                    # wdnai ≥ 0.4: ``RKNN3Lite`` is the vendored ctypes
                    # shim (drives /usr/lib/librknn3_api.so directly);
                    # the rknn3lite / rknn3-toolkit-lite wheel is no
                    # longer installed.
                    from wdnai._librknn import RKNN3Lite
                    probe = RKNN3Lite()
                    holder["probe"] = probe
                    holder["device_ids"] = probe.get_devices_id()
                except BaseException as e:  # noqa: BLE001
                    holder["error"] = e

            t = threading.Thread(target=_probe, daemon=True)
            t.start()
            t.join(timeout=_RKNN_PROBE_TIMEOUT)
            if t.is_alive():
                with self._lock:
                    self._cards_failed = (
                        f"RKNN probe timeout after {_RKNN_PROBE_TIMEOUT:.1f}s "
                        f"(/dev/rknn3 contended with worker)"
                    )
                LOG.warning("list_cards: %s", self._cards_failed)
                return []
            if "error" in holder:
                with self._lock:
                    self._cards = []
                    self._cards_failed = str(holder["error"])
                    self._rknn_probe = None
                    self._rknn_device_ids = None
                return []
            self._rknn_probe = holder["probe"]
            self._rknn_device_ids = holder["device_ids"]

        cards = []
        for i, x in enumerate(self._rknn_device_ids):
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

    def model_cards(self) -> List[int]:
        """能承载 RKNN 模型的协卡索引（每张卡一个模型）。

        权威来源 —— ``list_cards()`` 会把 USB 序列号形态的 RGA/VPU 设备
        也算进去（见 :data:`_PCI_BDF_RE`）。受 ``compute.card_count``
        上限约束（双卡板可用它模拟单卡板）。
        """
        detected = [
            c["npu_index"] for c in self.list_cards()
            if _PCI_BDF_RE.match(c.get("device_id") or "")
        ]
        cap = card_count_override()
        if cap is not None:
            detected = detected[:cap]
        return detected

    def list_supported_models(self) -> list[dict]:
        base = get_config().get_section("base", {})
        # Priority: config → env var → on-disk model root chosen by
        # ``default_models_root`` (/userdata/models → /mnt/oem/models →
        # <HOME>/models). Keeps this fallback chain in lock-step with
        # ``ConfigStore._seed_defaults`` so the file-existence probe
        # below agrees with what a fresh install would write to
        # config.jsonc.
        model_path = (
            base.get("model_path")
            or os.environ.get("WDNAI_MODELS_DIR")
            or f"{default_models_root()}/"
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
        autostart_npus = _autostart_npu_map()
        with self._lock:
            instances = [
                _with_autostart_markers(inst.to_dict(), autostart_npus)
                for inst in self._instances.values()
            ]

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
        # v0.15: per-source 拆分,顶部汇总"本机 X / 远端 Y"用。本机 =
        # spawned ∪ adopted_local;远端 = adopted。unavailable 不进任何桶。
        def _is_local(s):
            return s in ("spawned", "adopted_local")
        local_running_count  = sum(1 for i in instances
                                   if i["status"] == "running" and _is_local(i.get("_source")))
        remote_running_count  = sum(1 for i in instances
                                   if i["status"] == "running" and i.get("_source") == "adopted")
        local_loading_count  = sum(1 for i in instances
                                   if i["status"] == "loading" and _is_local(i.get("_source")))
        remote_loading_count = sum(1 for i in instances
                                   if i["status"] == "loading" and i.get("_source") == "adopted")
        local_error_count    = sum(1 for i in instances
                                   if i["status"] == "error" and _is_local(i.get("_source")))
        remote_error_count   = sum(1 for i in instances
                                   if i["status"] == "error" and i.get("_source") == "adopted")
        capabilities = self.list_active_capabilities()
        services = self.list_with_source()
        # v0.15: 不再产出 queued 计数 —— not_loaded 是默认态,顶部再报是
        # 重复 + "排队"在中文里易误读。

        from app.core.tools.registry import get_tool_registry
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
        model_cards = self.model_cards()
        return {
            "cards":             cards,
            "cards_failed":      self.cards_failed_reason(),
            # 能跑 rknn 模型的卡。``cards`` 里还混着 USB 序列号形态的
            # RGA/VPU 设备 —— 那些卡选不得（见 _PCI_BDF_RE）。
            # ``card_count`` == 1 → 单卡板；== 2 → 双卡板。
            "model_cards":       model_cards,
            "card_count":        len(model_cards),
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
            "local_running_count":   local_running_count,
            "remote_running_count":  remote_running_count,
            "local_loading_count":   local_loading_count,
            "remote_loading_count":  remote_loading_count,
            "local_error_count":     local_error_count,
            "remote_error_count":    remote_error_count,
            "estimated_init_seconds_per_model": estimated_per_model,
            "default_estimated_init_seconds":  DEFAULT_ESTIMATED_INIT_SECONDS,
            # Model ids in the order the manifests declare (ascending
            # ``display_order``, then id) — the compute page's 本地算力
            # list order. Computed from MODEL_PRESETS rather than from
            # ``services``: list_with_source() sorts by (_source,
            # model_id) so a spawned model would sort ahead of an
            # unavailable one regardless of display_order.
            "preset_order":       sorted(
                MODEL_PRESETS,
                key=lambda mid: (MODEL_PRESETS[mid].get("display_order", 9999),
                                 mid),
            ),
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
            # Write to base.hardware_capabilities (not compute — moved).
            with self._lock:
                cfg = get_config().get_section("base", {})
                if not isinstance(cfg, dict):
                    cfg = {}
                cfg["hardware_capabilities"] = new_state
                try:
                    get_config().set_section("base", cfg)
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
    # 禁用 = unload + shutdown + SIGTERM + SIGKILL（进程真的关掉，
    # 重启用走冷启 spawn + load）；UI "禁用"按钮走 stop_instance_local，
    # 与"重启"共用同一条关闭路径。unload_instance 单独使用的地方只剩
    # stop-all 端点，那里进程保留。

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
            # key 带上卡号（跟 spawn 时一致）：同模型两张卡都失败时不能撞同一个占位 key。
            err_npu = inst.npu_index if isinstance(inst.npu_index, int) and inst.npu_index >= 0 else -1
            inst.key = (err_npu, inst.model_id, "spawned")
            if old_key != inst.key:
                self._instances.pop(old_key, None)
            self._instances[inst.key] = inst
        LOG.info("registered errored instance: %s status=error err=%r",
                 inst.model_id, inst.error[:200])

    def _spawn_compute_worker(self, model_id: str,
                            allow_adopt: bool = True,
                            npu_index: int | None = None,
                            eager_load: bool | None = None) -> ModelInstance:
        """Spawn a fresh ``compute_worker.py --model <id>`` subprocess.

        ``npu_index`` has **two independent jobs** and they must not be
        conflated:

        * *matching* — an existing instance already bound to this same
          card is reused instead of spawning a second one. Needed once a
          model may live on several cards at once.
        * *``--npu`` on the cmdline* — controlled by ``eager_load``, NOT
          by ``npu_index`` alone. The worker then builds its
          ``LLMSession`` itself, on an eager-load thread that starts
          *after* its replier is registered (so the first ``info`` probe
          is answered rather than timing out). That is the autostart
          path.

        The compute page's 启用 deliberately passes ``eager_load=False``:
        it spawns **without** ``--npu`` and then sends
        ``{"action":"load","npu_index":N}`` via :meth:`bind_instance`.
        Passing ``--npu`` there as well makes the worker eager-load
        *while* the RPC load arrives — two loads racing on the same
        session slot, which aborts the worker with ``rc=-6``. (Seen on
        real hardware: every enable turned into ``rc=-6``.) Hence the
        split.

        ``eager_load`` defaults to ``npu_index is not None``, which keeps
        the historical autostart behaviour for callers that don't say
        otherwise.

        v0.8 dedup: before spawning, scan ``/proc`` for any pre-existing
        ``compute_worker.py --model <id>`` process. If found, refuse to
        spawn a duplicate and instead adopt the existing one (handles
        the uvicorn-reload orphan case where a previous main process
        spawned a worker that survived SIGTERM).

        ``allow_adopt`` propagates from ``ensure_worker_for_model`` —
        the LOCAL "启用" API path passes False to skip both the
        ``/proc`` orphan-adopt and the inner ``_instances`` dedup so a
        fresh local worker is always spawned even when a peer adopted
        entry already exists.

        Returns the new ``ModelInstance`` keyed ``(-1, model_id)``, or
        the adopted one if dedup kicks in.
        """
        if model_id not in MODEL_PRESETS:
            raise ValueError(f"未知 model: {model_id!r}")
        preset = MODEL_PRESETS[model_id]
        # v0.8.22 dedup pass 1: LOCAL 启用 (allow_adopt=False) → 主动 drop
        # 任何现有 adopted/adopted_local,避免 spawn 新本地 worker 后留下
        # NETWORK adopted 卡,造成 LOCAL + NETWORK 重复(板B 03:36 那次的 bug)。
        # Boot 路径 (allow_adopt=True) 不动 — 让下面的 orphan / dedup 自己处理。
        if not allow_adopt:
            with self._lock:
                drop_keys = [k for k, inst in self._instances.items()
                             if inst.model_id == model_id
                             and inst._source in ("adopted", "adopted_local")]
                for k in drop_keys:
                    old = self._instances.pop(k)
                    LOG.info(
                        "spawn (allow_adopt=False): dropping existing %s "
                        "instance for %s (peer_pid=%s) to make room for local",
                        old._source, model_id, getattr(old, "_peer_pid", None))
        # 1. 进程级 dedup:扫 /proc 同 comm 名的 worker
        orphan_pids = _find_workers_by_name(model_id)
        # **排除本 registry 已经跟踪的 spawned worker。** 这条是「同一个
        # 模型跑多张卡」的前提：给卡 1 启第二个实例时，`/proc` 里必然还
        # 躺着刚给卡 0 spawn 的那个 worker，needle 只认 `--model <id>`，
        # 分不清它是我们自己的。若不排除，`orphan_pids` 会命中它 →
        # 注册成 adopted_local → 把卡 0 的那个实例从 dict 里踢掉 →
        # 两个 registry 条目指向同一个进程，然后 bind 会让**同一个**
        # worker 去 load 第二份模型（同卡再叠模型会 rc=-6，跨卡则状态
        # 全乱）。实测症状：POST [0,1] 返回两个 pid 相同的实例。
        tracked_pids = set()
        with self._lock:
            for i in self._instances.values():
                if (i.model_id == model_id and i._source == "spawned"
                        and i.proc is not None and i.proc.poll() is None):
                    tracked_pids.add(i.proc.pid)
        reusable = [p for p in orphan_pids if p not in tracked_pids]
        if orphan_pids and not reusable:
            LOG.info(
                "spawn: %d existing %s worker(s) (pids=%s) are all ours "
                "(already-tracked spawned) — proceeding to spawn a NEW "
                "worker for the requested card", len(orphan_pids), model_id,
                orphan_pids)
        orphan_pids = reusable
        if allow_adopt and orphan_pids:
            LOG.warning(
                "spawn skipped: %d existing %s worker(s) found (pids=%s); "
                "adopting one and cleaning the rest",
                len(orphan_pids), model_id, orphan_pids)
            adopted = self._adopt_existing_worker(model_id)
            if adopted is not None:
                return adopted
            err_inst = ModelInstance(
                key=(-1, model_id, "spawned"),
                npu_index=-1,
                model_id=model_id,
                capability_types=list(preset["capability_types"]),
                instance_name=preset["instance_name"],
                label=preset["label"],
                memory_estimate_mb=preset["memory_estimate_mb"],
                status="error",
                started_at=time.time(),
                _source="spawned",
            )
            err_inst.error = (
                f"发现 {len(orphan_pids)} 个现存的 {model_id} worker "
                f"(pid={orphan_pids}) 但 dds rpc 全部探测失败 —— "
                f"无法接管。请刷新页面或重启服务（前者会 SIGTERM 孤儿 + spawn，"
                f"后者手动 kill {orphan_pids[0]} 后再启用）"
            )
            self._register_errored_instance(err_inst)
            return err_inst
        if not allow_adopt and orphan_pids:
            # v0.8.22: LOCAL 启用 + /proc orphan → 直接注册 adopted_local,
            # 不走 _adopt_existing_worker 的 DDS probe(避免 grab 错 peer
            # worker 把 NETWORK 卡弄回来;orphan 是我们自己的,直接用 /proc)。
            orphan_pid = orphan_pids[0]
            LOG.info(
                "spawn (allow_adopt=False): reusing local /proc orphan "
                "pid=%d for %s as adopted_local", orphan_pid, model_id)
            device_name = (get_config().get_section("base", {}).get(
                "device_name", "") or socket.gethostname())
            inst = ModelInstance(
                key=(-1, model_id, "adopted_local"),
                npu_index=-1,
                model_id=model_id,
                capability_types=list(preset["capability_types"]),
                instance_name=preset["instance_name"],
                label=preset["label"],
                memory_estimate_mb=preset["memory_estimate_mb"],
                status="not_loaded",
                started_at=time.time(),
                _source="adopted_local",
            )
            inst._peer_pid = orphan_pid
            inst._peer_name = f"{device_name}（本机 pid={orphan_pid}）"
            with self._lock:
                # v0.8.27: 已有 spawned 指向同一 pid 时 drop 它。
                # 同一 worker 进程被两个 key 注册会出双 LOCAL 卡
                # (spawned + adopted_local 指向同一 pid),冗余且误导。
                # adopted_local 是更准的描述(/proc 真实存在)。
                for _k in list(self._instances.keys()):
                    _ex = self._instances[_k]
                    if (_ex.model_id == model_id
                            and _ex._source == "spawned"
                            and _ex.proc is not None
                            and _ex.proc.pid == orphan_pid):
                        LOG.info(
                            "local-orphan path: dropping duplicate spawned "
                            "entry for %s pid=%d", model_id, orphan_pid)
                        del self._instances[_k]
                        break
                self._instances[inst.key] = inst
            try:
                self._setup_rpc_client(inst)
            except Exception:  # noqa: BLE001
                LOG.warning("local-orphan RpcClient setup failed: %s",
                            model_id, exc_info=True)
            probe = threading.Thread(
                target=self._probe_instance,
                args=(inst,),
                name=f"compute-probe-local-orphan-{model_id}",
                daemon=True,
            )
            inst._probe_thread = probe
            probe.start()
            return inst
        # 2. 让 ensure_worker_for_model 阶段不重复进入。Same card-aware rule as
        # upstream: an instance on a *different* card must not short-circuit
        # the spawn, otherwise 「同一模型跑两张卡」 永远只有第一张卡。
        with self._lock:
            for inst in self._instances.values():
                if inst.model_id != model_id:
                    continue
                if not allow_adopt and inst._source == "adopted":
                    continue
                # Same card-aware rule as the outer ensure_worker_for_model
                # (see _instance_reusable).
                if not _instance_reusable(
                        inst.npu_index, inst._source, npu_index, eager_load):
                    continue
                return inst
        preset = MODEL_PRESETS[model_id]
        # **卡号已知就把 key 定成带卡号的。** 两次 spawn 若都用占位 key，第二
        # 次会把第一次的实例从 _instances 里挤掉（dict 覆盖）→ 那张卡的 worker
        # 变孤儿 → 随后被 adopt 当「多余孤儿」杀掉。boot 路径不走
        # bind_instance，光靠 bind 时改 key 救不了它。
        spawn_npu = (npu_index
                     if isinstance(npu_index, int) and npu_index >= 0
                     else -1)
        inst = ModelInstance(
            key=(spawn_npu, model_id, "spawned"),
            npu_index=spawn_npu,
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
        # v0.4: worker is now at app/core/compute/compute_worker.py (was
        # app/services/compute_worker.py). One extra parent level up.
        env.setdefault("PYTHONPATH", str(Path(__file__).resolve().parents[3]))
        env.setdefault(
            "WDNAI_MODELS_DIR",
            get_config().get_section("base", {}).get(
                "model_path", f"{default_models_root()}/"))

        manifest = load_worker_manifest(model_id)
        entry = (manifest.entry_path if manifest is not None
                 else str(Path(__file__).resolve().parent / "worker.py"))
        cmd = [
            sys.executable, "-u",
            # The single shared entry script under ``workers/``. Every model
            # runs the same file; the manifest decides what gets built.
            # The basename is deliberately unchanged: both the spawn dedup
            # needle and ``system_monitor``'s process scan match this exact
            # string in /proc/<pid>/cmdline, so renaming it would silently
            # break both. The fallback above only fires if a model has no
            # manifest at all (it would then be unspawnable anyway).
            entry,
            "--model", model_id,
            # v0.8.15: 主进程 spawn 本机 worker 时传 --local-only,让 worker
            # 只注册 per-PID service,不注册 standard。避免本机 spawned worker
            # 与 LAN peer 共用 standard topic 让 adopted (NETWORK) RpcClient
            # 路由到本机,造成 NETWORK 卡 status 翻转。
            "--local-only",
        ]
        # autostart path: the card is known now, so hand it over and let
        # the worker's own eager-load thread build the model. The replier
        # is registered before that thread starts, so probe and ``info``
        # keep answering while the weights load. The compute page passes
        # eager_load=False and loads over RPC instead — see the docstring.
        cmd += _worker_npu_arg(npu_index, eager_load)
        try:
            # v0.8.6 改:worker stdout/stderr 写独立 log file,而不是 PIPE。
            # 原因:PIPE 在 parent(uwsgi worker / uvicorn)死掉时 read 端
            # 关闭,worker 写 stderr 收到 SIGPIPE → 默认行为是 terminate
            # → 孤儿 worker 全部死亡,网络算力栏空。
            # 每个 worker 一个 log file 在 /tmp/wdnwebui-worker/ 下,
            # logrotation 由用户管理(目前不需要,文件小)。
            log_dir = Path("/tmp/wdnwebui-worker")
            try:
                log_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                log_dir = None
            if log_dir is not None:
                log_path = log_dir / f"{model_id}.log"
                log_fp = open(log_path, "ab", buffering=0)
            else:
                log_fp = open(os.devnull, "ab")
            proc = subprocess.Popen(
                cmd, env=env,
                stdout=log_fp,
                stderr=log_fp,
                start_new_session=True,
                # 注意：进程 comm 是在 worker 端 main() 里
                # _set_comm_name(...) 调的，不是在 preexec_fn 里
                # —— preexec_fn 设的 PR_SET_NAME 会被紧随的 exec() 覆盖
                # （新 argv[0] basename 变成 comm）。所以这里不传
                # preexec_fn，命名逻辑下沉到 compute_worker.py。
            )
            inst.proc = proc
            inst._log_path = str(log_path)
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

    def _find_inst_by_worker_pid(self, model_id: str, worker_pid) -> Optional[ModelInstance]:
        """v0.8.28: 找 _instances 里 model_id + worker_pid 匹配的已有 entry。
        
        用于 _adopt_existing_worker / inline-peer retry / _discover_additional_peers
        在 insert 之前 dedup 同 worker。防止同 worker 因 npu_index
        在不同时间点变化(key=(npu, model_id, source) 不同)被 register 多次,
        进而导致双 LOCAL 卡 + NETWORK stale status(黄灯)。
        """
        if not isinstance(worker_pid, int):
            return None
        for inst in self._instances.values():
            if inst.model_id != model_id:
                continue
            if inst.proc is not None and inst.proc.pid == worker_pid:
                return inst
            if getattr(inst, "_peer_pid", None) == worker_pid:
                return inst
        return None

    def _upsert_adopted_inst(self, existing, info: dict, model_id: str,
                              peer_name: str, worker_ip: str, worker_npu) -> None:
        """v0.8.28: in-place update existing inst + drop 旧 key + re-insert。
        
        key 包含 npu_index — npu 从 -1 → 0 时 key 必须更新。直接
        删所有指向此 inst 的旧 key 再 re-insert 是最干净的写法,
        避免 dict 留死残留 + stale status。
        """
        with self._lock:
            for k in [k for k, v in list(self._instances.items()) if v is existing]:
                del self._instances[k]
            if isinstance(worker_npu, int):
                existing.npu_index = worker_npu
            existing.status = info.get("status", existing.status)
            existing.loaded_at = info.get("loaded_at")
            existing.load_time_sec = info.get("load_time_sec")
            existing.finished_at = info.get("loaded_at")
            existing._peer_name = peer_name
            existing._peer_ip = worker_ip
            existing.available_image_sizes = _coerce_image_sizes(
                info.get("available_image_sizes"))
            if isinstance(existing.npu_index, int) and existing.npu_index >= 0:
                existing.key = (existing.npu_index, model_id, existing._source)
            else:
                existing.key = (-1, model_id, existing._source)
            self._instances[existing.key] = existing
        LOG.info("v0.8.28: in-place updated %s for %s (pid=%d npu=%s status=%s)",
                 existing._source, model_id, getattr(existing, "_peer_pid", 0),
                 existing.npu_index, existing.status)

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
        # DDS 选 endpoint 是 best-effort 的,可能反复命中坏 peer。多探
        # 几次:每次 create_requester 拿到新 client,cshim 可能重选 endpoint。
        # 全 error 则拒绝采纳(避免脏数据进 network 栏)。
        info = None
        for probe_try in range(3):
            info = self._probe_service(model_id)
            if info is None:
                break
            if info.get("status") != "error":
                break
            time.sleep(2.0)
        if info is None:
            return None
        if info.get("status") == "error":
            LOG.warning("adopt: %s: probes returned error, skipping", model_id)
            return None
        preset = MODEL_PRESETS[model_id]
        worker_npu = info.get("npu_index")
        worker_pid = info.get("pid")
        worker_host = (info.get("hostname") or "").strip()
        worker_ip = (info.get("peer_ip") or "").strip()
        worker_device = (info.get("device_name") or "").strip()
        local_host = socket.gethostname()
        local_ip = _get_local_ip()
        # 优先 IP 比对 —— 克隆镜像板 hostname 撞车;peer_ip 缺失时退到 hostname。
        if worker_ip and local_ip:
            is_local = worker_ip == local_ip
        else:
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
            key=(-1, model_id, source),
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
            # Adopted workers also carry ``available_image_sizes`` if
            # they report it (their info RPC goes through the same
            # ``info_handler`` we just extended). Empty list when the
            # info didn't include it (older peer worker that hasn't
            # been updated yet) or when the model isn't vision-bearing.
            available_image_sizes=_coerce_image_sizes(
                info.get("available_image_sizes")),
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
        # v0.8.28: 同 worker pid dedup → in-place update 旧 entry(避免
        # 同一 worker 因 npu_index 变化在不同时间点被 register 多次)。
        existing_pid = self._find_inst_by_worker_pid(model_id, worker_pid)
        if existing_pid is not None:
            self._upsert_adopted_inst(existing_pid, info, model_id, peer_name,
                                      worker_ip, worker_npu)
            return existing_pid
        with self._lock:
            # v0.8.5 改:只 dedup 同 source。spawned / adopted / adopted_local
            # 三种 source 可共存 —— 本机 spawned 不应阻止 LAN adopted。
            existing = next((v for v in self._instances.values()
                             if v.model_id == model_id
                             and v._source == source), None)
            if existing is not None:
                return existing
            # v0.8.12:本机已有 spawned entry 时跳过 adopt。用户在算力配置页
            # 点 LOCAL 启用的结果是 spawned(真相);boot probe / peer listener
            # 看到本地 worker 也是 DDS participant 会探到它,这时不应该再创建
            # adopted / adopted_local 跟 spawned 撞同一个 dict key (npu,
            # model_id) 把用户 spawn 的 entry 覆盖掉。
            if source in ("adopted", "adopted_local"):
                # v0.8.23: 不再因本地 spawned 跳过 adopt。设计意图是
                # LOCAL = 本机 worker(spawned 或 adopted_local),
                # NETWORK = peer worker(adopted,不论本机是否有同 model)。
                # 两张卡可同 model 共存,各指向不同板子的实例。
                # 之前 skip 让板B 在 04:27:05 看到 board A 的 qwen2.5-omni-3b
                # probe 成功但被拦下,NETWORK 永远不显示 peer 的同 model 卡。
                pass
            if inst.npu_index >= 0:
                inst.key = (inst.npu_index, model_id, source)
            else:
                inst.key = (-1, model_id, source)
            with self._lock:
                # v0.8.22: 必须在 lock 下插,避免 peer_listener 线程插入
                # adopted 卡与 _spawn_compute_worker 顶部 dedup 之间的
                # 可见性 race(板B 03:36 那次 spawn 看到空 _instances 然后
                # 又被这条 insert 写入,导致 LOCAL + NETWORK 双卡)。
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

        # v0.8.25: 如果首个 probe 拿到本机(/proc orphan 之类),cshim
        # Requester routing 非确定 — boot 之前看到的可能一直是本机,
        # 漏了 LAN peer。inline 再 retry 一次,跳过本机响应,找非本机
        # peer 加成单独的 NETWORK 卡。设计上 LOCAL + NETWORK 可同 model
        # 共存:一张指本机、一张指 LAN peer,各跑各的。
        if source == "adopted_local":
            for peer_retry in range(3):
                try:
                    peer_info = self._probe_service(model_id)
                except Exception:  # noqa: BLE001
                    peer_info = None
                if peer_info is None or peer_info.get("status") == "error":
                    time.sleep(2.0)
                    continue
                peer_ip = (peer_info.get("peer_ip") or "").strip()
                if peer_ip and peer_ip == local_ip:
                    time.sleep(1.0)
                    continue
                # 找到 LAN peer — 注册成独立 adopted entry(key 不同,
                # 不会覆盖 adopted_local 卡)。
                try:
                    peer_inst = self._build_adopted_from_probe(
                        peer_info, model_id)
                    # v0.8.28: 同 worker dedup — 防止 peer_retry 多次命中
                    # 同一 peer worker 时 insert 两次。
                    existing_peer = self._find_inst_by_worker_pid(
                        model_id, peer_inst._peer_pid)
                    if existing_peer is not None:
                        self._upsert_adopted_inst(
                            existing_peer, peer_info, model_id,
                            peer_inst._peer_name, peer_info.get("peer_ip", ""),
                            peer_inst.npu_index)
                    else:
                        with self._lock:
                            # 防御性 dedup:多线程并发下避免重复 insert
                            if peer_inst.key not in self._instances:
                                self._instances[peer_inst.key] = peer_inst
                            peer_t = threading.Thread(
                                target=self._probe_instance,
                                args=(peer_inst,),
                                name=f"compute-probe-peer-{model_id}",
                                daemon=True,
                            )
                            peer_inst._probe_thread = peer_t
                            peer_t.start()
                            LOG.info(
                                "v0.8.25 inline-peer: found LAN peer for %s "
                                "alongside local orphan (peer_ip=%s pid=%s)",
                                model_id, peer_ip,
                                peer_info.get("pid"))
                except Exception as e:  # noqa: BLE001
                    LOG.warning("inline-peer register failed for %s: %s",
                                model_id, e)
                break

        # 接管完后清理同一 model 的多余孤儿（同 comm 名但 probe 没接上的 PID）。
        # 这是修 uvicorn-reload 留下的 NPU 内存泄漏的关键：
        # 旧实现 _find_orphan_worker_pid 只返第一个 PID，剩下的孤儿无人收；
        # 现在按 comm 名扫出全部，probe 接走一个，剩下的 SIGTERM → 等 5s → SIGKILL。
        if isinstance(worker_pid, int) and source == "adopted_local":
            # v0.8.26: 只在 probe 命中本机时清理 extras。boot 拿到 peer
            # (source=adopted) 时,extras 实际是**本机**自己的 /proc worker
            # (v0.8.25 的 inline-peer 把 peer 加进 NETWORK 后,/proc 还能
            # 扫到我们的本地 worker,这时不能误杀)。这是修板B 在 04:43:35
            # boot probe 命中 board A pid=5509 后,cleanup 把本机 pid=4510
            # 当成 orphan SIGTERM 掉的 bug。
            #
            # registry 已经跟踪的本机 worker 不算孤儿 —— 同一个模型跑多张卡时
            # 每张卡一个 worker，都是合法的。
            with self._lock:
                tracked_pids = set()
                for i in self._instances.values():
                    if i.model_id != model_id:
                        continue
                    if i.proc is not None and i.proc.pid:
                        tracked_pids.add(i.proc.pid)
                    peer_pid = getattr(i, "_peer_pid", None)
                    if isinstance(peer_pid, int):
                        tracked_pids.add(peer_pid)
            existing_pids = _find_workers_by_name(model_id)
            extras = [p for p in existing_pids
                      if p != worker_pid and p not in tracked_pids]
            if extras:
                LOG.warning(
                    "adopted %s from local pid=%d; cleaning %d extra orphan(s): %s",
                    model_id, worker_pid, len(extras), extras)
                _terminate_orphan_pids(extras, grace_sec=5)
        elif isinstance(worker_pid, int):
            LOG.info(
                "adopted %s from peer pid=%d (peer_ip=%s); skipping "
                "orphan cleanup — /proc extras are local workers, not orphans",
                model_id, worker_pid, worker_ip)

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
        return self.bind_instance(inst, npu_index)

    def ensure_worker_for_model(self, model_id: str,
                               allow_adopt: bool = True,
                               npu_index: int | None = None,
                               eager_load: bool | None = None) -> ModelInstance:
        """User-facing: ensure *some* worker exists for this model.

        Order:
          1. already in ``_instances``? return it (any source)
          2. if ``allow_adopt``: probe the LAN daemon — if a peer
             responds, adopt
          3. otherwise spawn a fresh local subprocess

        ``allow_adopt`` is True for boot probe / peer listener (zero-
        config fast path — adopt peer if any), False for the API
        ``start_instance`` endpoint where the user clicked the LOCAL
        "启用" card and explicitly wants a fresh local worker on the
        chosen NPU regardless of peer presence.

        ``npu_index`` is forwarded to the spawn as ``--npu`` so the
        worker starts loading on its own — that is the ``autostart``
        path. The compute page leaves it ``None`` because the user picks
        the card in the UI and :meth:`bind_instance` sends the ``load``
        RPC afterwards.

        When ``npu_index`` is given, an **existing instance on that same
        card** is reused. That is what makes 「一个模型跑两张卡」 work:
        card 0 already holding ``qwen3-vl-4b`` does not stop the caller
        from getting one on card 1 — it gets its own worker with its own
        per-PID DDS service, and the two coexist in ``_instances`` (the
        instance key has always been ``(npu, model_id, source)``).

        Status returned may be ``not_loaded`` if the worker is up but
        the user has not yet bound an NPU (call ``bind_instance`` for
        that).
        """
        with self._lock:
            for inst in self._instances.values():
                if inst.model_id != model_id:
                    continue
                if not allow_adopt and inst._source == "adopted":
                    continue
                # Card-aware matching: without this, a model already on
                # card 0 would be handed back for a request naming card 1
                # and bind_instance would then ask one worker to load a
                # second model onto a second card. Rule: _instance_reusable.
                if not _instance_reusable(
                        inst.npu_index, inst._source, npu_index, eager_load):
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
                    # v0.14.3: full teardown — also destroy the wdndds
                    # Service (was missing pre-fix; old code only closed
                    # the proxy + destroyed the Requester, leaking the
                    # Service in hub._services).
                    self._teardown_rpc_client(inst)
                    self._instances.pop(inst.key, None)
                    # fall through to adopt / spawn
                    break
                return inst
        if allow_adopt:
            adopted = self._adopt_existing_worker(model_id)
            if adopted is not None:
                return adopted
        spawned = self._spawn_compute_worker(model_id, allow_adopt=allow_adopt,
                                             npu_index=npu_index,
                                             eager_load=eager_load)
        return spawned

    def _call_load_with_warmup(self, rpc_client, inst: ModelInstance,
                               npu_index: int) -> dict:
        """Send the ``load`` RPC, retrying while the cshim Requester is
        still matching the worker's per-PID DDS service.

        The first worker on a board gets its endpoint matched long before
        anyone enables it, so a flat 5 s timeout has always sufficed. But
        when one model is loaded on a **second** card, that worker was
        spawned seconds ago: its ``wdndds/rpc/<model>/worker-<pid>``
        service has to go through SEDP/SPDP before the Requester can
        reach it, and a bare 5 s call comes back ``{"ok": false, "error":
        "timeout"}`` while the worker sits there perfectly healthy.

        Bounded backoff: 3 attempts with a 5 s → 15 s → 30 s budget. A
        genuine load failure still surfaces (the worker replies
        ``ok: false`` with a real message, which is returned as-is and
        never retried) — this only rides out discovery.
        """
        payload = {"action": "load", "op": "load", "npu_index": npu_index}
        timeouts = (5_000, 15_000, 30_000)
        r = None
        for attempt, timeout_ms in enumerate(timeouts):
            if attempt:
                time.sleep(2.0)
            LOG.debug(
                "load RPC attempt %d/%d model=%s npu=%d timeout=%dms",
                attempt + 1, len(timeouts), inst.model_id, npu_index,
                timeout_ms)
            r = rpc_client.call(payload, timeout_ms=timeout_ms)
            if not (r or {}).get("error") == "timeout":
                return r or {}
            LOG.info(
                "load RPC timed out (attempt %d/%d) model=%s npu=%d — "
                "likely DDS discovery still settling for worker pid=%s",
                attempt + 1, len(timeouts), inst.model_id, npu_index,
                getattr(inst.proc, "pid", None))
        return r or {}

    def bind_instance(self, inst: ModelInstance, npu_index: int) -> ModelInstance:
        """v0.8 fire-and-forget：立即把 ``inst.status="loading"`` 返回，
        让前端 1Hz tick 看到 loading 状态 + 倒计时；后台线程跑 RPC
        ``load``，完成后翻 status=running / error，失败时 npu_index
        回退 -1。

        v0.7 之前是同步阻塞等 RPC load（120s timeout）—— 前端
        ``await fetch POST`` 卡 60s 才看到状态变化，用户体验
        "点了就变蓝，不知道什么时候加载完"。fire-and-forget 后
        用户能看到 is-load 脉动 + 倒计时（"约 30s"），加载完成
        toggle 切到 running 绿色 + 显示加载耗时。

        v0.8.11:caller 必须传 inst(从 ``ensure_worker_for_model``
        返回的那个),而不是再传 model_id 让我们重查 _instances。
        之前按 model_id 查会撞到多个同 model 不同 source 的 inst
        (e.g. adopted + spawned) 时返回 boot probe 先加的那个
        adopted,导致 spawn 完了却 bind 到 adopted → 403。

        Raises ``RuntimeError`` for pre-flight checks (peer source,
        no RPC client, bad npu_index). The RPC call itself is async
        — its failures land on ``inst.status = "error"`` + ``inst.error``.
        """
        import threading

        if inst is None:
            raise RuntimeError("bind_instance: inst 不能为空")
        if inst._source == "adopted":
            raise RuntimeError(
                f"{inst.model_id} 是 peer 提供的 service，本机无权 bind")
        if inst.rpc_client is None:
            # Service was torn down on a previous unload (v0.14.3+) so
            # the cshim payload pool doesn't pin in hub._services.
            # Worker process is still alive (inst.proc preserved);
            # spin a fresh Service + Requester + Proxy for this re-bind.
            self._setup_rpc_client(inst)
            if inst.rpc_client is None:
                raise RuntimeError(
                    f"{inst.model_id} worker RPC client 未就绪 (re-setup 失败)")
        if not isinstance(npu_index, int) or npu_index < 0:
            raise RuntimeError(f"npu_index 非法: {npu_index!r}")

        # 乐观更新：前端 1Hz tick 立即看到 status="loading" + npu_index。
        #
        # key 也在**这里**换成带卡号的 —— 不能等异步 load 跑完（~35s），否则
        # 同模型第二张卡的 spawn 会用同一个占位 key 把第一张卡挤掉（见 AGENTS.md）。
        with self._lock:
            inst.npu_index = npu_index
            inst.status = "loading"
            inst.started_at = time.time()
            inst.error = ""
            new_key = (npu_index, inst.model_id, inst._source)
            if inst.key != new_key:
                self._instances.pop(inst.key, None)
                inst.key = new_key
                self._instances[new_key] = inst
        inst._bind_inflight = True

        def _run_load():
            # v0.14.3: capture the RPC client at thread start. If the
            # user disables mid-load, ``unload_instance`` →
            # ``_teardown_rpc_client`` sets ``inst.rpc_client = None``
            # while this thread is still in flight. The old code would
            # then hit ``NoneType.call(...)`` → silently fall into the
            # ``except`` branch → overwrite the just-set ``not_loaded``
            # status with ``error``. Bail early instead.
            rpc_client = inst.rpc_client
            if rpc_client is None:
                return
            try:
                # load_handler 立即返回 {"status": "loading"} + npu_index
                # (实际 init 在 worker 后台线程跑,~50-90s)。所以这里只需
                # 等 RPC round-trip + cshim discovery grace,5 秒足够。
                with inst._probe_lock:
                    if rpc_client is None or inst.rpc_client is not rpc_client:
                        return
                    LOG.debug(
                        "compute_registry: _run_load.call load model=%s "
                        "npu=%d thread=%s",
                        inst.model_id, npu_index, threading.get_ident())
                    r = self._call_load_with_warmup(
                        rpc_client, inst, npu_index)
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
                        inst.model_id, err_msg, rc, len(stderr_tail),
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
                                inst.model_id, err_msg,
                                f" (rc={rc})" if rc is not None else "")
                    return
                if data.get("status") == "error":
                    err_msg = data.get("init_error", "load worker error")
                    with self._lock:
                        inst.status = "error"
                        inst.error = str(err_msg)
                        inst.npu_index = -1
                    LOG.warning("bind %s load worker error: %s",
                                inst.model_id, err_msg)
                    return
                if data.get("status") == "missing_model_dir":
                    err_msg = data.get("error", "模型目录不存在")
                    with self._lock:
                        inst.status = "missing_model_dir"
                        inst.error = err_msg
                        inst.npu_index = -1
                    LOG.warning("bind %s missing_model_dir: %s",
                                inst.model_id, err_msg)
                    return
                if data.get("status") == "loading":
                    # Worker 把 LLMSession init 挪到独立线程跑（避免
                    # cshim Replier pump 线程持 rknn 资源跟 init 死锁），
                    # RPC 立即返 loading。_probe_instance 1Hz 探测 info
                    # RPC，看到 worker 的 state["status"] 翻 running /
                    # error 自动同步到 inst。这里 fire-and-forget 直接
                    # 退场：probe 接管。
                    # v0.8.13:key 用 inst._source 不硬编码 —— bind 也能调到
                    # adopted_local (orphan dedup 返回的 inst)。
                    new_key = (npu_index, inst.model_id, inst._source)
                    with self._lock:
                        if inst.key != new_key:
                            self._instances.pop(inst.key, None)
                            inst.key = new_key
                            self._instances[new_key] = inst
                        # inst.status 已经是 loading（bind_instance 入口
                        # 乐观设置）；保留不要改 running。
                    LOG.info("bind %s @ npu=%d: async load started, "
                             "deferring to probe", inst.model_id, npu_index)
                    return
                # 翻 running + 更新 key + 加载耗时
                new_key = (npu_index, inst.model_id, inst._source)
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
                         inst.model_id, npu_index, inst.load_time_sec or 0.0)
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
                              inst.model_id,
                              f" (rc={rc})" if rc is not None else "")
            finally:
                inst._bind_inflight = False
                LOG.debug(
                    "compute_registry: _run_load done model=%s npu=%d "
                    "final_status=%s thread=%s",
                    inst.model_id, npu_index, inst.status,
                    threading.get_ident())

        threading.Thread(target=_run_load, daemon=True,
                         name=f"compute-bind-{inst.model_id}").start()
        return inst

    def unload_instance(self, model_id: str, teardown: bool = True) -> None:
        """RPC ``unload``: worker drops its LLMSession, status returns
        to ``not_loaded``, npu_index resets to -1.

        Works for adopted peers too — we just ask the peer to release
        the resource; we don't terminate anything we don't own.

        The RPC is **synchronous**: ``rpc_client.call`` blocks until the
        worker replies, and the worker only replies after ``unload_handler``
        has run ``sess.close()`` → ``backend.close()`` → ``rknn3_destroy``.
        So when this returns, the NPU memory pool has actually been
        returned — not merely dereferenced. Callers that are about to kill
        the worker process depend on that ordering.

        v0.14.3: after the worker drops its session, also tear down
        the per-instance FastDDS Service / Requester / LLMRPCSession
        in the main process via :meth:`_teardown_rpc_client`. Without
        this, every enable→disable cycle leaks one Service (cshim
        payload pool ~60 MB per the AGENTS.md "Service 生命周期"
        note) + one Python Requester + one ThreadPoolExecutor until
        the next registry stop. The worker process (``inst.proc``) is
        preserved — re-enable rebuilds the Service via
        :meth:`_setup_rpc_client` in :meth:`bind_instance`.

        v0.15 (compute_disable flicker fix): 两个协同防御避免 disable
        期间 算力配置页 toggle 抖动 关闭→开启→关闭：
        (1) 发 RPC 前乐观写 ``status="not_loaded"`` —— 1Hz 轮询在
        RPC 期间（500-1000ms）也看到一致状态；
        (2) ``_probe_gen += 1`` —— 失效 probe in-flight 的陈旧 ``r``，
        避免覆盖乐观写。

        ``teardown=False`` defers :meth:`_teardown_rpc_client` to the
        caller, leaving ``rpc_client`` / ``_svc`` usable. **Only
        :meth:`stop_instance_local` may pass False** — it is about to
        send a ``shutdown`` RPC, and that call is guarded by
        ``if inst.rpc_client is not None``. Tearing down first makes
        the shutdown unreachable, the worker never sees the stop
        event, and the whole graceful-exit path (audio writer close,
        served-session close, side-hub close, atexit ``/dev/shm``
        sweep) is skipped in favour of a bare SIGKILL. Leave it True
        everywhere else — nothing else has a follow-up RPC to make.

        Acts on **every** instance of the model: one model can be loaded
        on several cards at once, and "停止这个模型" that only released
        card 0 would leave card 1 still holding NPU memory while the UI
        showed it as stopped.
        """
        for inst in self._instances_of(model_id):
            inst._probe_gen += 1  # 失效 in-flight probe 的 r
            if inst.rpc_client is not None:
                try:
                    # unload_handler 立即返回 {"status": "unloaded"}。5s 足够。
                    with inst._probe_lock:
                        if inst.rpc_client is None:
                            continue
                        inst.rpc_client.call(
                            {"action": "unload", "op": "unload"},
                            timeout_ms=5_000)
                except Exception as e:  # noqa: BLE001
                    LOG.warning("unload %s (npu=%s) RPC failed: %s",
                                model_id, inst.npu_index, e)
            with self._lock:
                old_key = inst.key
                inst.npu_index = -1
                inst.key = (-1, model_id, inst._source)
                inst.status = "not_loaded"
                inst.load_time_sec = None
                inst.loaded_at = None
                self._instances.pop(old_key, None)
                # Two instances unloading at once would both land on
                # key (-1, model, source) and the second would overwrite
                # the first, orphaning it. Keep them apart by suffixing.
                if (-1, model_id, inst._source) in self._instances:
                    inst.key = (-1 - len(self._instances), model_id,
                                inst._source)
                self._instances[inst.key] = inst
        # Tear down the local FastDDS endpoints so the cshim payload
        # pool doesn't pin in hub._services. The hub Service / Requester
        # is what AGENTS.md calls "每个 ~60 MB of RSS" per Service;
        # stopping here gives the user real memory reclaim on disable
        # without a worker process restart.
        #
        # Skipped when the caller defers it (see the ``teardown`` arg) —
        # ``stop_instance_local`` still needs this Requester to deliver
        # the shutdown RPC that follows.
            if teardown:
                self._teardown_rpc_client(inst)
            else:
                LOG.debug(
                    "unload %s (npu=%s): rpc_client teardown deferred to caller",
                    model_id, inst.npu_index)

    def stop_instance_local(self, model_id: str,
                            npu_index: Optional[int] = None) -> None:
        """Unload → RPC shutdown → SIGTERM → wait 3 s → kill -9.

        ``npu_index`` 缺省停该模型全部实例；给了卡号只停那一张。

        The order is load-bearing, and reversing any two steps breaks
        something real:

        1. ``unload_instance(teardown=False)`` runs **first**. It blocks
           on the worker's ``unload`` reply, and the worker only replies
           after ``unload_handler`` has run ``rknn3_destroy`` — so the
           NPU memory pool is genuinely returned before we do anything
           to the process. Shutting down first would let the worker exit
           while its ``LLMSession`` is still alive, and the pool would
           stay claimed: killing the process would then be pointless.
           ``teardown=False`` keeps ``rpc_client`` alive for step 2.
        2. ``shutdown`` over RPC so the worker runs its graceful path:
           stop event → main-loop ``finally`` → audio writer close →
           served-session close → side-hub close → atexit ``/dev/shm``
           sweep (``max_age=0``).
        3. SIGTERM as a second, transport-independent nudge. The worker
           installs a SIGTERM handler (``workers/_base.py``) equivalent
           to the shutdown RPC, and unlike step 2 this does not depend on
           the Requester still working — which is the *only* route to a
           graceful exit for an ``adopted_local`` instance (``proc`` is
           None there, so 4/5 are skipped).
        4. 3 s grace, then SIGKILL.

        Raises ``RuntimeError`` if the instance is adopted (proc is
        None) — the caller (API layer) turns this into HTTP 403.

        Acts on **every** instance of the model, matching
        :meth:`unload_instance` — a model loaded on two cards must not
        leave one of them running behind the UI's back.
        """
        targets = self._instances_of(model_id, npu_index)
        if not targets:
            raise RuntimeError(f"{model_id} 不在 registry")
        t0 = time.time()
        for inst in targets:
            self._stop_inst(model_id, inst)
        LOG.info(
            "compute_registry: stop_instance_local exit model=%s "
            "instances=%d elapsed=%.2fs thread=%s",
            model_id, len(targets), time.time() - t0, threading.get_ident())

    def _stop_inst(self, model_id: str, inst: ModelInstance) -> None:
        """The 5-step teardown for one instance. See
        :meth:`stop_instance_local` for why the order is load-bearing."""
        if inst._source == "adopted":
            raise RuntimeError(
                f"{model_id} 是远端 peer 提供的 service，本机无权停止")
        inst._probe_gen += 1  # 同 unload_instance 理由
        LOG.debug(
            "compute_registry: stop enter model=%s npu=%s source=%s thread=%s",
            model_id, inst.npu_index, inst._source, threading.get_ident())
        # (1) unload first — must not tear down the Requester, step 2
        # needs it. See the method docstring for why this order.
        try:
            self.unload_instance(model_id, teardown=False)
        except Exception as e:  # noqa: BLE001
            LOG.warning("stop: unload %s raised: %s", model_id, e)
        # (2) graceful shutdown over the still-live Requester
        try:
            if inst.rpc_client is not None:
                inst.rpc_client.call(
                    {"action": "shutdown", "op": "shutdown"}, timeout_ms=5_000)
        except Exception as e:  # noqa: BLE001
            LOG.info("stop: shutdown RPC %s raised: %s", model_id, e)
        proc = inst.proc
        if proc is not None and proc.poll() is None:
            # (3) SIGTERM nudge; (4) grace period, then SIGKILL
            try:
                proc.terminate()
            except Exception as e:  # noqa: BLE001
                LOG.info("stop: terminate %s raised: %s", model_id, e)
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                LOG.warning(
                    "stop: %s did not exit within 3s of SIGTERM/SIGKILL "
                    "shutdown — SIGKILL", model_id)
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
        # The local endpoints are still live at this point — step 1
        # deferred its teardown, and steps 2/3 needed them. The locks
        # below mirror _teardown_rpc_client. This block is idempotent:
        # it is a no-op if something above already dropped a handle.
        try:
            with inst._probe_lock:
                if inst.rpc_client is not None:
                    inst.rpc_client.destroy()
                    inst.rpc_client = None
        except Exception:
            pass
        svc = getattr(inst, "_svc", None)
        if svc is not None:
            try:
                with inst._probe_lock:
                    svc_name = svc.name
                    hub = self._hub
                    if hub is not None:
                        hub.destroy_service(svc_name)
                inst._svc = None
            except Exception as e:  # noqa: BLE001
                LOG.warning("stop: hub.destroy_service(%s) raised: %s",
                            model_id, e)
        with self._lock:
            self._instances.pop(inst.key, None)
        LOG.debug(
            "compute_registry: stop_inst done model=%s npu=%s thread=%s",
            model_id, inst.npu_index, threading.get_ident())

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
            # A model on two cards contributes two instances — stop it once.
            for inst in self._instances.values():
                if inst.proc is not None and inst.model_id not in models:
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
        """Create the RpcClient + LLMRPCSession for the given instance.

        Used by both ``_start_one_subprocess`` (fresh spawn) and
        ``_adopt_existing_worker`` (boot-time adoption of an already
        responding peer).

        v0.8.14: for **local** workers (spawned / adopted_local) we bind
        to the per-worker service ``wdndds/rpc/<model_id>/worker-<pid>``
        so FastDDS routing is deterministic — the standard
        ``wdndds/rpc/<model_id>`` topic is shared with any LAN peer
        also providing the same model, and a non-deterministic load RPC
        would land on the peer instead of the local worker, leaving the
        local worker permanently unloaded. For **adopted (LAN peer)**
        insts we keep the standard service name — there is no per-worker
        PID for a remote peer.
        """
        LOG.debug(
            "compute_registry: _setup_rpc_client enter model=%s "
            "source=%s thread=%s",
            inst.model_id, inst._source, threading.get_ident())
        hub = self._hub_or_raise()
        if inst._source == "spawned" and inst.proc is not None and inst.proc.pid:
            service_name = (
                f"wdndds/rpc/{inst.model_id}/worker-{inst.proc.pid}")
        elif inst._source == "adopted_local" and inst._peer_pid:
            service_name = (
                f"wdndds/rpc/{inst.model_id}/worker-{inst._peer_pid}")
        else:
            service_name = f"wdndds/rpc/{inst.model_id}"
        svc = hub.create_service(service_name)
        # create_requester 内部不发 RPC,只是 cshim entity 创建 + SEDP
        # discovery grace ~5s。5s 足够。
        rpc_client = svc.create_requester(timeout_ms=5_000)
        inst.rpc_client = rpc_client
        # Stash the Service on the instance so ``stop_instance_local``
        # / ``unload_instance`` can tear it down and pop it from
        # ``hub._services``. Without this, every enable→disable cycle
        # accumulates one Service per (npu, model, worker_pid),
        # each pinning a DataReader + DataWriter + payload pool
        # totalling ~60 MB of RSS that never gets reclaimed (the
        # pre-fix main-process RSS jumped ~80 MB per restart cycle).
        inst._svc = svc
        try:
            from app.core.compute.proxy import make_rpc_session
            # The **real** ``wdnai.ModelSpec`` — NOT the manifest preset
            # dict.  The preset view carries UI metadata but no ``kind``,
            # and ``kind`` is what the SDK uses to decide whether the
            # JSON-schema hint may enter the prompt.  Passing the dict
            # made ``spec.kind`` resolve to ``None``, the schema
            # suppression silently switched itself off, and a 2 KB
            # ``LocatorDoc`` JSON Schema rode along into every grounding
            # prompt — the model then grounded the schema's own field
            # names ("description", "ROI 左上角 x，0–1000 归一化。").
            spec = build_model_spec(inst.model_id)
            inst.rpc_session_proxy = make_rpc_session(
                model=spec,
                model_id=inst.model_id,
                service_name=service_name,
                hub=hub,
                requester=rpc_client,
                side_hub=self._side_hub(),
                _svc=svc,
                # LAN peer (adopted from another machine): the peer's
                # /dev/shm is a different filesystem namespace, so the
                # transport falls back to JPEG + base64 for frames —
                # slightly lossy, but it survives FastDDS's builtin
                # UDPv4/TCPv4 transports on the LAN hop.
                is_remote=(inst._source == "adopted"),
            )
            inst.llm_session = inst.rpc_session_proxy
        except Exception as e:  # noqa: BLE001
            LOG.warning("LLMRPCSession create failed for %s: %s",
                        inst.model_id, e)

    def _teardown_rpc_client(self, inst: ModelInstance) -> None:
        """Tear down the per-instance FastDDS Requester / RpcSessionProxy
        / wdndds Service in the **main process** so the cshim payload
        pool (~60 MB per Service per the AGENTS.md "Service 生命周期"
        note) doesn't pin in ``hub._services`` until next restart.

        Worker process (``inst.proc``) is NOT touched — caller is
        responsible for keeping / killing the process independently
        (``unload_instance`` keeps it alive for fast re-enable;
        ``stop_instance_local`` kills it).

        For ``_source == "adopted"`` (LAN peer): keep the local Service
        (the peer's Replier is still subscribed to the same service
        name; destroying the local Service would break cross-machine
        discovery). Only close the local Requester + Proxy.

        Idempotent: safe to call twice or on a never-set-up instance.
        Each step is wrapped in ``try/except`` so one failure doesn't
        short-circuit the rest.
        """
        proxy = getattr(inst, "rpc_session_proxy", None)
        if proxy is not None:
            try:
                # ``LLMRPCSession.close()`` shuts down its own
                # ThreadPoolExecutor + the cshim client and the delta /
                # stop side channel. It does NOT destroy the registry's
                # Service — for an ``adopted`` peer that Service has to
                # survive, so we do that separately below.
                proxy.close()
            except Exception as e:  # noqa: BLE001
                LOG.debug("teardown[%s]: proxy.close raised: %s",
                          inst.model_id, e)
            inst.rpc_session_proxy = None

        rpc = inst.rpc_client
        svc = getattr(inst, "_svc", None)
        LOG.debug(
            "compute_registry: _teardown_rpc_client enter model=%s "
            "rpc_client=%s svc=%s source=%s thread=%s",
            inst.model_id, rpc is not None, svc is not None,
            inst._source, threading.get_ident())
        if rpc is not None:
            # Clear Python handle BEFORE cshim destroy so any thread
            # that passed the rpc_client truthy check sees AttributeError
            # instead of deref'ing a freed cshim _native. The C-level
            # UAF is closed by inst._probe_lock below.
            inst.rpc_client = None
            with inst._probe_lock:
                try:
                    rpc.destroy()
                except Exception as e:  # noqa: BLE001
                    LOG.debug("teardown[%s]: rpc_client.destroy raised: %s",
                              inst.model_id, e)
                # Drop the dead Requester from the Service's tracking list
                # BEFORE tearing down the Service, so the Service's
                # ``_requesters`` list goes to 0 (or stays at 0 if we're
                # about to destroy the Service entirely).
                if svc is not None:
                    try:
                        svc.remove_requester(rpc)
                    except Exception as e:  # noqa: BLE001
                        LOG.debug("teardown[%s]: svc.remove_requester raised: %s",
                                  inst.model_id, e)

        if svc is not None and inst._source != "adopted":
            # Same lock for Service destroy: prevents probe mid-call
            # from racing with Service.destroy → Requester.destroy.
            with inst._probe_lock:
                try:
                    svc_name = svc.name
                    hub = self._hub
                    if hub is not None:
                        hub.destroy_service(svc_name)
                except Exception as e:  # noqa: BLE001
                    LOG.warning("teardown[%s]: hub.destroy_service raised: %s",
                                inst.model_id, e)
            inst._svc = None
        elif svc is not None:
            # adopted (LAN peer): keep Service; just clear our handle
            # to the local Service so we don't accidentally re-destroy
            # it on a subsequent teardown attempt.
            inst._svc = None

        inst.llm_session = None
        LOG.debug(
            "compute_registry: _teardown_rpc_client exit model=%s "
            "rpc_client=%s svc=%s thread=%s",
            inst.model_id, inst.rpc_client is not None,
            inst._svc is not None, threading.get_ident())

    def _probe_service(self, model_id: str) -> Optional[dict]:
        """Short-timeout RPC ``info`` probe on ``wdndds/rpc/<model_id>``.

        Returns the worker's info dict on success, ``None`` after all
        attempts are exhausted. Used by ``_adopt_existing_worker`` to
        avoid spawning duplicate processes when a peer (or a stale
        orphan from a previous boot) is already providing the service
        on the LAN.

        **首次调用 race 兜底**：cshim client 在 daemon 刚握手 / endpoint
        还没匹配时，第一次 ``RpcClient.call()`` 会 timeout（约 5s），
        第二次就成功。所以这里走 3 次尝试 × 5s timeout + 1s 间隔：
        第 1 次内若 endpoint 已匹配就直接返；不匹配就睡 1s 再试，
        覆盖 daemon 重启 + cshim 重新握手这条链路。

        旧版单次 1500ms timeout 的实现在 web 进程 reload 场景下会因
        race 全 timeout，触发后续 fallback ``_spawn_compute_worker``，
        在已经加载好模型的 NPU 上又 spawn 一个 worker 吃双份显存。
        3 次 × 5s 上限 ~16s 总耗时，boot 阶段可接受。

        Returns:
            dict  on success (the worker's ``info_handler`` payload);
            ``None`` when all retries are exhausted (worker is dead
            OR hub unavailable OR RPC channel truly broken).
        """
        if self._hub is None:
            return None
        attempts = 3
        timeout_ms = 5000
        sleep_between = 1.0
        for attempt in range(1, attempts + 1):
            svc = None
            rpc = None
            try:
                svc = self._hub.create_service(f"wdndds/rpc/{model_id}")
                rpc = svc.create_requester(timeout_ms=timeout_ms)
            except Exception as e:  # noqa: BLE001
                LOG.debug("probe: create requester failed for %s "
                          "(attempt %d/%d): %s",
                          model_id, attempt, attempts, e)
                if attempt < attempts:
                    time.sleep(sleep_between)
                    continue
                return None
            try:
                r = rpc.call({"action": "info", "op": "info"}, timeout_ms=timeout_ms)
            except Exception as e:  # noqa: BLE001
                LOG.debug("probe: call failed for %s (attempt %d/%d): %s",
                          model_id, attempt, attempts, e)
                if attempt < attempts:
                    time.sleep(sleep_between)
                    continue
                return None
            finally:
                try:
                    rpc.destroy()
                except Exception:
                    pass
            if r and r.get("ok") and r.get("data"):
                if attempt > 1:
                    LOG.info("probe: %s responded on attempt %d/%d "
                             "(race recovery)",
                             model_id, attempt, attempts)
                return r.get("data") or {}
            # 空响应（ok 但 data 为空 / 不存在）—— 也是 race 未结束。
            # 不 sleep 一下立刻重试代价太高会刷爆 cshim；sleep 1s 走下次。
            LOG.debug("probe: %s returned empty/ok=False "
                      "(attempt %d/%d)",
                      model_id, attempt, attempts)
            if attempt < attempts:
                time.sleep(sleep_between)
        LOG.warning("probe: %s unreachable after %d attempts; "
                    "giving up",
                    model_id, attempts)
        return None

    def _find_by_model(self, model_id: str) -> Optional[ModelInstance]:
        """First registered instance of ``model_id`` (本机优先).

        Kept for call sites that only care about "some instance". When a
        model runs on several cards this returns an arbitrary one — use
        :meth:`_instances_of` (or pass ``npu_index``) when the card
        matters.
        """
        with self._lock:
            adopted = None
            for inst in self._instances.values():
                if inst.model_id != model_id:
                    continue
                if inst._source in ("spawned", "adopted_local"):
                    return inst
                adopted = adopted or inst
            return adopted

    def _instances_of(self, model_id: str,
                      npu_index: Optional[int] = None) -> List[ModelInstance]:
        """Every registered instance of ``model_id``, optionally narrowed
        to one card.

        One model can hold several instances at once — the compute page's
        运行在 is multi-select, so the same manifest may be loaded on card
        0 and card 1 simultaneously. Ordering is 本机优先, then ascending
        ``npu_index``, so "the first" is deterministic instead of
        whatever the dict happens to yield.
        """
        local_first = {"spawned": 0, "adopted_local": 1, "adopted": 2}
        with self._lock:
            found = [i for i in self._instances.values()
                     if i.model_id == model_id
                     and (npu_index is None or i.npu_index == npu_index)]
        found.sort(key=lambda i: (local_first.get(i._source, 9),
                                  i.npu_index))
        return found

    def list_with_source(self) -> list[dict]:
        """UI-friendly snapshot of every catalog model's current state.

        Returns one entry per ``MODEL_PRESETS`` key (so the page can
        always render the full catalog), with a ``source`` field:

          * ``"spawned"``  — this machine spawned the worker (proc set)
          * ``"adopted"``  — peer on the LAN is providing the service
          * ``"unavailable"`` — daemon probed, no peer responded

        Ordered: spawned first, adopted second, unavailable last.

        Hidden-by-manifest filter
        -------------------------
        A model whose ``workers/<id>.json`` sets ``"hidden": true`` is
        dropped from the snapshot entirely — it is "configured to never
        be exposed", and it also never appears as an "unavailable"
        placeholder. This replaces the old ``compute.services[].autostart
        == "hidden"`` flag, which meant the same thing but lived in a
        table that had to enumerate every model to express it.
        """
        # Hidden models come from their own manifest now, not from
        # config — a broken config can't accidentally hide a model.
        hidden_set: set[str] = {
            m.id for m in discover_workers() if m.hidden
        }

        out: list[dict] = []
        # config 的 autostart 列表 —— 徽章 / 默认卡位的**真源**（见
        # _autostart_npu_map）。跑一次，别每个实例读一遍 config。
        autostart_npus = _autostart_npu_map()
        with self._lock:
            # v0.8.13:by_model 改成 list of insts per model_id,而不是只留
            # 最后一个。之前 dict comprehension inst.model_id -> inst 会让
            # 多个同 model 不同 source 的 inst(spawned + adopted 共存)
            # 被互相挤掉,UI 列表只看到其中一个。
            by_model: dict[str, list[ModelInstance]] = {}
            for inst in self._instances.values():
                if inst.model_id in hidden_set:
                    # Hidden: should never have an instance; defensive
                    # drop in case boot missed it (e.g. user just
                    # flipped hidden=true at runtime).
                    continue
                by_model.setdefault(inst.model_id, []).append(inst)

        def _unavailable_entry(model_id: str) -> dict:
            preset = MODEL_PRESETS[model_id]
            # No label override anymore — the manifest owns it. The
            # autostart card is only a preselect for the radio; -1 means
            # "this model isn't autostarted, let the user pick".
            cfg_default_npu = autostart_npus.get(model_id, -1)
            return {
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
                "available_image_sizes": [],
                "_source":            "unavailable",
                "_peer_guid":         "",
                "_peer_name":         "",
                "_peer_ip":           "",
                "_autostart":         "enabled" if cfg_default_npu >= 0 else "",
                "_default_npu_index": cfg_default_npu,
                "_config_label":      "",
                "display_order":      preset.get("display_order", 9999),
            }

        for model_id, preset in MODEL_PRESETS.items():
            if model_id in hidden_set:
                # Hidden entries are not rendered at all.
                continue
            insts = by_model.get(model_id, [])
            if not insts:
                out.append(_unavailable_entry(model_id))
                continue
            for inst in insts:
                out.append(_with_autostart_markers(inst.to_dict(),
                                                    autostart_npus))
            # v0.8.7:只对 **远端 adopted** 补 unavailable 占位卡(让用户
            # 能用本机 NPU 覆盖)。adopted_local 已经是本机孤儿 worker 在跑,
            # 再补一条会让本地栏出现 2 个同名 model,用户困惑哪个是真的。
            # spawned 本来就是本机在跑,补一条也没意义。只有远端 adopted
            # 没本地 spawn / orphan 时才需要补占位卡提示"可以启用本机覆盖"。
            # v0.8.18:原版只检查 adopted,漏了 spawned/adopted_local 共存
            # (LOCAL 启用后 LOCAL 栏出现 2 个同名 model 的 bug)。改判
            # `adopted AND not has_local`。
            has_local = any(i._source in ("spawned", "adopted_local")
                            for i in insts)
            if (any(i._source == "adopted" for i in insts)
                    and not has_local):
                out.append(_unavailable_entry(model_id))
        # Stable ordering: spawned → adopted_local → adopted → unavailable.
        order = {"spawned": 0, "adopted_local": 1, "adopted": 2, "unavailable": 3}
        out.sort(key=lambda x: (order.get(x.get("_source", ""), 9),
                                x.get("model_id", "")))
        return out

    def boot_compute_services(self) -> None:
        """App boot: start every model listed in ``compute.autostart``.

        Each entry is ``{"model": <id>, "npu_index": <card>}``. The model
        is spawned with ``--npu`` so the worker starts building its
        ``LLMSession`` on its own background thread the moment its
        replier is up — no second RPC round-trip, and ``info`` answers
        immediately (``not_loaded`` → ``loading`` → ``running`` as the
        build progresses).

        A model already **running on this board's card** is not spawned
        again. A LAN peer is deliberately **not** consulted here:
        ``compute.autostart`` says "this board, this card" — borrowing a
        peer's worker would leave the pinned card idle, and borrowed
        compute disappears silently the moment the peer unloads it (its
        side effect is the same symptom as "autostart never started").
        Sharing compute across boards is a **runtime** feature
        (:meth:`get_session` falls back to a peer only when this board
        has no instance at all).

        Models *not* in ``autostart`` do not participate in boot at all:
        no spawn, no adopt, no NPU memory. The compute page still lists
        them as unavailable so the user can enable one by hand.

        A malformed ``autostart`` (unknown model, bad card index, a
        leftover ``services``-style entry) is logged and dropped rather
        than raised — one bad line must not stop the other models from
        coming up.

        Ordering matters: the orphan sweep runs **before** the spawn
        loop. :meth:`_boot_spawn_async` is fire-and-forget, so a sweep
        that ran afterwards would race the spawn threads for
        ``tracked_pids`` and SIGTERM a worker that had just been Popen'd
        but not yet registered in ``_instances`` (observed: the first
        autostart model died ``rc=-15`` within the same second it was
        spawned). Sweeping first is also semantically right — it reaps
        the *previous* boot's leftovers, before this boot adds any.
        """
        try:
            self.list_cards()
        except Exception:
            pass
        try:
            self._cleanup_unregistered_workers()
        except Exception as e:  # noqa: BLE001
            LOG.warning("boot cleanup sweep failed: %s", e)
        try:
            stored = get_config().get_section("compute", {})
        except Exception:
            stored = {}
        raw = stored.get("autostart") if isinstance(stored, dict) else None
        try:
            entries = _parse_autostart_config(raw)
        except ValueError as e:
            LOG.error("compute.autostart 配置非法，已跳过开机启动: %s", e)
            entries = []
        # autostart 裁剪：非模型卡的条目丢弃 + 每张卡只留第一条
        # （理由与规则见 _crop_autostart_for_cards）。
        model_cards = self.model_cards()
        if entries:
            kept, dropped = _crop_autostart_for_cards(entries, model_cards)
            for e in dropped:
                if e["npu_index"] not in model_cards:
                    LOG.info("boot: autostart 跳过 %s @ npu%s —— 该卡不在"
                             "模型卡位 %s 内", e["model"], e["npu_index"],
                             model_cards)
                else:
                    LOG.info("boot: autostart 跳过 %s @ npu%s —— 一张卡只能"
                             "承载一个模型（同卡叠模型 rc=-6）",
                             e["model"], e["npu_index"])
            entries = kept
        # v0.4+: 顺便读一下 active_app 标记 —— boot 后日志会顺手打印
        # 一下当前记的"用户上次所在的卡片"。前端 bootRestore 会读
        # 这个值并跳过去，这里**不**做导航（主进程没有 webview），
        # 也不**清**它：active_app 是用户态偏好，boot 之后仍要保留
        # 至少到用户主动离开卡片；自动失效由前端 _evaluate 检测
        # cap 不全时 DELETE。这里打 log 是给运维 / 排障一个提示。
        active_app_id = self.get_active_app()
        if active_app_id:
            LOG.info("boot: compute.active_app=%r （前端会读此值并自动"
                     "跳到对应 #%s 页面）", active_app_id, active_app_id)
        if not entries:
            LOG.info("boot: compute.autostart 为空 —— 不自动启动任何模型"
                     "（算力页可手动启用）")
        else:
            known = sorted(e["model"] for e in entries)
            LOG.info("boot: compute.autostart 声明了 %d 个模型: %s",
                     len(entries), known)

        for entry in entries:
            model_id = entry["model"]
            npu_index = entry["npu_index"]

            with self._lock:
                already = [i for i in self._instances.values()
                           if i.model_id == model_id]
            for inst in already:
                # config no longer overrides labels (that lives in the
                # manifest), but the autostart card is still the right
                # default to preselect on the compute page's radio.
                if inst._default_npu_index < 0:
                    inst._default_npu_index = npu_index
                if not inst._autostart:
                    inst._autostart = "enabled"

            # 判据：**本板**这张卡上有没有在跑的 worker。规则见
            # _autostart_plan（纯函数，可单测）。
            if _autostart_plan(list(self._instances.values()),
                               model_id, npu_index) == "skip":
                LOG.info("boot: %s 已在 npu=%d 跑着，跳过 adopt/spawn",
                         model_id, npu_index)
                continue

            # 本板负责：起一个带 --npu N 的 worker。_boot_spawn_async 内部
            # allow_adopt=False → 不去 adopt peer（否则会捡回那个空壳，
            # 本板照样空转）。
            self._boot_spawn_async(model_id, npu_index)

        # NOTE: the orphan sweep already ran above the loop. Do not add a
        # second one here — it would race the fire-and-forget spawn
        # threads (see the docstring).

        # v0.8.24: 后台 daemon 再扫一次 peer。boot probe 接受首个 responder,
        # 如果本机已有 /proc orphan(cshim 路由非确定),LAN peer 可能漏掉。
        threading.Thread(
            target=self._discover_additional_peers,
            name="compute-peer-discovery",
            daemon=True,
        ).start()

        # v0.8.29：后台 daemon 对每个 adopted LAN peer 主动发一次 ``info``
        # RPC，把 SEDP/SPDP endpoint 匹配提前到 boot 阶段 —— 否则用户
        # 首次调用（TTS 长文本）会在 cshim Requester discovery race 上
        # 卡 120s。详见 :meth:`_warm_adopted_workers`。
        threading.Thread(
            target=self._warm_adopted_workers,
            name="compute-prewarm-adopted",
            daemon=True,
        ).start()

    def _warm_adopted_workers(self) -> None:
        """v0.8.29：boot 时 pre-warm adopted LAN peer 的 cshim Requester。

        问题：cshim Requester 在 fresh boot 后**首次** LAN RPC 调用时撞
        DDS discovery race —— reply topic 的 SEDP endpoint 映射还没建立，
        即使 boot-time ``_probe_service`` 已通过（probe 用的是另一把
        一次性 requester，详见 :meth:`_probe_service`）。首次真实用户
        调用会把 requester 卡住 ~120s 才超时，用户看到 hung inference。

        修法：遍历所有 ``adopted`` (LAN peer) 且 ``status="running"`` 的
        instance，直接在 ``inst.rpc_client`` 上发一次 ``info`` RPC，强制
        SEDP endpoint 匹配在 boot 时完成。这样之后
        ``LLMRPCSession.respond()`` 跑时 endpoint 已经 warm。

        约束：
          * 故意不走 ``_call_with_recreate`` —— 该 helper 在 wedged client
            上调 ``RpcClient.destroy()`` 会 SEGV cshim (status=11)。boot
            期间 pre-warm 失败非致命：卡在 boot 阶段用户无感；下一次
            ``/start`` 要么成功（probe daemon 内重试成功），要么出干净错。
          * 只 warm ``adopted``（LAN peer）—— ``spawned`` 和 ``adopted_local``
            已经在 boot 时被 ``_probe_service``（一次性 client）和
            ``_probe_instance``（原地 probe）warm 过了。
          * 只 warm ``status="running"`` —— warm 一个 not_loaded instance
            会强制 worker 提前加载。
        """
        warmed = 0
        failed = 0
        skipped = 0
        for inst in list(self._instances.values()):
            if inst._source != "adopted":
                skipped += 1
                continue
            if inst.rpc_client is None:
                skipped += 1
                continue
            if inst.status != "running":
                skipped += 1
                continue
            try:
                t0 = time.time()
                reply = inst.rpc_client.call(
                    {"action": "info", "op": "info"}, timeout_ms=5000)
                dt_ms = (time.time() - t0) * 1000
                if reply.get("ok"):
                    LOG.info(
                        "compute_registry: pre-warmed %s (%.0fms, peer=%s)",
                        inst.model_id, dt_ms, inst._peer_ip)
                    warmed += 1
                else:
                    LOG.warning(
                        "compute_registry: pre-warm %s returned not-ok: %s",
                        inst.model_id, reply.get("error"))
                    failed += 1
            except Exception as e:  # noqa: BLE001
                LOG.warning(
                    "compute_registry: pre-warm %s raised: %s",
                    inst.model_id, e)
                failed += 1
        LOG.info(
            "compute_registry: pre-warm done (%d ok, %d failed, %d skipped)",
            warmed, failed, skipped)

    def _boot_bind_async(self, inst: "ModelInstance", npu_index: int,
                         *, reason: str) -> None:
        """Fire-and-forget ``bind_instance`` from boot context."""
        def _runner():
            try:
                self.bind_instance(inst, npu_index)
                LOG.info("boot-bind (%s): %s → npu=%d → status=%s",
                         reason, inst.model_id, npu_index, inst.status)
            except Exception as e:  # noqa: BLE001
                LOG.warning("boot-bind (%s) failed for %s @ npu=%d: %s",
                            reason, inst.model_id, npu_index, e)
        threading.Thread(target=_runner, name=f"boot-bind-{inst.model_id}",
                         daemon=True).start()

    def _worker_info(self, inst: ModelInstance) -> dict | None:
        """问 worker 自己现在什么状态（``info`` RPC）；问不到返回 None。

        主进程缓存的 ``inst.status`` 可能过期（``/proc`` 孤儿登记成
        ``adopted_local`` 时状态是硬编码的 ``not_loaded``）。
        """
        if inst.rpc_client is None:
            return None
        try:
            return inst.rpc_client.call(
                {"action": "info", "op": "info"}, timeout_ms=5000)
        except Exception:  # noqa: BLE001
            return None

    def _boot_spawn_async(self, model_id: str, npu_index: int) -> None:
        """Fire-and-forget spawn for one ``compute.autostart`` entry.

        The card travels to the worker as ``--npu``, so the model starts
        building itself on the worker's own eager-load thread. We do
        **not** follow up with ``bind_instance``: that would send a second
        ``load`` RPC for a model that is already building, and the two
        would race on the worker's session slot.

        ``_probe_instance`` is still started by the spawn path, so the
        instance's status walks ``not_loaded`` → ``loading`` →
        ``running`` on its own and the compute page renders the same
        countdown it always did.

        The autostart markers (``_autostart`` / ``_default_npu_index``)
        are stamped here rather than in the ``ensure_worker_for_model``
        path: that path is shared with ``POST /compute/instances``, where
        the user picked the card by hand and nothing is autostarted. Doing
        it on the returned instance keeps the two callers honest.

        ``allow_adopt`` 固定 False：autostart 声明的是「**本板**这张卡跑
        这个模型」，捡回 peer 的实例（哪怕它正在跑）也会让本板这张卡空着
        ——「借算力」是省卡优化，由 boot 循环单独判定，这里一律不 adopt。
        """
        def _runner():
            try:
                inst = self.ensure_worker_for_model(
                    model_id, allow_adopt=False, npu_index=npu_index)
                inst._autostart = "enabled"
                inst._default_npu_index = npu_index
                # 报 worker 命令行里**实际**的 --npu 值：ensure_worker_for_model
                # 也可能返回复用的实例，不能自称「spawned」。
                argv = [str(a) for a in
                        (getattr(getattr(inst, "proc", None), "args", None) or [])]
                i = argv.index("--npu") if "--npu" in argv else -1
                LOG.info("boot autostart: %s @ npu=%d → %s（worker %s）",
                         model_id, npu_index, inst.status,
                         "命令行带 --npu %s" % argv[i + 1]
                         if 0 <= i < len(argv) - 1
                         else "复用已绑定 npu=%s 的实例" % inst.npu_index)
                # 兜底：复用到的 worker（/proc 孤儿 → adopted_local）没带 --npu，
                # 不会自己加载 —— 补一次 load RPC。判据见 _autostart_needs_load。
                if "--npu" not in argv and inst._source != "adopted":
                    info = self._worker_info(inst)
                    if _autostart_needs_load(argv, (info or {}).get("status")):
                        LOG.info("boot autostart: %s 复用的 worker 没带 --npu"
                                 "（%s），补发 load 让它加载到 npu=%d",
                                 model_id, inst._source, npu_index)
                        self.bind_instance(inst, npu_index)
            except Exception as e:  # noqa: BLE001
                LOG.warning("boot autostart failed for %s @ npu=%d: %s",
                            model_id, npu_index, e)
        threading.Thread(target=_runner,
                         name=f"boot-autostart-{model_id}",
                         daemon=True).start()

    def _discover_additional_peers(self) -> None:
        """v0.8.24: 后台扫 LAN peer,补 boot probe 漏掉的 NETWORK 卡。

        设计意图:NETWORK 卡显示"我能不能从 LAN 调到这台 model",LOCAL
        卡显示"这台 model 在我本机跑没跑"。两个独立维度 — 即使本机在跑,
        LAN 上其他板子也跑同一 model 的话,NETWORK 卡也应该显示那个 peer
        (只读、不能 bind,仅作可见性 + 状态参考)。
        """
        # 让 FastDDS SEDP 收敛 + boot probe 跑完。
        time.sleep(8.0)
        local_ip = _get_local_ip()
        if not local_ip:
            return
        for model_id in MODEL_PRESETS.keys():
            with self._lock:
                # v0.8.27: 跟 _maybe_adopt_from_peer 同——只 skip 已 adopted
                # (远端),adopted_local(/proc orphan)不应阻止。
                if any(i.model_id == model_id and i._source == "adopted"
                       for i in self._instances.values()):
                    continue
            peer_info = None
            for retry in range(5):
                try:
                    info = self._probe_service(model_id)
                except Exception:
                    info = None
                if info is None or info.get("status") == "error":
                    time.sleep(2.0)
                    continue
                info_ip = (info.get("peer_ip") or "").strip()
                if info_ip and info_ip == local_ip:
                    time.sleep(1.0)
                    continue
                peer_info = info
                break
            if peer_info is None:
                continue
            try:
                inst = self._build_adopted_from_probe(peer_info, model_id)
                # v0.8.28: 同 worker dedup — peer-discovery daemon 反复跑
                # 时防止 insert 多次。
                existing_d = self._find_inst_by_worker_pid(
                    model_id, inst._peer_pid)
                if existing_d is not None:
                    self._upsert_adopted_inst(
                        existing_d, peer_info, model_id,
                        inst._peer_name, peer_info.get("peer_ip", ""),
                        inst.npu_index)
                else:
                    with self._lock:
                        self._instances[inst.key] = inst
                    probe_t = threading.Thread(
                        target=self._probe_instance,
                        args=(inst,),
                        name=f"compute-probe-peer-discovery-{model_id}",
                        daemon=True,
                    )
                    inst._probe_thread = probe_t
                    probe_t.start()
                LOG.info("v0.8.24 peer-discovery: found LAN peer for %s "
                         "(peer_ip=%s status=%s)", model_id,
                         peer_info.get("peer_ip"),
                         peer_info.get("status"))
            except Exception as e:  # noqa: BLE001
                LOG.warning("v0.8.24 peer-discovery register failed for %s: %s",
                            model_id, e)

    def _build_adopted_from_probe(self, info: dict,
                                  model_id: str) -> "ModelInstance":
        """v0.8.24: 把 probe 返回的 dict 包装成 ModelInstance (source=adopted)。

        与 :meth:`_adopt_existing_worker` 共用字段抽取 + IP/hostname 比较
        分类的逻辑,但永远强制 source="adopted" — 调用方已经确认是 LAN peer
        (跳过了本机响应),不重复做 IP 比较。
        """
        preset = MODEL_PRESETS[model_id]
        worker_npu = info.get("npu_index")
        worker_pid = info.get("pid")
        worker_host = (info.get("hostname") or "").strip()
        worker_device = (info.get("device_name") or "").strip()
        if worker_device:
            peer_name = worker_device
        elif worker_host:
            peer_name = worker_host
        else:
            peer_name = "远端"
        inst = ModelInstance(
            key=(-1, model_id, "adopted"),
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
            available_image_sizes=_coerce_image_sizes(
                info.get("available_image_sizes")),
            _source="adopted",
            _peer_guid=info.get("_peer_guid", ""),
            _peer_name=peer_name,
            _peer_ip=info.get("peer_ip", ""),
        )
        if isinstance(worker_pid, int):
            inst._peer_pid = worker_pid
        return inst

    def _cleanup_unregistered_workers(self) -> int:
        """Boot-time defensive sweep.

        对每个在 ``config.compute.autostart`` 列出的 model 调
        :func:`_find_workers_by_name`（先按 comm、再 fallback cmdline
        兼容老 worker），扫出所有同名进程：
          * 已经在 registry 里的（spawned 的 proc.pid / adopted_local
            的 _peer_pid）→ 保留；
          * 剩下的（不在 registry 但 comm/cmdline 匹配）→ 视为孤儿，
            SIGTERM → 5s grace → SIGKILL。

        对**不在** autostart 里的 model_id 的孤儿 worker 也会扫（用
        ``MODEL_PRESETS`` 全集）—— 用户从 autostart 删除某个 model
        后，那个孤儿进程应该被回收，不能继续吃 NPU 内存。

        Returns the count of PIDs killed.
        """
        try:
            # autostart entries are objects, so take the ``model`` field;
            # a raw set() over the list would raise on unhashable dicts
            # and silently degrade ``configured`` to empty.
            configured = {
                entry["model"]
                for entry in get_config()
                .get_section("compute", {})
                .get("autostart", [])
                if isinstance(entry, dict) and isinstance(
                    entry.get("model"), str)
            }
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

        # 扫的范围：autostart 里的 + preset 全集（处理 user 从
        # autostart 删掉的孤儿）。preset 里没的 model_id 不会被
        # _find_workers_by_name 误识（needle 不匹配），所以多扫也安全。
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
                        "config.compute.autostart; killing",
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
        # The shared side-channel participant outlives every session, so
        # no ``LLMRPCSession.close()`` will release it — it is ours.
        try:
            if self._side_channel_hub is not None:
                self._side_channel_hub.close()
                self._side_channel_hub = None
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # Boot                                                              #
    # ------------------------------------------------------------------ #
    # Note: boot 不再自动 spawn worker。实例 (npu, model) 由用户在算力
    # 配置页点击启动时通过 ``POST /api/compute/instances`` 指定，npu_index
    # 作为命令行 ``--npu <idx>`` 透传给子进程，不写 config.jsonc；主进程
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
        LOG.debug(
            "compute_registry: _probe_instance enter model=%s source=%s "
            "status=%s thread=%s",
            inst.model_id, inst._source, inst.status, threading.get_ident())
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
                    gen_at_call = inst._probe_gen  # 见 _probe_gen 注释
                    with inst._probe_lock:
                        if inst.rpc_client is None:
                            continue
                        try:
                            r = inst.rpc_client.call(
                                {"action": "info", "op": "info"}, timeout_ms=5000)
                        except Exception as e:
                            consecutive_failures += 1
                            LOG.warning("info call failed for %s (%d): %s",
                                       inst.model_id, consecutive_failures, e)
                            # BrokenPipe 后 cshim 端 endpoint 已坏;重建 RpcClient
                            # 让 cshim 重新走 endpoint 发现,可能命中别的健康 peer。
                            old_svc = getattr(inst, "_svc", None)
                            old_client = inst.rpc_client
                            try:
                                old_client.destroy()
                            except Exception:
                                pass
                            # Drop the dead Requester from the Service's
                            # tracking list BEFORE re-allocating, otherwise
                            # ``Service._requesters`` grows unbounded on
                            # every consecutive-failure rebuild and the
                            # wedged cshim handle stays pinned in memory.
                            if old_svc is not None:
                                try:
                                    old_svc.remove_requester(old_client)
                                except Exception:
                                    pass
                            # v0.14.3: also destroy the old Service so the
                            # cshim payload pool doesn't pin in hub._services
                            # on every consecutive-failure rebuild. The new
                            # ``_setup_rpc_client`` call will create a fresh
                            # Service with the same name. adopted (LAN peer)
                            # is excluded — see _teardown_rpc_client for why.
                            if old_svc is not None and inst._source != "adopted":
                                try:
                                    svc_name = old_svc.name
                                    hub = self._hub
                                    if hub is not None:
                                        hub.destroy_service(svc_name)
                                except Exception as svc_e:  # noqa: BLE001
                                    LOG.warning(
                                        "probe-rebuild: hub.destroy_service"
                                        "(%s) raised: %s",
                                        inst.model_id, svc_e)
                            try:
                                self._setup_rpc_client(inst)
                                LOG.info("rebuilt rpc_client for %s after "
                                         "%d consecutive failures",
                                         inst.model_id, consecutive_failures)
                            except Exception as setup_e:
                                LOG.warning("rpc_client rebuild failed for "
                                            "%s: %s",
                                            inst.model_id, setup_e)
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
                    # 失效检测:unload/stop 在我们 in-flight 期间 bump 了 gen,
                    # ``r`` 陈旧,跳过 status 写回（保留 unload 乐观写的 "not_loaded"）。
                    if inst._probe_gen != gen_at_call:
                        LOG.debug(
                            "compute_registry: _probe_instance %s "
                            "skipping stale r (gen %d→%d during in-flight)",
                            inst.model_id, gen_at_call, inst._probe_gen)
                        continue
                    data = r.get("data") or {}
                    worker_status = data.get("status", "not_loaded")
                    # 每轮都把 worker 报告的 model_dir 缓存到实例上——
                    # missing_model_dir 状态时 UI 直接拿这个字段渲染路径。
                    if isinstance(data.get("model_dir"), str) and data["model_dir"]:
                        inst.model_dir = data["model_dir"]
                    # Worker 报告的 vision-model native sizes 也缓存到
                    # 实例上 —— 音视频描述页通过这个字段动态填充
                    # ``#vd-detect-size`` 下拉框，不在客户端硬编码
                    # 384/768。Empty list for non-vision backends
                    # (text / TTS); non-empty for VLM / embedding
                    # once the session reaches ``running``.
                    cleaned_sizes = _coerce_image_sizes(
                        data.get("available_image_sizes"))
                    if cleaned_sizes != inst.available_image_sizes:
                        with self._lock:
                            inst.available_image_sizes = cleaned_sizes
                    if worker_status == "running":
                        with self._lock:
                            inst.status = "running"
                            worker_npu = data.get("npu_index")
                            if (isinstance(worker_npu, int)
                                    and worker_npu >= 0
                                    and inst.npu_index != worker_npu):
                                self._instances.pop(inst.key, None)
                                inst.npu_index = worker_npu
                                inst.key = (worker_npu, inst.model_id, inst._source)
                                self._instances[inst.key] = inst
                            inst.finished_at = data.get(
                                "loaded_at", time.time())
                            inst.load_time_sec = data.get("load_time_sec", 0.0)
                            inst.loaded_at = inst.finished_at
                            inst.error = ""
                            if not inst._logged_running:
                                inst._logged_running = True
                                LOG.info(
                                    "instance %s @ npu=%d → running (load=%.1fs)",
                                    inst.model_id, inst.npu_index,
                                    inst.load_time_sec)
                        # 不 return — 继续 poll + drain stderr，保留对 running
                        # worker 的可见性（embed_video 错误/NPU 卡死不会显示）
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
                            # ``missing_model_dir`` deliberately absent
                            # from the sticky set: the worker's info_handler
                            # self-heals back to ``not_loaded`` once the
                            # directory reappears, so we want the next
                            # probe to converge. Other terminal states
                            # (running / loading / error) stay untouched.
                            if inst.status not in {"not_loaded", "loading",
                                                    "error"}:
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

                tail = _drain_worker_stderr(inst.proc)
                if tail:
                    for line in tail.splitlines():
                        if line.strip():
                            LOG.info("compute_worker[%s]: %s",
                                     inst.model_id, line)
                time.sleep(1.0)
        except Exception as e:
            LOG.exception("probe_instance crashed")
            with self._lock:
                inst.status = "error"
                inst.error = f"probe crashed: {type(e).__name__}: {e}"
        finally:
            LOG.debug(
                "compute_registry: _probe_instance exit model=%s "
                "final_status=%s thread=%s",
                inst.model_id, inst.status, threading.get_ident())

    def _persist(self) -> None:
        """No-op stub kept for backward compat with older call sites.

        ``config.jsonc["compute"]["instances"]`` 不再被 registry 维护——
        实例 (npu, model) 由用户在算力配置页点击启动时通过
        ``POST /api/compute/instances`` 指定并作为子进程命令行
        （``--npu <idx>``）传入，不持久化。
        """
        return

    # ------------------------------------------------------------------ #
    # 消费者 API                                                         #
    # ------------------------------------------------------------------ #
    def list_sessions(self, capability_type: str, *,
                       include_remote: bool = False,
                       model_id: Optional[str] = None) -> List[SessionHandle]:
        """Every running endpoint exposing ``capability_type``.

        This is the enumeration a consumer should reach for when it needs
        to *choose* a card rather than accept whatever is first: the same
        model may be loaded on card 0 and card 1 at the same time, and a
        LAN peer may additionally be serving it.

        ``include_remote=False`` (the default) returns **本机** instances
        only — ``spawned`` / ``adopted_local``. Video consumers pass
        frames over ``/dev/shm`` path lists, which a peer on another
        machine cannot read, so they must not silently land on a remote
        endpoint. Pass ``include_remote=True`` when base64-over-the-wire
        frames are acceptable (and you want the peer's capacity).

        Ordering: 本机优先 (``spawned`` → ``adopted_local``), then ascending
        ``npu_index``; remote peers last. Instances whose proxy failed to
        build are skipped — a half-usable handle is worse than a short
        list.

        Returns ``[]`` when nothing matches; callers that need the old
        raise-on-empty behaviour should use :meth:`get_session`.
        """
        local_first = {"spawned": 0, "adopted_local": 1, "adopted": 2}
        with self._lock:
            candidates = [
                inst for inst in self._instances.values()
                if inst.status == "running"
                and capability_type in inst.capability_types
                and (model_id is None or inst.model_id == model_id)
                and inst.rpc_session_proxy is not None
                and (include_remote
                     or inst._source in ("spawned", "adopted_local"))
            ]
        candidates.sort(key=lambda i: (local_first.get(i._source, 9),
                                       i.npu_index))
        out: List[SessionHandle] = []
        for inst in candidates:
            pid = 0
            if inst.proc is not None and inst.proc.pid:
                pid = inst.proc.pid
            elif isinstance(inst._peer_pid, int):
                pid = inst._peer_pid
            out.append(SessionHandle(
                model_id=inst.model_id,
                npu_index=inst.npu_index,
                source=inst._source,
                pid=pid,
                is_remote=(inst._source == "adopted"),
                session=inst.rpc_session_proxy,
            ))
        return out

    def get_session(self, capability_type: str,
                        npu_index: Optional[int] = None) -> Any:
        """Return the ``LLMRPCSession`` of one running instance exposing
        ``capability_type``. Raises RuntimeError if none.

        本机优先：默认只在 spawned / adopted_local 里挑（视频类消费方
        （VR / wechat_video_manager）走 ``/dev/shm`` path-list 传帧，
        跨机 Worker 读不到本机 tmpfs，命中远端会逐片 imread 失败；本机
        NPU 延迟也远低于 LAN RPC）。无本机实例时才退回 LAN peer。

        ``npu_index`` 用于指名某张卡 —— 同一个模型跑在两张卡上时，不传
        就是"取第一个"（本机优先 + 卡号升序，确定但不是你选的）；需要
        让用户选卡请改用 :meth:`list_sessions`。
        """
        handles = self.list_sessions(capability_type, include_remote=True)
        if npu_index is not None:
            handles = [h for h in handles if h.npu_index == npu_index]
        if not handles:
            raise RuntimeError(f"功能未启用: {capability_type}")
        chosen = handles[0]
        LOG.info(
            "get_session(%s): %s instance %s %s (npu=%s pid=%s)",
            capability_type,
            "REMOTE" if chosen.is_remote else "LOCAL",
            chosen.model_id, chosen.source, chosen.npu_index, chosen.pid)
        return chosen.session

    def get_session_for_model(self, capability_type: str, model_id: str,
                               npu_index: Optional[int] = None) -> Any:
        """Return the ``LLMRPCSession`` for one running instance matching
        ``capability_type`` **and** ``model_id`` (optionally pinned to a
        card).

        Used by the consumer pages (音视频描述 / 视频目标检测 / 音频理解)
        after the user picks one entry from the instance dropdown.

        ``npu_index`` names the card when the same model is loaded on
        several of them; without it the first 本机 instance wins
        (本机优先, then ascending 卡号). When you need to present the
        choices rather than pick one, enumerate with :meth:`list_sessions`.

        Raises ``RuntimeError`` if no running instance matches.
        """
        handles = self.list_sessions(capability_type,
                                         include_remote=True,
                                         model_id=model_id)
        if npu_index is not None:
            handles = [h for h in handles if h.npu_index == npu_index]
        if not handles:
            raise RuntimeError(
                f"功能未启用: {capability_type} (model_id={model_id!r} "
                f"未在 running 实例中找到)")
        chosen = handles[0]
        LOG.info(
            "get_session_for_model(%s, %s, npu=%s): %s instance %s (pid=%s)",
            capability_type, model_id, npu_index,
            "REMOTE" if chosen.is_remote else "LOCAL",
            chosen.source, chosen.pid)
        return chosen.session

    def _list_running_covering(self, wanted: set[str]) -> list[dict]:
        """Running instances whose ``capability_types`` contain **all** of ``wanted``.

        One pass, one body — ``list_running_by_capability`` and
        ``list_running_by_capabilities`` are the same query with one or many
        requirements. Local-first ordering (spawned + adopted_local before
        adopted) matches ``get_session`` semantics so a dropdown's default is
        the instance the user would have got without asking. Hidden-by-config
        entries are filtered out so the dropdown never surfaces something the
        boot path won't start.
        """
        if not wanted:
            # "No requirements" is not "any instance qualifies". Returning
            # everything here would make a misconfigured group look ready.
            return []
        hidden_set: set[str] = set()
        try:
            stored = get_config().get_section("compute", {})
            if isinstance(stored, dict):
                raw = stored.get("services")
                if isinstance(raw, list):
                    for e in raw:
                        if (isinstance(e, dict)
                                and isinstance(e.get("name"), str)
                                and e.get("autostart") == "hidden"):
                            hidden_set.add(e["name"])
        except Exception:  # noqa: BLE001
            pass

        def _source_rank(src: str) -> int:
            return 0 if src in ("spawned", "adopted_local") else 1

        out: list[dict] = []
        with self._lock:
            for inst in self._instances.values():
                if (inst.status != "running"
                        or inst.model_id in hidden_set):
                    continue
                if not wanted.issubset(set(inst.capability_types)):
                    continue
                pid_val = (inst._peer_pid if isinstance(inst._peer_pid, int)
                           else getattr(inst.proc, "pid", None))
                out.append({
                    "model_id":        inst.model_id,
                    "label":           inst.instance_name,
                    "source":          inst._source or "spawned",
                    "npu_index":       inst.npu_index,
                    "pid":             pid_val or 0,
                    "memory_estimate_mb": inst.memory_estimate_mb,
                    "capability_types": list(inst.capability_types),
                    "_rank":           _source_rank(inst._source or ""),
                })
        out.sort(key=lambda x: (x["_rank"], x["model_id"]))
        for e in out:
            e.pop("_rank", None)
        return out

    def list_running_by_capability(self, capability_type: str) -> list[dict]:
        """Return one entry per running instance exposing ``capability_type``.

        Powers the consumer-page "model dropdown": each entry carries the
        minimum fields the UI needs to render an option + submit a choice
        back.  See :meth:`_list_running_covering` for ordering and the
        hidden-set rule.
        """
        return self._list_running_covering({capability_type})

    def list_running_by_capabilities(self, capabilities: List[str]) -> list[dict]:
        """Running instances exposing **all** of ``capabilities`` (AND).

        This is the query behind a card's *requirement group*, and the reason
        :meth:`list_running_by_capability` alone was not enough. When a card
        needs several capabilities from **one** model (usb_hid needs both
        vision and tool calling), asking per-capability and intersecting
        client-side cannot tell "one instance covers both" from "two instances
        each cover half" — the second case passes a naive per-capability check
        and then fails at inference time.

        Order and duplicates in ``capabilities`` are irrelevant.
        """
        return self._list_running_covering(set(capabilities))

    # ------------------------------------------------------------------ #
    # active_app：用户当前所在的"卡片模式"                              #
    #                                                                    #
    # 用途：当用户进入一个 demo 卡片、且其所有 software_capabilities      #
    # 都已 running 时，前端把 app_id 写到这里的 ``compute.active_app``。  #
    # 下次开机 / 重启时：                                                #
    #   * 浏览器拉 ``GET /api/compute/active-app`` → 前端直接跳转到该    #
    #     卡片页（无需再走一次 wizard）。                                  #
    #   * 必要时 ``set_active_app`` 会把当前承载该 app 的本地 (model,    #
    #     npu_index) 增量写进 ``compute.autostart``，让 worker 提前     #
    #     spawn —— 否则 reboot 后 30~60s 的 NPU 加载会让用户以为"卡     #
    #     住了"。                                                         #
    #                                                                    #
    # 故意不耦合 manifest：manifest 的 software_capabilities 由 API      #
    # 层做查找与传入，registry 只负责"按给的 caps 校验 + 落盘"。         #
    # ------------------------------------------------------------------ #

    def get_active_app(self) -> Optional[str]:
        """读取 ``compute.active_app`` —— 用户最近一次进入的卡片 id。

        缺省 / 非字符串 → ``None``。**不**触发任何启动 / 校验。
        """
        try:
            section = get_config().get_section("compute", {}) or {}
        except Exception:  # noqa: BLE001
            return None
        v = section.get("active_app") if isinstance(section, dict) else None
        return v if isinstance(v, str) and v else None

    def set_active_app(
        self,
        app_id: str,
        required_capabilities: list[str],
        required_groups: Optional[List[List[str]]] = None,
    ) -> dict:
        """记录 ``compute.active_app = app_id``，并按需扩展 autostart。

        Parameters
        ----------
        app_id:
            卡片 id（必须非空字符串）。调用方负责保证它对应一张
            ``status="ok"`` 的 manifest —— registry 不耦合 manifest。
        required_capabilities:
            该 app 声明的 ``software_capabilities``。会逐个检查是否
            至少有一个 **local**（spawned ∪ adopted_local）running 实例
            提供该能力 —— 不接受远端 adopted（peer 那台板子 reboot
            后就没了，留 autostart 等于留个空头）。
        required_groups:
            可选的**能力组**：每个组必须由**单个** local running 实例
            同时满足（见 :func:`_normalize_capability_groups`）。缺省时
            每个能力各自成组，即历史上「逐能力各找一个实例」的语义 ——
            那正是同组能力分散在两个模型时会误判就绪的原因，所以卡片
            声明了 ``group`` 的场合必须传进来。
        Returns
        -------
        dict
            ``{"ok": True, "active_app": app_id,
                "autostart": [...], "autostart_extended": [...],
                "autostart_replaced": [...]}``
            其中 ``autostart_extended`` 列出了本次新增的 (model,
            npu_index) —— 方便前端 / 运维看出"这次开启"实际多挂了几张
            卡的 worker；``autostart_replaced`` 列出被替换掉的同卡旧条目
            （维护「一卡一模型」不变量）。
        Raises
        ------
        ValueError
            * ``app_id`` 不是非空字符串
            * ``required_capabilities`` 里有不在 ``MODEL_PRESETS`` 注册
              表中的 capability（用户拼错 / 老 manifest 残留字段）
            * 任一能力组当前**没有单个** local running 实例同时满足
              （意味着 wizard 还没跑完 / 模型还没加载，贸然记下"active"
              等于自我欺骗）。
        """
        if not isinstance(app_id, str) or not app_id:
            raise ValueError("app_id 必须是非空字符串")

        # 1. 校验 capability 拼写：所有 req 必须在 MODEL_PRESETS 至少一
        #    个 preset 的 capability_types 里出现。否则就是用户 manifest
        #    拼错 / 老字段。
        registered_caps: set[str] = set()
        for preset in MODEL_PRESETS.values():
            for c in (preset.get("capability_types") or []):
                registered_caps.add(c)
        unknown = [
            c for c in required_capabilities
            if c not in registered_caps
        ]
        if unknown:
            raise ValueError(
                "未注册的 software_capability: "
                + ", ".join(sorted(unknown))
                + "（请检查 manifest 的 software_capabilities 字段）"
            )

        # 2. 每个**组**必须由 local running 实例满足，且组内全部能力
        #    由**同一个**实例承载。local = spawned ∪ adopted_local ——
        #    adopted (LAN peer) 故意排除：那台板子随时可能掉电，留个
        #    autostart 进去下次启动等于啥也没起来。
        #
        #    两个维度都要，缺一不可：
        #
        #    (a) 按组而非逐能力。逐能力找会让 cap A 落在模型 X、
        #        cap B 落在模型 Y 时**误判就绪**——两个实例各满足
        #        一半，逐项检查全绿，但那台卡真正需要的是「同一个模型
        #        同时具备 A 和 B」。这种假就绪写进 active_app 后，
        #        用户进卡才发现功能不能用。
        #    (b) 一个 cap 可以对应**多个**实例。同一个模型跑两张卡时，
        #        两张卡的实例都承载这些 capability，两张都要记进
        #        autostart —— 只记一张的话下次开机另一张卡空着
        #        （这是 d144747 修的，不能在这次合并里丢掉）。
        with self._lock:
            local_running = [
                inst for inst in self._instances.values()
                if inst.status == "running"
                and inst._source in ("spawned", "adopted_local")
            ]

        groups = _normalize_capability_groups(required_capabilities, required_groups)
        cap_to_local: dict[str, list["ModelInstance"]] = {}
        unsatisfied: list[str] = []
        for group in groups:
            chosen = [i for i in local_running
                      if set(group).issubset(set(i.capability_types))]
            if not chosen:
                unsatisfied.append(" + ".join(group))
                continue
            for cap in group:
                bucket = cap_to_local.setdefault(cap, [])
                seen = {i.key for i in bucket}
                for inst in chosen:
                    # 按实例 key 去重：两个组含同一 cap 时不能把同一个
                    # 实例记两次，否则 autostart 里出现重复条目
                    if inst.key not in seen:
                        bucket.append(inst)
                        seen.add(inst.key)
        if unsatisfied:
            raise ValueError(
                "以下能力组当前没有单个 local running 实例同时满足: "
                + ", ".join(unsatisfied)
                + " —— 等模型加载完再进入此 app，或先在算力页启用对应模型"
                + ("（同组能力必须由同一个模型提供）" if any(
                    len(g) > 1 for g in groups) else "")
            )

        # 3. 落盘：active_app + 增量扩展 autostart。任一失败抛 OSError
        #    让上层 HTTP 层 502 透传。
        try:
            section = get_config().get_section("compute", {}) or {}
        except Exception:  # noqa: BLE001
            section = {}
        if not isinstance(section, dict):
            section = {}

        prev_autostart = _parse_autostart_config(
            section.get("autostart") if isinstance(section, dict) else None
        )
        extended: list[dict] = []
        replaced: list[dict] = []
        new_autostart = prev_autostart
        for cap, insts in cap_to_local.items():
            # 同一个模型跑两张卡时两张卡都要记 —— 只记一张的话下次开机另一张卡空着。
            for inst in insts:
                # 维护「一卡一模型」不变量 —— 同卡的旧条目是陈旧的，替换掉
                # （理由见 _merge_autostart_entry）。
                new_autostart, added, stale = _merge_autostart_entry(
                    new_autostart, inst.model_id, inst.npu_index)
                replaced.extend(stale)
                for e in stale:
                    LOG.info("compute.active_app=%s: autostart 移除同卡旧条目 "
                             "%s @ npu%s（该卡现在跑 %s）",
                             app_id, e["model"], e["npu_index"], inst.model_id)
                if not added:
                    continue
                extended.append({"capability": cap,
                                 "model": inst.model_id,
                                 "npu_index": inst.npu_index})

        section["autostart"] = new_autostart
        section["active_app"] = app_id
        get_config().set_section("compute", section)

        if extended:
            LOG.info(
                "compute.active_app=%s：autostart 新增 %d 条 —— %s",
                app_id, len(extended), extended,
            )
        else:
            LOG.info("compute.active_app=%s：autostart 无需扩展", app_id)

        return {
            "ok": True,
            "active_app": app_id,
            "autostart": list(new_autostart),
            "autostart_extended": extended,
            # 同卡旧条目被替换掉的那些（维护「一卡一模型」不变量）。
            "autostart_replaced": replaced,
        }

    def clear_active_app(self) -> None:
        """清除 ``compute.active_app`` **和** ``compute.autostart``。

        用户显式"退出应用"（back-home 按钮）时调用，语义是"我不在这张卡
        的模式里了，也不想让这些算力下次开机自己起来"。

        为什么要连 autostart 一起清
        ----------------------------
        旧行为只清 ``active_app``、**故意**留着 autostart，理由是
        "autostart 负责该启的算力，由用户在算力页显式关闭"。但配合
        :meth:`set_active_app` 的"进入卡片就增量追加 autostart"，结果是
        autostart **只增不减**：每进一张卡就多一条，永远没人删。实测
        跑过几张视频/音频卡之后 config.jsonc 里攒到 5 条、npu0 挤了 3 个
        模型（估算 12500 MB），``qwen3-vl-embedding-2b`` 直接以 rc=-6
        （NPU 分配失败）起不来，而"去算力页手动关"这一步没人会记得做。

        现在退出即清空，autostart 回到"本次会话用到的算力"这个语义：
        进卡 → 追加（中途重启能把本会话需要的模型拉回来）；
        退出 → 全清（下次开机不自动起，用户要在算力页自己开）。

        幂等：``active_app`` 和 ``autostart`` 都已经是干净的就直接返回，
        省一次 fs write（back-home 可以被连点）。
        """
        try:
            section = get_config().get_section("compute", {}) or {}
        except Exception:  # noqa: BLE001
            return
        if not isinstance(section, dict):
            return
        had_app = "active_app" in section
        prev_autostart = section.get("autostart")
        # 只有"非空 list"才算需要清 —— 缺字段 / None / 已是 [] 都不动，
        # 避免把 autostart 从"字段不存在"改成"显式空 list"这种无意义 diff。
        had_autostart = isinstance(prev_autostart, list) and bool(prev_autostart)
        if not had_app and not had_autostart:
            return  # 已经是干净的，省一次 fs write
        section.pop("active_app", None)
        section["autostart"] = []
        try:
            get_config().set_section("compute", section)
        except Exception as e:  # noqa: BLE001
            LOG.warning("clear_active_app persist failed: %s", e)
            return
        n = len(prev_autostart) if isinstance(prev_autostart, list) else 0
        LOG.info("compute.active_app 已清空；compute.autostart 已清空（%d 条）", n)


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
# instance). This module-level lock is kept so existing call sites             #
# (video_retrieval/, video_search.py, audio_analysis.py) keep    #
# compiling. It is a no-op in practice because nothing else in the main        #
# process ever acquires it.                                                    #
# --------------------------------------------------------------------------- #
_call_lock = threading.Lock()


def get_session_call_lock() -> threading.Lock:
    """Legacy no-op call lock. Returns a reusable ``threading.Lock``
    so call sites can use it in multiple ``with`` blocks (e.g. inside
    a loop) without draining a single-use ``@contextmanager``."""
    return _call_lock


# DDS TTS 音频分片 topic 订阅（LAN 流式，plan tts-streaming-lan）
def subscribe_tts_chunk_topic(
    participant: Any,
    model_id: str,
    callback: "Callable[[bytes], None]",
) -> Any:
    """给 worker TTS 分片 topic 起 cshim-backed DDS reader。

    每片 payload 线缆格式与本地 UDS 一致：
        struct.pack("<III", sr, ch, n) + n * ch * 4 bytes float32 LE PCM

    callback 在 cshim daemon 线程里触发（wdndds 内部托管），保持短小。
    返回的 TopicReader.close() 会 join pump 线程并拆 DataReader；
    调用方必须持有 handle 并清理时关闭 —— Hub 的 DomainParticipant
    跨 worker 生命周期常驻，reader 泄漏会叠加。
    """
    from wdndds.dds import create_topic_reader
    topic_name = f"wdndds/audio/tts_chunk/{model_id}"
    return create_topic_reader(participant, topic_name, callback)
