#!/usr/bin/env python3
"""探针：**同传播放线程的停顿快照**（2026-11-26）。

用法::

    .venv/bin/python scripts/probe_interp_queue.py 90 /tmp/q.log

每秒抓一次 ``GET /api/tts/queue/status`` ⇒ 落成一行行 JSON ⇒
用来定位「译文没出声的那几十秒里，队列到底在干什么」。

⚠️ **只读** ⇒ 不改任何状态 ⇒ 可以和端到端回归一起跑。
"""
from __future__ import annotations

import json
import ssl
import sys
import time
import urllib.request

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


def get(path: str):
    req = urllib.request.Request("https://127.0.0.1:8000" + path, method="GET")
    try:
        with urllib.request.urlopen(req, context=_CTX, timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def main() -> int:
    secs = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    out = sys.argv[2] if len(sys.argv) > 2 else "/tmp/q.log"
    t0 = time.time()
    with open(out, "w", encoding="utf-8") as fh:
        while time.time() - t0 < secs:
            q = get("/api/tts/queue/status")
            ts = q.get("timings") or []
            rows = []
            for t in ts[-8:]:
                rows.append("s%s@%.2f sub=%.1f fpc=%.1f play=%.1f" % (
                    t.get("seq"), float(t.get("src_start_sec") or -1),
                    float(t.get("t_submit") or 0), float(t.get("t_first_pcm") or 0),
                    float(t.get("t_play") or 0)))
            fh.write(json.dumps({
                "t": round(time.time() - t0, 1),
                "depth": q.get("depth"),
                "inflight": q.get("inflight"),
                "playing": q.get("playing"),
                "playing_text": (q.get("playing_text") or "")[:18],
                "queued_audio_sec": q.get("queued_audio_sec"),
                "clocked": q.get("clocked"),
                "clock_last": q.get("clock_last"),
                "clock_stall_sec": q.get("clock_stall_sec"),
                "media_state": q.get("media_state"),
                "tempo": q.get("tempo"),
                "done": q.get("done"),
                "merged": q.get("merged"),
                "dropped": q.get("dropped"),
                "timings": rows,
            }, ensure_ascii=False) + "\n")
            fh.flush()
            time.sleep(1.0)
    print("探针结束 ⇒ %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
