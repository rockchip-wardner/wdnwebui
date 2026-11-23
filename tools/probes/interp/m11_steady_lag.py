#!/usr/bin/env python3
"""M11 —— **稳档为什么慢**的诊断探针。

跑一遍「文件源 + 稳档」同传，逐秒记录：

* ``segments`` / ``translated`` / ``submitted`` / ``played``（各阶段的计数）；
* ``lag_sec`` / ``lag_avg_sec``（面板上的滞后）；
* TTS 队列深度 / 积压秒数（**播放端欠了多少**）；
* tempo（是否在提速）。

⇒ 用来分辨「慢」到底慢在哪一段：切句？翻译？合成？播放？

用法::

    .venv/bin/python tools/probes/interp/m11_steady_lag.py [now|follow]
"""
from __future__ import annotations

import json
import ssl
import sys
import time
import urllib.request

BASE = "https://127.0.0.1:8000"
SSL_CTX = ssl.create_default_context()
WAV = "/userdata/assets/uploads/asr_en.wav"


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


def main() -> int:
    gate = sys.argv[1] if len(sys.argv) > 1 else "now"
    print(f"=== 文件源 + 稳档 + gate={gate} ===")
    api("POST", "/api/qwen3-asr/interp/start",
        {"mode": "steady", "gate": gate, "target_lang": "zh-CN",
         "volume": 0.05, "source": "file"})
    time.sleep(0.5)
    t0 = time.monotonic()
    api("POST", "/api/qwen3-asr/transcribe", payload=None) if False else None
    # 文件识别是 multipart ⇒ 单独走
    import subprocess
    subprocess.run([
        "curl", "-sk", "-X", "POST", BASE + "/api/qwen3-asr/transcribe",
        "-F", "path=" + WAV,
    ], capture_output=True, text=True)

    prev = None
    for i in range(70):
        time.sleep(1.0)
        st = api("GET", "/api/qwen3-asr/interp/status")
        q = api("GET", "/api/tts/queue/status")
        row = {
            "t": round(time.monotonic() - t0, 1),
            "seg": st.get("segments"), "tr": st.get("translated"),
            "sub": st.get("submitted"), "play": st.get("played"),
            "lag": st.get("lag_sec"), "lagavg": st.get("lag_avg_sec"),
            "qdepth": q.get("depth"), "qsec": q.get("queued_audio_sec"),
            "tempo": st.get("tempo"), "err": st.get("last_error") or "",
        }
        if row != prev:
            print("  ", json.dumps(row, ensure_ascii=False))
            prev = row
    api("POST", "/api/qwen3-asr/interp/stop")
    st = api("GET", "/api/qwen3-asr/interp/status")
    print("最终:", json.dumps(st, ensure_ascii=False)[:600])
    return 0


if __name__ == "__main__":
    sys.exit(main())
