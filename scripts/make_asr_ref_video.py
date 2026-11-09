#!/usr/bin/env python3
"""生成「设备端 mp4（含音轨）」用例的测试素材。

产物
====

============================  ==============================================
``/tmp/asr_ref/asr_real_audio.mp4``  真实 ``asr_en.wav`` → AAC 音轨 +
                              320×240 黑帧（字幕同步 / 视频路径用例要用）
============================  ==============================================

为什么要有这个素材
==================

``scripts/verify_asr_wizard.py`` 的 **W7**（设备端视频路径）和 **U14**
（字幕随播放头变化）都要一个**设备端路径的 mp4**：

* W7 断言「视频路径识别出的文本」与 **C++ demo 对同一段音频的转写**
  （``C_FINAL``）**逐字相同** → 所以 mp4 里的音轨必须是**原始
  ``asr_en.wav``** 的内容（不是任何处理过的版本）；
* U14 断言字幕的**时间轴**跟着播放头走 → 需要真实时长的视频流。

⚠️ **``/tmp`` 是 tmpfs —— 板子重启就没了**
----------------------------------------
素材丢了不会报错，只会让用例**静默 SKIP**（上一轮就出现过：板子重启后
``verify_asr_wizard.py`` 从 88 通过掉到 81 通过 / 3 失败）。所以这个脚本
是「跑回归前的第一步」：

::

    .venv/bin/python scripts/make_asr_live_wav.py      # 两个采样率的 wav
    .venv/bin/python scripts/make_asr_ref_video.py     # 视频素材

跑法：``.venv/bin/python scripts/make_asr_ref_video.py``
"""
from __future__ import annotations

import argparse
import sys
import wave
from fractions import Fraction
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

#: C demo 的原始资产（48 kHz / s24le / 单声道）。
SRC = ("/userdata/rknn3/rknn3-model-zoo/install/rk3588_linux_aarch64/"
       "rknn_Qwen3_ASR_demo/asr_en.wav")

DST = "/tmp/asr_ref/asr_real_audio.mp4"

WIDTH, HEIGHT, FPS = 320, 240, 25


def _to_s16_mono(raw: bytes) -> bytes:
    """任意位深的单声道 PCM → s16le。

    与 ``scripts/make_asr_live_wav.py`` 同一套转换（24-bit 需要手工解码：
    numpy 没有 int24）。
    """
    import numpy as np  # noqa: PLC0415

    from app.core.audio.wav import parse_wav  # noqa: PLC0415

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


def build(src: Path, dst: Path) -> int:
    """把 ``src`` 的音轨塞进一个黑帧视频里。"""
    import av  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415

    with wave.open(str(src), "rb") as w:
        rate = w.getframerate()
    s16 = _to_s16_mono(src.read_bytes())
    samples = np.frombuffer(s16, dtype="<i2").reshape(1, -1)

    dst.parent.mkdir(parents=True, exist_ok=True)
    out = av.open(str(dst), mode="w")
    vstream = out.add_stream("mpeg4", rate=FPS)
    vstream.width = WIDTH
    vstream.height = HEIGHT
    vstream.pix_fmt = "yuv420p"
    astream = out.add_stream("aac", rate=rate)
    astream.layout = "mono"

    frame = av.VideoFrame(WIDTH, HEIGHT, "yuv420p")
    frame.planes[0].update(np.zeros((HEIGHT, WIDTH), dtype=np.uint8))
    frame.planes[1].update(np.full((HEIGHT // 2, WIDTH // 2), 128, dtype=np.uint8))
    frame.planes[2].update(np.full((HEIGHT // 2, WIDTH // 2), 128, dtype=np.uint8))

    step = rate  # 1 秒音频配 1 帧视频
    for i in range(0, samples.shape[1], step):
        chunk = np.ascontiguousarray(samples[:, i:i + step])
        af = av.AudioFrame.from_ndarray(chunk, format="s16", layout="mono")
        af.sample_rate = rate
        af.pts = i
        for pkt in astream.encode(af):
            out.mux(pkt)
        frame.pts = i // step
        frame.time_base = Fraction(1, FPS)
        for pkt in vstream.encode(frame):
            out.mux(pkt)
    for pkt in astream.encode(None):
        out.mux(pkt)
    for pkt in vstream.encode(None):
        out.mux(pkt)
    out.close()
    return dst.stat().st_size


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=SRC)
    ap.add_argument("--dst", default=DST)
    args = ap.parse_args(argv)
    src = Path(args.src)
    if not src.is_file():
        print("找不到源音频: {}".format(src), file=sys.stderr)
        return 1
    dst = Path(args.dst)
    size = build(src, dst)
    print("wrote {} ({} bytes)".format(dst, size))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
