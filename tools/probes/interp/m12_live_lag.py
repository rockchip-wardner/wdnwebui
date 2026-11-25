#!/usr/bin/env python3
"""M12 —— **实时流 + 稳档**的滞后时间线（决定性实验）。

模拟浏览器麦（``/live/chunk`` 上行）喂真实音频，逐秒记录：

* ``segments``（切句） / ``translated`` / ``submitted`` / ``played``；
* ``lag_sec``（面板上的滞后） / 队列深度 / 积压秒数 / tempo；
* **每句的墙钟时间线**：成句 → 翻译 → 入队 → 播完。

⇒ 回答「稳档为什么滞后 18.5 秒」：
是**切句晚**（endpoint 要等静音）？还是**播放端欠账**（队列积压）？

用法::

    .venv/bin/python tools/probes/interp/m12_live_lag.py [fast|steady] [秒数]
"""
from __future__ import annotations

import json
import ssl
import sys
import time
import urllib.request
import wave
from pathlib import Path

BASE = "https://127.0.0.1:8000"
SSL_CTX = ssl.create_default_context()
WAV = "/userdata/assets/uploads/asr_en.wav"


def api(method: str, path: str, payload=None, headers=None, timeout: int = 60):
    data = None
    hdrs = dict(headers or {})
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


def load_pcm() -> bytes:
    import subprocess
    import tempfile
    out = Path(tempfile.gettempdir()) / ".m12_live16.raw"
    subprocess.run(["sox", WAV, "-r", "16000", "-c", "1", "-b", "16",
                    "-e", "signed-integer", str(out)], check=True)
    return out.read_bytes()


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "steady"
    seconds = int(sys.argv[2]) if len(sys.argv) > 2 else 25
    pcm = load_pcm()

    print(f"=== 实时流 + {mode} 档 + {seconds}s 音频 ===")
    api("POST", "/api/qwen3-asr/interp/start",
        {"mode": mode, "gate": "now", "target_lang": "zh-CN",
         "volume": 0.01, "source": "live"})
    time.sleep(0.5)
    base = api("GET", "/api/qwen3-asr/interp/status")

    sid = api("POST", "/api/qwen3-asr/live/start",
              {"source": "browser", "sample_rate": 16000, "n_channels": 1,
               "sample_width": 2, "chunk_ms": 1000})["stream_id"]
    chunk = 32000
    n = min(seconds, max(1, len(pcm) // chunk))
    t0 = time.monotonic()
    prev = None
    for i in range(n):
        target = t0 + i * 1.0
        wait = target - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        off = (i * chunk) % max(1, len(pcm) - chunk)
        try:
            api("POST", "/api/qwen3-asr/live/chunk", payload=None,
                headers={"X-Asr-Stream-Id": sid, "X-Asr-Seq": str(i),
                         "Content-Type": "audio/pcm"},
                ) if False else None
            req = urllib.request.Request(
                BASE + "/api/qwen3-asr/live/chunk", data=pcm[off:off + chunk],
                headers={"X-Asr-Stream-Id": sid, "X-Asr-Seq": str(i),
                         "Content-Type": "audio/pcm"}, method="POST")
            with urllib.request.urlopen(req, timeout=60,
                                        context=SSL_CTX) as r:
                r.read()
        except Exception as exc:  # noqa: BLE001
            print("  送块失败:", exc)
        # 逐秒采样
        st = api("GET", "/api/qwen3-asr/interp/status")
        q = api("GET", "/api/tts/queue/status")
        row = {
            "t": round(time.monotonic() - t0, 1),
            "seg": st.get("segments", 0) - base.get("segments", 0),
            "tr": st.get("translated", 0) - base.get("translated", 0),
            "sub": st.get("submitted", 0) - base.get("submitted", 0),
            "play": st.get("played"),
            "lag": st.get("lag_sec"), "qdepth": q.get("depth"),
            "qsec": q.get("queued_audio_sec"), "tempo": st.get("tempo"),
        }
        if row != prev:
            print("  ", json.dumps(row, ensure_ascii=False))
            prev = row

    print("  … 收尾（说完就停）")
    try:
        api("POST", "/api/qwen3-asr/live/finish",
            headers={"X-Asr-Stream-Id": sid})
    except Exception as exc:  # noqa: BLE001
        print("  finish:", exc)
    for _ in range(40):
        time.sleep(2)
        st = api("GET", "/api/qwen3-asr/interp/status")
        q = api("GET", "/api/tts/queue/status")
        print("   ", json.dumps({
            "seg": st.get("segments", 0) - base.get("segments", 0),
            "tr": st.get("translated", 0) - base.get("translated", 0),
            "sub": st.get("submitted", 0) - base.get("submitted", 0),
            "play": st.get("played"), "lag": st.get("lag_sec"),
            "qdepth": q.get("depth"), "qsec": q.get("queued_audio_sec"),
            "tempo": st.get("tempo"),
        }, ensure_ascii=False))
        if q.get("depth") == 0 and not q.get("playing"):
            break
    api("POST", "/api/qwen3-asr/interp/stop")
    return 0


if __name__ == "__main__":
    sys.exit(main())
