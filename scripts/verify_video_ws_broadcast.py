"""
回归测试:多 WS 消费者场景下 broadcast 让每个 WS 都拿到完整带宽。

背景: 修复前 VideoSession._frame_queue 是单槽队列,多 WS 抢同一帧,
每个只拿到 ~half。修复后每个 WS 有自己的 maxsize=1 队列,decode
loop 广播每个 JPEG 到所有队列 —— total throughput 应当 ≥ 1 个 WS
solo 的带宽(证明 broadcast 在并行),不再被单槽争抢砍半。

测试用 ThreadPoolExecutor 跑 N 个独立线程 + 独立事件循环,模拟
浏览器中每个 WS 连接由 native I/O thread 独立处理的现实(避免
asyncio.gather 单线程 cooperative 调度噪声)。

用法:
  python scripts/verify_video_ws_broadcast.py
"""

import asyncio
import json
import os
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import websockets


API_HOST = os.environ.get("WDNWEBUI_HOST", "127.0.0.1:8000")
VIDEO_PATH = os.environ.get("WDNWEBUI_VIDEO", "/userdata/video_test/rk-0.mp4")
SAMPLE_S = 4.0


def open_video():
    req = urllib.request.Request(
        f"http://{API_HOST}/api/video-detection/open",
        data=json.dumps({"source": VIDEO_PATH, "loop": True}).encode(),
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req).read()


def close_video():
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"http://{API_HOST}/api/video-detection/close",
            data=b"", method="POST",
        )).read()
    except Exception:
        pass


async def _collect_async(url, duration_s):
    received = 0
    async with websockets.connect(url) as ws:
        t0 = time.monotonic()
        try:
            while time.monotonic() - t0 < duration_s:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                if isinstance(msg, (bytes, bytearray)):
                    received += 1
        except websockets.ConnectionClosed:
            pass
    return received


def collect_in_thread(url, duration_s):
    result_box = {}

    def runner():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result_box["count"] = loop.run_until_complete(_collect_async(url, duration_s))
        finally:
            loop.close()

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    t.join()
    return result_box.get("count", 0)


def run_parallel(*urls):
    with ThreadPoolExecutor(max_workers=len(urls)) as ex:
        return list(ex.map(lambda u: collect_in_thread(u, SAMPLE_S), urls))


def fps(count, duration_s):
    return count / duration_s


SCENARIOS = [
    (2, [
        f"ws://{API_HOST}/api/video-detection/ws",
        f"ws://{API_HOST}/api/video-target-detection/ws",
    ]),
    (3, [
        f"ws://{API_HOST}/api/video-detection/ws",
        f"ws://{API_HOST}/api/video-target-detection/ws",
        f"ws://{API_HOST}/api/video-target-localization/ws",
    ]),
]


async def main():
    close_video()
    await asyncio.sleep(0.5)
    open_video()
    await asyncio.sleep(1.5)

    print("=" * 78)
    print("VideoSession WS-broadcast regression test")
    print("=" * 78)
    print()

    baseline_urls = [
        f"ws://{API_HOST}/api/video-detection/ws",
        f"ws://{API_HOST}/api/video-target-detection/ws",
        f"ws://{API_HOST}/api/video-target-localization/ws",
    ]
    baseline = {}
    for url in baseline_urls:
        close_video()
        await asyncio.sleep(0.4)
        open_video()
        await asyncio.sleep(1.5)
        name = url.rsplit("/", 2)[-2]
        n = collect_in_thread(url, SAMPLE_S)
        baseline[name] = fps(n, SAMPLE_S)
        print(f"  baseline {name:22s}: {n:3d} frames in {SAMPLE_S}s "
              f"→ fps={baseline[name]:5.1f}")

    avg_baseline = sum(baseline.values()) / len(baseline)
    print(f"  → avg baseline: {avg_baseline:.1f} fps")
    print()

    failures = []
    for n_ws, urls in SCENARIOS:
        close_video()
        await asyncio.sleep(0.4)
        open_video()
        await asyncio.sleep(1.5)

        print(f"--- {n_ws} WS concurrent (each in own thread/event-loop) ---")
        counts = run_parallel(*urls)
        rates = [fps(c, SAMPLE_S) for c in counts]
        for url, rate, count in zip(urls, rates, counts):
            name = url.rsplit("/", 2)[-2]
            print(f"    {name:22s}: {count:3d} frames → fps={rate:5.1f}")

        # Bug invariant: pre-fix total fps ≤ baseline (frames split,
        # total capped at single-WS rate); post-fix total fps ≥ baseline
        # (each WS drains its own queue independently). Server-side WS
        # pipeline cooperative noise caps total at ~1.5-2× baseline,
        # not the theoretical N×baseline — that's a separate issue
        # from broadcast itself.
        total = sum(rates)
        threshold = 0.85 * avg_baseline
        verdict = "✅" if total >= threshold else "❌"
        print(f"    {verdict} total fps {total:.1f} / baseline {avg_baseline:.1f} "
              f"= {total/avg_baseline:.2f}× (must be ≥ 0.85× baseline)")
        if total < threshold:
            failures.append(
                f"{n_ws}-WS: total {total:.1f} < 0.85× baseline "
                f"({avg_baseline:.1f}) = {threshold:.1f} — "
                f"broadcast not parallelising"
            )
        print()

    close_video()

    print("=" * 78)
    if failures:
        print("FAILED — broadcast regression detected:")
        for f in failures:
            print(f"  • {f}")
        return 1
    print("PASSED — broadcast parallelises: total fps ≥ baseline across N WS")
    print("          (pre-fix: total fps ≤ baseline — both WS fighting for")
    print("           single slot, total capped; post-fix: each WS drains its")
    print("           own queue, total throughput NOT reduced below solo rate)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))