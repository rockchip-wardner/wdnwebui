#!/usr/bin/env python3
"""同卡双实例**并行性**实测（hy-mt-1-5，纯 LLM = 每实例 1 个 llm_mode 8 核 runtime）。

用法：python mt_probe.py OUT_JSON GATE_FILE N
  - 每个进程独立 build 一个 hy-mt-1-5 会话，**都落在卡 0**
  - 等 GATE_FILE 出现后同时开跑 ⇒ 两个进程同一时刻压同一张卡
  - 结果写 OUT_JSON：{"latencies": [...], "text": "..."}

单跑一个进程 = 基线；并发跑两个 = 看单请求延迟是否翻倍。
  并发延迟 ≈ 基线       ⇒ 同卡两实例**真并行**（总产能 2×）
  并发延迟 ≈ 2×基线     ⇒ 同卡两实例**时间片轮转**（总产能 1×）
"""
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, "/userdata/wdnwebui")

CARD = int(os.environ.get("PROBE_CARD", "0"))
OUT = sys.argv[1]
GATE = sys.argv[2]
N = int(sys.argv[3]) if len(sys.argv) > 3 else 5

SRC = (
    "The weather is very nice today, and the park near my house is full of "
    "people who came out for a walk after dinner. I have been thinking about "
    "how much has changed in this small town over the past ten years, when the "
    "old factory on the north side was still running and half of the young "
    "people here used to work there. Now the factory has become a small park "
    "with a playground and a coffee shop, and the school has doubled in size. "
    "I do not miss the noise from the factory, but I do miss the way the whole "
    "street used to be busy every morning. Anyway, the weather is nice, so I "
    "think I will take another walk before it gets dark."
)


async def main():
    from app.core.compute.workers._base import _build_session
    t0 = time.perf_counter()
    sess = _build_session("hy-mt-1-5", CARD, None)
    load_s = time.perf_counter() - t0
    print("[pid %d] session ready in %.1fs, waiting for gate %s"
          % (os.getpid(), load_s, GATE))
    sys.stdout.flush()

    # 等闸门
    while not os.path.exists(GATE):
        time.sleep(0.02)

    prompt = "Translate the following text from English to Chinese:\n" + SRC
    lat = []
    text = ""
    for i in range(N):
        t1 = time.perf_counter()
        out = await sess.respond(prompt=prompt)
        dt = time.perf_counter() - t1
        lat.append(dt)
        text = str(out)[:80]
        print("[pid %d] req%d %.3fs" % (os.getpid(), i + 1, dt))
        sys.stdout.flush()

    with open(OUT, "w") as f:
        json.dump({"pid": os.getpid(), "load_s": load_s,
                   "latencies": lat, "text": text}, f)


if __name__ == "__main__":
    asyncio.run(main())
