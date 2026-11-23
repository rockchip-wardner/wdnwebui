#!/usr/bin/env python3
"""M27 —— **英文文件 → 中文翻译 → 播报**：端到端耗时与顿挫体检（基线增量口径）。

用户报（2026-11-23）：
    1、ASR+翻译之后，TTS 播出声音**太久了**
    2、中间还是会有**顿挫**
    3、素材 = ``/userdata/assets/uploads/asr_en.wav``（英文，15.05 s）

=========================  ==================================================
量                         口径
=========================  ==================================================
``播报总时长``              第一句出声 → 最后一句播完（墙钟）
``源话总时长``              源媒体的时长（15.05 s）
``长度比 k``                译文音频秒 ÷ 源话秒
``gap``                     **句间间隔**（管道空着等下一句）⇒ 顿挫
``starved``                 **句内饿死**（管道空着等合成）⇒ 顿挫
``产能 r``                  边际生产速率 / 整体速率（单卡）
``聚合产能``                 单卡 r × 本机 TTS 实例数
=========================  ==================================================

⚠️ **为什么必须用「基线增量」**：队列的 ``timings`` 是**累计**的（跨会话
不清零）⇒ 直接读会把上一轮（甚至用户的浏览器那一轮）的句子算进来
⇒ 「播报总时长」会变成几万秒的垃圾数。

用法::

    .venv/bin/python tools/probes/interp/m27_en2zh_e2e.py [--gate now|follow]
                                            [--seconds 60] [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import threading
import time
import urllib.request
import wave

BASE = "https://127.0.0.1:8000"
SRC_WAV = "/userdata/assets/uploads/asr_en.wav"

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


def _req(path: str, payload=None, method=None, timeout=180):
    url = BASE + path
    data = None
    headers = {}
    if payload is not None:
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


def _multipart(path: str, fields: dict, timeout=900):
    boundary = "----wdnprobe%s" % int(time.time() * 1000)
    body = b""
    for name, value in fields.items():
        body += (("--%s\r\n" % boundary).encode()
                 + ('Content-Disposition: form-data; name="%s"\r\n\r\n' % name).encode()
                 + str(value).encode() + b"\r\n")
    body += ("--%s--\r\n" % boundary).encode()
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
        with wave.open(path, "rb") as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except Exception:  # noqa: BLE001
        return 0.0


def _playhead_sim(duration: float) -> None:
    """模拟浏览器的播放头（跟播模式必需）。"""
    t0 = time.time()
    while True:
        el = time.time() - t0
        if el > duration + 1.0:
            _req("/api/qwen3-asr/interp/playhead",
                 {"stream_id": "", "playhead_sec": min(el, duration),
                  "playing": el < duration, "ended": el >= duration})
            return
        _req("/api/qwen3-asr/interp/playhead",
             {"stream_id": "", "playhead_sec": el, "playing": True,
              "ended": False})
        time.sleep(0.25)


def _baseline() -> int:
    """当前队列里**最大的 seq**（之后只统计比它新的句子）。"""
    st = _req("/api/tts/queue/status")
    seqs = [t.get("seq") for t in (st.get("timings") or [])
            if isinstance(t.get("seq"), int)]
    return max(seqs) if seqs else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gate", default="follow", choices=["now", "follow"])
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    dur = _wav_duration(SRC_WAV)
    print("== 素材 ==")
    print("   %s  源时长 %.2f s" % (SRC_WAV, dur))

    base_seq = _baseline()
    print("== 基线 ==")
    print("   队列 max seq = %d（之后只统计新句子）" % base_seq)

    _req("/api/qwen3-asr/interp/stop", {}, method="POST")
    time.sleep(0.5)
    st = _req("/api/qwen3-asr/interp/start",
              {"mode": "steady", "gate": args.gate, "target_lang": "zh-CN",
               "volume": 0.5, "source": "file"})
    print("== 起同传（gate=%s）==" % args.gate)
    print("   interp:", {k: st.get(k) for k in
                         ("running", "mt_ready", "tts_local_ready", "tempo")})
    deadline = time.time() + 150
    while time.time() < deadline:
        s = _req("/api/qwen3-asr/interp/status")
        if s.get("mt_ready") and s.get("tts_local_ready"):
            print("   模型就绪 ✓")
            break
        time.sleep(1.0)
    else:
        print("   ⚠️ 模型没就绪")

    _req("/api/tts/queue/clear", {}, method="POST")
    time.sleep(0.5)

    print("== 识别 + 翻译 + 播报 ==")
    t_run0 = time.time()
    t0 = t_run0
    _multipart("/api/qwen3-asr/transcribe", {"path": SRC_WAV, "append": "false"})
    print("   识别请求已发（%.1f s）" % (time.time() - t0))

    if args.gate == "follow":
        threading.Thread(target=_playhead_sim, args=(dur,), daemon=True).start()

    seen: dict = {}
    t_end = time.time() + args.seconds
    while time.time() < t_end:
        q = _req("/api/tts/queue/status")
        for t in (q.get("timings") or []):
            if isinstance(t.get("seq"), int) and t["seq"] > base_seq:
                seen[t["seq"]] = t
        time.sleep(0.25)

    rows = [seen[k] for k in sorted(seen)]
    if not rows:
        print("   ⚠️ 没采到新句子的 timings")
        return 1

    ist = _req("/api/qwen3-asr/interp/status")
    # ⚠️ **只取本轮的句子** —— 引擎的 ``timings`` 是**累计**的（跨会话不清零）
    # ⇒ 不按 ``t_seg``（本轮起点之后）过滤 ⇒ k 会被上一轮算错。
    iseg = [t for t in (ist.get("timings") or [])
            if isinstance(t.get("src_start"), (int, float))
            and float(t.get("t_seg") or 0) >= (t_run0 - 2.0)]
    tr_by_pos = {}
    for x in iseg:
        tr_by_pos.setdefault(round(float(x.get("src_start") or -1), 1), x)

    print("\n== 逐句 ==")
    print("%-3s %-30s %-28s %-11s %-7s %-7s %-7s %-6s %-6s" % (
        "#", "源文", "译文", "源秒", "音频秒", "首窗", "出场", "gap", "starv"))
    for t in rows:
        src = float(t.get("src_start_sec") or -1.0)
        x = tr_by_pos.get(round(src, 1)) or {}
        play = float(t.get("t_play") or 0)
        end = float(t.get("t_end") or 0)
        print("%-3s %-30s %-28s %-11s %-7s %-7s %-7s %-6s %-6s" % (
            t.get("seq"),
            (x.get("text") or "")[:15],
            (x.get("tr") or "")[:14],
            ("%.1f-%.1f" % (src, float(t.get("src_end_ts") and 0 or 0))) if False else
            ("%.1f" % src),
            t.get("audio_sec"), t.get("synth_lat"),
            ("%.2f" % (end - play)) if play and end else "-",
            t.get("gap"), t.get("starved")))

    # ---- 汇总 ----
    tr_audio = sum(float(t.get("audio_sec") or 0.0) for t in rows)
    src_audio = 0.0
    for x in iseg:
        a, b = x.get("src_start"), x.get("src_end")
        if a is None or b is None or a < 0 or b < 0:
            continue
        src_audio += float(b) - float(a)
    plays = [float(t.get("t_play") or 0.0) for t in rows if t.get("t_play")]
    ends = [float(t.get("t_end") or 0.0) for t in rows if t.get("t_end")]
    span = (max(ends) - min(plays)) if plays and ends else 0.0
    gaps = [float(t.get("gap") or 0.0) for t in rows]
    starv = [float(t.get("starved") or 0.0) for t in rows]
    mr = [float(t.get("m_rate") or 0.0) for t in rows if t.get("m_rate")]
    o_r = [float(t.get("o_rate") or 0.0) for t in rows if t.get("o_rate")]
    q = _req("/api/tts/queue/status")

    print("\n== 汇总 ==")
    print("   句数              : %d" % len(rows))
    print("   源媒体时长        : %.2f s" % dur)
    print("   源话音频合计      : %.2f s" % src_audio)
    print("   译文音频合计      : %.2f s" % tr_audio)
    if src_audio > 0.5:
        k = tr_audio / src_audio
        print("   **长度比 k**      : %.3f（译文比源话长 %.1f%%）" % (k, (k - 1) * 100))
    print("   **播报总时长**    : %.2f s（第一句出声 → 最后一句播完）" % span)
    print("   ⇒ 播报总时长 / 源时长 = %.2f×" % (span / dur if dur else 0))
    print("   首句出声时刻      : %+.2f s（相对第一句提交）" % (
        (min(plays) - min(float(t.get("t_submit") or 0) for t in rows))
        if plays else 0))
    print("   变速 tempo        : %s（队列当前 %s）"
          % (rows[-1].get("tempo"), q.get("tempo")))
    print("   单卡边际产能      : %s" % (
        " / ".join("%.3f" % x for x in mr)))
    print("   单卡整体产能      : %s" % (
        " / ".join("%.3f" % x for x in o_r)))
    print("   **本机 TTS 实例**  : %s ⇒ 聚合产能 ≈ %.2f×"
          % (q.get("tts_instances"), float(q.get("sustain_rate") or 0)
             * int(q.get("tts_instances") or 1)))
    print("   队列 need_tempo   : %s（变速上限 = 聚合产能）" % q.get("need_tempo"))
    print("   **句间间隔 gap**  : max %.3f s / 合计 %.3f s / >0.1 s 的句数 %d"
          % (max(gaps) if gaps else 0, sum(gaps),
             sum(1 for g in gaps if g > 0.1)))
    print("   **句内饿死 starved**: max %.2f s / 合计 %.2f s"
          % (max(starv) if starv else 0, sum(starv)))
    print("   可听滞后 a_lag    : %s s" % q.get("last_a_lag"))

    def _avg(vals):
        vals = [v for v in vals if isinstance(v, (int, float)) and v > 0]
        return (sum(vals) / len(vals)) if vals else 0.0

    print("\n== 每一段耗时（平均 / 最大，秒）==")
    print("   %-16s %-8s %-8s" % ("段", "平均", "最大"))
    seg_def = [
        ("ASR切句→出队", lambda x: float(x.get("t_deq") or 0) - float(x.get("t_seg") or 0)),
        ("等MT就绪", lambda x: float(x.get("wait_mt") or 0)),
        ("翻译推理", lambda x: float(x.get("infer") or 0)),
        ("翻译完→入队", lambda x: float(x.get("t_submit") or 0) - float(x.get("t_tr") or 0)),
    ]
    for name, fn in seg_def:
        vals = [fn(x) for x in iseg]
        vals = [v for v in vals if v >= 0]
        print("   %-16s %-8.2f %-8.2f" % (name, _avg(vals), max(vals) if vals else 0))
    q_defs = [
        ("入队→开始合成", lambda t: float(t.get("t_synth_start") or 0) - float(t.get("t_submit") or 0)),
        ("合成首窗", lambda t: float(t.get("t_first_pcm") or 0) - float(t.get("t_synth_start") or 0)),
        ("首窗→出声", lambda t: float(t.get("t_play") or 0) - float(t.get("t_first_pcm") or 0)),
        ("出声→播完", lambda t: float(t.get("t_end") or 0) - float(t.get("t_play") or 0)),
    ]
    for name, fn in q_defs:
        vals = [fn(t) for t in rows]
        vals = [v for v in vals if v >= 0]
        print("   %-16s %-8.2f %-8.2f" % (name, _avg(vals), max(vals) if vals else 0))

    if args.json:
        out = {
            "gate": args.gate, "src_dur": dur, "sentences": len(rows),
            "src_audio": round(src_audio, 2), "tr_audio": round(tr_audio, 2),
            "k": round(tr_audio / src_audio, 3) if src_audio else 0,
            "span": round(span, 2), "span_ratio": round(span / dur, 3) if dur else 0,
            "gap_sum": round(sum(gaps), 3), "gap_max": round(max(gaps) if gaps else 0, 3),
            "gap_over_100ms": sum(1 for g in gaps if g > 0.1),
            "starved_sum": round(sum(starv), 2),
            "tempo": q.get("tempo"), "need_tempo": q.get("need_tempo"),
            "tts_instances": q.get("tts_instances"),
            "sustain_rate": q.get("sustain_rate"), "rate": q.get("rate"),
            "a_lag": q.get("last_a_lag"),
            "rows": rows,
        }
        with open(args.json, "w") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        print("\n   JSON 已写: %s" % args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
