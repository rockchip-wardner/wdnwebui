"""实时采集契约 + 块累积器。

**采集端只负责「产生符合契约的块」** —— 服务端不关心这块音频是从浏览器
来的还是从板载麦克风来的，两个采集端互不感知（清单 §5.1）。

统一契约
========

服务端消费的最小单位 = **PCM 块**：

===================  ===========================================
字段                 约束
===================  ===========================================
``pcm``              ``s16le`` / **单声道** / **16 kHz**
                     （重采样在**采集端**完成，服务端不再重采样）
``seq``              从 0 单调递增；**缺号 = 丢包**
``stream_id``        会话标识
===================  ===========================================

为什么是 16 kHz
---------------

Qwen3-ASR 的输入就是 16 kHz（SDK 侧的 ``TARGET_SAMPLE_RATE``）。契约把
采样率**钉在采集端**有两个好处：

1. 服务端收到什么就是模型要吃的什么 —— 中间没有第二条重采样链，
   也就没有「两条链的相位差」这种玄学问题（见 SDK
   ``_resample_linear`` 的注释：重采样器的选择会**逐 token 可观测**）。
2. **两个采集端用同一套重采样算法** —— 板载端用
   :class:`StreamResampler`（带 carry 的流式线性插值），浏览器端用
   前端那份**逐样本等价**的 JS 实现
   （``wdnapps_audio_chat/cards/qwen3_asr/static/app.js`` 的 ``_resampleTo16k``）。
   ⚠️ **不要**让浏览器自己做（``new AudioContext({sampleRate:16000})``）：
   那是 WebAudio 自己的重采样器，与我们的算法不同 → 同一段录音从两端
   进来会得到不同的 16 kHz PCM → 文本分叉（清单 B3）。
   服务端只管喂。

块累积器
========

:class:`ChunkAccumulator` 把「任意长度的 PCM 推流」切成**定长块**。
服务端用它把「500 ms 一块的采集节奏」对齐到「1 秒一块的推理节奏」。

> ⚠️ **定长必须是 1 秒** —— SDK 的在线路径就在 1 秒的网格上切块
> （``online_chunk_seconds`` 默认 1.0）。服务端切的块边界若与 SDK 的
> 网格错位，SDK 仍会自己重新对齐（它内部有 carry），但**端点检测**会
> 在服务端侧看到与 SDK 不同的边界，字幕时间戳就会飘。所以服务端和
> SDK 用同一个 1 秒网格。

重采样
======

:func:`resample_s16_linear` 是**整块**线性插值（不是逐帧独立插值 ——
后者在帧边界相位不连续，误差会累积漂移；这是官方 demo 的已知缺陷，
见报告 §5.3.6 第 7 条）。它与 SDK 的 ``_resample_linear`` 同款算法：
``idx = j * src / dst``，floor 取左邻居，线性混合。

但**流式**场景不能逐块独立重采样（每块相位从 0 重来 = 每块错一个相位）。
板载采集进程因此使用 :class:`StreamResampler`（带 carry 的流式版本），
它保证「N 次 push 的拼接结果 == 整段一次性重采样的结果」。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import List

import numpy as np

__all__ = [
    "CAPTURE_SAMPLE_RATE",
    "CAPTURE_N_CHANNELS",
    "CAPTURE_SAMPLE_WIDTH",
    "DEFAULT_BLOCK_SECONDS",
    "DEFAULT_CHUNK_MS",
    "AudioChunk",
    "ChunkAccumulator",
    "PcmFeedBuffer",
    "StreamResampler",
    "resample_s16_linear",
    "validate_capture_format",
]


#: 契约钉死的采样率（Qwen3-ASR 的输入采样率）。
CAPTURE_SAMPLE_RATE = 16000

#: 契约钉死的声道数 —— 单声道。
CAPTURE_N_CHANNELS = 1

#: 契约钉死的字节每采样 —— s16le。
CAPTURE_SAMPLE_WIDTH = 2

#: 服务端切块粒度（秒）—— 与 SDK 的 ``online_chunk_seconds`` 默认值一致。
DEFAULT_BLOCK_SECONDS = 1.0

#: 采集端攒块粒度（毫秒）—— 浏览器 / 板载的默认发送节奏。
DEFAULT_CHUNK_MS = 500


@dataclass(frozen=True)
class AudioChunk:
    """一块符合采集契约的音频。

    * ``pcm`` —— 裸 PCM 字节（s16le / 单声道 / 16 kHz，**无** WAV 头）
    * ``seq`` —— 块序号；从 0 单调递增，**缺号即丢包**
    * ``stream_id`` —— 所属会话
    * ``captured_at`` —— 采集时刻（``time.monotonic()``），仅用于日志
    """

    pcm: bytes
    seq: int
    stream_id: str = ""
    sample_rate: int = CAPTURE_SAMPLE_RATE
    n_channels: int = CAPTURE_N_CHANNELS
    sample_width: int = CAPTURE_SAMPLE_WIDTH
    captured_at: float = field(default_factory=time.monotonic)

    @property
    def n_frames(self) -> int:
        if self.sample_width <= 0 or self.n_channels <= 0:
            return 0
        return len(self.pcm) // (self.sample_width * self.n_channels)

    @property
    def duration_sec(self) -> float:
        if self.sample_rate <= 0:
            return 0.0
        return self.n_frames / float(self.sample_rate)

    @property
    def is_empty(self) -> bool:
        return not self.pcm


def validate_capture_format(
    sample_rate: int,
    n_channels: int,
    sample_width: int,
    *,
    require_16k: bool = True,
) -> None:
    """校验采集格式是否符合契约；不符合抛 ``ValueError``。

    ``require_16k=False`` 时允许任意采样率（供「先收下、后面再说」
    的宽松场景）；但**声道数**和**位宽**任何时候都必须是 1 / 2 ——
    多声道或非 s16 的 PCM 在服务端无法被 SDK 正确解读。
    """
    if require_16k and int(sample_rate) != CAPTURE_SAMPLE_RATE:
        raise ValueError(
            "sample_rate 必须是 {}（当前 {}）".format(
                CAPTURE_SAMPLE_RATE, sample_rate))
    if int(sample_rate) <= 0:
        raise ValueError("sample_rate 必须为正数")
    if int(n_channels) != CAPTURE_N_CHANNELS:
        raise ValueError(
            "n_channels 只支持 {}（当前 {}）".format(
                CAPTURE_N_CHANNELS, n_channels))
    if int(sample_width) != CAPTURE_SAMPLE_WIDTH:
        raise ValueError(
            "sample_width 只支持 {}（s16le，当前 {}）".format(
                CAPTURE_SAMPLE_WIDTH, sample_width))


class ChunkAccumulator:
    """把任意长度的 PCM 推流切成**定长块**。

    用法::

        acc = ChunkAccumulator(block_seconds=1.0)
        for pcm in incoming:
            for block in acc.push(pcm):
                submit(block)          # 整秒的块
        tail = acc.flush()               # 收尾时把剩下的不足一秒的尾巴取出

    统计量
    --------

    * ``total_bytes`` —— 累计推入的字节数
    * ``total_frames`` —— 累计推入的采样数
    * ``dropped_bytes`` —— 被丢弃的字节数（当前实现不丢，见 §6 L3 的
      「有界缓冲」在服务端会话里实现，不在累积器里）
    """

    def __init__(
        self,
        *,
        sample_rate: int = CAPTURE_SAMPLE_RATE,
        n_channels: int = CAPTURE_N_CHANNELS,
        sample_width: int = CAPTURE_SAMPLE_WIDTH,
        block_seconds: float = DEFAULT_BLOCK_SECONDS,
    ) -> None:
        self.sample_rate = int(sample_rate)
        self.n_channels = int(n_channels)
        self.sample_width = int(sample_width)
        self.block_seconds = float(block_seconds)
        self._buf = bytearray()
        self.total_bytes = 0
        self.total_frames = 0
        self.dropped_bytes = 0

    # ------------------------------------------------------------------ #
    @property
    def frame_bytes(self) -> int:
        """每采样帧的字节数（声道数 × 位宽）。"""
        return max(1, self.n_channels * self.sample_width)

    @property
    def block_bytes(self) -> int:
        """一个完整块要多少字节。"""
        return max(self.frame_bytes,
                   int(round(self.block_seconds * self.sample_rate))
                   * self.frame_bytes)

    @property
    def pending_bytes(self) -> int:
        """当前缓冲里还没凑满一块的字节数。"""
        return len(self._buf)

    @property
    def pending_sec(self) -> float:
        return self.pending_bytes / float(self.frame_bytes) / float(
            self.sample_rate or 1)

    @property
    def total_sec(self) -> float:
        if self.sample_rate <= 0:
            return 0.0
        return self.total_frames / float(self.sample_rate)

    # ------------------------------------------------------------------ #
    def push(self, pcm: bytes) -> List[bytes]:
        """推入一段 PCM，返回本次**凑满**的块列表。

        不足一块的尾巴留在缓冲里，等下一次 ``push`` 或 ``flush``。
        """
        if not pcm:
            return []
        self._buf += pcm
        self.total_bytes += len(pcm)
        self.total_frames += len(pcm) // self.frame_bytes

        out: List[bytes] = []
        bb = self.block_bytes
        while len(self._buf) >= bb:
            out.append(bytes(self._buf[:bb]))
            del self._buf[:bb]
        return out

    def flush(self) -> bytes:
        """取出缓冲区里剩下的尾巴（可能为空），并清空缓冲。"""
        if not self._buf:
            return b""
        tail = bytes(self._buf)
        self._buf.clear()
        return tail

    def reset(self) -> None:
        """丢掉缓冲（会话重置时用）。统计量**不清零**。"""
        self._buf.clear()

    def drop_pending(self) -> int:
        """丢掉当前缓冲（丢块时用），返回被丢弃的字节数。"""
        n = len(self._buf)
        self._buf.clear()
        self.dropped_bytes += n
        return n


class PcmFeedBuffer:
    """把 PCM 累积成**整秒块**，并且**永远留住最后 1 个采样**做收尾。

    为什么留住最后 1 个采样
    ----------------------

    SDK 的在线路径有一条硬契约：**带 ``asr_stream_final=True`` 的那次
    调用必须携带本会话的最后一块**。最后一块不打回滚，所以最后几个
    token 才会被确认 —— 若收尾时没有**新**音频可带，SDK 会警告
    「最后 N 个 token 未确认」，最终文本会**丢掉最后几个词**。

    留住最后 1 个采样（2 字节）就够：

    * SDK 内部按**累计采样数**切 1 秒块，与调用方怎么切块无关；
    * 收尾时把这 1 个采样补上，累计数与「一次性喂完整段」**完全一致**
      → 切出来的块边界、每块内容、最后一块的打标全都一致
      → 与文件路径**逐字节等价**；
    * 代价是 1 个采样 = 62.5 µs —— **零可感知延迟**，而「多留一块」
      的方案要付整整 1 秒。

    对比两种收尾策略（总长 16.0 s / 16.5 s）：

    ================  ==========================  ====================
    方案               收尾调用携带                  与文件路径
    ================  ==========================  ====================
    留住最后 1 采样   1.0 s / 0.5 s（= 末块）      **逐字节相同**
    多留一整块         空 / 空（→ 丢最后几个词）      **不一致** ❌
    ================  ==========================  ====================

    用法::

        feed = PcmFeedBuffer(block_seconds=1.0)
        for pcm in incoming:
            for block in feed.push(pcm):
                await submit(block)          # 非 final
        await submit(feed.flush(), final=True)   # 收尾：末块 + 保留的采样
    """

    def __init__(
        self,
        *,
        sample_rate: int = CAPTURE_SAMPLE_RATE,
        n_channels: int = CAPTURE_N_CHANNELS,
        sample_width: int = CAPTURE_SAMPLE_WIDTH,
        block_seconds: float = DEFAULT_BLOCK_SECONDS,
    ) -> None:
        self.sample_rate = int(sample_rate)
        self.n_channels = int(n_channels)
        self.sample_width = int(sample_width)
        self.block_seconds = float(block_seconds)
        self._buf = bytearray()
        self.total_bytes = 0

    # ------------------------------------------------------------------ #
    @property
    def frame_bytes(self) -> int:
        return max(1, self.n_channels * self.sample_width)

    @property
    def block_bytes(self) -> int:
        """一个完整块要多少字节。"""
        return max(self.frame_bytes,
                   int(round(self.block_seconds * self.sample_rate))
                   * self.frame_bytes)

    @property
    def hold_bytes(self) -> int:
        """收尾时留住的字节数（1 个采样）。"""
        return self.frame_bytes

    @property
    def pending_bytes(self) -> int:
        """还没喂出去的字节数（含留住的最后 1 个采样）。"""
        return len(self._buf)

    @property
    def total_sec(self) -> float:
        if self.sample_rate <= 0:
            return 0.0
        return (self.total_bytes / self.frame_bytes) / float(self.sample_rate)

    # ------------------------------------------------------------------ #
    def push(self, pcm: bytes) -> List[bytes]:
        """推入一段 PCM，返回本次**凑满**的块列表（可直接喂给 SDK）。"""
        if not pcm:
            return []
        self._buf += pcm
        self.total_bytes += len(pcm)

        hold = self.hold_bytes
        if len(self._buf) <= hold:
            return []
        # 最后 1 个采样先扣下来 —— 它是「未来的末块」的种子。
        feed = bytes(self._buf[:-hold])
        self._buf = bytearray(self._buf[-hold:])

        blocks: List[bytes] = []
        bb = self.block_bytes
        while len(feed) >= bb:
            blocks.append(feed[:bb])
            feed = feed[bb:]
        # 不足一块的尾巴排在「留住的采样」**之前** → 拼回去时顺序不变。
        if feed:
            self._buf = bytearray(feed) + self._buf
        return blocks

    def flush(self) -> bytes:
        """收尾：返回「剩下的不足一块的音频 + 留住的最后 1 个采样」。

        这就是 ``asr_stream_final=True`` 那次调用要带的东西。
        """
        out = bytes(self._buf)
        self._buf.clear()
        return out

    def reset(self) -> None:
        """丢掉缓冲（会话中止时用）。"""
        self._buf.clear()


# --------------------------------------------------------------------------- #
# Resampling
# --------------------------------------------------------------------------- #
def resample_s16_linear(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    """s16le 单声道**整块**线性插值重采样。

    与 SDK 的 ``_resample_linear`` 同算法：输出第 ``j`` 个采样取自输入
    的全局下标 ``j * src_rate / dst_rate``，floor 取左邻居、线性混合。

    ⚠️ 这是**整块**版本 —— 逐块调用它会每块相位从 0 重来。流式场景
    用 :class:`StreamResampler`。

    ⚠️ **量化是「向零截断」**（``astype("<i2")`` 的语义），**不是**
    四舍五入。浏览器那边（``wdnapps_audio_chat/cards/qwen3_asr/static/app.js`` 的
    ``_resampleTo16k``）必须用同样的规则 —— 差 1 LSB 就会让「跨端
    逐字节一致」（清单 B3）不成立。回归见
    ``scripts/verify_asr_wizard.py`` 的 **V6a**。
    """
    if src_rate == dst_rate or not pcm:
        return pcm
    audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    out = _resample_linear_f32(audio, src_rate, dst_rate)
    return np.clip(out, -32768.0, 32767.0).astype("<i2").tobytes()


def _resample_linear_f32(audio: np.ndarray, src_rate: int,
                         dst_rate: int) -> np.ndarray:
    """线性插值重采样（float32 in / float32 out）。"""
    if src_rate == dst_rate or audio.size == 0:
        return audio.astype(np.float32, copy=False)
    out_len = int(round(audio.size * dst_rate / src_rate))
    if out_len <= 1:
        return np.zeros(1, dtype=np.float32)
    idx = np.arange(out_len) * src_rate / dst_rate
    left = np.floor(idx).astype(np.int64)
    right = np.minimum(left + 1, audio.size - 1)
    frac = idx - left
    return (audio[left] * (1.0 - frac) + audio[right] * frac).astype(np.float32)


class StreamResampler:
    """带 carry 的流式线性重采样器。

    「N 次 :meth:`push` 的拼接结果 == 整段一次性
    :func:`resample_s16_linear` 的结果」—— 这是流式路径的硬要求：
    逐块独立重采样会让每块相位从 0 重来，与整段结果差一个相位。

    算法与 SDK 的 ``wdnai.backends.asr._StreamResampler`` **逐行同构**
    （全局输出索引 + carry + 只在右邻居是真实样本时产出 + 末尾外插推迟
    到 :meth:`flush`）。这里**不复用** SDK 的实现，原因是导入
    ``wdnai.backends.asr`` 会连带加载 RKNN 原生库 —— 在 pytest 收集期
    加载它会让整个测试套件间歇性段错误（已在 SDK 侧踩过）。

    ⚠️ **浏览器那边有一份逐行同构的 JS 实现**
    （``wdnapps_audio_chat/cards/qwen3_asr/static/app.js`` 的 ``_resampleTo16k``）——
    两边的输出必须**逐字节相同**（清单 B3「跨端一致」）。改了任何一边
    都要重跑 ``scripts/verify_asr_wizard.py`` 的 **V6a**。
    """

    def __init__(self, src_rate: int, dst_rate: int) -> None:
        if src_rate <= 0 or dst_rate <= 0:
            raise ValueError("采样率必须为正数")
        if src_rate < dst_rate:
            raise ValueError(
                "StreamResampler 只支持降采样（src_rate >= dst_rate），"
                "收到 {} -> {}".format(src_rate, dst_rate))
        self.src_rate = int(src_rate)
        self.dst_rate = int(dst_rate)
        self.ratio = float(src_rate) / float(dst_rate)
        self._carry = np.zeros((0,), dtype=np.float32)
        self._out_index = 0
        self._total_in = 0

    # -- internals ------------------------------------------------------- #
    def _render(self, x: np.ndarray, base_global: int, j0: int,
                n_new: int) -> np.ndarray:
        """渲染输出采样 ``j0 … j0+n_new-1``。

        索引算术与 SDK 的 ``_StreamResampler._render`` 完全一致（包括
        ``(j * src) / dst`` 的浮点写法），两条路径因此逐样本相同。
        """
        idx = np.arange(j0, j0 + n_new) * self.src_rate / self.dst_rate
        left_global = np.floor(idx).astype(np.int64)
        left = left_global - base_global
        right = np.minimum(left + 1, x.size - 1)
        frac = idx - left_global
        return (x[left] * (1.0 - frac) + x[right] * frac).astype(np.float32)

    def _offline_count(self) -> int:
        """整段路径的输出采样数 ``round(total_in / ratio)``。"""
        return int(round(self._total_in * self.dst_rate / self.src_rate))

    # -- public ---------------------------------------------------------- #
    @property
    def pending_samples(self) -> int:
        """还没被消费的输入采样数（carry）。"""
        return int(self._carry.size)

    @property
    def total_in(self) -> int:
        return self._total_in

    def push(self, pcm: bytes) -> bytes:
        """推入 s16le PCM，返回本次产出的 s16le PCM。"""
        if self.src_rate == self.dst_rate:
            return pcm
        if pcm:
            audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
            x = (np.concatenate([self._carry, audio]) if self._carry.size
                 else audio)
            self._total_in += int(audio.size)
        else:
            x = self._carry
        if x.size == 0:
            return b""

        # 输出 j 需要输入 floor(j*ratio) **和** floor(j*ratio)+1。
        # 只在第二个邻居是真实样本时产出；整段路径对最后一个采样的外插
        # 留给 flush()（只有它知道总长）。
        j_limit = int((self._total_in - 2) // self.ratio) + 1
        n_target = min(j_limit, self._offline_count())
        n_new = n_target - self._out_index
        if n_new <= 0:
            self._carry = x
            return b""

        base_global = self._total_in - x.size
        out = self._render(x, base_global, self._out_index, n_new)
        self._out_index += n_new
        consumed_global = int(np.floor((self._out_index - 1) * self.ratio)) + 1
        self._carry = x[consumed_global - base_global:]
        return np.clip(out, -32768.0, 32767.0).astype("<i2").tobytes()

    def flush(self) -> bytes:
        """收尾：补上整段路径会外插出来的最后几个采样。"""
        if self.src_rate == self.dst_rate:
            return b""
        if self._total_in == 0:
            self._carry = np.zeros((0,), dtype=np.float32)
            return b""
        n_new = self._offline_count() - self._out_index
        if n_new <= 0 or self._carry.size == 0:
            self._carry = np.zeros((0,), dtype=np.float32)
            return b""
        base_global = self._total_in - self._carry.size
        out = self._render(self._carry, base_global, self._out_index, n_new)
        self._out_index += n_new
        self._carry = np.zeros((0,), dtype=np.float32)
        return np.clip(out, -32768.0, 32767.0).astype("<i2").tobytes()
