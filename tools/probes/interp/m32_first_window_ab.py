#!/usr/bin/env python3
"""M32 —— **首窗帧数**的 A/B 实测（25 帧 vs 12 帧）。

背景（实测 2026-11-25，用户报「播放卡顿 + 切音」）：

* 每个 TTS 作业开头都要等**首窗**（首窗帧数 × 1920 / 24000 秒）。
* 首窗 25 帧 = **2.0 s** ⇒ 每个作业开头固定死 2 s。
* 首窗 12 帧 = **0.96 s** ⇒ 开头快 1 s。

⚠️ **但 12 帧被试过一次并被回退**（commit ``cd62ff4``）：
   「TTS 按窗口吐 PCM，窗口之间的间隔 ~1.9 s ⇒ 0.96 s 的首窗会让管道
   在**句子中间**空掉 ⇒ 音频断在半句」⇒ 本探针就是要把这条**再量一遍**
   （用队列自带的 ``starved`` 指标 = 管道空着等 PCM 的累计时长）。

本探针量三个口径（**每个作业**）：

====================  ===================================================
口径                  含义
====================  ===================================================
``synth_lat``        **首声延迟**（开始合成 → 出声）—— 首窗有多大
``starved``          **句中饿死**（管道空着等 PCM）—— 句子中间断音
``stock``            **开口时管道里的存货**（秒）—— 有没有余量
====================  ===================================================

用法::

    .venv/bin/python -u tools/probes/interp/m32_first_window_ab.py
"""
from __future__ import annotations

import json
import ssl
import time
import urllib.request

BASE = "https://127.0.0.1:8000"

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

#: 一整段译文（文件源那一段的合并版）—— 重复 3 遍 ⇒ ~45 s 音频 ⇒
#: 二十多个窗口 ⇒ 「句中饿死」会累积到能量出来。
TEXT = ("嗯嗯哦对了当然了 当我开始听他的声音时他甚至还没有变得那么大呢 "
        "但在他的独奏作品中他的表现并不出色不过当他开始为其他人创作音乐时"
        "他的表现却非常出色 ") * 3

#: 要对比的首窗帧数。
CASES = (25, 12)


def _req(path: str, payload=None, method=None, timeout=180):
    url = BASE + path
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method or
                                 ("POST" if data is not None else "GET"))
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def _drain(timeout=240.0):
    """等 TTS 队列排空（正在播的那句放完）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        q = _req("/api/tts/queue/status")
        if (int(q.get("depth") or 0) == 0
                and int(q.get("inflight") or 0) == 0
                and not q.get("playing")):
            return True
        time.sleep(0.5)
    return False


def run_case(frames: int) -> dict:
    print("\n===== 首窗 %d 帧（≈ %.2f s）=====" % (frames, frames * 1920 / 24000.0))
    _drain()
    _req("/api/tts/queue/clear", method="POST")
    time.sleep(1.0)
    before = int(_req("/api/tts/queue/status").get("done") or 0)
    res = _req("/api/tts/queue/submit", {
        "text": TEXT,
        "language": "chinese",
        "volume": 0.01,          # 静默测（时间口径与音量无关）
        "tempo": 1.0,            # 两组同速 ⇒ A/B 公平
        "first_window_frames": frames,
    })
    print("  提交 seq=%s est=%.1fs" % (res.get("seq"),
                                       float(res.get("est_audio_sec") or 0)))
    _drain()
    st = _req("/api/tts/queue/status")
    rows = [t for t in (st.get("timings") or [])
            if int(t.get("seq") or 0) > before]
    for t in rows:
        print("  seq=%-3s synth_lat=%5.2f  starved=%5.2f  gap=%5.2f  "
              "stock=%4.2f  audio=%6.2f  m_rate=%.2f"
              % (t.get("seq"), t.get("synth_lat", 0), t.get("starved", 0),
                 t.get("gap", 0), t.get("stock", 0), t.get("audio_sec", 0),
                 t.get("m_rate", 0)))
    if not rows:
        print("  ⚠️ 没有抓到本次的作业")
        return {"frames": frames, "jobs": 0}
    tot_audio = sum(float(t.get("audio_sec") or 0) for t in rows)
    out = {
        "frames": frames,
        "jobs": len(rows),
        "audio_sec": round(tot_audio, 2),
        "synth_lat": round(sum(float(t.get("synth_lat") or 0)
                                for t in rows) / len(rows), 3),
        "starved_sum": round(sum(float(t.get("starved") or 0) for t in rows), 2),
        "starved_max": round(max(float(t.get("starved") or 0) for t in rows), 2),
        "gap_sum": round(sum(float(t.get("gap") or 0) for t in rows), 2),
        "stock_max": round(max(float(t.get("stock") or 0) for t in rows), 2),
        "m_rate": round(sum(float(t.get("m_rate") or 0) for t in rows) / len(rows), 3),
    }
    print("  → %s" % json.dumps(out, ensure_ascii=False))
    return out


def main() -> int:
    print("== M32：首窗帧数 A/B（25 帧 vs 12 帧）==")
    results = [run_case(f) for f in CASES]
    print("\n===== 结论 =====")
    print("%-8s %-6s %-12s %-12s %-10s %-10s"
          % ("首窗", "作业", "首声延迟", "句中饿死(和)", "句中饿死(最大)", "开口存货"))
    for r in results:
        print("%-8s %-6s %-12s %-12s %-10s %-10s"
              % (str(r.get("frames")) + " 帧", r.get("jobs"),
                 r.get("synth_lat"), r.get("starved_sum"),
                 r.get("starved_max"), r.get("stock_max")))
    a, b = (results + [None, None])[:2]
    if a and b and a.get("jobs") and b.get("jobs"):
        print("\n  首声延迟：%.2f s → %.2f s（%+.2f s）"
              % (a["synth_lat"], b["synth_lat"], b["synth_lat"] - a["synth_lat"]))
        print("  句中饿死：%.2f s → %.2f s（%+.2f s）"
              % (a["starved_sum"], b["starved_sum"],
                 b["starved_sum"] - a["starved_sum"]))
        verdict = ("✅ 12 帧更划算（首声更快且没有更多句中饿死）"
                   if b["starved_sum"] <= a["starved_sum"] + 0.05
                   else "❌ 12 帧更差（句中饿死明显增加 ⇒ 句子中间会断）")
        print("  判定：%s" % verdict)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
