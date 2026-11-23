#!/usr/bin/env python3
"""M26 —— **英文文件 → 中文翻译 → 播报**：时长与顿挫的**逐段耗时**体检。

用户报（2026-11-23）：
    1、ASR+翻译之后，TTS 播出声音**太久了**
    2、中间还是会有**顿挫**
    3、素材 = ``/userdata/assets/uploads/asr_en.wav``（英文，15.05 s）

这一版把两件事变成**可测量的数**：

=========================  ==================================================
量                        口径
=========================  ==================================================
``播报总时长``            第一句出声 → 最后一句播完（墙钟）⇒ **「太久了」的真身**
``源话总时长``            源媒体的时长（15.05 s）
``译文音频合计``          Σ 每句译文音频秒（变速前）
``k``                     译文音频 ÷ 源话秒 ⇒ **译文天然比源话长多少**
``gap``                   **句间间隔**（管道空着等下一句）⇒ 句与句之间的顿挫
``starved``               **句内饿死**（管道空着等合成）⇒ 句内部的顿挫
``m_rate``                **边际生产速率**（产能真值）
``产能缺口``              1 − m_rate/k ⇒ 译文每秒落后多少
=========================  ==================================================

外加**每一段耗时**（ASR 切句 → 出队 → 等 MT → 翻译推理 → 放行等待 →
排队等待 → 合成首窗 → 出声 → 播完）。

用法::

    .venv/bin/python tools/probes/interp/m26_en2zh_duration.py [--gate now|follow]
"""
from __future__ import annotations

import argparse
import json
import ssl
import sys
import threading
import time
import urllib.request
import wave

BASE = "https://127.0.0.1:8000"
SRC_WAV = "/userdata/assets/uploads/asr_en.wav"

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


def _req(path: str, payload=None, method=None, timeout=120):
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gate", default="follow", choices=["now", "follow"])
    ap.add_argument("--seconds", type=float, default=90.0)
    args = ap.parse_args()

    dur = _wav_duration(SRC_WAV)
    print("== 素材 ==")
    print("   %s  源时长 %.2f s" % (SRC_WAV, dur))

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
    t0 = time.time()
    _multipart("/api/qwen3-asr/transcribe", {"path": SRC_WAV, "append": "false"})
    print("   识别请求已发（%.1f s）" % (time.time() - t0))

    if args.gate == "follow":
        threading.Thread(target=_playhead_sim, args=(dur,), daemon=True).start()

    seen: dict = {}
    t_end = time.time() + args.seconds
    while time.time() < t_end:
        q = _req("/api/tts/queue/status")
        for t in (q.get("timings") or []):
            seen[t.get("seq")] = t
        time.sleep(0.25)

    rows = [seen[k] for k in sorted(seen, key=lambda x: (x is None, x))]
    if not rows:
        print("   ⚠️ 没采到 timings")
        print(json.dumps(_req("/api/tts/queue/status"), ensure_ascii=False)[:1200])
        return 1

    # 引擎侧的逐段耗时（ASR → 翻译 → 放行）
    ist = _req("/api/qwen3-asr/interp/status")
    iseg = list(ist.get("timings") or [])

    print("\n== 逐句：源文 / 译文 / 耗时 ==")
    print("%-3s %-34s %-30s %-7s %-7s %-7s %-7s %-6s %-6s" % (
        "#", "源文", "译文", "源秒", "音频秒", "首窗", "出场", "gap", "starv"))
    tr_by_pos = {}
    for x in iseg:
        tr_by_pos.setdefault(round(float(x.get("src_start") or -1), 1), x)
    for i, t in enumerate(rows):
        src = float(t.get("src_start_sec") or -1.0)
        x = tr_by_pos.get(round(src, 1)) or {}
        print("%-3s %-34s %-30s %-7s %-7s %-7s %-7s %-6s %-6s" % (
            t.get("seq"),
            (x.get("text") or "")[:16],
            (x.get("tr") or "")[:15],
            ("%.1f-%.1f" % (src, float(t.get("src_end_sec") or 0))) if src >= 0 else "-",
            t.get("audio_sec"),
            t.get("synth_lat"),
            ("%.2f" % (float(t.get("t_end") or 0) - float(t.get("t_play") or 0)))
            if t.get("t_play") and t.get("t_end") else "-",
            t.get("gap"),
            t.get("starved")))

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
    tempo = rows[-1].get("tempo")

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
    print("   变速 tempo        : %s" % tempo)
    if mr:
        m = sorted(mr)[len(mr) // 2]
        print("   边际产能 m_rate   : %.3f×（%s）" % (m, " / ".join(
            "%.3f" % x for x in mr)))
        if src_audio > 0.5:
            k = tr_audio / src_audio
            print("   **产能缺口**      : 1 − m_rate/k = %.3f ⇒ 每秒落后 %.2f s"
                  % (1.0 - m / k, max(0.0, 1.0 - m / k)))
    print("   句间间隔 gap      : max %.3f s / 合计 %.3f s / >0.1 s 的句数 %d"
          % (max(gaps) if gaps else 0, sum(gaps),
             sum(1 for g in gaps if g > 0.1)))
    print("   句内饿死 starved  : max %.2f s / 合计 %.2f s"
          % (max(starv) if starv else 0, sum(starv)))
    # 每一段的耗时（从引擎侧 + 队列侧）
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
        ("入队→开始合成", lambda x: float(x.get("t_synth_start") or 0) - float(x.get("t_submit") or 0)),
        ("合成首窗", lambda x: float(x.get("t_first_pcm") or 0) - float(x.get("t_synth_start") or 0)),
        ("首窗→出声", lambda x: float(t.get("t_play") or 0) - float(t.get("t_first_pcm") or 0)),
        ("出声→播完", lambda x: float(t.get("t_end") or 0) - float(t.get("t_play") or 0)),
    ]
    for name, fn in seg_def:
        if name == "合成首窗":
            vals = [float(t.get("t_first_pcm") or 0) - float(t.get("t_synth_start") or 0)
                    for t in rows]
        elif name == "首窗→出声":
            vals = [float(t.get("t_play") or 0) - float(t.get("t_first_pcm") or 0)
                    for t in rows]
        elif name == "出声→播完":
            vals = [float(t.get("t_end") or 0) - float(t.get("t_play") or 0)
                    for t in rows]
        else:
            vals = [fn(x) for x in iseg]
        vals = [v for v in vals if v >= 0]
        print("   %-16s %-8.2f %-8.2f" % (name, _avg(vals), max(vals) if vals else 0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
