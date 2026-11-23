#!/usr/bin/env python3
"""M22 —— **aplay 到底是不是按 1.0× 实时消费**？

「播放中途卡顿断音」的判定全靠一个模型：aplay 每墙钟秒消费 ``tempo``
秒的音频 ⇒ 管道存货按 ``tempo`` 递减。

⚠️ **这个模型必须实测验证** —— 如果板载 codec / ALSA 缓冲让 aplay
**消费得比 1.0× 慢**，那「存货见底」就会被**误判成断音**，而真正的
原因是「设备本身就慢」。

做法：往管道里灌 5 s 静音，然后每 0.5 s 读一次存货 ⇒ 看它怎么衰减。
衰减斜率 = 实际消费速率（1.0 = 实时；< 1.0 = 设备更慢）。

用法::

    .venv/bin/python tools/probes/interp/m22_aplay_rate.py
"""
from __future__ import annotations

import os
import sys
import time

_ROOT = "/userdata/wdnwebui"
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import numpy as np  # noqa: E402

from wdnapps_audio_chat.cards.tts.service import AplayWriter  # noqa: E402

SR = 24000


def main() -> int:
    tempo = float(sys.argv[1]) if len(sys.argv) > 1 else 1.0
    w = AplayWriter(volume=0.0, tempo=tempo)
    if not w.ok:
        print("aplay 起不来")
        return 1
    print("aplay ok, tempo=%.2f" % tempo)
    total = 6.0
    w.write(np.zeros(int(SR * total), dtype=np.float32))
    t0 = time.time()
    print("%-6s %-10s %-10s %s" % ("t", "elapsed", "backlog", "消耗速率"))
    last_b, last_t = w.backlog_sec(), 0.0
    for i in range(int(total / 0.5) + 4):
        time.sleep(0.5)
        el = time.time() - t0
        b = w.backlog_sec()
        rate = ""
        if el > last_t and b < last_b:
            rate = "%.3f×" % ((last_b - b) / (el - last_t))
        print("%-6.1f %-10.2f %-10.2f %s" % (el, el, b, rate))
        last_b, last_t = b, el
    w.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
