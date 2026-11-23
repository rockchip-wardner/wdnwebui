#!/usr/bin/env python3
"""M28b —— **TTS 每句的固定开销是什么？**（首尾静音实测）

M28 的拟合：``audio_sec ≈ 2.25 + 0.119 × 字数`` ⇒ 每句有 **~2.25 s 固定开销**。
如果那是**首尾静音**，裁掉它 ⇒ 4 句的音频从 17.0 s 降到 ~9 s ⇒
长度比 k 从 1.2 掉到 ~0.65 ⇒ **可听滞后不再增长**（单卡下唯一能动 k 的
物理手段，且**不动内容**）。

做法：调 TTS 卡的 ``/api/tts/start``（``play_to=browser`` ⇒ 不碰声卡），
拿回 WAV ⇒ 逐 10 ms 算 RMS ⇒ 量首尾静音。

用法::

    .venv/bin/python -u tools/probes/interp/m28_tts_silence.py
"""
from __future__ import annotations

import json
import ssl
import struct
import time
import urllib.request
import wave

BASE = "https://127.0.0.1:8000"

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

TEXTS = [
    "嗯嗯。哦对了，当然了！",
    "当他开始给我听的时候，他甚至还没有那么大呢。",
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


def _rms_windows(path: str, win_ms: int = 10):
    """逐窗口 RMS（相对满刻度）。

    ⚠️ SDK 写的 WAV 是 **format 3（IEEE float32）** ⇒ ``wave`` 模块不认
    ⇒ 自己解 RIFF 块。
    """
    import array

    with open(path, "rb") as f:
        blob = f.read()
    if blob[:4] != b"RIFF":
        raise ValueError("不是 WAV: %s" % path)
    pos = 12
    fmt = None
    data = None
    while pos + 8 <= len(blob):
        cid = blob[pos:pos + 4]
        size = int.from_bytes(blob[pos + 4:pos + 8], "little")
        body = blob[pos + 8:pos + 8 + size]
        if cid == b"fmt ":
            fmt = (int.from_bytes(body[0:2], "little"),   # tag
                   int.from_bytes(body[2:4], "little"),   # channels
                   int.from_bytes(body[4:8], "little"),   # rate
                   int.from_bytes(body[14:16], "little"))  # bits
        elif cid == b"data":
            data = body
        pos += 8 + size + (size & 1)
    if fmt is None or data is None:
        raise ValueError("WAV 缺 fmt/data: %s" % path)
    tag, ch, sr, bits = fmt
    if tag == 3 and bits == 32:
        arr = array.array("f")
        arr.frombytes(data[:len(data) // 4 * 4])
        samples = list(arr)
        peak = 1.0
    elif tag == 1 and bits == 16:
        arr = array.array("h")
        arr.frombytes(data[:len(data) // 2 * 2])
        samples = [s / 32768.0 for s in arr]
        peak = 1.0
    else:
        raise ValueError("不认识的 WAV 格式: tag=%s bits=%s" % (tag, bits))
    if ch > 1:
        samples = samples[::ch]
    win = max(1, int(sr * win_ms / 1000))
    out = []
    for i in range(0, len(samples) - win + 1, win):
        acc = 0.0
        for s in samples[i:i + win]:
            acc += s * s
        out.append((i / float(sr), (acc / win) ** 0.5 / peak))
    return out, sr, len(samples) / float(sr)


def _silence(win, thr=0.004):
    """首尾静音秒数（阈值 thr，相对满刻度 RMS）。"""
    if not win:
        return 0.0, 0.0
    loud = [(t, r) for t, r in win if r > thr]
    if not loud:
        return 0.0, 0.0
    first, last = loud[0], loud[-1]
    # 首静音 = 第一个有声窗口的时刻；尾静音 = 末尾时刻 − 最后有声窗口
    head = first[0]
    tail = (win[-1][0] - last[0]) + (win[-1][0] - win[-2][0] if len(win) > 1 else 0.01)
    return head, tail


def main() -> int:
    for text in TEXTS:
        r = _req("/api/tts/start", {"text": text, "language": "chinese",
                                    "play_to": "browser", "volume": 0.0})
        # ⚠️ ``/api/tts/start`` 是**异步**的 ⇒ 返回时 output 还是空的
        # ⇒ 得轮询到 WAV 落盘。
        out = r.get("output") or ""
        deadline = time.time() + 180
        while not out and time.time() < deadline:
            time.sleep(0.5)
            st = _req("/api/tts/")
            out = st.get("output") or ""
            if st.get("state") not in ("running", "loading"):
                break
        if not out:
            print("⚠️ 没拿到 WAV: %r" % (r,))
            continue
        win, sr, dur = _rms_windows(out)
        head, tail = _silence(win)
        print("\n== %s ==" % text)
        print("   WAV %s" % out)
        print("   总时长 %.2f s（%d Hz）" % (dur, sr))
        print("   首静音 %.2f s / 尾静音 %.2f s ⇒ 净语音 %.2f s"
              % (head, tail, max(0.0, dur - head - tail)))
        # 每 0.5 s 打个 RMS 轮廓
        line = []
        for i in range(0, len(win), 50):
            seg = win[i:i + 50]
            if not seg:
                continue
            m = max(r for _, r in seg)
            line.append("%4.1f:%s" % (seg[0][0], "#" * int(min(m, 0.2) * 200)))
        print("   轮廓 " + " ".join(line))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
