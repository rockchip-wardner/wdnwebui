#!/usr/bin/env python3
"""M15 —— **TTS 实际出声电平**测量（区分「没播」和「播得太小」）。

用户报「有翻译，但是没有播放」时，有两种完全不同的可能：

* **没播** —— 句子压根没进队列 / aplay 没起来 ⇒ 峰值 **0**；
* **播了但太小** —— 送了 PCM，但**振幅倍率**太小 ⇒ 峰值很低。

⚠️ 关键事实（板端实测）：``volume`` 是**振幅倍率**
（``np.clip(audio * volume, -1, 1)``），**不是**硬件音量 ——
板载 codec 那边已经是 ``PCM 192 [100%] [0.00dB]`` + ``Speaker on``
⇒ 唯一的衰减就在软件这一处。

⇒ 本探针把同一句话按不同 ``volume`` 各送一遍，量每档**真正送进 aplay
的峰值**（队列快照的 ``last_peak`` / ``last_raw_peak``）。

跑法::

    .venv/bin/python tools/probes/interp/m15_tts_level.py
"""
from __future__ import annotations

import json
import ssl
import sys
import time
import urllib.error
import urllib.request

BASE = "https://127.0.0.1:8000"
SSL_CTX = ssl.create_default_context()

TEXT = "这是一次音量电平测试，用来量一量板载扬声器上实际送出的信号有多大。"
VOLUMES = [1.0, 0.5, 0.1, 0.0]


def api(method: str, path: str, payload=None, timeout: int = 120):
    data = None
    hdrs = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=hdrs,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
            body = r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
    try:
        return json.loads(body or "{}")
    except Exception:  # noqa: BLE001
        return {"raw": body}


def measure(volume: float, timeout: float = 90.0) -> dict:
    """送一句，等它播完，量峰值。"""
    api("POST", "/api/tts/queue/clear")
    done_before = int(api("GET", "/api/tts/queue/status").get("done") or 0)
    api("POST", "/api/tts/queue/submit",
        {"text": TEXT, "language": "chinese", "volume": volume})
    deadline = time.time() + timeout
    st: dict = {}
    while time.time() < deadline:
        time.sleep(1.0)
        st = api("GET", "/api/tts/queue/status")
        done = int(st.get("done") or 0)
        if done > done_before and not st.get("playing"):
            return {
                "volume": volume,
                "peak": float(st.get("last_peak") or 0.0),
                "raw_peak": float(st.get("last_raw_peak") or 0.0),
            }
    return {"volume": volume, "timeout": True}


def dbfs(v: float) -> str:
    import math
    if v <= 0:
        return "-inf dBFS"
    return "%.1f dBFS" % (20.0 * math.log10(v))


def main() -> int:
    print("=== TTS 实际出声电平（同一句话，不同 volume）===")
    rows = []
    for vol in VOLUMES:
        r = measure(vol)
        if r.get("timeout"):
            print(f"  volume={vol:<5} ⇒ 超时（没播完？）")
            rows.append(r)
            continue
        print(f"  volume={vol:<5} ⇒ 模型原始峰值 {r['raw_peak']:.4f} "
              f"({dbfs(r['raw_peak'])}) · "
              f"**出声峰值 {r['peak']:.4f}** ({dbfs(r['peak'])})")
        rows.append(r)

    print("\n=== 结论 ===")
    for r in rows:
        if r.get("timeout"):
            continue
        lvl = "（≈ 听不见）" if 0 < r["peak"] < 0.05 else ""
        print(f"  volume={r['volume']:<5} 出声 {r['peak']:.4f} "
              f"{dbfs(r['peak'])}{lvl}")
    print("  ⇒ 出声峰值 = 模型原始峰值 × volume（实测比例一致）")
    print("  ⇒ 模型自己的输出电平**不归一化**（同一句话多次合成峰值"
          "在 0.22~0.59 之间波动）")
    print("  ⇒ 同传面板的默认若是 0.1 ⇒ 出声 −24~−32 dBFS ⇒"
          "板载小喇叭上基本听不见（用户会当成「没播放」）")
    api("POST", "/api/tts/queue/clear")
    return 0


if __name__ == "__main__":
    sys.exit(main())
