#!/usr/bin/env python3
"""M13 —— **TTS 队列的真实吞吐**测量。

稳档实时流的滞后在持续增长 ⇒ 要分清是「**产能不够**」还是「**指标算错**」。

量两件事：

1. **单句**从入队到播完的墙钟时间（拆成「合成」和「播放」两段）；
2. **批量**（N 句）的总吞吐 = 译文总时长 / 墙钟时长
   ⇒ 这个比值 < 1 ⇒ 队列**追不上**源速（滞后必然增长）。

⇒ 顺带验「**音色**」是否真的生效（看 worker 日志里有没有
``[speaker] using voice-clone ref_speaker='…'``）。

用法::

    .venv/bin/python tools/probes/interp/m13_tts_throughput.py
"""
from __future__ import annotations

import json
import ssl
import sys
import time
import urllib.request

BASE = "https://127.0.0.1:8000"
SSL_CTX = ssl.create_default_context()

SENTENCES = [
    "这是一次语音合成吞吐量的测试，用来判断播放队列能不能跟得上实时语音的速度。",
    "如果队列吞吐低于一，那就意味着实时同传的滞后会一直增长，永远追不上。",
    "所以这里要把合成耗时和播放耗时分开量，看看瓶颈到底在哪里。",
    "最后一句用来收尾，顺便看自动变速有没有把积压吃下去。",
]


def api(method: str, path: str, payload=None, timeout: int = 120):
    data = None
    hdrs = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=hdrs,
                                 method=method)
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
        body = r.read().decode("utf-8")
    try:
        return json.loads(body)
    except Exception:  # noqa: BLE001
        return {"raw": body}


def wait_idle(timeout: float = 240.0) -> dict:
    deadline = time.time() + timeout
    st = {}
    while time.time() < deadline:
        st = api("GET", "/api/tts/queue/status")
        if st.get("depth") == 0 and not st.get("playing"):
            return st
        time.sleep(0.2)
    return st


def main() -> int:
    ref = sys.argv[1] if len(sys.argv) > 1 else "news_female"
    print(f"=== TTS 吞吐测量（参考音色 {ref}）===")
    api("POST", "/api/tts/queue/tempo", {"tempo": 1.0})
    api("POST", "/api/tts/queue/clear")
    wait_idle()

    # ---- 单句 ----
    t0 = time.monotonic()
    api("POST", "/api/tts/queue/submit",
        {"text": SENTENCES[0], "language": "chinese",
         "ref_speaker": ref, "volume": 0.05})
    # 等它开始播（= 合成完了）
    synth = None
    deadline = time.time() + 120
    while time.time() < deadline:
        st = api("GET", "/api/tts/queue/status")
        if st.get("playing"):
            synth = time.monotonic() - t0
            break
        time.sleep(0.05)
    wait_idle()
    single = time.monotonic() - t0
    snap = api("GET", "/api/tts/queue/status")
    print(f"  单句：入队→开始播 {synth:.2f}s（≈合成耗时）· 入队→播完 "
          f"{single:.2f}s · 播放 {single - (synth or 0):.2f}s")

    # ---- 批量 ----
    api("POST", "/api/tts/queue/clear")
    wait_idle()
    est_total = 0.0
    t0 = time.monotonic()
    for s in SENTENCES:
        r = api("POST", "/api/tts/queue/submit",
                {"text": s, "language": "chinese",
                 "ref_speaker": ref, "volume": 0.05})
        est_total += float(r.get("est_audio_sec") or 0)
    wait_idle()
    batch = time.monotonic() - t0
    snap2 = api("GET", "/api/tts/queue/status")
    print(f"  批量：{len(SENTENCES)} 句 · 估算音频 {est_total:.1f}s · "
          f"墙钟 {batch:.1f}s ⇒ **吞吐 {est_total / max(batch, 0.01):.2f}× 实时**")
    print(f"  队列：done={snap2.get('done')} failed={snap2.get('failed')} "
          f"dropped={snap2.get('dropped')} merged={snap2.get('merged')} "
          f"avg_lag={snap2.get('avg_lag_sec')}")

    print("\n判据：")
    rt = est_total / max(batch, 0.01)
    c1 = rt >= 0.95
    print(f"  {'ok  ' if c1 else 'WARN'} 吞吐 {rt:.2f}× 实时"
          f"（< 1 ⇒ 实时流必然越落越远）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
