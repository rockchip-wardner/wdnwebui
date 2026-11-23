#!/usr/bin/env python3
"""M21 —— **同传播放侧的量化体检**（用户报「播放滞后很多」+「中途卡顿断音」）。

这一轮把两个投诉都变成**可测量的数字**：

=========================  ==========================================
指标                      口径（队列的 ``timings``）
=========================  ==========================================
``gap``                   **句间间隔**：上一句播完 → 这一句出声。
                          > 0.1 s ⇒ **听得出来的断音**。
``starved``               **饿死了多久**：管道空着等合成。
                          ⇒ 句**内部**的断音。
``stock``                 开口时**管道里的存货**（秒）。
``pipe_lat``              管线延迟（入队 → 出声）。
``m_rate``                **边际生产速率**（首窗之后的音频秒/墙钟秒）
                          = **产能**。
``o_rate``                整体速率（含首窗延迟）—— 只作诊断。
``audio_sec``             这句译文的音频时长（变速前）。
=========================  ==========================================

外加两个**结构性**数字：

* **k = 译文长度比** = Σ译文音频秒 ÷ Σ源话秒
  ⇒ k > 1 ⇒ 译文天然比源话长 ⇒ 变速必须 = k 才跟得住。
* **产能缺口** = 1 − m_rate/k
  ⇒ > 0 ⇒ **物理上跟不平**（译文再快也追不上源话）。

用法::

    .venv/bin/python tools/probes/interp/m21_pipeline_metrics.py [--now|--follow]
"""
from __future__ import annotations

import argparse
import json
import ssl
import sys
import time
import urllib.error
import urllib.request

BASE = "https://127.0.0.1:8000"
SRC_WAV = "/userdata/assets/uploads/asr_en.wav"

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


def _req(path: str, payload=None, method=None, timeout=60):
    url = BASE + path
    data = None
    headers = {}
    if payload is not None:
        if isinstance(payload, (bytes, bytearray)):
            data = bytes(payload)
        else:
            data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method=method or ("POST" if data else "GET"))
    with urllib.request.urlopen(req, context=_CTX, timeout=timeout) as r:
        raw = r.read()
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return {"_raw": raw[:200].decode("utf-8", "replace")}


def _multipart(path: str, field: str, value: str, timeout=300):
    """最小 multipart/form-data（单字段）。"""
    boundary = "----wdnprobe%s" % int(time.time() * 1000)
    body = (
        ("--%s\r\n" % boundary).encode()
        + ('Content-Disposition: form-data; name="%s"\r\n\r\n' % field).encode()
        + value.encode() + b"\r\n"
        + ("--%s--\r\n" % boundary).encode()
    )
    req = urllib.request.Request(
        BASE + path, data=body,
        headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary})
    with urllib.request.urlopen(req, context=_CTX, timeout=timeout) as r:
        raw = r.read()
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return {"_raw": raw[:300].decode("utf-8", "replace")}


def _wav_duration(path: str) -> float:
    try:
        import wave
        with wave.open(path, "rb") as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except Exception:  # noqa: BLE001
        return 0.0


def _playhead_sim(duration: float, stream_id: str = "") -> None:
    """**模拟浏览器的播放头**（跟播模式必需）。

    浏览器每 250 ms 上报一次 ``currentTime`` ⇒ 这里照做 ⇒
    不用开浏览器就能量「跟播」的真实滞后。
    """
    t0 = time.time()
    while True:
        el = time.time() - t0
        if el > duration + 1.0:
            _req("/api/qwen3-asr/interp/playhead",
                 {"stream_id": stream_id, "playhead_sec": min(el, duration),
                  "playing": el < duration, "ended": el >= duration})
            return
        _req("/api/qwen3-asr/interp/playhead",
             {"stream_id": stream_id, "playhead_sec": el,
              "playing": True, "ended": False})
        time.sleep(0.25)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gate", default="now", choices=["now", "follow"])
    ap.add_argument("--seconds", type=float, default=45.0)
    ap.add_argument("--warm", action="store_true", default=True)
    args = ap.parse_args()

    print("== 起同传（gate=%s）==" % args.gate)
    st = _req("/api/qwen3-asr/interp/start",
              {"mode": "steady", "gate": args.gate, "target_lang": "zh-CN",
               "volume": 0.5, "source": "file"})
    print("   interp:", {k: st.get(k) for k in
                         ("running", "mt_ready", "tts_local_ready", "tempo")})

    # 热身：等 MT / TTS 就绪（冷启动 20~30 s）
    deadline = time.time() + 120
    while time.time() < deadline:
        s = _req("/api/qwen3-asr/interp/status")
        if s.get("mt_ready") and s.get("tts_local_ready"):
            print("   模型就绪 ✓")
            break
        time.sleep(1.0)
    else:
        print("   ⚠️ 模型没就绪（继续测，数字会偏冷启动）")

    # 清队列（热身残留）
    _req("/api/tts/queue/clear", {}, method="POST")
    time.sleep(0.5)

    print("== 识别文件 ==")
    t0 = time.time()
    _multipart("/api/qwen3-asr/transcribe", "path", SRC_WAV)
    print("   识别耗时 %.1f s" % (time.time() - t0))

    # 跟播模式 ⇒ 模拟浏览器的播放头（不然放行器会一直压着句子）
    sim = None
    if args.gate == "follow":
        import threading
        dur = _wav_duration(SRC_WAV)
        print("   媒体时长 %.2f s ⇒ 模拟播放头" % dur)
        sim = threading.Thread(target=_playhead_sim, args=(dur, ""),
                               daemon=True)
        sim.start()

    print("== 采样 %d s ==" % int(args.seconds))
    seen = {}
    t_end = time.time() + args.seconds
    while time.time() < t_end:
        q = _req("/api/tts/queue/status")
        for t in (q.get("timings") or []):
            seen[t.get("seq")] = t
        time.sleep(0.25)

    rows = [seen[k] for k in sorted(seen, key=lambda x: (x is None, x))]
    if not rows:
        print("   ⚠️ 没有采到 timings")
        print(json.dumps(_req("/api/tts/queue/status"), ensure_ascii=False)[:1200])
        return 1

    print("\n%-5s %-8s %-6s %-8s %-7s %-7s %-6s %-7s %-7s %s" % (
        "seq", "synth_lat", "gap", "starved", "stock", "audio", "m_rate",
        "tempo", "a_lag", "lag"))
    for t in rows:
        print("%-5s %-8s %-6s %-8s %-7s %-7s %-6s %-7s %-7s %s" % (
            t.get("seq"), t.get("synth_lat"), t.get("gap"), t.get("starved"),
            t.get("stock"), t.get("audio_sec"), t.get("m_rate"),
            t.get("tempo"), t.get("a_lag"), t.get("lag")))

    gaps = [float(t.get("gap") or 0.0) for t in rows]
    starv = [float(t.get("starved") or 0.0) for t in rows]
    mr = [float(t.get("m_rate") or 0.0) for t in rows if t.get("m_rate")]
    tr_audio = sum(float(t.get("audio_sec") or 0.0) for t in rows)
    src_audio = 0.0
    try:
        ist = _req("/api/qwen3-asr/interp/status")
        for x in (ist.get("timings") or []):
            a, b = x.get("src_start"), x.get("src_end")
            if a is None or b is None or a < 0 or b < 0:
                continue
            src_audio += float(b) - float(a)
    except Exception as exc:  # noqa: BLE001
        print("   （取源话时长失败：%s）" % exc)

    q = _req("/api/tts/queue/status")
    print("\n== 汇总 ==")
    print("   句数            : %d" % len(rows))
    print("   句间间隔 gap    : max %.3f s / 平均 %.3f s / >0.1s 的句数 %d"
          % (max(gaps), sum(gaps) / len(gaps),
             sum(1 for g in gaps if g > 0.1)))
    print("   句内饿死 starved: max %.2f s / 合计 %.2f s"
          % (max(starv), sum(starv)))
    if mr:
        mr_sorted = sorted(mr)
        print("   边际产能 m_rate : 中位 %.3f×（最小 %.3f / 最大 %.3f）"
              % (mr_sorted[len(mr_sorted) // 2], min(mr), max(mr)))
    else:
        print("   边际产能 m_rate : 没测到")
    print("   队列 rate/need  : %s / %s" % (q.get("rate"), q.get("need_tempo")))
    print("   译文音频合计    : %.2f s" % tr_audio)
    print("   源话音频合计    : %.2f s" % src_audio)
    if src_audio > 0.5:
        k = tr_audio / src_audio
        print("   **长度比 k**     : %.3f" % k)
        al = [float(t.get("a_lag") or 0.0) for t in rows if t.get("a_lag")]
        if al:
            print("   **可听滞后**     : %s（>0 = 译文比源话晚）"
                  % " / ".join("%.2f" % x for x in al[-8:]))
        if mr:
            m = mr_sorted[len(mr_sorted) // 2]
            print("   **产能缺口**     : 1 − m_rate/k = %.3f"
                  % (1.0 - m / k))
            print("   ⇒ 译文最快只能跑到源话的 %.1f%% ⇒ 每秒落后 %.2f s"
                  % (100.0 * m / k, max(0.0, 1.0 - m / k)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
