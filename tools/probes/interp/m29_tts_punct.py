#!/usr/bin/env python3
"""M29b —— **标点停顿到底值多少秒？**（长度比 k 能不能压下来）

用户报「TTS 播放滞后非常多」（2026-11-23）。M29 的实测：

    长度比 k = 1.104（译文念出来比源话长 10.4%）
    ⇒ 产能 ~1.0 ⇒ **可听滞后每 1 秒源话增长 0.1 秒**（5 分钟素材 ⇒ +30 秒）

⇒ 要让滞后**不再增长**，必须把 k 压到 ~1.0 以下。k 里除了「内容本身更长」，
还有两块**纯开销**：

    ① **标点停顿**（"，" "。" "！" …）—— 模型会为每个标点留一段静音
    ② **首尾静音**（每段开头/结尾各 ~0.1~0.65 s）

这个探针对**同一段文本**做 A/B：带标点 vs 去标点 ⇒ 直接量出差值。

做法：调 TTS 卡的 ``/api/tts/start``（``play_to=browser`` ⇒ **不碰声卡**，
``volume=0`` ⇒ 完全不出声），拿回 WAV 量总时长 + 首尾静音。

用法::

    .venv/bin/python -u tools/probes/interp/m29_tts_punct.py
"""
from __future__ import annotations

import json
import re
import ssl
import time
import urllib.request

BASE = "https://127.0.0.1:8000"

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

#: M29 实测里真正送进 TTS 的三段译文（合并后）。
TEXTS = [
    "嗯嗯。哦对了，当然了！ 当他开始给我听的时候，他甚至还没有那么大呢。",
    "在他的独奏作品中，表现并不出色，但他的表现还是不错的。",
    "嗯，当他开始为其他人写作的时候。",
]

#: 中文标点（模型会为它们留停顿）。
PUNCT = "，。！？；：、…—～·「」『』（）《》〈〉【】“”‘’"


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


def _synth(text: str) -> str:
    """合成一段，返回 WAV 路径（``play_to=browser`` ⇒ 不出声）。"""
    r = _req("/api/tts/start", {"text": text, "language": "chinese",
                                "play_to": "browser", "volume": 0.0})
    out = r.get("output") or ""
    deadline = time.time() + 180
    while not out and time.time() < deadline:
        time.sleep(0.5)
        st = _req("/api/tts/")
        out = st.get("output") or ""
        if st.get("state") not in ("running", "loading"):
            break
    return out


def _wav_info(path: str):
    """(总时长, 首静音, 尾静音, 内部静音合计) —— 自己解 RIFF（可能是 float32）。"""
    import array

    with open(path, "rb") as f:
        blob = f.read()
    if blob[:4] != b"RIFF":
        raise ValueError("不是 WAV")
    pos = 12
    fmt = None
    data = None
    while pos + 8 <= len(blob):
        cid = blob[pos:pos + 4]
        size = int.from_bytes(blob[pos + 4:pos + 8], "little")
        body = blob[pos + 8:pos + 8 + size]
        if cid == b"fmt ":
            fmt = (int.from_bytes(body[0:2], "little"),
                   int.from_bytes(body[2:4], "little"),
                   int.from_bytes(body[4:8], "little"),
                   int.from_bytes(body[14:16], "little"))
        elif cid == b"data":
            data = body
        pos += 8 + size + (size & 1)
    tag, ch, sr, bits = fmt
    if tag == 3 and bits == 32:
        arr = array.array("f")
        arr.frombytes(data[:len(data) // 4 * 4])
        samples = list(arr)
    else:
        arr = array.array("h")
        arr.frombytes(data[:len(data) // 2 * 2])
        samples = [x / 32768.0 for x in arr]
    if ch > 1:
        samples = samples[::ch]
    win = max(1, int(sr * 0.01))
    rms = []
    for i in range(0, len(samples) - win + 1, win):
        acc = 0.0
        for s in samples[i:i + win]:
            acc += s * s
        rms.append((i / float(sr), (acc / win) ** 0.5))
    thr = 0.004
    loud = [t for t, r in rms if r > thr]
    if not loud:
        return 0.0, 0.0, 0.0, 0.0
    head = loud[0]
    tail = (rms[-1][0] - loud[-1]) + 0.01
    # 内部静音（首尾之间）
    inner = 0.0
    prev_end = None
    for t, r in rms:
        if r <= thr:
            if prev_end is None:
                prev_end = t
        else:
            if prev_end is not None:
                inner += (t - prev_end)
                prev_end = None
    total = len(samples) / float(sr)
    return total, head, tail, inner


#: 变体：``strip`` = 全删；``space`` = 标点换成空格（保留一点断句韵律）。
VARIANT = "strip"


def _strip_punct(text: str) -> str:
    if VARIANT == "space":
        out = "".join((" " if ch in PUNCT else ch) for ch in text)
    else:
        out = "".join(ch for ch in text if ch not in PUNCT)
    return re.sub(r"\s+", " ", out).strip()


def main() -> int:
    global VARIANT
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", default="strip", choices=["strip", "space"])
    VARIANT = ap.parse_args().variant
    print("== A/B：带标点 vs 变体 %s（play_to=browser ⇒ 完全不出声）==\n" % VARIANT)
    rows = []
    tot_a = tot_b = 0.0
    for text in TEXTS:
        plain = _strip_punct(text)
        wa = _synth(text)
        time.sleep(0.3)
        wb = _synth(plain)
        if not wa or not wb:
            print("⚠️ 没拿到 WAV: %r %r" % (wa, wb))
            continue
        ta, ha, tla, ia = _wav_info(wa)
        tb, hb, tlb, ib = _wav_info(wb)
        tot_a += ta
        tot_b += tb
        rows.append({"text": text, "plain": plain,
                     "dur_a": ta, "dur_b": tb, "save": ta - tb,
                     "head_a": ha, "tail_a": tla, "inner_a": ia,
                     "head_b": hb, "tail_b": tlb, "inner_b": ib})
        print("== %s" % text[:34])
        print("   带标点: %.2f s（首静音 %.2f + 尾 %.2f + 内部静音 %.2f）"
              % (ta, ha, tla, ia))
        print("   去标点: %.2f s（首静音 %.2f + 尾 %.2f + 内部静音 %.2f）"
              % (tb, hb, tlb, ib))
        print("   ⇒ **省 %.2f s（%.1f%%）**" % (ta - tb,
                                                (ta - tb) / ta * 100 if ta else 0))
        print("   文本: %s" % plain)
        print()

    if tot_a > 0:
        print("== 合计 ==")
        print("   带标点 %.2f s → 去标点 %.2f s ⇒ 省 %.2f s（%.1f%%）"
              % (tot_a, tot_b, tot_a - tot_b, (tot_a - tot_b) / tot_a * 100))
        print("   ⇒ 对长度比 k 的影响：M29 实测 k = 1.104（源话 14.17 s）")
        print("      去标点后 k ≈ %.3f"
              % ((tot_b / 14.17) if rows else 0.0))
        print("      再把首尾静音裁掉 ⇒ k ≈ %.3f"
              % (((tot_b - sum(r["head_b"] + r["tail_b"] for r in rows)) / 14.17)
                 if rows else 0.0))
    with open("/tmp/m29_punct.json", "w") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
