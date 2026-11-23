#!/usr/bin/env python3
"""M28 —— **TTS 音频时长 / 合成耗时** 对「译文写法」的敏感度。

背景（实测 2026-11-23，单卡 npu1）：

* TTS 模型是 **talker 自回归** ⇒ 产能 ≈ **1.0× 实时**（物理上限）。
* EN→ZH 译文念出来比源话长 **k ≈ 1.2** ⇒ 产能**天生不够** ⇒
  「译文出声时刻」比「源话开口时刻」越落越远 ⇒ **可听滞后**单调增长。
* ⇒ **变速（sox tempo）救不了**：变速只加快*播放*，不加快*合成* ⇒
  播放还是被合成卡住 ⇒ 滞后一分不减。
* ⇒ **唯一能动的是「要念多少字」**（内容长度）与「念多快」（模型语速）。

本探针量三件事（全部用 **volume=0** 静默提交，不出声）：

===========================  ==================================================
量                          口径
===========================  ==================================================
``audio_sec``               **实际合成出的音频秒数**（队列 timing 的真值）
``m_rate`` / ``o_rate``     边际速率 / 整体速率（产能）
``chars``                   译文字符数（不含标点）
===========================  ==================================================

对比组：

1. **带标点 vs 不带标点** —— 标点会让 TTS 停顿 ⇒ 直接增加音频时长；
2. **长句 vs 简洁句** —— 「同声传译」压缩内容能降多少。

用法::

    .venv/bin/python -u tools/probes/interp/m28_tts_len.py
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

#: (标签, 文本) —— 每组只差一个变量。
CASES = [
    ("原文·带标点", "嗯嗯。哦对了，当然了！"),
    ("原文·去标点", "嗯嗯哦对了当然了"),
    ("长句·带标点", "当他开始给我听的时候，他甚至还没有那么大呢。"),
    ("长句·去标点", "当他开始给我听的时候他甚至还没有那么大呢"),
    ("简洁句·带标点", "他给我听的时候，甚至还没那么大。"),
]


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
    return json.loads(raw.decode()) if raw else {}


def _baseline_seq() -> int:
    st = _req("/api/tts/queue/status")
    seqs = [t.get("seq") for t in (st.get("timings") or [])
            if isinstance(t.get("seq"), int)]
    return max(seqs) if seqs else 0


def _chars(text: str) -> int:
    """不算标点的字数（标点会让 TTS 停顿 ⇒ 单独看）。"""
    out = 0
    for ch in text:
        if ch in "。！？!?，,、；;：:…—（）()「」『』\"'":
            continue
        out += 1
    return out


def main() -> int:
    base = _baseline_seq()
    print("== 基线 ==  队列 max seq = %d" % base)

    print("\n%-14s %-34s %5s %9s %9s %8s %8s" % (
        "组", "文本", "字数", "音频秒", "合成耗时", "边际r", "整体r"))
    results = []
    for label, text in CASES:
        r = _req("/api/tts/queue/submit",
                 {"text": text, "language": "chinese", "volume": 0.0})
        seq = r.get("seq")
        deadline = time.time() + 120
        tm = None
        while time.time() < deadline:
            st = _req("/api/tts/queue/status")
            for t in (st.get("timings") or []):
                if t.get("seq") == seq:
                    tm = t
                    break
            if tm is not None:
                break
            time.sleep(0.3)
        if tm is None:
            print("%-14s %-34s  ⚠️ 没等到 timing" % (label, text))
            continue
        audio = float(tm.get("audio_sec") or 0.0)
        # 合成耗时 = 整体速率的倒数 × 音频秒（队列的 o_rate 定义）
        o_rate = float(tm.get("o_rate") or 0.0)
        m_rate = float(tm.get("m_rate") or 0.0)
        synth = (audio / o_rate) if o_rate > 0 else 0.0
        print("%-14s %-34s %5d %9.2f %9.2f %8.3f %8.3f" % (
            label, text[:32], _chars(text), audio, synth, m_rate, o_rate))
        results.append({
            "label": label, "text": text, "chars": _chars(text),
            "audio_sec": audio, "synth_sec": round(synth, 2),
            "m_rate": m_rate, "o_rate": o_rate, "seq": seq,
        })

    print("\n== 汇总 ==")
    if results:
        base_case = results[0]
        for x in results:
            d = x["audio_sec"] - base_case["audio_sec"]
            print("%-14s 音频 %6.2f s（相对首组 %+6.2f s） 字数 %3d"
                  " ⇒ %.3f s/字" % (
                      x["label"], x["audio_sec"], d, x["chars"],
                      x["audio_sec"] / max(x["chars"], 1)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
