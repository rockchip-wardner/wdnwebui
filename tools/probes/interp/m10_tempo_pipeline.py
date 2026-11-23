#!/usr/bin/env python3
"""M10 —— **TTS 卡的变速管道**真机验证（``AplayWriter(tempo=1.15)``）。

M7 只验了 sox 本身（tempo 精确、音高不变、板载 codec 打开）⇒ 这次验的是
**TTS 卡里真正的接线**：

* ``tempo=1.0`` ⇒ **原路径**（只有 aplay，一个字没改）；
* ``tempo=1.15`` ⇒ **sox → aplay 两段管道**（本板 sox 14.4.2 的 ``play``
  没法选设备 ⇒ 必须拆两段）。

判据：

1. 变速那趟**确实变短**：用**两种长度**各测一次 ⇒ 拟合出
   ``播放时长 = 固定开销 + 系数 × 源长`` ⇒ **系数比 = 1.15**
   （固定开销被消掉 ⇒ 不受进程启动延迟影响）；
2. 播放期间**板载 codec 真的被打开**（``hw_params`` 轮询到 ES8388 那张卡）；
3. **音高不变** —— sox ``tempo`` 是时间域算法（M7 已用 FFT 峰值 440 Hz 验过）。

用法::

    .venv/bin/python tools/probes/interp/m10_tempo_pipeline.py
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import numpy as np

HWP = "/proc/asound/card1/pcm0p/sub0/hw_params"
RATE = 24000
TONE_HZ = 440.0
LENGTHS = (4.0, 12.0)
TOLERANCE = 0.05          # 系数比允许 ±5%


def _poll_hw_params(stop: threading.Event, out: list) -> None:
    """播放期间**持续**轮询 ``hw_params``（采样一次会撞上「还没打开 / 已关闭」）。"""
    while not stop.is_set():
        try:
            txt = Path(HWP).read_text(errors="ignore").strip()
        except OSError:
            txt = ""
        if txt and txt not in ("closed", "no setup"):
            if txt not in out:
                out.append(txt)
        time.sleep(0.05)


def play(tempo: float, src_sec: float) -> tuple[float, str]:
    """走 TTS 卡真正的 ``AplayWriter`` 播一段正弦，返回（耗时秒, 板载参数）。"""
    from wdnapps_audio_chat.cards.tts.service import AplayWriter

    w = AplayWriter(volume=0.05, device="plughw:1,0", tempo=tempo)
    n = int(RATE * src_sec)
    t = np.arange(n, dtype=np.float32) / RATE
    audio = (0.3 * np.sin(2 * np.pi * TONE_HZ * t)).astype(np.float32)

    seen: list = []
    stop = threading.Event()
    th = threading.Thread(target=_poll_hw_params, args=(stop, seen), daemon=True)
    th.start()
    t0 = time.monotonic()
    step = RATE // 4                    # 0.25 s 一块
    for i in range(0, n, step):
        w.write(audio[i:i + step])
    w.close(drain_timeout=20.0)
    dt = time.monotonic() - t0
    stop.set()
    th.join(timeout=1.0)
    return dt, (seen[0] if seen else "")


def slope(tempo: float) -> tuple[float, dict]:
    """两种长度各测一次 ⇒ 拟合斜率（去掉固定开销）。"""
    pts = []
    hw = ""
    for sec in LENGTHS:
        dt, h = play(tempo, sec)
        pts.append((sec, dt))
        hw = hw or h
        print(f"    tempo={tempo} 源 {sec:.0f}s ⇒ 播放 {dt:.2f}s")
    (x1, y1), (x2, y2) = pts
    k = (y2 - y1) / (x2 - x1)
    b = y1 - k * x1
    return k, {"hw": hw, "pts": pts, "intercept": b}


def main() -> int:
    print("=== tempo=1.0（原路径：只有 aplay）===")
    k1, info1 = slope(1.0)
    print(f"    斜率 {k1:.4f}（≈1.0 ⇒ 原速）+ 固定开销 {info1['intercept']:.2f}s")
    print(f"    板载 hw_params: {info1['hw'][:140]}")

    print("\n=== tempo=1.15（sox → aplay 两段管道）===")
    k2, info2 = slope(1.15)
    print(f"    斜率 {k2:.4f}（≈1/1.15={1 / 1.15:.4f}）+ 固定开销 "
          f"{info2['intercept']:.2f}s")
    print(f"    板载 hw_params: {info2['hw'][:140]}")

    ratio = k1 / k2
    ok = True
    print("\n=== 判据 ===")
    c1 = abs(ratio - 1.15) / 1.15 < TOLERANCE
    print(f"  {'ok  ' if c1 else 'FAIL'} 变速倍率 = {ratio:.4f}（期望 1.15，"
          f"偏差 {abs(ratio - 1.15) / 1.15 * 100:.1f}%）")
    ok = ok and c1
    # ⚠️ ``hw_params`` 里**没有卡名**（只有格式）⇒ 别找 "es8388" ——
    # 要认的是**板载 codec 的格式特征**（与 M7 实测一致）：
    # MMAP_INTERLEAVED / S24_LE / 2ch / 48000 Hz。
    hw = info2["hw"].lower()
    c2 = ("mmap_interleaved" in hw and "s24_le" in hw
          and "48000" in hw and "channels: 2" in hw)
    print(f"  {'ok  ' if c2 else 'FAIL'} 板载 codec 被打开（MMAP_INTERLEAVED / "
          f"S24_LE / 2ch / 48kHz）：{info2['hw'][:90]!r}")
    ok = ok and c2
    print("  info 音高：sox tempo 是时间域算法 ⇒ 音高不变（M7 已用 FFT 峰值 "
          "440 Hz 验过）")

    print("\n结论:", "全部通过" if ok else "有失败项")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
