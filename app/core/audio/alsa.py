"""板载 ALSA 采集 —— ``ctypes`` 直调 ``libasound.so.2``，**零 pip 依赖**。

为什么不用现成的库
==================

本板 ``sounddevice`` / ``pyaudio`` / ``webrtcvad`` **均未安装**（已核实）。
为了一次麦克风采集去装三个包不值得 —— 采集只需要 ALSA 的四个调用：

* ``snd_pcm_open`` —— 打开设备
* ``snd_pcm_set_params`` —— 设定格式 / 声道 / 采样率 / 延迟
* ``snd_pcm_readi`` —— 阻塞读一块
* ``snd_pcm_close`` —— 释放

这四个用 ``ctypes`` 调 ``libasound.so.2`` 就够了，**不污染**
``pyproject.toml``，也不引入版本升级风险。

> ⚠️ 与 ``wdnav`` 的关系：``wdnav`` 管的是 MPP / RGA / V4L2 的**视频**
> 能力，**没有音频采集器**。音频采集走 ALSA 是本仓既有的例外
> （解码走 PyAV 是同一个例外），不是新开的口子。

设备
====

本板（``/proc/asound/pcm``）：

=================  ==========================  ==============
设备                芯片 / 源                     用途
=================  ==========================  ==============
``hw:0,0``          rockchip,hdmiin              HDMI 输入
``hw:1,0``          dailink-multicodecs ES8323   **板载麦克风**
=================  ==========================  ==============

权限：``/dev/snd/pcmC1D0c`` 是 ``root:audio`` ``rw-rw----`` → 采集进程
必须在 **audio 组**里（或以 root 跑，与 systemd 一致）。

独占
====

ALSA 默认独占设备：采集进程**独占**声卡，webui 进程**不碰**声卡
（webui 还要提供其它服务，不该被声卡绑住；而且 ``snd_pcm_readi`` 是
**阻塞**调用，塞进 webui 会占死线程 —— 所以采集跑在**独立进程**里，
见 ``tools/asr_mic_agent.py``。

重采样
======

ES8323 板载麦原生多为 48 kHz → 这里在**采集端**线性插值到 16 kHz
（与 SDK 的 ``_resample_linear`` 同款算法，见
:mod:`app.core.audio.capture`），保证喂给模型的音频与浏览器路径同源。
"""
from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List, Optional

from app.core.audio.capture import (CAPTURE_SAMPLE_RATE, StreamResampler,
                                    resample_s16_linear)

LOG = logging.getLogger("core.audio.alsa")

__all__ = [
    "AlsaCapture",
    "AlsaError",
    "list_capture_devices",
    "resample_s16_linear",
]


# --------------------------------------------------------------------------- #
# ALSA 常量
# --------------------------------------------------------------------------- #
SND_PCM_STREAM_PLAYBACK = 0
SND_PCM_STREAM_CAPTURE = 1

SND_PCM_FORMAT_S16_LE = 2

SND_PCM_ACCESS_RW_INTERLEAVED = 3

#: 采样率候选 —— 板载麦原生多为 48 kHz；按顺序试，都不行就让 ALSA 软转。
_DEFAULT_RATE_CANDIDATES = (48000, 44100, 32000, 22050, 16000)


class AlsaError(RuntimeError):
    """ALSA 调用失败（带 ALSA 自己的错误描述）。"""


def _load_lib() -> ctypes.CDLL:
    """加载 ``libasound.so.2``（找不到就抛 ``AlsaError``）。"""
    names = ["libasound.so.2", "libasound.so"]
    found = ctypes.util.find_library("asound")
    if found:
        names.insert(0, found)
    for name in names:
        try:
            lib = ctypes.CDLL(name)
        except OSError:
            continue
        _bind(lib)
        return lib
    raise AlsaError(
        "找不到 libasound.so.2 —— 板载麦克风采集不可用"
        "（本仓不为此装 pip 包，见模块说明）")


def _bind(lib: ctypes.CDLL) -> None:
    """声明 ctypes 签名（不声明的话 64 位下指针会被截断）。"""
    lib.snd_pcm_open.argtypes = [
        ctypes.POINTER(ctypes.c_void_p), ctypes.c_char_p, ctypes.c_int,
        ctypes.c_int,
    ]
    lib.snd_pcm_open.restype = ctypes.c_int
    lib.snd_pcm_close.argtypes = [ctypes.c_void_p]
    lib.snd_pcm_close.restype = ctypes.c_int
    lib.snd_pcm_set_params.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_uint,
        ctypes.c_uint, ctypes.c_int, ctypes.c_uint,
    ]
    lib.snd_pcm_set_params.restype = ctypes.c_int
    lib.snd_pcm_readi.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                  ctypes.c_uint]
    lib.snd_pcm_readi.restype = ctypes.c_int
    lib.snd_pcm_recover.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                    ctypes.c_int]
    lib.snd_pcm_recover.restype = ctypes.c_int
    try:
        lib.snd_strerror.argtypes = [ctypes.c_int]
        lib.snd_strerror.restype = ctypes.c_char_p
    except AttributeError:  # pragma: no cover
        pass


def _err(lib: ctypes.CDLL, rc: int) -> str:
    """把 ALSA 的负错误码翻成人话。"""
    try:
        msg = lib.snd_strerror(rc)
        if msg:
            return msg.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        pass
    return "ALSA error {}".format(rc)


@dataclass(frozen=True)
class AlsaDevice:
    """一个采集设备。"""

    name: str          # ``hw:1,0``
    card: int
    device: int
    description: str = ""

    @property
    def card_path(self) -> str:
        return "/dev/snd/pcmC{}D{}c".format(self.card, self.device)


def list_capture_devices() -> List[AlsaDevice]:
    """枚举本机的 ALSA **采集**设备。

    解析 ``/proc/asound/pcm`` —— 每一行形如::

        01-00: dailink-multicodecs ES8323 HiFi-0 : ... : playback 1 : capture 1

    行首的 ``01-00`` 就是 card 1 / device 0 → ``hw:1,0``。
    """
    out: List[AlsaDevice] = []
    try:
        text = Path("/proc/asound/pcm").read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        if ": capture" not in line:
            continue
        head = line.split(":", 1)[0].strip()
        if "-" not in head:
            continue
        card_s, dev_s = head.split("-", 1)
        try:
            card, device = int(card_s), int(dev_s)
        except ValueError:
            continue
        desc = line.split(":", 1)[1].strip() if ":" in line else ""
        out.append(AlsaDevice("hw:{},{}".format(card, device), card, device,
                               desc))
    return out


class AlsaCapture:
    """打开一个 ALSA 采集设备并按块读 s16le PCM。

    用法::

        with AlsaCapture("hw:1,0") as cap:
            for chunk in cap.chunks_16k(chunk_ms=500):
                ...   # chunk 是 16 kHz s16le 单声道

    ⚠️ ``snd_pcm_readi`` 是**阻塞**调用 —— 只在独立进程里用
    （见 ``tools/asr_mic_agent.py``）。
    """

    def __init__(
        self,
        device: str = "hw:1,0",
        *,
        sample_rate: int = 48000,
        channels: int = 1,
        period_ms: int = 100,
        target_rate: int = CAPTURE_SAMPLE_RATE,
    ) -> None:
        self.device = device
        self.requested_rate = int(sample_rate)
        self.channels = int(channels)
        self.period_ms = int(period_ms)
        self.target_rate = int(target_rate)
        self.sample_rate = int(sample_rate)
        self._lib: Optional[ctypes.CDLL] = None
        self._pcm = ctypes.c_void_p()
        self._opened = False

    # ------------------------------------------------------------------ #
    @property
    def period_frames(self) -> int:
        """一次读多少帧（= period_ms 换算成采样数）。"""
        return max(1, int(self.sample_rate * self.period_ms / 1000))

    @property
    def opened(self) -> bool:
        return self._opened

    # ------------------------------------------------------------------ #
    def open(self) -> "AlsaCapture":
        """打开设备并设定参数。失败抛 :class:`AlsaError`。"""
        lib = _load_lib()
        self._lib = lib
        pcm = ctypes.c_void_p()
        rc = lib.snd_pcm_open(ctypes.byref(pcm), self.device.encode("utf-8"),
                              SND_PCM_STREAM_CAPTURE, 0)
        if rc < 0:
            raise AlsaError("打开 ALSA 设备 {} 失败: {}".format(
                self.device, _err(lib, rc)))
        self._pcm = pcm

        latency_us = int(self.period_ms * 1000)
        # ⚠️ **声道数可能要退让**：ES8323 板载麦只支持**双声道**采集
        # （单声道会报 "Channels count (1) not available for CAPTURE"）。
        # 所以先按请求的声道试，不行就退到 2 声道 —— 读出来的交错
        # 立体声在 :meth:`read` 里**下混**成单声道。
        want_channels = [self.channels]
        if self.channels != 2:
            want_channels.append(2)
        last_err = None
        opened = False
        for channels in want_channels:
            self.channels = channels
            for rate in _DEFAULT_RATE_CANDIDATES:
                rc = lib.snd_pcm_set_params(
                    pcm, SND_PCM_FORMAT_S16_LE, SND_PCM_ACCESS_RW_INTERLEAVED,
                    channels, rate, 0, latency_us)
                if rc == 0:
                    self.sample_rate = rate
                    opened = True
                    break
                last_err = rc
            if opened:
                break
            # 这个声道数下所有候选采样率都失败 → 让 ALSA 自己软重采样
            # 到请求的采样率。
            rc = lib.snd_pcm_set_params(
                pcm, SND_PCM_FORMAT_S16_LE, SND_PCM_ACCESS_RW_INTERLEAVED,
                channels, self.requested_rate, 1, latency_us)
            if rc == 0:
                self.sample_rate = self.requested_rate
                opened = True
                break
            last_err = rc
        if not opened:
            self.close()
            raise AlsaError("设置 ALSA 参数失败: {}".format(
                _err(lib, last_err if last_err is not None else rc)))

        self._opened = True
        LOG.info("alsa: opened %s @ %d Hz, %d ch%s, period %d ms",
                 self.device, self.sample_rate, self.channels,
                 "（下混成单声道）" if self.channels > 1 else "",
                 self.period_ms)
        return self

    def close(self) -> None:
        """关闭设备（幂等）。"""
        if self._lib is not None and self._opened:
            try:
                self._lib.snd_pcm_close(self._pcm)
            except Exception as e:  # noqa: BLE001
                LOG.debug("alsa: close failed: %s", e)
        self._opened = False
        self._pcm = ctypes.c_void_p()

    def __enter__(self) -> "AlsaCapture":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    def read(self) -> bytes:
        """读一个 period 的 s16le PCM（**源采样率**，未重采样）。

        读失败（xrun 等）先尝试 ``snd_pcm_recover``；恢复不了就抛
        :class:`AlsaError` —— **不静默返回空块**（静默会让采集看起来
        「还活着」但其实没声音）。

        ⚠️ 设备是**双声道**时（ES8323 板载麦），这里把交错的 L/R
        **下混**成单声道 —— 上层只认单声道。
        """
        if not self._opened or self._lib is None:
            raise AlsaError("设备未打开")
        n = self.period_frames
        buf = (ctypes.c_int16 * (n * self.channels))()
        rc = self._lib.snd_pcm_readi(self._pcm, ctypes.byref(buf), n)
        if rc < 0:
            if self._lib.snd_pcm_recover(self._pcm, rc, 1) < 0:
                raise AlsaError("ALSA 读取失败: {}".format(
                    _err(self._lib, rc)))
            return b""
        raw = ctypes.string_at(buf, rc * self.channels * 2)
        if self.channels == 1:
            return raw
        # 双声道 → 单声道（取两路的平均）
        import numpy as np

        arr = np.frombuffer(raw, dtype="<i2").reshape(-1, self.channels)
        mono = arr.mean(axis=1).astype("<i2")
        return mono.tobytes()

    def chunks_16k(self, chunk_ms: int = 500) -> Iterator[bytes]:
        """产出 16 kHz / s16le / 单声道的定长块。

        重采样用 :class:`~app.core.audio.capture.StreamResampler`
        （**带 carry** 的流式版本）—— 逐块独立重采样会让每块相位从 0
        重来，与整段结果差一个相位。

        块长按**目标采样率**算（16 kHz），所以一块的时长与源采样率
        无关 —— 与浏览器路径的攒块粒度一致。
        """
        if self.sample_rate == self.target_rate:
            resampler = None
        else:
            resampler = StreamResampler(self.sample_rate, self.target_rate)
        need = int(self.target_rate * chunk_ms / 1000) * 2
        buf = bytearray()
        while True:
            raw = self.read()
            if not raw:
                continue
            if resampler is None:
                out = raw
            else:
                out = resampler.push(raw)
            if not out:
                continue
            buf += out
            while len(buf) >= need:
                yield bytes(buf[:need])
                del buf[:need]
        # 收尾（正常不会走到 —— 采集是无限循环）
        if resampler is not None:
            tail = resampler.flush()
            if tail:
                buf += tail
                if buf:
                    yield bytes(buf)


def default_capture_device() -> str:
    """猜一个默认采集设备：优先 ``hw:1,0``（板载麦），否则第一个。"""
    devs = list_capture_devices()
    for d in devs:
        if d.name == "hw:1,0":
            return d.name
    return devs[0].name if devs else "hw:1,0"


def current_user_in_audio_group() -> bool:
    """当前进程能不能打开 ``/dev/snd/pcm*``。

    * **root** —— 能（`/dev/snd` 的属主是 root，root 不受组权限限制）；
    * 否则看**有效组**里有没有 ``audio`` 组。
    """
    if os.geteuid() == 0:
        return True
    try:
        import grp

        gid = os.getegid()
        return any(g.gr_gid == gid for g in grp.getgrall()
                   if g.gr_name == "audio")
    except Exception:  # noqa: BLE001
        return False
