"""音频源抽象 —— 文件 / RTSP 两种来源的「取一段连续音频」。

``wdnav`` 只有 MPP / RGA / V4L2 的**视频**能力，没有任何音频采集器，音频
只能走 PyAV(FFmpeg) 软解 —— 这是设计原则 2 的唯一例外。

两种来源的语义**不同**，接口统一但实现分家：

* **文件源**：音轨有尽头，段**不能丢**（丢了内容就永久缺失）→ 顺序解码 +
  有界队列，满了阻塞生产者。
* **RTSP 源**：音轨没尽头，段**必须丢**（不丢滞后会越滚越大）→ 环形缓冲
  取**尾部最新**的一段。

``next_chunk()`` 对两种源是不同的动作，调用方（轮转调度器）不区分：
文件源 = 顺序取下一段，RTSP 源 = 取最新一段。

返回 ``None`` 有两种含义，用 :attr:`exhausted` 区分：还没准备好 → 本轮跳过
下轮再来；已放完 → 该路标记完成。文件源放完是正常结束，RTSP 源永远不
``exhausted``（只能靠用户停止）。

Omni-3B 单次推理上限 **6.0s** —— 那是 RKNN 音频编码器的编译期输入形状
``[1, 128, 600]``，超长会被 ``mel[:, :600]`` 静默截断，所以切片责任在本模块。

形状与 dtype 与 ``wdnai`` 的 ``run_audio`` 对齐：16kHz / mono / s16le。
"""
from __future__ import annotations

import collections
import logging
import math
import os
import queue
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Deque, List, Optional

LOG = logging.getLogger("wdnwebui.audio.source")


# Omni 单次推理的窗口上限（秒）。= 600 帧 × HOP_LENGTH 160 / SAMPLE_RATE 16000。
# 与 wdnai/backends/omni.py 的 AUDIO_SHAPES[0]["n_frame"]=600 一致。
DEFAULT_WINDOW_S = 6.0
SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2          # s16le
# SDK 硬门槛：omni.py 的 run_audio 里 `len(audio) < N_FFT(400)` 抛
# "audio too short"。400 samples @ 16kHz = 25ms。不足这个长度的尾巴段丢弃。
MIN_CHUNK_S = 0.025

# 源状态
STATE_IDLE = "idle"
STATE_STARTING = "starting"
STATE_READY = "ready"
STATE_WARMING = "warming"     # RTSP 还没攒够一个窗口
STATE_NO_AUDIO = "no_audio"   # 没有音轨
STATE_ERROR = "error"
STATE_DONE = "done"           # 已放完（文件源）

# 文件源：生产者队列深度。2 段足够让轮转线程永远有料可取，内存压在
# 32 路 × 2 段 × 192KB ≈ 12MB。
FILE_QUEUE_DEPTH = 2
# RTSP 源：环形缓冲保留的时长。要 > 一个窗口，给重连留余量。
RTSP_RING_SECONDS = 8.0
# RTSP 重连退避（秒）。
RTSP_RECONNECT_BACKOFF = 2.0


@dataclass
class AudioChunk:
    """一段连续音频。

    字段口径
    --------
    * ``pcm`` —— 16kHz / mono / s16le 裸字节（**不是** wav 容器）
    * ``win_start_s`` / ``win_end_s`` —— 该段在**源时间轴**上的位置。
      文件源 = 文件内的秒数；RTSP 源 = 该流启动以来的秒数。
    * ``captured_at`` —— 该段被**产出**的 wall clock 时刻（不是送检时刻）
    * ``duration_s`` —— 实际长度。尾巴段可能 < 一个窗口，如实记。
    """

    pcm: bytes
    win_start_s: float
    win_end_s: float
    captured_at: float
    duration_s: float


class AudioSource(ABC):
    """音频源基类。

    子类负责「不断产出 :class:`AudioChunk`」，本类只定义取用契约。
    """

    #: 总段数。文件源在 open 时按音轨时长算出；RTSP 源为 None（无穷）
    total_chunks: Optional[int] = None
    #: 源状态
    state: str = STATE_IDLE
    #: 段已全部产出且被取走 → 该路可以标记完成
    exhausted: bool = False

    def __init__(self, source: str, *, window_s: float = DEFAULT_WINDOW_S,
                 rate: int = SAMPLE_RATE) -> None:
        self.source = source
        self.window_s = float(window_s)
        self.rate = int(rate)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._error = ""

    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """启动采集线程。子类实现。"""
        raise NotImplementedError

    @abstractmethod
    def next_chunk(self) -> Optional[AudioChunk]:
        """取一段。

        返回 ``None`` 表示「现在没有」—— 要么还没准备好，要么已经放完，
        两种情况用 :attr:`exhausted` 区分。
        """
        raise NotImplementedError

    def stop(self) -> None:
        """停止采集，回收线程。"""
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=2.0)
        self._thread = None

    def primed(self) -> bool:
        """首段是否就绪（或已确定放完）。会话 open 后等它归位再开轮转。"""
        return self.state in (STATE_READY, STATE_NO_AUDIO, STATE_DONE,
                              STATE_ERROR) or self.exhausted

    def snapshot(self) -> dict:
        return {
            "kind": self.kind,
            "state": self.state,
            "total_chunks": self.total_chunks,
            "exhausted": self.exhausted,
            "error": self._error,
        }

    @property
    def kind(self) -> str:
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# 文件源
# --------------------------------------------------------------------------- #
class FileAudioSource(AudioSource):
    """带音轨的视频文件（mp4 等）。

    **顺序惰性解码**，不用 seek：``win_start_s`` 按采样点累计得出，不依赖
    seek 精度（VFR 文件的 seek 会偏），且音轨放完能自然 ``exhausted``，
    不需要额外的结束协议。

    实测（本机 720p + AAC 44.1k 立体声）：整条 30s 音轨顺序解码 + 重采样
    共 280ms，即 **56ms/段**；相对 700ms 的推理占 8%。
    """

    def __init__(self, source: str, *, window_s: float = DEFAULT_WINDOW_S,
                 rate: int = SAMPLE_RATE,
                 queue_depth: int = FILE_QUEUE_DEPTH) -> None:
        super().__init__(source, window_s=window_s, rate=rate)
        self._q: "queue.Queue[AudioChunk]" = queue.Queue(maxsize=queue_depth)
        self._producer_finished = False

    @property
    def kind(self) -> str:
        return "file"

    # ------------------------------------------------------------------ #
    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop = threading.Event()
            self._producer_finished = False
            self.state = STATE_STARTING
        # 探测（开容器 + 读音轨元信息）必须在**生产者线程里**做：放主线程
        # 串行探测 32 路是 32 × av.open(~200ms) = 6.5s，放进线程则并行，
        # 总耗时≈单个。
        self._thread = threading.Thread(
            target=self._producer_loop,
            name=f"audio-file-{os.path.basename(self.source)}",
            daemon=True)
        self._thread.start()

    def _state_no_audio(self) -> None:
        self.state = STATE_NO_AUDIO
        self.exhausted = True
        self.total_chunks = 0

    # ------------------------------------------------------------------ #
    def _producer_loop(self) -> None:
        """顺序解码音轨 → 每满一个窗口产一段 → 塞进有界队列。"""
        try:
            import av
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._error = f"PyAV 不可用: {exc}"
                self.state = STATE_ERROR
                self.exhausted = True
            return

        need = int(self.window_s * self.rate)
        try:
            container = av.open(self.source, timeout=(2.0, 5.0))
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._error = f"打开失败: {type(exc).__name__}: {exc}"
                self.state = STATE_ERROR
                self.exhausted = True
            return

        try:
            streams = list(container.streams.audio)
            if not streams:
                with self._lock:
                    self._state_no_audio()
                return
            st = streams[0]
            # 音轨时长 → 总段数（进度显示用）。探测不准（duration=0）时留
            # None，前端显示「≥N」。
            if st.duration and st.time_base:
                dur = float(st.duration * float(st.time_base))
                if dur > 0:
                    with self._lock:
                        self.total_chunks = max(
                            1, int(math.ceil(dur / self.window_s)))
            resampler = av.AudioResampler(format="s16", layout="mono",
                                          rate=self.rate)
            buf = bytearray()
            produced_samples = 0          # 已解码的采样点总数（算时间轴用）
            tail: Optional[bytes] = None

            for frame in container.decode(audio=0):
                if self._stop.is_set():
                    return
                resampled = resampler.resample(frame)
                items = resampled if isinstance(resampled, list) else [resampled]
                for item in items:
                    if item is None:
                        continue
                    buf += item.to_ndarray().tobytes()
                    while len(buf) >= need * SAMPLE_WIDTH:
                        pcm = bytes(buf[:need * SAMPLE_WIDTH])
                        del buf[:need * SAMPLE_WIDTH]
                        if not self._put_chunk(pcm, produced_samples,
                                                self.window_s):
                            return
                        produced_samples += need
            # 音轨放完 → 尾巴段（可能不足一个窗口）
            if buf:
                tail = bytes(buf)
                buf = bytearray()
            if tail is not None:
                n = len(tail) // SAMPLE_WIDTH
                # 太短（< SDK 的 25ms 门槛）直接丢，否则 SDK 会抛
                # "audio too short"。丢掉的是几十毫秒的尾音，不是内容缺失。
                if n * (1.0 / self.rate) >= MIN_CHUNK_S:
                    if not self._put_chunk(tail, produced_samples,
                                           n / self.rate):
                        return
                    produced_samples += n
        except Exception as exc:  # noqa: BLE001
            if not self._stop.is_set():
                LOG.warning("audio file source %s 解码失败: %s",
                            self.source, exc)
                with self._lock:
                    self._error = f"{type(exc).__name__}: {exc}"
                    self.state = STATE_ERROR
        finally:
            try:
                container.close()
            except Exception:  # noqa: BLE001
                pass
            # 队列里可能还有段没被取走 —— exhausted 要等它们取完才为 True
            with self._lock:
                if self.state not in (STATE_ERROR, STATE_NO_AUDIO):
                    self.state = STATE_READY
                self._producer_finished = True

    def _put_chunk(self, pcm: bytes, start_sample: int,
                   duration_s: float) -> bool:
        """把一段塞进队列。队列满时**阻塞**（不丢段）。返回 False = 被停止。"""
        chunk = AudioChunk(
            pcm=pcm,
            win_start_s=start_sample / self.rate,
            win_end_s=(start_sample + int(duration_s * self.rate)) / self.rate,
            captured_at=time.time(),
            duration_s=duration_s,
        )
        while not self._stop.is_set():
            try:
                self._q.put(chunk, timeout=0.2)
                with self._lock:
                    self.state = STATE_READY
                return True
            except queue.Full:
                continue
        return False

    def next_chunk(self) -> Optional[AudioChunk]:
        try:
            return self._q.get_nowait()
        except queue.Empty:
            with self._lock:
                if self._producer_finished and self._q.empty():
                    self.exhausted = True
                    # **不要覆盖 no_audio / error**：那是「这一路没得分析」的
                    # 原因，比「放完了」更有信息量 —— 前端要靠它区分
                    # 「音轨里没有声音」和「音轨分析完毕」。
                    if self.state not in (STATE_NO_AUDIO, STATE_ERROR):
                        self.state = STATE_DONE
            return None


# --------------------------------------------------------------------------- #
# RTSP 源
# --------------------------------------------------------------------------- #
class RtspAudioSource(AudioSource):
    """RTSP 直播流。

    **常驻解码 + 环形缓冲**：后台线程一直解，PCM 写进环形缓冲，
    ``next_chunk()`` 取**尾部最新**的一段。缓冲一旦跑起来，任何时刻都能立刻
    取到「最近 6s」，只有第一次（缓冲没攒够）才要等。

    断流重连：解码抛异常 → 退避 → 重开流 → **清空缓冲**。清空是必须的：
    留着断流前后的两段，拼起来的 6s 中间会有一段是旧数据/空洞。
    """

    def __init__(self, source: str, *, window_s: float = DEFAULT_WINDOW_S,
                 rate: int = SAMPLE_RATE) -> None:
        super().__init__(source, window_s=window_s, rate=rate)
        self._ring: Deque[bytes] = collections.deque()
        self._ring_bytes = 0
        self._ring_cap = int(RTSP_RING_SECONDS * rate * SAMPLE_WIDTH)
        self._decoded_samples = 0
        self._producer_finished = False

    @property
    def kind(self) -> str:
        return "rtsp"

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop = threading.Event()
            self.state = STATE_STARTING
        self._thread = threading.Thread(
            target=self._producer_loop, name="audio-rtsp", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------------ #
    def _producer_loop(self) -> None:
        need = int(self.window_s * self.rate)
        backoff = RTSP_RECONNECT_BACKOFF
        while not self._stop.is_set():
            try:
                import av
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    self._error = f"PyAV 不可用: {exc}"
                    self.state = STATE_ERROR
                return
            try:
                container = av.open(self.source, timeout=(2.0, 5.0))
            except Exception as exc:  # noqa: BLE001
                LOG.warning("audio rtsp %s 打开失败（%.1fs 后重试）: %s",
                            self.source, backoff, exc)
                with self._lock:
                    self._error = f"{type(exc).__name__}: {exc}"
                    self.state = STATE_ERROR
                if self._stop.wait(backoff):
                    return
                backoff = min(backoff * 2, 15.0)
                continue

            try:
                streams = list(container.streams.audio)
                if not streams:
                    with self._lock:
                        self._error = "流无音轨"
                        self.state = STATE_NO_AUDIO
                        self.exhausted = True
                    return
                resampler = av.AudioResampler(format="s16", layout="mono",
                                              rate=self.rate)
                backoff = RTSP_RECONNECT_BACKOFF
                for frame in container.decode(audio=0):
                    if self._stop.is_set():
                        return
                    resampled = resampler.resample(frame)
                    items = (resampled if isinstance(resampled, list)
                             else [resampled])
                    for item in items:
                        if item is None:
                            continue
                        self._append(item.to_ndarray().tobytes())
                    with self._lock:
                        if self._ring_bytes >= need * SAMPLE_WIDTH:
                            if self.state != STATE_READY:
                                self.state = STATE_READY
                        elif self.state != STATE_READY:
                            self.state = STATE_WARMING
            except Exception as exc:  # noqa: BLE001
                if self._stop.is_set():
                    return
                LOG.warning("audio rtsp %s 解码中断（%.1fs 后重连）: %s",
                            self.source, backoff, exc)
                with self._lock:
                    self._error = f"{type(exc).__name__}: {exc}"
                    self.state = STATE_ERROR
                    # 断流 → 缓冲作废。留着的话拼出来的「最近 6s」中间会有
                    # 一段是断流前的旧数据，那不是真实的连续 6 秒。
                    self._ring.clear()
                    self._ring_bytes = 0
            finally:
                try:
                    container.close()
                except Exception:  # noqa: BLE001
                    pass
            if self._stop.wait(backoff):
                return
            backoff = min(backoff * 2, 15.0)
        with self._lock:
            self._producer_finished = True

    def _append(self, pcm: bytes) -> None:
        with self._lock:
            self._ring.append(pcm)
            self._ring_bytes += len(pcm)
            self._decoded_samples += len(pcm) // SAMPLE_WIDTH
            while self._ring_bytes > self._ring_cap and len(self._ring) > 1:
                old = self._ring.popleft()
                self._ring_bytes -= len(old)

    def next_chunk(self) -> Optional[AudioChunk]:
        need = int(self.window_s * self.rate) * SAMPLE_WIDTH
        with self._lock:
            if self._ring_bytes < need:
                if self.state == STATE_READY:
                    self.state = STATE_WARMING
                return None
            # 取尾部 need 字节：从队尾往回凑。
            parts: List[bytes] = []
            got = 0
            for blk in reversed(self._ring):
                parts.append(blk)
                got += len(blk)
                if got >= need:
                    break
            parts.reverse()
            pcm = b"".join(parts)[-need:]
            end_sample = self._decoded_samples
            start_sample = end_sample - need // SAMPLE_WIDTH
            if self.state != STATE_READY:
                self.state = STATE_READY
        return AudioChunk(
            pcm=pcm,
            win_start_s=start_sample / self.rate,
            win_end_s=end_sample / self.rate,
            captured_at=time.time(),
            duration_s=need / SAMPLE_WIDTH / self.rate,
        )


# --------------------------------------------------------------------------- #
def make_audio_source(source: str, *, window_s: float = DEFAULT_WINDOW_S,
                      rate: int = SAMPLE_RATE) -> AudioSource:
    """按 URL 形态选实现。

    带 ``://`` 的一律当网络流（rtsp / rtmp / http…）。**rtsp:// 之外的协议
    不做特殊处理**：PyAV 能开就开，开不了就在状态里报错。
    """
    src = (source or "").strip()
    if not src:
        raise ValueError("source 不能为空")
    if "://" in src:
        return RtspAudioSource(src, window_s=window_s, rate=rate)
    if not os.path.isfile(src):
        raise FileNotFoundError(f"音频源不存在: {src!r}")
    return FileAudioSource(src, window_s=window_s, rate=rate)
