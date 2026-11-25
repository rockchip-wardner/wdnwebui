#!/usr/bin/env python3
"""M9 —— **浏览器麦上行 → 同传** 端到端探针。

背景：S3 的端到端只验过**文件源**（``/transcribe``）。用户最关心的其实是
**实时流**（浏览器麦 / 板载麦）⇒ 这里走**麦上行那条链路**
（``POST /api/qwen3-asr/live/chunk``）把真实音频灌进去，看同传引擎收不收得到句子。

验三件事：

1. **快档**（``mode=fast``）—— ``unfixed`` 的稳定前缀成句 ⇒ 边说边出句；
2. **稳档**（``mode=steady``）—— endpoint（静音 1200 ms）成句；
   ⚠️ 并且验「**说完就停**（finish）时最后一句也不丢」
   （``finish`` 不产生 endpoint ⇒ 收尾钩子必须补这一下）；
3. 两种档位下 **translated / submitted > 0**（真的翻出来了、真的进队列了）。

用法::

    .venv/bin/python tools/probes/interp/m9_live_interp.py
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


def post_pcm(path: str, pcm: bytes, headers: dict) -> dict:
    req = urllib.request.Request(BASE + path, data=pcm, headers=headers,
                                 method="POST")
    with urllib.request.urlopen(req, timeout=60, context=SSL_CTX) as r:
        return json.loads(r.read().decode("utf-8"))


def load_wav(path: str) -> bytes:
    """转成实时路径要的 **16 kHz / 单声道 / s16le** 裸 PCM。

    ⚠️ 素材 ``asr_en.wav`` 是 48 kHz / s24 ⇒ 直接喂会被拒 ⇒ 用 sox 转
    （sox 在本板是有的，见 :mod:`m6b_live_contention`）。
    """
    import subprocess
    import tempfile

    with wave.open(path, "rb") as w:
        if (w.getsampwidth(), w.getnchannels(), w.getframerate()) == (2, 1, 16000):
            return w.readframes(w.getnframes())
    out = Path(tempfile.gettempdir()) / ".m9_live16.raw"
    subprocess.run(["sox", path, "-r", "16000", "-c", "1", "-b", "16",
                    "-e", "signed-integer", str(out)], check=True)
    return out.read_bytes()


def run_mode(mode: str, pcm: bytes, seconds: int) -> dict:
    print(f"\n=== {mode} 档 ===")
    api("POST", "/api/qwen3-asr/interp/start",
        {"mode": mode, "gate": "now", "target_lang": "zh-CN",
         "volume": 0.01, "source": "live"})
    time.sleep(0.5)
    before = api("GET", "/api/qwen3-asr/interp/status")

    sid = api("POST", "/api/qwen3-asr/live/start",
              {"source": "browser", "sample_rate": 16000, "n_channels": 1,
               "sample_width": 2, "chunk_ms": 1000})["stream_id"]
    chunk_bytes = 32000                      # 1 s @ 16 kHz s16le
    n = min(seconds, max(1, len(pcm) // chunk_bytes))
    t0 = time.monotonic()
    lag, dropped = [], 0
    for i in range(n):
        target = t0 + i * 1.0                 # 真实时间节拍
        wait = target - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        off = (i * chunk_bytes) % max(1, len(pcm) - chunk_bytes)
        try:
            js = post_pcm("/api/qwen3-asr/live/chunk", pcm[off:off + chunk_bytes],
                          {"X-Asr-Stream-Id": sid, "X-Asr-Seq": str(i),
                           "Content-Type": "audio/pcm"})
            if js.get("lag_ms") is not None:
                lag.append(int(js["lag_ms"]))
            dropped += int(js.get("dropped_chunks") or 0)
        except Exception as exc:  # noqa: BLE001
            print("  送块失败:", exc)
            dropped += 1
    # 「说完就停」⇒ finish（**不产生 endpoint**）⇒ 验最后一句也没丢
    try:
        api("POST", "/api/qwen3-asr/live/finish", headers={"X-Asr-Stream-Id": sid})
    except Exception as exc:  # noqa: BLE001
        print("  finish 失败:", exc)

    # 等翻译 + 播报队列排空
    deadline = time.time() + 90
    st = {}
    while time.time() < deadline:
        time.sleep(2)
        st = api("GET", "/api/qwen3-asr/interp/status")
        if st.get("segments", 0) >= before.get("segments", 0) + 1 and \
                st.get("submitted", 0) >= before.get("submitted", 0) + 1:
            break
    time.sleep(3)
    st = api("GET", "/api/qwen3-asr/interp/status")
    api("POST", "/api/qwen3-asr/interp/stop")
    d = {
        "mode": mode,
        "segments": st.get("segments", 0) - before.get("segments", 0),
        "translated": st.get("translated", 0) - before.get("translated", 0),
        "submitted": st.get("submitted", 0) - before.get("submitted", 0),
        "played": st.get("played", 0),
        "failed": st.get("failed", 0),
        "dropped_chunks": dropped,
        "lag_ms_max": max(lag) if lag else None,
        "last_error": st.get("last_error", ""),
    }
    print("  ", json.dumps(d, ensure_ascii=False))
    return d


def main() -> int:
    pcm = load_wav(WAV)
    print(f"素材 {WAV}: {len(pcm)} 字节 = {len(pcm) / 32000:.1f} s")

    fast = run_mode("fast", pcm, seconds=12)
    time.sleep(2)
    steady = run_mode("steady", pcm, seconds=12)

    ok = True
    print("\n=== 判据 ===")
    for d in (fast, steady):
        tag = d["mode"]
        c1 = d["segments"] > 0
        c2 = d["translated"] > 0
        c3 = d["submitted"] > 0
        c4 = d["dropped_chunks"] == 0
        print(f"  {'ok  ' if c1 else 'FAIL'} {tag}: 切出句子 {d['segments']} 句")
        print(f"  {'ok  ' if c2 else 'FAIL'} {tag}: 翻译 {d['translated']} 句")
        print(f"  {'ok  ' if c3 else 'FAIL'} {tag}: 进 TTS 队列 {d['submitted']} 句")
        print(f"  {'ok  ' if c4 else 'FAIL'} {tag}: 上行丢块 {d['dropped_chunks']}")
        ok = ok and c1 and c2 and c3 and c4
    print("\n结论:", "全部通过" if ok else "有失败项")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
