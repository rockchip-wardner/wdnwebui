"""WAV 解析 —— 上传的音频 → (PCM, 采样率, 声道数, 采样位宽)。

**为什么自己解析而不用标准库 ``wave``**：``wave`` 拒绝
``WAVE_FORMAT_IEEE_FLOAT``（format tag 3），而 TTS 链路
（``Qwen3TTSDriver._write_wav``）产出的正是这种格式；浏览器/录音工具
也常吐 float32。标准库还只认 s16。

支持的格式：

* tag 1（PCM）—— s16le / s24le / s32le 原样透传；
* tag 3（IEEE float32）—— 下混成单声道并量化成 s16le。

**采样位宽是原样保留的**：ASR worker 侧把 PCM 按位宽解码成 float32，
不同位宽的量化误差不同，别在这里提前转码 —— 转了就和 C demo 的
输入不是同一条数据了。
"""
from __future__ import annotations

import io
import struct
from dataclasses import dataclass

import numpy as np


__all__ = ["WavAudio", "parse_wav"]


@dataclass(frozen=True)
class WavAudio:
    """一段已解码的 WAV 音频。"""

    pcm: bytes
    sample_rate: int
    n_channels: int
    sample_width: int

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


def _iter_chunks(raw: bytes):
    """Yield ``(chunk_id, payload)`` for a RIFF/WAVE byte string."""
    bio = io.BytesIO(raw)
    header = bio.read(12)
    if len(header) < 12 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
        raise ValueError("不是 RIFF/WAVE 文件")
    while True:
        head = bio.read(8)
        if len(head) < 8:
            return
        cid = head[:4]
        csize = struct.unpack("<I", head[4:8])[0]
        payload = bio.read(csize)
        yield cid, payload
        if csize % 2:          # RIFF chunks are word-aligned
            bio.read(1)


def parse_wav(raw: bytes) -> WavAudio:
    """Parse a WAV byte string.

    Raises
    ------
    ValueError
        不是 WAV、缺少 ``fmt``/``data`` 块，或格式不支持。
    """
    fmt_tag = n_channels = sample_rate = sample_width = 0
    data = b""
    for cid, payload in _iter_chunks(raw):
        if cid == b"fmt " and len(payload) >= 16:
            fmt_tag, n_channels, sample_rate, _, _, bits = struct.unpack(
                "<HHIIHH", payload[:16]
            )
            sample_width = bits // 8
        elif cid == b"data":
            data = payload
            break

    if not fmt_tag:
        raise ValueError("WAV 缺少 fmt 块")
    if not data:
        raise ValueError("WAV 缺少 data 块")

    if fmt_tag == 1 and sample_width in (2, 3, 4):
        # Integer PCM — pass the payload through untouched.
        return WavAudio(data, sample_rate, n_channels, sample_width)

    if fmt_tag == 3 and sample_width == 4:
        audio = np.frombuffer(data, dtype="<f4").copy()
        if n_channels > 1:
            audio = audio.reshape(-1, n_channels).mean(axis=1)
        np.clip(audio, -1.0, 1.0, out=audio)
        pcm = (audio * 32767.0).astype("<i2").tobytes()
        return WavAudio(pcm, sample_rate, 1, 2)

    raise ValueError(
        "不支持的 WAV 格式: format_tag={} sample_width={}".format(
            fmt_tag, sample_width
        )
    )
