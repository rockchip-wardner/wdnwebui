#!/usr/bin/env python3
"""M29 —— **「ASR+翻译后，TTS 播放滞后很多」**的**可听滞后**体检（用户报，2026-11-23）。

用户报（2026-11-23 06:12）：
    「流式语音识别」中，ASR+翻译后，TTS 播放**滞后非常多**。

这个探针量的是**用户真正感受到的那一口**：

    **可听滞后 a_lag = 译文「出声」的时刻 − 源话「开口」的时刻**

⚠️ **为什么要自己重算一遍**：面板上的 ``lag_sec`` 是「播完 − 源话说完」
（另一个口径），而文件源的「源话说完时刻」要用**播放头反推的媒体零点**
⇒ 播放头没上报时那个数是**假的**（实测能到 27 s）⇒ 不能拿来下结论。

=========================  ==================================================
量                         口径
=========================  ==================================================
``媒体零点``                播放头第一次报 0 的墙钟时刻（= 源话开口的基准）
``可听滞后``                出声时刻 − (媒体零点 + src_start_sec)
``译文出声时刻``            队列写进 aplay 的第一块 PCM 的时刻
``合成首窗``                开始合成 → 第一块 PCM（同传链路上最大的固定延迟）
``放行等待``                译文译完 → 放行器放行（跟播时被播放头压住的时间）
``产能 m_rate``             首窗之后的边际生产速率（**产能真值**）
``长度比 k``                译文音频秒 ÷ 源话秒（**滞后增长的根源**）
=========================  ==================================================

⚠️ **播放头必须带 ``stream_id``**（跟浏览器一样）—— 带空 id ⇒ 服务端配不上
⇒ 放行器走「卡住兜底」⇒ **整条链路晚 10 s 起跳**（实测踩过）。

⚠️ **默认音量 0.03**（自测要小声）—— 音量是**振幅倍率**，不动系统混音器。

用法::

    .venv/bin/python tools/probes/interp/m29_lag_floor.py --gate follow
    .venv/bin/python tools/probes/interp/m29_lag_floor.py --gate now
    .venv/bin/python tools/probes/interp/m29_lag_floor.py --repeat 4 --seconds 120
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import subprocess
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


def _make_repeat(src: str, times: int) -> str:
    """把素材**首尾相接** N 遍 ⇒ 长的媒体（量「滞后随时间的增长」）。"""
    if times <= 1:
        return src
    out = "/tmp/m29_repeat_%d.wav" % times
    with wave.open(src, "rb") as w:
        params = w.getparams()
        frames = w.readframes(w.getnframes())
    with wave.open(out, "wb") as w:
        w.setparams(params)
        for _ in range(times):
            w.writeframes(frames)
    return out


class _PlayheadSim:
    """模拟浏览器的播放头上报（250 ms 一次，**带 stream_id**）。"""

    def __init__(self, duration: float, sid: str):
        self.duration = duration
        self.sid = sid
        self.t0 = 0.0
        self.zero_at = 0.0          # 媒体零点（墙钟）
        self._stop = threading.Event()
        self._th = None

    def start(self):
        self.t0 = time.time()
        self.zero_at = self.t0
        self._th = threading.Thread(target=self._run, daemon=True)
        self._th.start()

    def _run(self):
        last = None
        while not self._stop.is_set():
            el = time.time() - self.t0
            ended = el >= self.duration
            ph = min(el, self.duration)
            # ⚠️ 跟浏览器一样：**只在「有变化」时上报**（省请求）。
            state = (round(ph, 2), not ended)
            if state != last:
                last = state
                _req("/api/qwen3-asr/interp/playhead",
                     {"stream_id": self.sid, "playhead_sec": ph,
                      "playing": not ended, "ended": ended, "failed": False})
            if ended:
                break
            self._stop.wait(0.25)

    def stop(self):
        self._stop.set()


def _baseline() -> int:
    st = _req("/api/tts/queue/status")
    seqs = [t.get("seq") for t in (st.get("timings") or [])
            if isinstance(t.get("seq"), int)]
    return max(seqs) if seqs else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gate", default="follow", choices=["now", "follow"])
    ap.add_argument("--volume", type=float, default=0.03,
                    help="振幅倍率（自测要小声；默认 0.03）")
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--repeat", type=int, default=1,
                    help="素材首尾相接几遍（量滞后增长）")
    ap.add_argument("--src", default=SRC_WAV)
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    src = _make_repeat(args.src, args.repeat)
    dur = _wav_duration(src)
    print("== 素材 ==")
    print("   %s" % src)
    print("   源时长 %.2f s" % dur)

    base_seq = _baseline()

    _req("/api/qwen3-asr/interp/stop", {}, method="POST")
    time.sleep(0.5)
    st = _req("/api/qwen3-asr/interp/start",
              {"mode": "steady", "gate": args.gate, "target_lang": "zh-CN",
               "volume": args.volume, "source": "file"})
    print("== 起同传（gate=%s, volume=%.3f）==" % (args.gate, args.volume))
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

    # ⚠️ **播放头要带引擎当前的 stream_id** —— 跟浏览器一样。
    sid = str(_req("/api/qwen3-asr/interp/status").get("stream_id") or "")
    print("   播放头上报的 stream_id = %r" % sid)

    sim = _PlayheadSim(dur, sid)
    print("\n== 识别 + 翻译 + 播报 ==")
    t_run0 = time.time()
    sim.start()
    _multipart("/api/qwen3-asr/transcribe", {"path": src, "append": "false"})
    print("   识别请求已发（媒体零点 = %.3f）" % sim.zero_at)

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
        sim.stop()
        return 1

    ist = _req("/api/qwen3-asr/interp/status")
    iseg = [t for t in (ist.get("timings") or [])
            if float(t.get("t_seg") or 0) >= (t_run0 - 2.0)]
    q = _req("/api/tts/queue/status")

    zero = sim.zero_at

    # ------------------------------------------------------------------ #
    print("\n== 逐句（可听滞后 = 出声 − 源话开口）==")
    print("%-4s %-26s %-7s %-8s %-8s %-7s %-7s %-7s %-7s" % (
        "seq", "译文", "源秒", "出声", "可听滞后", "合成首窗", "放行等", "gap", "starv"))
    for t in rows:
        src_sec = float(t.get("src_start_sec") or -1.0)
        t_play = float(t.get("t_play") or 0.0)
        t_submit = float(t.get("t_submit") or 0.0)
        t_tr = None
        # 译文译完的时刻（引擎侧）—— 找 src_start 对得上的一句
        for x in iseg:
            if abs(float(x.get("src_start") or -1) - src_sec) < 0.2:
                t_tr = float(x.get("t_tr") or 0.0)
                break
        a_lag = t_play - (zero + src_sec) if src_sec >= 0 else float("nan")
        rel = t_play - zero
        print("%-4s %-26s %-7s %-8s %-8s %-7s %-7s %-7s %-7s" % (
            t.get("seq"),
            (t.get("text") or "")[:13],
            ("%.1f" % src_sec) if src_sec >= 0 else "-",
            ("%.2f" % rel) if t_play else "-",
            ("%.2f" % a_lag) if src_sec >= 0 else "-",
            t.get("synth_lat"),
            ("%.2f" % (t_submit - t_tr)) if t_tr else "-",
            t.get("gap"), t.get("starved")))

    # ------------------------------------------------------------------ #
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
    lags = []
    for t in rows:
        s = float(t.get("src_start_sec") or -1.0)
        p = float(t.get("t_play") or 0.0)
        if s >= 0 and p:
            lags.append(p - (zero + s))
    mr = [float(t.get("m_rate") or 0.0) for t in rows if t.get("m_rate")]
    orr = [float(t.get("o_rate") or 0.0) for t in rows if t.get("o_rate")]
    gaps = [float(t.get("gap") or 0.0) for t in rows]
    starv = [float(t.get("starved") or 0.0) for t in rows]
    synth = [float(t.get("synth_lat") or 0.0) for t in rows if t.get("synth_lat")]

    print("\n== 汇总 ==")
    print("   句数              : %d（合并后送进 TTS 的段数）" % len(rows))
    print("   源话音频合计      : %.2f s" % src_audio)
    print("   译文音频合计      : %.2f s" % tr_audio)
    k = (tr_audio / src_audio) if src_audio > 0.5 else 0.0
    if k:
        print("   **长度比 k**      : %.3f（译文比源话长 %.1f%%）" % (k, (k - 1) * 100))
    print("   播报总时长        : %.2f s（第一句出声 → 最后一句播完）" % span)
    print("   **可听滞后**      : 首 %.2f s → 末 %.2f s（平均 %.2f s）"
          % (lags[0], lags[-1], sum(lags) / len(lags)) if lags else "   （无）")
    print("   合成首窗（中位）  : %.2f s" % (
        sorted(synth)[len(synth) // 2] if synth else 0.0))
    print("   边际产能 m_rate   : %s" % " / ".join("%.3f" % x for x in mr))
    print("   整体产能 o_rate   : %s" % " / ".join("%.3f" % x for x in orr))
    print("   本机 TTS 实例     : %s" % q.get("tts_instances"))
    print("   变速 tempo        : %s（need_tempo %s）" % (q.get("tempo"),
                                                          q.get("need_tempo")))
    print("   **句间间隔 gap**  : max %.3f s / 合计 %.3f s / >0.1 s 的句数 %d"
          % (max(gaps) if gaps else 0, sum(gaps),
             sum(1 for g in gaps if g > 0.1)))
    print("   **句内饿死**      : max %.2f s / 合计 %.2f s"
          % (max(starv) if starv else 0, sum(starv)))
    print("   引擎 lag_sec      : %s（另一个口径）" % ist.get("lag_sec"))
    print("   引擎 playhead     : moved=%s stalled=%s released=%s"
          % (ist.get("playhead_moved"), ist.get("playhead_stalled"),
             ist.get("stalled_released")))

    # ---- 滞后增长曲线 ---- #
    if len(lags) >= 2:
        print("\n== 滞后增长（可听滞后 vs 源话位置）==")
        for t, lag in zip(rows, lags):
            s = float(t.get("src_start_sec") or -1.0)
            print("   源 %6.1f s  →  滞后 %6.2f s" % (s, lag))
        growth = (lags[-1] - lags[0]) if len(lags) >= 2 else 0.0
        span_src = (float(rows[-1].get("src_start_sec") or 0.0)
                    - float(rows[0].get("src_start_sec") or 0.0))
        if span_src > 1.0:
            print("   ⇒ 每秒源话增长 %.3f s（媒体 %.1f s 内长了 %.2f s）"
                  % (growth / span_src, span_src, growth))

    sim.stop()

    if args.json:
        out = {
            "gate": args.gate, "src": src, "src_dur": dur,
            "sentences": len(rows), "src_audio": round(src_audio, 2),
            "tr_audio": round(tr_audio, 2), "k": round(k, 3),
            "span": round(span, 2), "lags": [round(x, 2) for x in lags],
            "synth_lat": synth, "gaps": gaps, "starved": starv,
            "m_rate": mr, "o_rate": orr,
            "tempo": q.get("tempo"), "tts_instances": q.get("tts_instances"),
            "rows": rows,
        }
        with open(args.json, "w") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        print("\n   JSON 已写: %s" % args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
