#!/usr/bin/env python3
"""测「单个 TTS 实例把 8 个核用掉了多少」—— 决定拆成 4+4 核值不值。

后台线程每 50 ms 读一次 `rknnsmi.get_npu_info()` 的 `util_rate_pct`，
主线程循环做 TTS 合成。输出：
  - 空闲时的 util_rate（基线）
  - 合成期间的 util_rate 分布（均值 / p90 / 峰值）
  - core_cnt（这张卡报告几个核）

用法：python util_probe.py [card] [rounds]
"""
import asyncio
import statistics
import sys
import threading
import time

sys.path.insert(0, "/userdata/wdnwebui")

CARD = int(sys.argv[1]) if len(sys.argv) > 1 else 1
ROUNDS = int(sys.argv[2]) if len(sys.argv) > 2 else 4

TEXT = (
    "这是一段用于测试核利用率的较长中文句子，"
    "它需要经过文本投影、自回归的说话人建模、"
    "以及逐帧的语音码本预测，最后再由声码器还原成波形。"
    "句子的长度会直接影响合成所耗费的步骤数，"
    "因此我们用它来观察单卡八个核在合成期间的真实忙碌程度。"
)

samples = []
stop = threading.Event()


def sampler():
    from wdnai._librknnsmi import RknnSmi
    c = RknnSmi()
    info = c.get_npu_info(device_id=CARD, chip_id=0)
    print("[npu] core_cnt=%s cur_freq=%sMHz" % (info["core_cnt"], info["cur_freq_mhz"]))
    while not stop.is_set():
        try:
            u = c.get_npu_info(device_id=CARD, chip_id=0)["util_rate_pct"]
            samples.append((time.time(), int(u)))
        except Exception:
            pass
        time.sleep(0.05)


async def main():
    from app.core.compute.workers._base import _build_session
    from wdnai import Attachment

    t0 = time.perf_counter()
    sess = _build_session("qwen3-tts", CARD, None)
    print("[load] %.1fs" % (time.perf_counter() - t0))

    th = threading.Thread(target=sampler, daemon=True)
    th.start()
    time.sleep(1.0)
    idle = list(samples)
    print("[idle] %d samples, util=%s" % (len(idle), [u for _, u in idle][:10]))

    base = len(samples)
    for i in range(ROUNDS):
        t1 = time.perf_counter()
        await sess.respond(
            attachment=[Attachment(TEXT, kind="text")],
            language="chinese", speaker="news_female", no_play=True,
        )
        print("[round %d] %.2fs" % (i + 1, time.perf_counter() - t1))
        sys.stdout.flush()

    time.sleep(0.5)
    stop.set()
    th.join(timeout=2)

    busy = [u for _, u in samples[base:]]
    if not busy:
        print("no busy samples")
        return
    busy_sorted = sorted(busy)
    print("\n=== 合成期间 util_rate_pct（%d samples）===" % len(busy))
    print("  均值 %.1f  中位 %.1f  p90 %.1f  峰值 %d  最小 %d"
          % (statistics.mean(busy), statistics.median(busy),
             busy_sorted[int(len(busy_sorted) * 0.9)], max(busy), min(busy)))
    hist = {}
    for u in busy:
        hist[u // 10 * 10] = hist.get(u // 10 * 10, 0) + 1
    print("  直方图:", "  ".join("%d%%×%d" % (k, v) for k, v in sorted(hist.items())))


if __name__ == "__main__":
    asyncio.run(main())
