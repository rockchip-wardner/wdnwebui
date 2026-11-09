#!/usr/bin/env python3
"""生成实时链路用的测试音频（两个采样率，同一份内容）+ 长音频。

产出
====

============================  ==============================================
``/tmp/asr_ref/asr_48k_mono_s16.wav``  48 kHz / s16le / 单声道
``/tmp/asr_ref/asr_16k_mono_s16.wav``  16 kHz / s16le / 单声道
``/tmp/asr_ref/asr_long_90s.wav``      16 kHz / s16le / **90 s**
============================  ==============================================

⚠️ **``/tmp`` 是 tmpfs —— 板子重启就没了**
----------------------------------------
素材丢了不会报错，只会让用例**静默 SKIP**（上一轮就遇到过：板子重启后
``verify_asr_wizard.py`` 的「长音频不截断」用例直接跳过）。所以跑回归前
先跑这个脚本（视频素材另有 ``scripts/make_asr_ref_video.py``）。

为什么是**两个**采样率
=====================

实时链路上有三条路径要吃同一段录音：

1. **文件路径**（``POST /transcribe``）—— 契约要求 16 kHz；
2. **板载路径**（``tools/asr_mic_agent.py --file``）—— 模拟 ALSA 采集，
   设备是 48 kHz → 用 ``StreamResampler`` 线性插值降到 16 kHz；
3. **浏览器路径**（``getUserMedia``）—— Chromium 的假麦克风**固定**
   以 48 kHz 播放文件（实测，见 §5.2.2）→ 前端用**同一套**线性插值
   降到 16 kHz。

所以测试资产必须有 48 kHz 版本（喂 2、3）和 16 kHz 版本（喂 1），
且两者必须由**同一份** s24le 源、用**同一个**算法降下来 —— 这样
「文件路径的参考文本」与「板载 / 浏览器路径的文本」才有可比性。

⚠️ **48 kHz 那个是 Chromium 假麦克风的硬要求**：假设备以 48 kHz 播放，
浏览器再降采样回 16 kHz 时会引入 **WebAudio 自己的重采样器**（带滤波
和延迟），与我们的线性插值**不是同一个算法** → 两端 PCM 永远不可能
逐字节相同。所以浏览器路径改为「原生采样率 + 我们自己的重采样」，
板载路径用同一套算法 —— 这样**除了浏览器采集链路自带的调理**之外，
两端的处理完全一致（清单 B3）。

末尾补静音
==========

Chromium 的假麦克风放完文件后会**继续输出静音**（``%noloop`` 只影响
循环，不影响「放完后变成静音」）。板载路径喂完文件就收尾。两边音频
因此不等长，末尾 token 会分叉。补足静音后**两条路径在同一点命中端点**
（端点 = 连续静音 1200 ms），切出的句子完全一致 → 文本可以逐字节比较。

用法::

    .venv/bin/python scripts/make_asr_live_wav.py            # 落盘
    .venv/bin/python scripts/make_asr_live_wav.py --print    # 顺便打印信息
"""
from __future__ import annotations

import argparse
import sys
import wave
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.core.audio.capture import resample_s16_linear  # noqa: E402
from app.core.audio.wav import parse_wav  # noqa: E402

SRC = ("/userdata/rknn3/rknn3-model-zoo/install/rk3588_linux_aarch64/"
       "rknn_Qwen3_ASR_demo/asr_en.wav")

#: 48 kHz 版本（浏览器假麦克风 + 板载 ``--file``）。
DST48 = "/tmp/asr_ref/asr_48k_mono_s16.wav"

#: 16 kHz 版本（文件路径的参考）。
DST16 = "/tmp/asr_ref/asr_16k_mono_s16.wav"

#: **> 60 s** 的长音频 —— 用来证明「文件路径不再 60 s 截断」
#: （旧实现按 C demo 的 ``ONLINE_MAX_AUDIO_SECONDS`` 砍尾巴）。
DST_LONG = "/tmp/asr_ref/asr_long_90s.wav"

#: 长音频的目标时长（秒）。
LONG_SEC = 90.0

#: 末尾补多少秒静音。
PAD_SILENCE_SEC = 3.0


def _to_s16_mono(raw: bytes) -> bytes:
    """任意位深的单声道 PCM → s16le（按幅度归一，不做降采样）。"""
    import numpy as np

    w = parse_wav(raw)
    if w.sample_width == 2:
        return w.pcm
    if w.sample_width == 3:
        b = np.frombuffer(w.pcm, dtype=np.uint8).reshape(-1, 3)
        v = (b[:, 0].astype(np.int32)
             | (b[:, 1].astype(np.int32) << 8)
             | (b[:, 2].astype(np.int32) << 16))
        sign = (b[:, 2] & 0x80) != 0
        v[sign] -= 0x1000000
        return np.clip((v.astype(np.float32) / 8388608.0) * 32768.0,
                       -32768.0, 32767.0).astype("<i2").tobytes()
    raise RuntimeError("暂不支持 {} 字节每采样".format(w.sample_width))


def build(src: str = SRC, dst48: str = DST48, dst16: str = DST16,
          pad_silence_sec: float = PAD_SILENCE_SEC) -> tuple:
    """生成 48 kHz 与 16 kHz 两个测试音频（内容同源）。"""
    import numpy as np

    w = parse_wav(Path(src).read_bytes())
    s16_48 = _to_s16_mono(Path(src).read_bytes())
    if w.sample_rate != 48000:
        s16_48 = resample_s16_linear(s16_48, w.sample_rate, 48000)
    # 16 kHz 版本用**同一个**线性插值降下来 —— 与板载路径
    # （StreamResampler 48k→16k）的整段结果逐字节相同。
    s16_16 = resample_s16_linear(s16_48, 48000, 16000)

    if pad_silence_sec > 0:
        pad48 = np.zeros(int(48000 * pad_silence_sec), dtype="<i2").tobytes()
        s16_48 = s16_48 + pad48
        s16_16 = s16_16 + np.zeros(
            int(16000 * pad_silence_sec), dtype="<i2").tobytes()

    out48 = Path(dst48)
    out48.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out48), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(48000)
        f.writeframes(s16_48)

    out16 = Path(dst16)
    with wave.open(str(out16), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(16000)
        f.writeframes(s16_16)
    return out48, out16


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=SRC)
    ap.add_argument("--dst48", default=DST48)
    ap.add_argument("--dst16", default=DST16)
    ap.add_argument("--dstlong", default=DST_LONG,
                    help=">60 s 的长音频（证明文件路径不再截断）")
    ap.add_argument("--print", dest="show", action="store_true")
    args = ap.parse_args(argv)
    out48, out16 = build(args.src, args.dst48, args.dst16)
    for out in (out48, out16):
        w = parse_wav(out.read_bytes())
        print("{}: rate={} ch={} width={} frames={} dur={:.3f}s".format(
            out, w.sample_rate, w.n_channels, w.sample_width, w.n_frames,
            w.duration_sec))

    # > 60 s 的长音频：把 16 kHz 版本**重复**到 LONG_SEC 秒。
    # 「不再 60 s 截断」这条用例（U6/U7）只要 audio_sec > 60 且有文本，
    # 内容重复不影响判定（这里也不做退化检查）。
    import numpy as np  # noqa: PLC0415

    pcm16 = np.frombuffer(parse_wav(out16.read_bytes()).pcm, dtype="<i2")
    need = int(LONG_SEC * 16000)
    reps = -(-need // max(len(pcm16), 1))
    long_pcm = np.tile(pcm16, reps)[:need]
    outlong = Path(args.dstlong)
    outlong.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(outlong), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(16000)
        f.writeframes(long_pcm.astype("<i2").tobytes())
    print("{}: rate=16000 ch=1 width=2 dur={:.3f}s（长音频 / 不截断用例）".format(
        outlong, len(long_pcm) / 16000.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
