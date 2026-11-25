#!/usr/bin/env python3
"""M33 —— **播放连续性**实测（句间断音 / 开口存货 / 变速余量）。

背景（实测 2026-11-25，用户报「播放卡顿 + 切音」）：

* 每个作业开头都要等**首窗**（~2 s）⇒ 首窗期间管道必然是空的；
* 播放速度 == 可持续产能 ⇒ 管道里的存货**不涨也不跌** ⇒ 恒为 0 ⇒
  每个作业边界都会**断一次音**；
* ⇒ 修法：播放速度要比产能**低一档**（队列的 ``tempo_floor``）⇒
  存货会涨 ⇒ 首窗的空档被填平。

本探针跑**真实的同传链路**（文件源 + 跟播放 ⇒ 短句逐个成作业），
逐作业打印：

====================  ===================================================
口径                  含义
====================  ===================================================
``synth_lat``        首声延迟（开始合成 → 出声）
``gap``              **句间断音**（上一句放完 → 这一句出声）
``starved``          **句中饿死**（管道空着等 PCM）
``stock``            **开口时管道里的存货**（有 ⇒ 不会断）
====================  ===================================================

用法::

    .venv/bin/python -u tools/probes/interp/m33_playback_continuity.py
"""
from __future__ import annotations

import json
import ssl
import sys
import subprocess
import time
import urllib.request

BASE = "https://127.0.0.1:8000"
WAV = "/userdata/assets/uploads/asr_en.wav"
DURATION = 15.0

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


def api(method: str, path: str, payload=None, timeout: int = 120):
    url = BASE + path
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def report(ph: float, playing: bool, ended: bool = False) -> None:
    api("POST", "/api/qwen3-asr/interp/playhead",
        {"stream_id": "file-1#0", "playhead_sec": ph, "playing": playing,
         "ended": ended})


def main() -> int:
    # ⚠️ 参数：**切句档**（steady/fast）+ **音量**（默认 0.01 = 探针口径；
    # 想**听**就传 0.1 ⇒ 用户在板子旁边能判断杂音 / 卡顿 / 断音）。
    mode = sys.argv[1] if len(sys.argv) > 1 else "steady"
    volume = float(sys.argv[2]) if len(sys.argv) > 2 else 0.01
    print("== M33：播放连续性实测（文件源 + 跟播放 · %s 档 · 音量 %.0f%%）=="
          % (mode, volume * 100))
    api("POST", "/api/qwen3-asr/interp/stop")
    api("POST", "/api/tts/queue/clear")
    time.sleep(1.0)

    api("POST", "/api/qwen3-asr/interp/start",
        {"mode": mode, "gate": "follow", "target_lang": "zh-CN",
         "volume": volume, "source": "file"})
    time.sleep(0.5)

    t0 = time.monotonic()
    proc = subprocess.Popen([
        "curl", "-sk", "-X", "POST", BASE + "/api/qwen3-asr/transcribe",
        "-F", "path=" + WAV,
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    print("--- 模拟媒体播放（0 → %.0f s）---" % DURATION)
    for i in range(140):
        t = time.monotonic() - t0
        report(min(t, DURATION), t < DURATION)
        time.sleep(0.25)
        st = api("GET", "/api/qwen3-asr/interp/status")
        q = api("GET", "/api/tts/queue/status")
        if t > DURATION + 6 and int(q.get("depth") or 0) == 0 \
                and int(q.get("inflight") or 0) == 0 and not q.get("playing") \
                and int(st.get("pending") or 0) == 0:
            break

    api("POST", "/api/qwen3-asr/interp/stop")
    time.sleep(1.0)
    q = api("GET", "/api/tts/queue/status")
    st = api("GET", "/api/qwen3-asr/interp/status")

    print("\n== 队列状态 ==")
    print("  tempo=%.3f  need_tempo=%.3f  tempo_floor=%.3f  rate=%.3f  "
          "sustain_rate=%.3f  done=%s  dropped=%s"
          % (q.get("tempo", 0), q.get("need_tempo", 0),
             q.get("tempo_floor", 0), q.get("rate", 0),
             q.get("sustain_rate", 0), q.get("done"), q.get("dropped")))
    print("  interp: segments=%s translated=%s submitted=%s pending=%s "
          "lag=%.2f"
          % (st.get("segments"), st.get("translated"), st.get("submitted"),
             st.get("pending"), float(st.get("lag_sec") or 0)))

    rows = q.get("timings") or []
    print("\n%-4s %-7s %-9s %-9s %-9s %-8s %-8s %-8s"
          % ("#", "变速", "首声", "句间断音", "句中饿死", "开口存货", "音频", "m_rate"))
    for t in rows:
        print("%-4s %-7.3f %-9.2f %-9.2f %-9.2f %-8.2f %-8.2f %-8.2f"
              % (t.get("seq"), t.get("tempo", 0), t.get("synth_lat", 0),
                 t.get("gap", 0), t.get("starved", 0), t.get("stock", 0),
                 t.get("audio_sec", 0), t.get("m_rate", 0)))
    if rows:
        print("\n  作业数=%d  句间断音合计=%.2f s（最大 %.2f）  "
              "句中饿死合计=%.2f s  开口存货最大=%.2f s"
              % (len(rows), sum(float(t.get("gap") or 0) for t in rows),
                 max(float(t.get("gap") or 0) for t in rows),
                 sum(float(t.get("starved") or 0) for t in rows),
                 max(float(t.get("stock") or 0) for t in rows)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
