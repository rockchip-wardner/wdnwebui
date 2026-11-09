"""端点检测 —— 纯能量门限，零额外依赖。

**为什么不用 ``webrtcvad``**：本板**没有装**（``sounddevice`` / ``pyaudio``
/ ``webrtcvad`` 均未安装，见清单 §5.6）。端点检测只需要一个能量门限，
纯 numpy 就够 —— 引入一个 C 扩展只为了让阈值更准，不值。

判定规则（清单 §7.1）
=====================

::

    每收到一块 PCM:
        rms_dbfs   = 20*log10(sqrt(mean(x^2)))
        peak_dbfs  = 20*log10(max|x|)
        if rms_dbfs >= silence_threshold_dbfs:      # 这一块不是静音
            speech_sec  += 本块时长
            silence_run  = 0
        else:
            silence_run += 本块时长
        if has_speech and silence_run >= endpoint_silence_ms:
            → 端点：final=True 收尾 → 关会话 → 开新会话
        if (not has_speech) and elapsed >= max_silence_sec:
            → 强制结束会话（不产 endpoint 帧）

参数口径
========

=========================  ========  ====================================
参数                       默认      口径
=========================  ========  ====================================
``silence_threshold_dbfs``  ``-45``  **RMS** 低于此值算静音
``endpoint_silence_ms``     ``1200``  连续静音达到此时长 → 端点
``max_silence_sec``         ``30``    **从未**识别到语音的时长上限
``min_speech_sec``          ``0.3``   累计语音时长达到此值才算「有语音」
``action_on_endpoint``      ``True``  命中端点时**是否真的动作**
=========================  ========  ====================================

``min_speech_sec`` 的作用是**迟滞**：一声「嗯」或一次咳嗽不该把整段
会话判成「有语音」—— 否则 30 秒纯静音的保护就永远不会触发。所以
「有语音」是**累计**量，不是单块的量。

``action_on_endpoint`` 是**观察 / 动作**的开关（清单 §8.6）：文件模式的
VAD 语义是「只观察，不动作」—— 检测器照常报告「这里是个端点」，但**不**
收尾、不重置会话。实时麦克风模式才把它打开。

三条硬约束（清单 §7.2）
======================

1. **静音仍然要喂给模型。** 端点检测**不改变**「全量重喂」语义 ——
   静音照样进 ``audio_embed_history``。剪掉静音会切碎上下文，反而伤害
   跨静音的识别连续性。
2. **端点只决定「何时收尾并重置会话」，不决定「识别什么」。**
3. **端点后开新会话 = 新 ``asr_stream_id``。** 已确认文本在 SDK 侧清零，
   前端负责把多句拼起来。
"""
from __future__ import annotations

import math
import statistics
from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional, Tuple

import numpy as np

__all__ = [
    "EndpointConfig",
    "EndpointEvent",
    "EndpointDetector",
    "AdaptivePauseDetector",
    "rms_dbfs",
    "peak_dbfs",
    "to_mono_s16",
]


def to_mono_s16(pcm: bytes, *, sample_width: int = 2,
                n_channels: int = 1) -> bytes:
    """任意位深 / 声道数的 PCM → **单声道 s16le**（能量检测的输入格式）。

    为什么需要这个转换
    ------------------
    :func:`rms_dbfs` / :func:`peak_dbfs` 用 ``np.frombuffer(pcm, "<i2")``
    读输入 —— **假定就是 s16le**。而文件来源的 PCM 位深并不固定：

    * 媒体（mp4/mp3/…）经 PyAV 解出来是 **s16le 单声道**；
    * **wav 可以是 s24le**（板载 demo 的 ``asr_en.wav`` 就是 s24le/48 kHz）。

    位深读错 → 幅度差 256 倍（48 dB）→ 能量算出来完全不对 → 静音判定
    形同虚设（一个 s24le 音频块会被当成「永远是静音」或「永远是巨响」）。

    转换语义
    --------
    * s16le → 原样（只做声道下混）；
    * s24le → 右移 8 位（``v >> 8``），等价于幅度除以 256，**不溢出**；
    * s32le → 右移 16 位；
    * 多声道 → 各声道**求平均**（和 :func:`iter_media_pcm` 的下混一致）。

    下混用「平均」而不是「取第一个声道」：立体声的两个声道相位接近，
    平均后 RMS 与单声道等价；而取第一个声道在反相时会互相抵消。
    """
    if not pcm:
        return b""
    nch = max(1, int(n_channels or 1))
    width = int(sample_width or 2)
    if width == 2:
        x = np.frombuffer(pcm, dtype="<i2")
    elif width == 3:
        raw = np.frombuffer(pcm, dtype=np.uint8).reshape(-1, 3)
        v = (raw[:, 0].astype(np.int32)
             | (raw[:, 1].astype(np.int32) << 8)
             | (raw[:, 2].astype(np.int32) << 16))
        sign = (raw[:, 2] & 0x80) != 0
        v[sign] -= 0x1000000
        x = (v >> 8).astype(np.int16)
    elif width == 4:
        x = (np.frombuffer(pcm, dtype="<i4") >> 16).astype(np.int16)
    else:
        raise ValueError("暂不支持 {} 字节每采样".format(width))
    if nch > 1:
        usable = (len(x) // nch) * nch
        if usable:
            x = x[:usable].reshape(-1, nch).mean(axis=1).astype(np.int16)
    return x.tobytes()

#: dBFS 的下限 —— 全零信号没有对数，用 -120 代替（哑巴区）。
_DBFS_FLOOR = -120.0


def _to_float32(pcm: bytes) -> np.ndarray:
    """s16le 字节 → float32（幅度归一到 [-1, 1)）。"""
    if not pcm:
        return np.zeros((0,), dtype=np.float32)
    return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0


def rms_dbfs(pcm: bytes) -> float:
    """一段 PCM 的 RMS 电平（dBFS）。全零 → :data:`_DBFS_FLOOR`。"""
    x = _to_float32(pcm)
    if x.size == 0:
        return _DBFS_FLOOR
    rms = float(np.sqrt(np.mean(np.square(x))))
    if rms <= 0.0:
        return _DBFS_FLOOR
    return max(_DBFS_FLOOR, 20.0 * math.log10(rms))


def peak_dbfs(pcm: bytes) -> float:
    """一段 PCM 的峰值电平（dBFS）。全零 → :data:`_DBFS_FLOOR`。"""
    x = _to_float32(pcm)
    if x.size == 0:
        return _DBFS_FLOOR
    peak = float(np.max(np.abs(x)))
    if peak <= 0.0:
        return _DBFS_FLOOR
    return max(_DBFS_FLOOR, 20.0 * math.log10(peak))


@dataclass(frozen=True)
class EndpointConfig:
    """端点检测参数。"""

    #: RMS 低于此值（dBFS）算静音。
    silence_threshold_dbfs: float = -45.0
    #: 连续静音达到此时长 → 端点。
    endpoint_silence_ms: int = 1200
    #: 从未识别到语音时，超过此时长 → 强制结束会话。
    max_silence_sec: float = 30.0
    #: 累计语音时长达到此值才算「有语音」。
    min_speech_sec: float = 0.3
    #: 命中端点时是否**真的动作**（收尾 + 重置会话）。
    action_on_endpoint: bool = True

    def as_dict(self) -> dict:
        return {
            "silence_ms": int(self.endpoint_silence_ms),
            "threshold_dbfs": float(self.silence_threshold_dbfs),
            "max_silence_sec": float(self.max_silence_sec),
            "min_speech_sec": float(self.min_speech_sec),
            "action_on_endpoint": bool(self.action_on_endpoint),
        }


@dataclass
class EndpointEvent:
    """一次喂入后的检测结果。

    * ``silent`` —— 本块是否算静音（RMS 低于门限）
    * ``voiced`` —— 本块是否含语音能量（峰值高于门限）
    * ``has_speech`` —— 累计语音是否已达到 ``min_speech_sec``（迟滞后）
    * ``silence_run_ms`` —— 当前连续静音时长
    * ``speech_sec`` —— 累计语音时长
    * ``elapsed_sec`` —— 本会话累计时长
    * ``endpoint`` —— **是否命中端点**（与 ``action_on_endpoint`` 无关）
    * ``force_end`` —— 是否命中「纯静音超时」
    * ``action`` —— 调用方**应该**动作（端点命中 **且** 开了
      ``action_on_endpoint``）
    """

    silent: bool = False
    voiced: bool = False
    has_speech: bool = False
    silence_run_ms: int = 0
    speech_sec: float = 0.0
    elapsed_sec: float = 0.0
    endpoint: bool = False
    force_end: bool = False
    action: bool = False
    rms_dbfs: float = _DBFS_FLOOR
    peak_dbfs: float = _DBFS_FLOOR


class EndpointDetector:
    """端点检测器（**有状态**：累计静音 / 语音时长）。

    一个检测器对应**一个 SDK 会话**。端点命中后调用方会关掉旧会话、
    开新会话 —— 那时应该 :meth:`reset`（或直接新建一个检测器）。

    用法::

        det = EndpointDetector(EndpointConfig())
        ev = det.feed(pcm, duration_sec=0.5)
        if ev.action:
            ...   # 收尾 + 重置会话
    """

    def __init__(self, config: Optional[EndpointConfig] = None) -> None:
        self.config = config or EndpointConfig()
        self._silence_run_ms = 0
        self._speech_sec = 0.0
        self._elapsed_sec = 0.0

    # ------------------------------------------------------------------ #
    @property
    def silence_run_ms(self) -> int:
        return self._silence_run_ms

    @property
    def speech_sec(self) -> float:
        return self._speech_sec

    @property
    def elapsed_sec(self) -> float:
        return self._elapsed_sec

    @property
    def has_speech(self) -> bool:
        """累计语音是否达到 ``min_speech_sec``（迟滞后）。"""
        return self._speech_sec >= self.config.min_speech_sec

    def reset(self) -> None:
        """清空累计量（换 SDK 会话时调用）。"""
        self._silence_run_ms = 0
        self._speech_sec = 0.0
        self._elapsed_sec = 0.0

    # ------------------------------------------------------------------ #
    def feed(self, pcm: bytes, *, duration_sec: Optional[float] = None,
             sample_rate: int = 16000, sample_width: int = 2,
             n_channels: int = 1) -> EndpointEvent:
        """喂入一块 PCM，返回检测结果。

        ``duration_sec`` 省略时按字节数与采样率算。
        """
        if duration_sec is None:
            frame_bytes = max(1, n_channels * sample_width)
            n_frames = len(pcm) // frame_bytes
            duration_sec = n_frames / float(sample_rate or 1)

        rms = rms_dbfs(pcm)
        peak = peak_dbfs(pcm)

        silent = rms < self.config.silence_threshold_dbfs
        voiced = (not silent) and peak > self.config.silence_threshold_dbfs

        dur_ms = int(round(duration_sec * 1000.0))
        self._elapsed_sec += duration_sec
        if silent:
            self._silence_run_ms += dur_ms
        else:
            self._silence_run_ms = 0
            if voiced:
                self._speech_sec += duration_sec

        has_speech = self.has_speech
        endpoint = bool(has_speech
                        and self._silence_run_ms >= self.config.endpoint_silence_ms)
        force_end = bool((not has_speech)
                         and self._elapsed_sec >= self.config.max_silence_sec)
        action = bool(endpoint and self.config.action_on_endpoint)

        return EndpointEvent(
            silent=silent,
            voiced=voiced,
            has_speech=has_speech,
            silence_run_ms=self._silence_run_ms,
            speech_sec=self._speech_sec,
            elapsed_sec=self._elapsed_sec,
            endpoint=endpoint,
            force_end=force_end,
            action=action,
            rms_dbfs=rms,
            peak_dbfs=peak,
        )


class AdaptivePauseDetector:
    """**自适应**停顿检测：拿当前电平和「最近若干秒的中位电平」比。

    为什么不能用绝对门限
    ------------------
    :class:`EndpointDetector` 的 ``silence_threshold_dbfs`` 是**绝对**门限，
    它假设「安静 = 底噪」。但真实视频的底噪差异极大：

    * 板载 demo 的 s24le 录音：安静段大约 -50 dBFS；
    * 电视访谈（《圆桌派》）：**有背景音乐**，说话中位电平就有
      **-23.6 dBFS**，150 秒里低于 -30 dBFS 的只有 2 秒；
    * 英文讲座（CS229）：中位 -21.1 dBFS。

    绝对门限要么切不出（-45 dBFS 在访谈里永远不命中），要么在别处误切。

    判定规则
    --------
    ::

        当前块的 RMS  <  最近 window_sec 秒电平的**中位数** - drop_db
        → 这一块算「说话停顿」

    中位数而不是平均数：停顿段是**少数**，平均值会被拖下来；中位数
    只反映「正常说话时的典型电平」。

    实测（每秒 RMS）
    ----------------
    ==========================  ==========  ==========================
    样本                          中位电平      真停顿处的电平
    ==========================  ==========  ==========================
    英文 CS229 讲座               -21.1        -35.3 / -42.1 / -45.5
    中文《圆桌派》（背景音乐）      -23.6        -58.7
    ==========================  ==========  ==========================

    真停顿比中位低 **12~35 dB**，说话中的秒基本都在中位 ±3 dB 之内 →
    ``drop_db = 12`` 分得很开。

    ⚠️ **静音段不会一直判成停顿**：中位数是**滑动**的，安静久了中位数
    自己也降下来 → 相对关系保持住，不会「一直判停顿」。

    ⚠️ 这个检测器看的是**音频信号**，不是 SDK 会话 → **不要**在换会话
    时 :meth:`reset`（电平参照要跨会话连续）。:class:`EndpointDetector`
    恰恰相反：它按会话走，换会话要 reset。
    """

    def __init__(self, *, drop_db: float = 12.0,
                 window_sec: float = 30.0,
                 min_history_sec: float = 5.0) -> None:
        self.drop_db = float(drop_db)
        self.window_sec = float(window_sec)
        self.min_history_sec = float(min_history_sec)
        self._levels: Deque[Tuple[float, float]] = deque()
        self._elapsed = 0.0

    # ------------------------------------------------------------------ #
    def reset(self) -> None:
        """清空电平历史（一般只在换文件时调用）。"""
        self._levels.clear()
        self._elapsed = 0.0

    @property
    def elapsed_sec(self) -> float:
        return self._elapsed

    @property
    def median_dbfs(self) -> float:
        """当前窗口内电平的中位数（dBFS）。窗口为空 → -120。"""
        if not self._levels:
            return _DBFS_FLOOR
        return float(statistics.median(d for _, d in self._levels))

    # ------------------------------------------------------------------ #
    def feed(self, rms_dbfs: float, duration_sec: float = 1.0) -> bool:
        """喂入一块的电平，返回**这一块是否算「说话停顿」**。

        ``duration_sec`` 用于维护滑动窗口的时间轴（块长不一时用它）。
        """
        dur = max(0.0, float(duration_sec or 0.0))
        self._elapsed += dur
        self._levels.append((self._elapsed, float(rms_dbfs)))
        cutoff = self._elapsed - self.window_sec
        while self._levels and self._levels[0][0] < cutoff:
            self._levels.popleft()
        # 历史太短 → 中位数不可信（开头可能是纯静音或纯说话）→ 不判。
        if self._elapsed < self.min_history_sec:
            return False
        return float(rms_dbfs) < (self.median_dbfs - self.drop_db)
