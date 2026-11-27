"""数字人卡片的服务层 —— 消费算力页上的「数字人」算力实例。

架构（2026-11-26 起）
=====================

数字人**不再自带引擎进程**，也不再自己管模型生命周期 —— 它是**算力页上的
一张卡**：

.. code-block:: text

    算力页启用「数字人 LiteAvatar 渲染」
      → compute worker 进程（wdnai 的 AvatarBackend，跑在板载 RK3588 NPU 上）
      → wdndds RPC 动词 ``avatar_render``
      → 本卡片：get_session("swcap_avatar_render") → render_avatar(...)

模型生命周期（加载 / 卸载 / 进程 / 显存）全部由 compute 层管；卡片只管
「把音频交给它，把帧序列接回来」。

零 torch
========

整条链路不需要 PyTorch：参考帧编码器（``net_encode``）与 AdaIN 调制投影
（``bs_proj``）都已是 numpy / OpenCV 等价实现（见 ``wdnai.avatar``），
decoder 一直在板载 NPU 上。

⚠️ **帧是边渲边落盘的**，而且**音频是边合成边吐给浏览器的**
================================================================

两条边信道：

1. **帧**：worker 把 JPEG 直接写进作业的 ``frames/`` 目录（``out_dir``），
   主进程随时能数出「已经渲好多少帧」⇒ 前端不用等整段渲完。
2. **音频**：流式作业里卡片自己驱动 TTS，PCM 一到就攒进作业缓冲，
   ``GET /jobs/{id}/audio`` 是个**长度未知的 chunked WAV** ⇒ 浏览器一边
   下载一边放。

⇒ 「合成」和「渲染」**并行**（还跑在两块 NPU 上：TTS 在 EP 协卡、渲染在
板载 NPU），所以台词再长也不用等合成完才开播。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import struct
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from app.apps.digital_human.avatars import AvatarError, get_registry
from app.core.uploads import get_card_uploads_root

LOG = logging.getLogger("apps.digital_human.service")

#: 嘴型参数的帧率（与 a2m 模型一致：30 帧/秒）
FPS = 30
#: **单段音频的时长上限**（秒）。
#:
#: 嘴型模型的 fbank 前端是**定长桶** —— ``fbank_spec_b160000_fp16.rknn``
#: 一共只接 160000 个采样（@16 kHz ≈ 10 秒）。超过就报「音频太长」。
#:
#: 所以超过上限的音频要**切段渲**：按 9 秒切（留 1 秒余量给重采样和边界），
#: 段与段之间靠 ``frame_offset`` 让帧号连续 ⇒ 流式渲染不受影响。
SEGMENT_SEC = 9.0
#: 流式模式的**单段上限**（秒）—— fbank 的定长桶是 10 秒，留点余量。
#:
#: 实际分段长度**不由这里决定**，而是按 TTS 音频块的边界切（TTS 一窗 4 秒）
#: —— 切点跟窗口对齐，渲染就不用空等下一个窗口。
MAX_SEGMENT_SEC = 9.0
#: 流式模式下**除第一段外**每段攒到多长（秒）。
#:
#: 特征前端（fbank + encoder）是**定长桶**：不管段多长，一次推理都是固定的
#: ~0.52 s。所以段越短，这笔固定开销摊到每��秒音频上就越多：
#:
#: * 每块就渲（TTS 的块约 2 秒）⇒ 0.26 s/音频秒（渲染总耗时 0.90 s/音频秒，
#:   只比 TTS 快 3%，余量很薄）；
#: * 攒到 ~4 秒（两个块）⇒ 0.13 s/音频秒（渲染总耗时 0.78 s/音频秒，
#:   比 TTS 快 16%，余量厚）。
#:
#: ⚠️ 目标要**可达**：TTS 的块约 1.98 秒，目标写 4 秒的话永远差一点
#: （要等 3 个块 = 5.9 秒），段就被拖长了 ⇒ 目标取 3 秒（两个块 = 3.96 秒）。
#:
#: 第一段例外 —— 它按 TTS 的**第一个块**切（latency 优先，拿到就渲）。
MIN_SEGMENT_SEC = 3.0
#: 开播前至少要渲好多少秒的画面（缓冲余量）。
#:
#: 帧是**成段**出来的：一段先花 ~0.9 s 算特征+嘴型，然后才逐帧出图。这段
#: "空档"里画面不前进、声音一直在走 ⇒ 缓冲要盖住它：
#:
#: * 段长 ~4 秒 ⇒ 空档约 1.5 s（实测缓冲只有 1 秒时，音频在段边界顿了两下）；
#: * 所以开播前要攒够 ~1.8 秒的画面，段边界才不会卡。
#:
#: 同时这也是「画面能追到音频前面」的保证：攒够 1.8 秒画面才开始放 ⇒
#: 后面每段渲染比 TTS 快 16% ⇒ 缓冲只会越来越大。
STREAM_START_BUFFER_SEC = 1.5
#: 流式模式的默认抽帧步长（3 = 嘴型 30 fps → 画面 10 fps）。
#:
#: 为什么不是 15 fps：渲染必须比 TTS（1:1 实时）快，画面才追得上声音。
#: 实测（``dh-diagnose/measure_segment_cost.py``，板载 NPU 实测）：
#:
#: ==========  =======  ==================  ==============
#: 分段          stride   渲染耗时/音频秒      开播滞后
#: ==========  =======  ==================  ==============
#: 3 s          2        1.077 s             —— 追不上
#: 3 s          3        0.801 s             3.8 s
#: 6 s          3        0.780 s             7.2 s
#: 9 s          3        0.712 s            10.3 s
#: ==========  =======  ==================  ==============
#:
#: ⇒ **stride 3（10 fps）**：渲染比 TTS 快 25%（追得上）。
#: 15 fps 在任何段长下都追不上（单帧 55 ms ⇒ 15 fps 就要 0.83 s/音频秒，
#: 加上每段固定的特征开销必然超过 1）。
STREAM_STRIDE = 3
#: 算力页上那张卡的 model id —— 与 ``models/Lite-Avatar/lite-avatar.json``
#: 的 ``id`` 一致（也就是 manifest 文件名的主干）。
COMPUTE_MODEL_ID = "lite-avatar"
#: 那张卡对外暴露的能力名 —— 卡片靠它拿到 RPC 会话。
COMPUTE_CAPABILITY = "swcap_avatar_render"


@dataclass
class Job:
    """一次「说话」作业的状态。"""

    id: str
    audio_path: Path
    work_dir: Path
    avatar_id: str = ""          # 本次作业用的形象 id
    avatar_name: str = ""        # 形象显示名
    stride: int = 1              # 渲染抽帧步长（2 = 15 fps）
    expected_frames: int = 0     # 预计总帧数（渲染途中就能给前端用）
    state: str = "pending"        # pending / running / done / error / stopped
    step: str = ""                # features / mouth / smooth / render
    error: str = ""
    frame_count: int = 0
    duration_s: float = 0.0
    fps: int = FPS
    #: 渲染出来的帧尺寸 —— 前端拿它把舞台调成形象的**真实比例**。
    width: int = 0
    height: int = 0
    timings: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    # ---- 流式模式（后端驱动 TTS，边合成边渲染） ---- #
    #: 是不是流式作业。
    streaming: bool = False
    #: TTS 已经合成完（收到 EOF 哨兵）。
    synth_done: bool = False
    #: 音频采样率 / 声道数（0 = 还没收到第一块音频）。
    audio_rate: int = 0
    audio_channels: int = 1
    #: 攒到的音频字节数（16 bit 整型 PCM）。
    pcm_bytes: int = 0
    #: 其中已经送去渲染的字节数。
    pcm_rendered: int = 0
    #: 攒到的音频（16 bit 整型 PCM 原始字节）—— 浏览器流式播放的就是它。
    pcm_buf: bytearray = field(default_factory=bytearray, repr=False)
    #: 每个 TTS 音频块的结束偏移（字节）—— **分段就按它切**。
    #:
    #: TTS 的解码窗口是**定长 4 秒**（SDK 一窗 96000 采样 @24 kHz）。如果按
    #: 自己的固定长度切段，切点跟窗口对不齐 ⇒ 渲染要空等下一个窗口才开工
    #: （实测多等 ~1.4 秒）。按块的边界切就没有这个空等。
    pcm_chunk_ends: list = field(default_factory=list, repr=False)
    #: 开播门槛：至少要渲好这么多帧才开播（= 第一段的帧数）。
    #:
    #: 为什么不能渲出第一帧就播：帧是**成段**出来的（段内 1.8 s 的特征准备
    #: 之后才出帧），第一帧就开播的话，每到段边界画面就会追不上声音。
    #: 等第一段整段渲完再播，缓冲就有 ~1.6 s 余量，段边界不会卡。
    playable_frames: int = 0
    #: 流式作业的协程（取消 = 停作业）。
    task: Optional[asyncio.Task] = field(default=None, repr=False)
    #: 缓冲区的锁（收音频的协程和渲染的协程都要动它）。
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "state": self.state,
            "step": self.step,
            "error": self.error,
            "avatar_id": self.avatar_id,
            "avatar_name": self.avatar_name,
            "stride": self.stride,
            "frame_count": self.frame_count,
            "expected_frames": self.expected_frames,
            "duration_s": self.duration_s,
            "fps": self.fps,
            "width": self.width,
            "height": self.height,
            "timings": self.timings,
            "audio_url": "/api/digital-human/jobs/{}/audio".format(self.id),
            "frame_url": "/api/digital-human/jobs/{}/frame/".format(self.id),
            # ---- 流式相关 ---- #
            "streaming": self.streaming,
            "synth_done": self.synth_done,
            "audio_rate": self.audio_rate,
            "pcm_bytes": self.pcm_bytes,
            "playable_frames": self.playable_frames,
        }


def jobs_root() -> Path:
    """作业目录（本卡自己的 uploads 子目录）。"""
    return get_card_uploads_root("digital_human") / "jobs"


def default_model_dir() -> Path:
    """模型目录（``~/models/Lite-Avatar``）。

    优先看环境变量 ``WDNAI_LITE_AVATAR_DIR``，与其它模型的环境变量约定
    一致；否则用 ``~/models/Lite-Avatar``。
    """
    env = os.environ.get("WDNAI_LITE_AVATAR_DIR")
    if env:
        return Path(env).expanduser()
    return Path.home() / "models" / "Lite-Avatar"


def _wav_chunks(path: Path):
    """极简 WAV 解析 → ``(fmt, data)``。

    ``fmt`` 是 ``{format, channels, rate, bits}``，``data`` 是 data 块的原始
    字节。**自己解析而不是用标准库 ``wave``** —— 因为 ``wave`` 只认
    format 1（整型 PCM），碰到 format 3（IEEE float）直接
    ``wave.Error: unknown format: 3``。
    """
    raw = Path(path).read_bytes()
    if len(raw) < 12 or raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise ValueError("不是 WAV 文件: {}".format(path))
    fmt = None
    data = None
    off = 12
    while off + 8 <= len(raw):
        cid = raw[off:off + 4]
        size = int.from_bytes(raw[off + 4:off + 8], "little")
        body = raw[off + 8:off + 8 + size]
        if cid == b"fmt " and len(body) >= 16:
            fmt = {
                "format": int.from_bytes(body[0:2], "little"),
                "channels": int.from_bytes(body[2:4], "little"),
                "rate": int.from_bytes(body[4:8], "little"),
                "bits": int.from_bytes(body[14:16], "little"),
            }
            # WAVE_FORMAT_EXTENSIBLE（0xFFFE）：真正的格式藏在 SubFormat
            # GUID 的头两个字节里。
            if fmt["format"] == 0xFFFE and len(body) >= 26:
                fmt["format"] = int.from_bytes(body[24:26], "little")
        elif cid == b"data":
            data = body
            break
        off += 8 + size + (size & 1)
    if fmt is None or data is None:
        raise ValueError("WAV 文件不完整（缺 fmt 或 data 块）: {}".format(path))
    return fmt, data


def _wav_header(sample_rate: int, channels: int, bits: int = 16) -> bytes:
    """WAV 头（整型 PCM）。

    ⚠️ **长度写 ``0xFFFFFFFF``** —— 「还在流」的标记。这份 WAV 是正在合成
    的那一份，事先不知道会有多长；浏览器要能边下边放。实测 Chrome 认这个
    标记：``duration`` 报 null（未知），但 ``currentTime`` 平滑推进、能出声。
    """
    byte_rate = sample_rate * channels * bits // 8
    block_align = channels * bits // 8
    return (b"RIFF" + struct.pack("<I", 0xFFFFFFFF) + b"WAVE"
            + b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels,
                                     sample_rate, byte_rate, block_align, bits)
            + b"data" + struct.pack("<I", 0xFFFFFFFF))


def read_wav_pcm(path: Path):
    """读 wav → ``(pcm_bytes, sample_rate, n_channels, sample_width)``。

    RPC 通道上音频是**裸 PCM**（没有容器头）—— 这是
    ``wdndds.ai_session`` 的 ``AudioAttachment`` 约定。重采样到 16 k 由
    worker 侧做（``wdnai.avatar.features.extract_features``），这里只负责
    如实把原始采样率报过去。

    ⚠️ **不只收整型 PCM（format 1）** —— 板端 TTS 落盘的是 **IEEE float
    （format 3 / 32 bit）**，而 worker 那边的解码器只认整型 PCM
    （``_pcm_to_float32_mono`` 收 width 1/2/4 的整型）。所以这里把 float
    和 24 bit 统一转成 **16 bit 整型 PCM** 再发出去：转换在板端做（几十
    毫秒），worker 和线上协议都不用动。

    只有 16 bit / 8 bit 整型 PCM 是**原样透传**的（ASR 产出的就是这种，
    转一遍等于白转）。
    """
    import numpy as np

    fmt, data = _wav_chunks(path)
    fmt_tag = fmt["format"]
    n_ch = max(1, int(fmt["channels"]))
    rate = int(fmt["rate"])
    bits = int(fmt["bits"])

    if fmt_tag == 1 and bits == 16:
        return data, rate, n_ch, 2
    if fmt_tag == 1 and bits == 8:
        return data, rate, n_ch, 1
    if fmt_tag == 1 and bits == 32:
        return data, rate, n_ch, 4

    if fmt_tag == 3 and bits == 32:
        # IEEE float → int16。float 幅度本来就该在 [-1, 1]。
        arr = np.frombuffer(data, dtype="<f4").astype(np.float32)
        arr = np.clip(arr, -1.0, 1.0)
        pcm16 = (arr * 32767.0).astype("<i2")
        return pcm16.tobytes(), rate, n_ch, 2

    if fmt_tag == 1 and bits == 24:
        # 24 bit 整型 → 16 bit（丢掉最低的 8 位）。
        raw8 = np.frombuffer(data, dtype=np.uint8)
        usable = (len(raw8) // 3) * 3
        raw8 = raw8[:usable].reshape(-1, 3)
        val = (raw8[:, 0].astype(np.int32)
               | (raw8[:, 1].astype(np.int32) << 8)
               | (raw8[:, 2].astype(np.int8).astype(np.int32) << 16))
        pcm16 = (val >> 8).astype("<i2")
        return pcm16.tobytes(), rate, n_ch, 2

    raise ValueError(
        "不支持的 wav 格式: format={} bits={}（支持整型 PCM 8/16/24/32 bit "
        "与 IEEE float 32 bit）".format(fmt_tag, bits))


class DigitalHumanService:
    """数字人卡片的服务单例。"""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._current: Optional[str] = None
        self._stop_flag = threading.Event()
        self._lock = threading.Lock()
        #: 渲染抽帧步长（2 = 隔帧渲染，嘴型 30fps → 画面 15fps）。
        self.render_stride = max(1, int(os.environ.get(
            "WDNAI_AVATAR_RENDER_STRIDE", "1") or 1))
        #: 上一段作业的 worker 回报（耗时 / decoder 后端 …）—— 状态面板用。
        self._last_summary: dict = {}
        self._last_error: str = ""

    # ------------------------------------------------------------------ #
    # 模型目录 / 形象库
    # ------------------------------------------------------------------ #
    @property
    def model_dir(self) -> Path:
        return default_model_dir()

    @property
    def model_path(self) -> Path:
        return self.model_dir / "model_1_w8a8_ah0.9999.rknn"

    @property
    def avatars(self):
        """形象库。"""
        return get_registry(self.model_dir)

    @property
    def avatar_dir(self) -> Optional[Path]:
        """默认形象的素材目录；没有可用形象时返回 ``None``。

        ⚠️ 没有兜底 —— 以前这里会悄悄回落到 ``avatars/sample``，现在
        解不出就返回 ``None``，让状态如实显示"没有可用形象"。
        """
        try:
            return self.avatars.resolve(None)
        except AvatarError:
            return None

    # ------------------------------------------------------------------ #
    # 算力实例
    # ------------------------------------------------------------------ #
    def compute_state(self) -> dict:
        """算力页上那张「数字人」卡的状态。

        **只看本机**（``spawned`` / ``adopted_local``）—— 远端 peer 的实例
        在对端板子上，帧的 side-file 在对方机器的 ``/dev/shm`` 里，本机读
        不到。
        """
        out = {
            "model_id": COMPUTE_MODEL_ID,
            "enabled": False,
            "status": "",
            "pid": 0,
            "npu_index": -1,
            "error": "",
            "hint": "",
        }
        try:
            from app.core.compute.registry import peek_registry
            reg = peek_registry()
        except Exception:  # noqa: BLE001
            reg = None
        if reg is None:
            out["status"] = "unavailable"
            out["hint"] = "算力层还没起来"
            return out
        try:
            services = reg.list_with_source()
        except Exception as exc:  # noqa: BLE001
            out["error"] = "{}: {}".format(type(exc).__name__, exc)
            out["status"] = "unavailable"
            return out
        for svc in services:
            if svc.get("model_id") != COMPUTE_MODEL_ID:
                continue
            if svc.get("_source") == "adopted":
                continue          # 远端 peer：帧的 side-file 读不到
            out["status"] = str(svc.get("status") or "")
            out["enabled"] = out["status"] == "running"
            out["pid"] = int(svc.get("pid") or 0)
            out["npu_index"] = int(svc.get("npu_index", -1))
            out["error"] = str(svc.get("error") or "")
            break
        else:
            out["status"] = "unavailable"
        if not out["enabled"]:
            out["hint"] = ("请到「算力」页启用「[AV]数字人 LiteAvatar 渲染」"
                           "—— 模型生命周期由算力层管")
        return out

    def _session(self):
        """拿算力实例的 RPC 会话（``LLMRPCSession``）。

        拿不到 ⇒ 抛 ``RuntimeError``（"功能未启用: swcap_avatar_render"），
        调用方把它翻译成给用户看的提示。
        """
        from app.core.compute.registry import get_registry
        return get_registry().get_session(COMPUTE_CAPABILITY)

    # ------------------------------------------------------------------ #
    # 状态
    # ------------------------------------------------------------------ #
    def status(self) -> dict:
        comp = self.compute_state()
        job = self.current_job()
        avatars = self.avatars.list_avatars()
        avatar_dir = self.avatar_dir
        # 「可渲染」= numpy 权重 + NPU decoder 都在位（由形象库判定）
        npu_models = sum(1 for a in avatars if a.get("npu_ready"))
        summary = dict(self._last_summary)
        return {
            # 算力实例 —— 这张卡的模型现在归算力层管
            "compute": comp,
            # 兼容旧字段名：``engine`` 现在指的是「算力 worker」
            "engine": {
                "available": comp["enabled"],
                "running": comp["enabled"],
                "kind": "compute-worker",
                "note": "模型由算力页的 worker 进程持有（板载 RK3588 NPU，零 torch）",
                "error": comp["error"],
            },
            "npu": {
                "loaded": comp["enabled"],
                "model": str(self.model_path),
                "model_exists": self.model_path.is_file(),
                # 主进程不再 dlopen RKNN2 运行时（那是 worker 的事）
                "api_version": "",
                "drv_version": "",
                "error": comp["error"],
            },
            "decoder": {
                # decoder 只有 NPU 一条路（worker 侧）
                "backend": summary.get("decoder_backend") or "npu",
                "error": self._last_error,
                "npu_models": npu_models,
                "avatar_count": len(avatars),
            },
            "encoder": dict(self._encoder_info(summary)),
            "warmup": {
                # 预热归算力层（worker 首次 render 时自己热）
                "state": "done" if comp["enabled"] else "skipped",
                "ms": 0,
            },
            "job": job.snapshot() if job else None,
            "fps": FPS,
            "avatar_dir": str(avatar_dir) if avatar_dir else "",
            "avatar_exists": bool(avatar_dir and (avatar_dir / "net_encode.npz").is_file()),
            "avatars": avatars,
            "avatar_count": len(avatars),
            "default_avatar": self.avatars.default_id(),
            "downloads": self.avatars.downloads(),
        }

    @staticmethod
    def _encoder_info(summary: dict) -> dict:
        """把 worker 回报的耗时整理成旧状态面板认识的形状。"""
        if not summary:
            return {}
        return {
            "backend": "npu",
            "ms": int(summary.get("features_ms") or 0),
            "frontend_backend": "npu",
            "frontend_ms": 0,
            "error": "",
        }

    # ------------------------------------------------------------------ #
    # 作业
    # ------------------------------------------------------------------ #
    def current_job(self) -> Optional[Job]:
        with self._lock:
            if self._current is None:
                return None
            return self._jobs.get(self._current)

    def get_job(self, job_id: str) -> Optional[Job]:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return None
        # 渲染途中：worker 边渲边写帧 —— 实时报已完成的帧数，
        # 前端才能「边渲边看」（首帧不用等整段渲完）。
        #
        # 流式作业不管 step 是什么都要数 —— 它在「等音频」和「渲染」之间
        # 来回切，而帧是一直在涨的。
        if job.state == "running" and (job.step == "render" or job.streaming):
            live = self._count_frames(job.work_dir / "frames")
            if live > job.frame_count:
                job.frame_count = live
        return job

    def start(self, audio_path: Path, avatar_id: Optional[str] = None,
              stride: Optional[int] = None) -> Job:
        """起一个作业（后台线程跑），立刻返回。

        ``avatar_id`` 指定用哪个形象；不给就用默认形象。
        ``stride`` 渲染抽帧步长（2 = 隔帧渲染，出 15 fps）；不给就用默认值。
        """
        avatar_dir = self.avatars.resolve(avatar_id)
        resolved_id = avatar_dir.name
        job_id = uuid.uuid4().hex[:12]
        work_dir = jobs_root() / job_id
        work_dir.mkdir(parents=True, exist_ok=True)
        job = Job(id=job_id, audio_path=Path(audio_path), work_dir=work_dir,
                  avatar_id=resolved_id, avatar_name=resolved_id,
                  stride=max(1, int(stride or self.render_stride)))
        # 画面帧率 = 源帧率 ÷ 抽帧步长。
        #
        # ⚠️ 必须在**建作业时**就定下来 —— 前端在作业还 running 的时候
        # 就要拿它判断「渲染速度追不追得上播放速度」（追得上才边渲边播）。
        # 早先这里留的是默认值 30，stride=2（15 fps）时前端拿 30 去比
        # 实际出帧速率，永远追不上 ⇒ 边渲边播形同虚设。
        job.fps = max(1, FPS // max(1, job.stride))
        with self._lock:
            self._jobs[job_id] = job
            self._current = job_id
        self._stop_flag.clear()
        threading.Thread(target=self._run, args=(job,), daemon=True,
                         name="digital-human-{}".format(job_id)).start()
        return job

    def stop(self) -> None:
        """停掉当前作业（尽量；已经跑到一半的会停在下一步）。"""
        self._stop_flag.set()
        job = self.current_job()
        if job is not None and job.state in ("pending", "running"):
            job.state = "stopped"
            job.error = "已被用户停止"
        # 流式作业：TTS 还在跑，一并停掉（否则它会接着合成没人要的音频）。
        if job is not None and job.streaming:
            try:
                from app.core.audio import tts_bridge
                tts_bridge.get_session().stop()
            except Exception as exc:  # noqa: BLE001
                LOG.debug("digital_human: 停 TTS 失败: %s", exc)

    def frame_path(self, job_id: str, index: int) -> Optional[Path]:
        job = self.get_job(job_id)
        if job is None:
            return None
        path = job.work_dir / "frames" / "{:05d}.jpg".format(index)
        return path if path.is_file() else None

    @staticmethod
    def _count_frames(frames_dir: Path) -> int:
        """数一下 frames 目录里已经落盘多少帧。

        worker 是**边渲边写**的（每帧先写 ``.part.jpg`` 再 rename 成
        ``.jpg``）—— 所以这里数到的永远是**完整帧**，不会是半截的 JPEG。
        """
        try:
            return sum(1 for p in frames_dir.iterdir()
                       if p.suffix == ".jpg" and not p.name.endswith(".part.jpg"))
        except OSError:
            return 0

    # ------------------------------------------------------------------ #
    # 流式作业（边合成边渲染）
    # ------------------------------------------------------------------ #
    def start_text(self, text: str, avatar_id: Optional[str] = None,
                   stride: Optional[int] = None, *, language: str = "auto",
                   volume: float = 0.1, speaker: str = "",
                   ref_speaker: str = "") -> Job:
        """起一个**流式**作业：后端驱动 TTS，语音一段段出来、一段段渲染。

        为什么不让前端「先合成完再提交」
        ==============================
        那样必须等**整段** TTS 合成完才能开始渲染 ⇒ 「40 多秒后才开始播」。
        TTS 是 1:1 实时（合成 35 秒音频就要 35 秒），所以「合成完再渲染」
        天然多等一个音频时长。

        改成后端驱动后：TTS 每吐一块 PCM 就攒够一段送渲染 ⇒ 第一段渲完就能
        开播（实测 ~5 秒）。而且**合成和渲染跑在两块 NPU 上**（TTS 在 EP
        协卡、渲染在板载 NPU）⇒ 真正并行，不是排队。

        ⚠️ 必须在 **event loop 里**调用（内部要 ``create_task``）。
        """
        from app.core.audio import tts_bridge

        text = (text or "").strip()
        if not text:
            raise ValueError("text 不能为空")

        avatar_dir = self.avatars.resolve(avatar_id)
        job_id = uuid.uuid4().hex[:12]
        work_dir = jobs_root() / job_id
        work_dir.mkdir(parents=True, exist_ok=True)
        step = max(1, int(stride or STREAM_STRIDE))
        job = Job(id=job_id, audio_path=work_dir / "speech.wav",
                  work_dir=work_dir, avatar_id=avatar_dir.name,
                  avatar_name=avatar_dir.name, stride=step, streaming=True)
        job.fps = max(1, FPS // step)
        # 开播门槛：攒够 STREAM_START_BUFFER_SEC 秒的画面再播（段边界的缓冲
        # 才够，画面才追得上声音）。
        job.playable_frames = max(1, int(round(
            STREAM_START_BUFFER_SEC * job.fps)))
        with self._lock:
            self._jobs[job_id] = job
            self._current = job_id
        self._stop_flag.clear()

        req = tts_bridge.TTSRequest(
            text=text,
            language=language or "auto",
            speaker=speaker or "",
            ref_speaker=ref_speaker or "",
            volume=max(0.0, min(1.0, float(volume))),
            # SDK 那边静默（不往板子喇叭出声）—— PCM 走订阅流给数字人自己用。
            play_to=tts_bridge.PLAY_BROWSER,
            output=str(job.audio_path),
        )
        loop = asyncio.get_running_loop()
        job.task = loop.create_task(self._stream_run(job, req))
        return job

    async def _stream_run(self, job: Job, req) -> None:
        """流式作业的主协程：起 TTS → 收 PCM → 分段渲染 → 收尾。"""
        from app.core.audio import tts_bridge

        loop = asyncio.get_running_loop()
        try:
            tts = tts_bridge.get_session()
        except ImportError as exc:
            job.state = "error"
            job.error = "语音合成不可用: {}".format(exc)
            self._last_error = job.error
            return

        sub_id = "dh-{}".format(job.id)
        queue = tts.subscribe_audio(sub_id, loop)
        try:
            tts.start(req)
        except Exception as exc:  # noqa: BLE001 —— 单例被占用 / 模型没启用
            tts.unsubscribe_audio(sub_id)
            job.state = "error"
            job.error = "语音合成失败: {}".format(exc)
            self._last_error = job.error
            LOG.warning("digital_human: 流式作业起不来: %s", exc)
            return

        job.state = "running"
        job.step = "synth"
        t0 = time.time()
        consume = asyncio.create_task(self._stream_consume(job, queue))
        render = asyncio.create_task(self._stream_render(job))
        try:
            await asyncio.gather(consume, render)
        except asyncio.CancelledError:
            consume.cancel()
            render.cancel()
            raise
        except Exception as exc:  # noqa: BLE001
            consume.cancel()
            render.cancel()
            if self._stop_flag.is_set():
                # 用户主动停的 —— 不算失败
                job.state = "stopped"
                job.error = "已被用户停止"
                return
            LOG.exception("digital_human: 流式作业失败")
            job.state = "error"
            job.error = "{}: {}".format(type(exc).__name__, exc)
            self._last_error = job.error
            return
        finally:
            tts.unsubscribe_audio(sub_id)

        # ---- 收尾：帧数 / 时长 / 尺寸 ---- #
        job.frame_count = self._count_frames(job.work_dir / "frames")
        job.expected_frames = job.frame_count
        if job.audio_rate:
            job.duration_s = round(
                job.pcm_bytes / float(job.audio_rate * job.audio_channels * 2), 3)
        job.timings["total_ms"] = int((time.time() - t0) * 1000)
        job.step = ""
        job.state = "done"

    async def _stream_consume(self, job: Job, queue) -> None:
        """收 TTS 的 PCM 块 → 转 16 bit 整型 → 攒进作业缓冲。

        ⚠️ **必须很快**：TTS 侧的订阅队列只有 4 格，满了会**丢最旧的块**
        （``TtsSession._enqueue_chunk`` 的 drop-oldest 策略）。消费慢了音频
        会缺一段 —— 浏览器和渲染吃的是同一份缺口的音频，音画仍然对得上，
        但语音本身会断。
        """
        import numpy as np

        while True:
            data = await queue.get()
            if data == b"EOF":
                job.synth_done = True
                return
            if len(data) < 12:
                continue
            sr, _ch, n = struct.unpack("<III", data[:12])
            payload = data[12:]
            if not payload or n <= 0:
                continue
            arr = np.frombuffer(payload, dtype="<f4")
            # 声道数按「头里的采样数」和「实际字节数」推 —— TTS 那边的头把
            # 声道硬编码成 1 了，多声道时这里能纠回来。
            ch = max(1, len(arr) // n)
            if ch > 1:
                arr = arr.reshape(-1, ch).mean(axis=1)
            arr = np.clip(arr, -1.0, 1.0)
            pcm16 = (arr * 32767.0).astype("<i2")
            with job.lock:
                job.pcm_buf.extend(pcm16.tobytes())
                job.pcm_bytes = len(job.pcm_buf)
                # 这一块的结束位置 —— 分段就按它切（见 ``pcm_chunk_ends``）。
                job.pcm_chunk_ends.append(len(job.pcm_buf))
                LOG.debug("digital_human: TTS 音频块 %d 采样 / %d 字节（累计 %d）",
                          int(arr.size), len(pcm16.tobytes()), job.pcm_bytes)
                if not job.audio_rate:
                    job.audio_rate = int(sr) or 24000
                    job.audio_channels = ch

    @staticmethod
    def _segment_bytes(job: Job) -> int:
        """一段最多能有多长（字节）—— fbank 定长桶是 10 秒，留点余量。"""
        if not job.audio_rate:
            return 0
        return int(MAX_SEGMENT_SEC * job.audio_rate) * job.audio_channels * 2

    @staticmethod
    def _min_segment_bytes(job: Job) -> int:
        """一段**至少**得有多长（字节）。

        特征前端重采样到 16 kHz 之后少于 1600 采样（约 0.1 秒）会直接报
        「音频太短」⇒ 按采样率换算回原始字节数。
        """
        if not job.audio_rate:
            return 0
        min_samples = int(job.audio_rate / 10.0)
        return min_samples * job.audio_channels * 2

    @staticmethod
    def _take_segment(job: Job):
        """切一段出来。

        **按 TTS 音频块切**（见 :attr:`Job.pcm_chunk_ends`）：TTS 的块不固定
        （实测首块 3.98 秒、后续 1.98 秒、尾巴可能几十毫秒）。

        * **第一段 = 第一个块** —— latency 优先，拿到就渲，不等下一块；
        * **之后每段攒到 ~4 秒** —— 特征前端是**定长桶**、开销固定（~0.52 s
          一段），段太短摊不薄：2 秒一段 ⇒ 0.26 s/音频秒，4 秒一段 ⇒
          0.13 s/音频秒（渲染总耗时 0.91 → 0.78 s/音频秒，余量厚得多）；
        * 块太短就并进下一块；TTS 的尾巴块补静音凑够特征要的最小长度。

        返回 ``(pcm, is_last, rate, channels)``；没有可切的返回 ``None``。
        """
        with job.lock:
            if not job.pcm_chunk_ends:
                return None
            last_end = job.pcm_chunk_ends[-1]
            need = DigitalHumanService._min_segment_bytes(job)
            cap = DigitalHumanService._segment_bytes(job)
            min_seg = (int(MIN_SEGMENT_SEC * job.audio_rate)
                       * job.audio_channels * 2) if job.audio_rate else 0
            done = job.synth_done
            if job.pcm_rendered == 0:
                # 第一段：第一个块（latency 优先）。太短就并进下一块。
                target = job.pcm_chunk_ends[0]
                if target - job.pcm_rendered < need:
                    target = last_end
                wait_bytes = need
            else:
                # 之后每段攒到 ~4 秒（固定开销摊薄）
                target = job.pcm_rendered + min_seg
                wait_bytes = min_seg
            target = min(target, last_end)
            avail = target - job.pcm_rendered
            if avail <= 0:
                if done:
                    job.pcm_chunk_ends.clear()
                return None
            if avail < wait_bytes and not done:
                return None          # 等下一块
            take = min(avail, cap or avail)
            start = job.pcm_rendered
            seg = bytes(job.pcm_buf[start:start + take])
            job.pcm_rendered += take
            # 尾巴块太短（TTS 收尾时的余量）—— 补静音凑够特征要的最小长度。
            if len(seg) < need:
                seg += b"\x00" * (need - len(seg))
            # 丢掉已经吃完的块
            while job.pcm_chunk_ends and job.pcm_chunk_ends[0] <= job.pcm_rendered:
                job.pcm_chunk_ends.pop(0)
            is_last = done and (len(job.pcm_buf) - job.pcm_rendered) == 0
            return seg, is_last, job.audio_rate, job.audio_channels

    async def _stream_render(self, job: Job) -> None:
        """TTS 每吐一块就送渲染 —— **不等整段音频合成完**。

        分段边界 = TTS 音频块的边界（见 :attr:`Job.pcm_chunk_ends`）⇒ 切点
        跟 TTS 的解码窗口对齐，渲染拿到就能开工，不空等。

        段与段之间靠 ``frame_offset`` 让帧号连续 ⇒ 拼起来是一条完整视频。
        尾巴淡出**只有最后一段**做（每段都做的话帧号会漂）。
        """
        loop = asyncio.get_running_loop()
        frame_offset = 0
        acc = {"features_ms": 0, "mouth_ms": 0, "smooth_ms": 0, "render_ms": 0}
        while True:
            if self._stop_flag.is_set():
                raise RuntimeError("作业已被停止")
            item = self._take_segment(job)
            if item is None:
                if job.synth_done:
                    return
                job.step = "synth"
                await asyncio.sleep(0.02)
                continue
            seg, is_last, rate, channels = item
            job.step = "render"
            LOG.debug("digital_human: t=%.2f 送渲染 %d 字节",
                      time.time() - job.created_at, len(seg))
            summary = await loop.run_in_executor(
                None, self._render_segment, job, seg, rate, channels,
                frame_offset, is_last)
            LOG.debug("digital_human: t=%.2f 渲完 %s", time.time() - job.created_at,
                      json.dumps(summary, ensure_ascii=False)[:200]
                      if isinstance(summary, dict) else summary)
            frame_offset += int((summary or {}).get("frame_count") or 0)
            for key in acc:
                acc[key] += int((summary or {}).get(key) or 0)
            job.timings.update(acc)
            # 帧尺寸（前端拿它把舞台调成形象的**真实比例**）
            w = int((summary or {}).get("width") or 0)
            h = int((summary or {}).get("height") or 0)
            if w and h:
                job.width, job.height = w, h

    def _render_segment(self, job: Job, pcm: bytes, rate: int, channels: int,
                        frame_offset: int, is_last: bool) -> dict:
        """渲一段（阻塞 RPC，跑在线程池里，不占事件循环）。"""
        sess = self._session()
        frames_dir = job.work_dir / "frames"
        frames_dir.mkdir(parents=True, exist_ok=True)
        LOG.debug("digital_human: 渲一段 %d 字节 / %d 采样 @%d Hz"
                  "（frame_offset=%d, 尾=%s）",
                  len(pcm), len(pcm) // max(1, 2 * channels), rate,
                  frame_offset, is_last)
        summary, _video = sess.render_avatar(
            pcm,
            avatar_id=job.avatar_id,
            fps=FPS,
            stride=job.stride,
            sample_rate=rate,
            n_channels=channels,
            sample_width=2,
            out_dir=str(frames_dir),
            frame_offset=frame_offset,
            tail_pad=is_last,
        )
        return summary or {}

    async def wav_stream(self, job: Job):
        """把作业的音频**边合成边吐**给浏览器（长度未知的 chunked WAV）。

        为什么长度未知也要给：这份 WAV 是「正在合成」的那一份，事先不知道
        会有多长。RIFF / data 块的长度都写 ``0xFFFFFFFF`` —— Chrome 认这个
        「还在流」的标记，会一边下一边放（实测 ``currentTime`` 平滑推进、
        ``duration`` 报 null）。
        """
        # 等第一块音频 —— 采样率在里面
        for _ in range(250):
            if job.audio_rate or job.state in ("done", "error", "stopped"):
                break
            await asyncio.sleep(0.02)
        yield _wav_header(job.audio_rate or 24000, job.audio_channels or 1)
        sent = 0
        while True:
            with job.lock:
                cur = len(job.pcm_buf)
            if cur > sent:
                chunk = bytes(job.pcm_buf[sent:cur])
                sent = cur
                yield chunk
                continue
            # 作业收尾才断流 —— 让浏览器等最后一段帧渲完再收到 ended。
            if job.state in ("done", "error", "stopped"):
                return
            await asyncio.sleep(0.04)

    # ------------------------------------------------------------------ #
    # 主流程
    # ------------------------------------------------------------------ #
    def _run(self, job: Job) -> None:
        try:
            job.state = "running"
            t_all = time.time()

            # 形象素材（嘴型参数跟形象无关，只有渲染用到素材）
            avatar_dir = self.avatars.resolve(job.avatar_id or None)
            job.avatar_id = avatar_dir.name

            # ---- 音频 → 裸 PCM ---- #
            job.step = "features"
            t0 = time.time()
            pcm, rate, n_ch, width = read_wav_pcm(job.audio_path)
            job.duration_s = (len(pcm) / float(max(1, rate * n_ch * width))) if rate else 0.0
            job.expected_frames = int(job.duration_s * FPS / max(1, job.stride)) + 1
            job.timings["decode_ms"] = int((time.time() - t0) * 1000)
            self._check_stop(job)

            # ---- 一次 RPC：worker 里跑完整条管线 ---- #
            #
            # worker 侧：fbank(NPU) → encoder(NPU) → 嘴型(NPU) → 平滑 →
            # decoder(NPU) → 合成，**整段渲完**才回一次。
            #
            # ⚠️ 但**帧是边渲边落盘**的（``out_dir``）—— worker 把 JPEG 直接
            # 写进作业的 frames 目录，主进程在 RPC 还没回来的时候就能数到
            # 已完成的帧。首帧因此不用等整段渲完。
            job.step = "render"
            t0 = time.time()
            sess = self._session()
            frames_dir = job.work_dir / "frames"
            frames_dir.mkdir(parents=True, exist_ok=True)

            # ---- 分段：fbank 的桶是定长的（160000 采样 ≈ 10 秒 @16 kHz）----
            #
            # 超过上限的音频**必须**切段渲，否则 worker 直接报「音频太长」。
            # 分段只是**渲染**上的分割：切的是同一段 PCM 的连续片段，所以
            # **音频本身不用拼**（前端播的还是原始 wav，天然连续）。
            #
            # 帧号通过 ``frame_offset`` 在整个作业里**连续** ⇒ 主进程数目录
            # 就能得到连续帧数 ⇒ 流式性（边渲边出帧、边渲边播）完全保留。
            frame_bytes = max(1, n_ch * width)
            n_samples = len(pcm) // frame_bytes
            max_samples = int(SEGMENT_SEC * rate)
            segments = [pcm]
            if n_samples > max_samples:
                segments = []
                for off in range(0, n_samples, max_samples):
                    end = min(off + max_samples, n_samples)
                    segments.append(pcm[off * frame_bytes:end * frame_bytes])
                LOG.info("digital_human: 音频 %.1f 秒超过单段上限 %.1f 秒 —— "
                         "切成 %d 段渲染", job.duration_s, SEGMENT_SEC, len(segments))

            frame_offset = 0
            summary: dict = {}
            video = None
            acc = {"features_ms": 0, "mouth_ms": 0, "smooth_ms": 0,
                   "render_ms": 0, "duration_s": 0.0}
            for seg_index, seg_pcm in enumerate(segments):
                self._check_stop(job)
                seg_summary, seg_video = sess.render_avatar(
                    seg_pcm,
                    avatar_id=job.avatar_id,
                    fps=FPS,
                    stride=job.stride,
                    sample_rate=rate,
                    n_channels=n_ch,
                    sample_width=width,
                    out_dir=str(frames_dir),
                    frame_offset=frame_offset,
                    # 尾巴淡出**只有最后一段**做 —— 每段都补的话帧号会漂
                    tail_pad=(seg_index == len(segments) - 1),
                )
                for key in ("features_ms", "mouth_ms", "smooth_ms", "render_ms"):
                    acc[key] += int(seg_summary.get(key) or 0)
                acc["duration_s"] += float(seg_summary.get("duration_s") or 0)
                summary = seg_summary
                video = seg_video
                frame_offset += int(seg_summary.get("frame_count") or 0)
            for key, value in acc.items():
                summary[key] = value
            job.timings["total_ms"] = int((time.time() - t0) * 1000)
            self._check_stop(job)

            # ---- 帧落盘 ---- #
            # worker 已经把帧写进 frames_dir 了（out_dir 模式）；只有在
            # worker 没拿到 out_dir（跨机 peer）时才走附件那条路。
            if video is not None and getattr(video, "frames", None):
                frames_dir.mkdir(parents=True, exist_ok=True)
                for i, blob in enumerate(video.frames):
                    (frames_dir / "{:05d}.jpg".format(i)).write_bytes(blob)
                job.frame_count = len(video.frames)
            else:
                job.frame_count = self._count_frames(frames_dir)
            # 画面帧率由 **stride** 决定（stride=2 → 15 fps）。
            # worker summary 里的 fps 是**源**帧率（30），不是画面帧率 ——
            # 直接拿它当画面帧率会让前端按 30 fps 判断「追不追得上」。
            job.fps = max(1, FPS // max(1, job.stride))
            job.width = int(summary.get("width") or 0)
            job.height = int(summary.get("height") or 0)

            # ---- 把 worker 的分段耗时搬过来 ---- #
            for key in ("features_ms", "mouth_ms", "smooth_ms", "render_ms"):
                if key in summary:
                    job.timings[key] = int(summary.get(key) or 0)
            job.duration_s = float(summary.get("duration_s") or job.duration_s) \
                if len(segments) == 1 else job.duration_s
            job.timings["total_ms"] = int((time.time() - t_all) * 1000)

            self._last_summary = dict(summary)
            self._last_error = ""

            job.state = "done"
            job.step = ""
        except Exception as exc:  # noqa: BLE001 —— 作业失败要回给前端
            LOG.exception("digital_human: 作业失败")
            job.state = "error"
            job.error = "{}: {}".format(type(exc).__name__, exc)
            self._last_error = job.error

    def _check_stop(self, job: Job) -> None:
        if self._stop_flag.is_set():
            job.state = "stopped"
            raise RuntimeError("作业已被停止")

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def warmup(self) -> None:
        """兼容旧调用方 —— **不再预热**。

        以前这里预热的是引擎进程（独立 venv + torch）。现在模型由算力层的
        worker 持有，卡片这边没有东西可热：第一次 render 由 worker 自己
        加载（fbank / encoder / 嘴型 / 渲染器都是懒加载）。
        """
        return

    def close(self) -> None:
        # 流式作业的协程要先停 —— 别让它在服务关掉之后还往 RPC 里塞活。
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            task = getattr(job, "task", None)
            if task is not None and not task.done():
                task.cancel()
        # 只清本卡自己的作业目录。算力实例的卸载归算力层（用户在算力页
        # 点「禁用」），卡片不越权去动别人的 worker。
        root = jobs_root()
        if root.is_dir():
            shutil.rmtree(root, ignore_errors=True)


_service: Optional[DigitalHumanService] = None
_service_lock = threading.Lock()


def get_service() -> DigitalHumanService:
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = DigitalHumanService()
    return _service
