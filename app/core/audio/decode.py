"""音频 / 视频 → PCM 解码。

**为什么走 PyAV（FFmpeg 软解）**：``wdnav`` 只有 MPP / RGA / V4L2 的**视频**
能力，没有任何音频解码器 —— 音频只能软解。这与
:mod:`app.core.audio.source`（Omni 链路）是**同一个例外**，不是新开的口子。

**两条路径，刻意分开**：

* **WAV** → :func:`app.core.audio.wav.parse_wav`，**原样透传 PCM**。
  不重编码是硬要求：Qwen3-ASR 与 C++ demo 的「逐字节一致」依赖两边喂进
  模型的 PCM 是同一条数据 —— 提前转码就等于换了一条输入链，mel 频谱的
  末位比特会变，token 可能翻转。
* **其它**（mp3 / m4a / flac / ogg / **含音轨的视频** mp4 / mkv / avi…）→
  PyAV 解第一条音轨 → **单声道 / s16le / 保持源采样率**。

  为什么**不**在解码时重采样到 16 kHz：重采样统一交给 wdnai ASR backend
  的线性插值器（``_resample_linear``），这样 WAV 路径与容器路径走的是
  **同一条重采样链**，行为一致。若在这里用 PyAV 的 swr 重采样到 16k，
  容器路径就换成了另一套滤波器，与 C demo 的 miniaudio FIR 既不同源也
  不同实现 —— 一致性就没了。

**时长截断**：C demo 的 ``ONLINE_MAX_AUDIO_SECONDS = 60``（音频过长会拉低
RTF），这里保持同一上限，超出部分**截断**而不是报错 —— 与 C demo 行为一致
（``online_main.cc:399-411``）。
"""
from __future__ import annotations

import io
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, List, Optional, Union

from app.core.audio.wav import parse_wav

LOG = logging.getLogger("core.audio.decode")

__all__ = [
    "DecodedAudio",
    "MediaPcmChunk",
    "decode_media",
    "iter_media_pcm",
    "MAX_AUDIO_SECONDS",
    "AUDIO_EXTS",
    "VIDEO_EXTS",
    "MEDIA_EXTS",
]

#: 最长处理时长（秒）—— 对齐 C demo 的 ``ONLINE_MAX_AUDIO_SECONDS``。
MAX_AUDIO_SECONDS = 60.0

#: 支持的音频容器 / 编码后缀（小写，含点）。
AUDIO_EXTS = (
    ".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma",
    ".amr", ".aiff", ".aif",
)

#: 支持的**视频**容器（取其中的音轨）。
VIDEO_EXTS = (
    ".mp4", ".mov", ".mkv", ".avi", ".flv", ".m4v", ".webm", ".ts",
    ".wmv", ".mpg", ".mpeg", ".3gp",
)

MEDIA_EXTS = AUDIO_EXTS + VIDEO_EXTS


@dataclass
class DecodedAudio:
    """解码后的音频（与 wdnai ``run_audio`` 的入参口径一致）。

    * ``pcm`` —— 裸 PCM 字节（**不是** wav 容器）
    * ``sample_rate`` —— 源采样率（**未**重采样到 16 kHz）
    * ``n_channels`` / ``sample_width`` —— 声道数 / 字节每采样
    * ``origin`` —— ``"wav"``（原样透传）或 ``"container"``（PyAV 解码）
    * ``truncated`` —— 是否因为超过时长上限被截断
    """

    pcm: bytes
    sample_rate: int
    n_channels: int
    sample_width: int
    origin: str = "wav"
    truncated: bool = False
    source_name: str = ""

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


def _is_wav_bytes(raw: bytes) -> bool:
    return len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WAVE"


def _looks_like_wav_path(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            head = f.read(12)
    except OSError:
        return False
    return _is_wav_bytes(head)


def _truncate(pcm: bytes, sample_rate: int, n_channels: int,
              sample_width: int, max_seconds: Optional[float]) -> tuple:
    """按 ``max_seconds`` 截断 PCM。返回 ``(pcm, truncated)``。"""
    if not max_seconds or max_seconds <= 0 or sample_rate <= 0:
        return pcm, False
    frame_bytes = max(1, n_channels * sample_width)
    max_frames = int(max_seconds * sample_rate)
    if len(pcm) <= max_frames * frame_bytes:
        return pcm, False
    return pcm[: max_frames * frame_bytes], True


def _decode_with_pyav(src: Any, source_name: str,
                      max_seconds: Optional[float]) -> DecodedAudio:
    """PyAV 解码第一条音轨 → 单声道 s16le（保持源采样率）。"""
    try:
        import av
    except Exception as exc:  # noqa: BLE001
        raise ValueError("PyAV 不可用: {}".format(exc)) from exc

    try:
        container = av.open(src, timeout=(3.0, 10.0))
    except Exception as exc:  # noqa: BLE001
        raise ValueError("无法打开媒体文件: {}: {}".format(
            type(exc).__name__, exc)) from exc

    try:
        streams = list(container.streams.audio)
        if not streams:
            raise ValueError("媒体文件里没有音轨")
        stream = streams[0]
        src_rate = int(getattr(stream, "rate", 0) or 0) or 48000

        # 不指定 rate → 输出保持源采样率（重采样交给 wdnai 的线性插值器）。
        resampler = av.AudioResampler(format="s16", layout="mono")

        buf = bytearray()
        out_rate = src_rate
        for frame in container.decode(audio=0):
            out_rate = int(getattr(frame, "sample_rate", 0) or 0) or out_rate
            resampled = resampler.resample(frame)
            items = resampled if isinstance(resampled, list) else [resampled]
            for item in items:
                if item is None:
                    continue
                out_rate = int(getattr(item, "sample_rate", 0) or 0) or out_rate
                buf += item.to_ndarray().tobytes()
            # 边解边截：超过上限就不再攒了（避免长视频把内存吃满）。
            if max_seconds and len(buf) >= int(max_seconds * out_rate) * 2:
                break
        # flush 掉重采样器里剩下的尾巴
        try:
            tail = resampler.resample(None)
            items = tail if isinstance(tail, list) else [tail]
            for item in items:
                if item is not None:
                    buf += item.to_ndarray().tobytes()
        except Exception:  # noqa: BLE001 — flush 失败不影响已解出的部分
            pass

        pcm = bytes(buf)
        pcm, truncated = _truncate(pcm, out_rate, 1, 2, max_seconds)
        if not pcm:
            raise ValueError("音轨解码后没有数据")
        return DecodedAudio(
            pcm=pcm,
            sample_rate=out_rate,
            n_channels=1,
            sample_width=2,
            origin="container",
            truncated=truncated,
            source_name=source_name,
        )
    finally:
        try:
            container.close()
        except Exception:  # noqa: BLE001
            pass


def decode_media(source: Union[bytes, str, os.PathLike],
                 *, filename: str = "",
                 max_seconds: Optional[float] = MAX_AUDIO_SECONDS) -> DecodedAudio:
    """把音频 / 视频解码成 PCM。

    Parameters
    ----------
    source
        * ``bytes`` —— 上传的文件内容；
        * ``str`` / ``PathLike`` —— 设备端文件路径。
    filename
        仅用于报错信息与日志。
    max_seconds
        最长处理时长（秒）；``None`` / ``<= 0`` 表示不截断。
        默认 :data:`MAX_AUDIO_SECONDS`（60 s，对齐 C demo）。

    Raises
    ------
    ValueError
        打不开、不是支持的媒体、或者没有音轨。
    """
    name = filename or (os.path.basename(str(source)) if not isinstance(
        source, bytes) else "")

    if isinstance(source, (bytes, bytearray, memoryview)):
        raw = bytes(source)
        if not raw:
            raise ValueError("文件内容为空")
        if _is_wav_bytes(raw):
            wav = parse_wav(raw)
            pcm, truncated = _truncate(wav.pcm, wav.sample_rate,
                                       wav.n_channels, wav.sample_width,
                                       max_seconds)
            return DecodedAudio(
                pcm=pcm,
                sample_rate=wav.sample_rate,
                n_channels=wav.n_channels,
                sample_width=wav.sample_width,
                origin="wav",
                truncated=truncated,
                source_name=name,
            )
        # 非 WAV 的上传内容 → PyAV 从内存里解
        return _decode_with_pyav(io.BytesIO(raw), name, max_seconds)

    path = Path(source)
    if not path.is_file():
        raise ValueError("文件不存在: {}".format(path))
    if _looks_like_wav_path(path):
        wav = parse_wav(path.read_bytes())
        pcm, truncated = _truncate(wav.pcm, wav.sample_rate,
                                   wav.n_channels, wav.sample_width,
                                   max_seconds)
        return DecodedAudio(
            pcm=pcm,
            sample_rate=wav.sample_rate,
            n_channels=wav.n_channels,
            sample_width=wav.sample_width,
            origin="wav",
            truncated=truncated,
            source_name=name or path.name,
        )
    return _decode_with_pyav(str(path), name or path.name, max_seconds)


# --------------------------------------------------------------------------- #
# 流式解码
# --------------------------------------------------------------------------- #
@dataclass
class MediaPcmChunk:
    """流式解码产出的一块 PCM。

    * ``pcm`` —— 裸 PCM 字节（**不是** wav 容器）
    * ``sample_rate`` —— 源采样率（**未**重采样到 16 kHz）
    * ``n_channels`` / ``sample_width`` —— 声道数 / 字节每采样
    * ``origin`` —— ``"wav"``（原样透传）或 ``"container"``（PyAV 解码）
    * ``source_name`` —— 来源名（日志用）
    * ``total_sec`` —— 已知总时长（容器可从流头拿到；WAV 也能算），
      未知时为 0
    * ``origin_sec`` —— **音轨在容器里的起点**（秒）。WAV 恒为 0；
      容器里音轨可能晚于画面起（如 3.5 s），字幕对齐要拿它换算：
      ``音频时间 = 播放头时间 - origin_sec``。
    * ``media_start_sec`` —— 本次解码**第一块**样本在**容器时间轴**上的
      位置（秒）。从中间位置开始解码时（``start_sec``），第一块不一定
      正好落在 ``start_sec``（seek 只能落到最近的包边界）→ 调用方靠它把
      「已解码时长」换算回**绝对**音频时间，字幕才不会错位。
    """

    pcm: bytes
    sample_rate: int
    n_channels: int
    sample_width: int
    origin: str = "wav"
    source_name: str = ""
    total_sec: float = 0.0
    origin_sec: float = 0.0
    media_start_sec: float = 0.0

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


def _iter_wav_pcm(raw: bytes, name: str, chunk_seconds: float,
                  start_sec: float = 0.0, end_sec: Optional[float] = None
                  ) -> Iterator[MediaPcmChunk]:
    """WAV → 逐块 PCM（原样透传，不重采样、不转码）。

    ``start_sec`` / ``end_sec`` —— 只取这段区间（WAV 是定长 PCM，直接
    按采样数切就行，不需要 seek）。
    """
    wav = parse_wav(raw)
    frame_bytes = max(1, wav.n_channels * wav.sample_width)
    total = wav.duration_sec
    # 每次切一块（按**帧**对齐，别把采样切一半）
    step = max(frame_bytes,
               int(round(chunk_seconds * wav.sample_rate)) * frame_bytes)
    pcm = wav.pcm
    # 起点：按采样数跳过（比按字节准，不会切半个采样）
    if start_sec > 0:
        skip = int(round(start_sec * wav.sample_rate)) * frame_bytes
        pcm = pcm[skip:]
    # 终点：截断
    if end_sec is not None and end_sec > 0:
        keep = int(round(end_sec * wav.sample_rate)) * frame_bytes
        if start_sec > 0:
            keep -= int(round(start_sec * wav.sample_rate)) * frame_bytes
        pcm = pcm[:max(0, keep)]
    for off in range(0, len(pcm), step):
        piece = pcm[off:off + step]
        if not piece:
            break
        yield MediaPcmChunk(
            pcm=piece,
            sample_rate=wav.sample_rate,
            n_channels=wav.n_channels,
            sample_width=wav.sample_width,
            origin="wav",
            source_name=name,
            total_sec=total,
            media_start_sec=start_sec,
        )


def _iter_pyav_pcm(src: Any, name: str, chunk_seconds: float,
                   start_sec: float = 0.0, end_sec: Optional[float] = None
                   ) -> Iterator[MediaPcmChunk]:
    """PyAV 增量解码第一条音轨 → 单声道 s16le（保持源采样率）。

    **不落全量** —— 一帧一帧地解、一块一块地产出。长视频因此不会把
    内存吃满（这是「文件 / 视频模式不设 60 s 上限」的前提）。

    ``start_sec`` / ``end_sec`` —— 只解这一段（**容器时间轴**，秒）。
    这是「拖动进度条 → 识别跟过去」的基础：不能每次都从头解一遍
    98 分钟的音轨。
    """
    try:
        import av
    except Exception as exc:  # noqa: BLE001
        raise ValueError("PyAV 不可用: {}".format(exc)) from exc

    try:
        container = av.open(src, timeout=(3.0, 10.0))
    except Exception as exc:  # noqa: BLE001
        raise ValueError("无法打开媒体文件: {}: {}".format(
            type(exc).__name__, exc)) from exc

    try:
        streams = list(container.streams.audio)
        if not streams:
            raise ValueError("媒体文件里没有音轨")
        stream = streams[0]
        src_rate = int(getattr(stream, "rate", 0) or 0) or 48000
        # 容器头里的时长（秒）；拿不到就 0（未知）
        total = 0.0
        dur = getattr(stream, "duration", None)
        tb = getattr(stream, "time_base", None)
        if dur and tb:
            try:
                total = float(dur * tb)
            except Exception:  # noqa: BLE001
                total = 0.0
        # ⚠️ **音轨在容器里的起点**。多数文件是 0，但音轨晚于画面起点的
        # 文件很常见（直播录制、转码产物）。字幕对齐要用它换算：
        # ``音频时间 = 播放头时间 - origin_sec``。
        origin_sec = 0.0
        st = getattr(stream, "start_time", None)
        if st and tb:
            try:
                origin_sec = float(st * tb)
            except Exception:  # noqa: BLE001
                origin_sec = 0.0

        # ⚠️ **seek 到起点**。mp4 的音频是逐包存储的 → 能 seek 到
        # 「最近的包边界」，不一定正好落在 ``start_sec`` → 解出来的第一块
        # 位置由 ``media_start_sec`` 报告给调用方（它把「已解码时长」换算回
        # 绝对音频时间，字幕才不会错位）。
        if start_sec > 0 and tb:
            try:
                container.seek(int(start_sec / float(tb)), stream=stream,
                               backward=True, any_frame=True)
            except Exception:  # noqa: BLE001 — seek 失败就从头解
                try:
                    container.seek(int(start_sec / float(tb)), stream=stream,
                                   backward=True)
                except Exception:  # noqa: BLE001
                    pass

        # 不指定 rate → 输出保持源采样率（重采样交给 wdnai 的线性插值器）
        resampler = av.AudioResampler(format="s16", layout="mono")
        out_rate = src_rate
        buf = bytearray()
        #: 本次解码**第一块**样本的容器时间（seek 之后不一定 = start_sec）
        first_media_sec = 0.0

        def _chunk_meta() -> MediaPcmChunk:
            return MediaPcmChunk(
                pcm=b"",
                sample_rate=out_rate,
                n_channels=1,
                sample_width=2,
                origin="container",
                source_name=name,
                total_sec=total,
                origin_sec=origin_sec,
                media_start_sec=first_media_sec,
            )

        for frame in container.decode(audio=0):
            fpts = None
            if frame.pts is not None and frame.time_base is not None:
                try:
                    fpts = float(frame.pts * frame.time_base)
                except Exception:  # noqa: BLE001
                    fpts = None
            if fpts is not None:
                if end_sec is not None and end_sec > 0 and fpts >= end_sec:
                    break
                fdur = 0.0
                if frame.sample_rate:
                    fdur = float(frame.samples) / float(frame.sample_rate)
                # 整帧都在起点之前 → 丢掉（seek 落到了前一个包）
                if start_sec > 0 and fpts + fdur <= start_sec:
                    continue
                if not first_media_sec:
                    first_media_sec = fpts
            out_rate = int(getattr(frame, "sample_rate", 0) or 0) or out_rate
            resampled = resampler.resample(frame)
            items = resampled if isinstance(resampled, list) else [resampled]
            for item in items:
                if item is None:
                    continue
                out_rate = int(getattr(item, "sample_rate", 0) or 0) or out_rate
                buf += item.to_ndarray().tobytes()
                need = int(round(chunk_seconds * out_rate)) * 2
                while len(buf) >= need:
                    meta = _chunk_meta()
                    meta.pcm = bytes(buf[:need])
                    del buf[:need]
                    yield meta

        # flush 掉重采样器里剩下的尾巴
        try:
            tail = resampler.resample(None)
            items = tail if isinstance(tail, list) else [tail]
            for item in items:
                if item is not None:
                    out_rate = int(getattr(item, "sample_rate", 0) or 0) or out_rate
                    buf += item.to_ndarray().tobytes()
        except Exception:  # noqa: BLE001 — flush 失败不影响已解出的部分
            pass

        need = int(round(chunk_seconds * out_rate)) * 2
        while len(buf) >= need:
            meta = _chunk_meta()
            meta.pcm = bytes(buf[:need])
            del buf[:need]
            yield meta
        if buf:
            meta = _chunk_meta()
            meta.pcm = bytes(buf)
            yield meta
    finally:
        try:
            container.close()
        except Exception:  # noqa: BLE001
            pass


def iter_media_pcm(source: Union[bytes, str, os.PathLike],
                   *, filename: str = "",
                   chunk_seconds: float = 1.0,
                   start_sec: float = 0.0,
                   end_sec: Optional[float] = None) -> Iterator[MediaPcmChunk]:
    """**流式**解码音频 / 视频 → 逐块 PCM。

    与 :func:`decode_media` 的区别：

    ==================  ==========================  ==========================
                        ``decode_media``            ``iter_media_pcm``
    ==================  ==========================  ==========================
    内存                 **全量**（长视频会吃满）     **不落全量**（边解边产）
    时长上限             默认 60 s（截断）            **没有上限**
    产出                 一次 ``DecodedAudio``       逐块 ``MediaPcmChunk``
    ==================  ==========================  ==========================

    「流式语音识别」卡片的文件 / 视频路径走**后者** —— 长录音不再被
    砍掉 60 s 之后的尾巴（那是 C demo 离线在线编码器的限制，见
    :data:`MAX_AUDIO_SECONDS` 的说明）。

    ⚠️ 这是**阻塞 I/O**（PyAV 解码 / 读文件）—— 必须在 worker 线程里
    迭代，不能在事件循环里跑。

    ``start_sec`` / ``end_sec`` —— 只解这一段（**容器时间轴**，秒）。
    「拖动进度条 → 识别跟过去」靠它：不能每次都从头解一遍 98 分钟的
    音轨（那是几十秒的纯浪费）。
    """
    name = filename or (os.path.basename(str(source)) if not isinstance(
        source, (bytes, bytearray, memoryview)) else "")
    chunk_seconds = max(0.1, float(chunk_seconds or 1.0))
    start_sec = max(0.0, float(start_sec or 0.0))

    if isinstance(source, (bytes, bytearray, memoryview)):
        raw = bytes(source)
        if not raw:
            raise ValueError("文件内容为空")
        if _is_wav_bytes(raw):
            yield from _iter_wav_pcm(raw, name, chunk_seconds,
                                     start_sec, end_sec)
            return
        yield from _iter_pyav_pcm(io.BytesIO(raw), name, chunk_seconds,
                                  start_sec, end_sec)
        return

    path = Path(source)
    if not path.is_file():
        raise ValueError("文件不存在: {}".format(path))
    if _looks_like_wav_path(path):
        yield from _iter_wav_pcm(path.read_bytes(), name or path.name,
                                 chunk_seconds, start_sec, end_sec)
        return
    yield from _iter_pyav_pcm(str(path), name or path.name, chunk_seconds,
                              start_sec, end_sec)
