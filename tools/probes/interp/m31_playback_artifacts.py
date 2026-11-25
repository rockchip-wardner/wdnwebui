#!/usr/bin/env python3
"""M31 —— **播放伪影归因**：卡顿（断音）与切音（咔哒）在哪一段。

要回答的两个问题
================
1. **卡顿感**：播放中途的「断音」到底有多长、在哪？
   ⇒ 查两处：① **模型输出里**有没有静音段（模型自己停了）；
     ② **供给节奏**：TTS 每次只交 **2.0 秒**（25 帧）一块
     （``kChunkSize=25`` ⇒ ``25 × 0.08 s``）⇒ 合成速率 < 1.0× 时
     **管道每 2 秒见底一次** ⇒ 每 2 秒断 0.2~0.4 s。
2. **切音感**：咔哒声（相邻样本跳变）是**模型自己产生的**，
   还是**播放侧切出来的**？
   ⇒ 判据：咔哒的位置是否落在 **2 秒的整数倍**（= 解码窗口接缝）上。

⚠️ 走 ``play_to=browser`` ⇒ **不碰板载喇叭** ⇒ 板上是安静的；
   量的是**模型输出**本身（= 送给 aplay 的那段 PCM 的来源）。

用法::

    .venv/bin/python tools/probes/interp/m31_playback_artifacts.py
"""
from __future__ import annotations

import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import wave

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import numpy as np  # noqa: E402

BASE = "https://127.0.0.1:8000"
OUT = "/tmp/m31_tts_out.wav"

#: 一段**长**中文（≈ 30 s 语音）⇒ 跨多个 2 秒窗口。
TEXT = (
    "当他开始给我听的时候，他甚至还没有变得那么大呢。"
    "不过说实话，在他的独奏作品里，他的表现并不出色。"
    "但当他开始为其他人写作的时候，他的表现就非常出色了。"
    "我第一次听到他的音乐的时候还以为是别人的录音。"
    "后来才知道那是他自己录的，而且是在很小的一个房间里完成的。"
)

#: TTS 每交给播放器一块音频的秒数（``kChunkSize=25`` 帧 × 0.08 s）。
CHUNK_SEC = 2.0


def _ctx():
    c = ssl.create_default_context()
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    return c


def api(method: str, path: str, payload=None, timeout: int = 240):
    data = None
    hdrs = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=hdrs,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ctx()) as r:
            return json.loads(r.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode("utf-8", "replace") or "{}")


def fetch(path: str) -> bytes:
    req = urllib.request.Request(BASE + path, method="GET")
    with urllib.request.urlopen(req, timeout=240, context=_ctx()) as r:
        return r.read()


def read_wav(data: bytes):
    """读 WAV（**支持 IEEE float / format 3** —— 服务落的是 float32）。"""
    import io
    import struct
    if data[:4] == b"RIFF":
        pos = 12
        fmt = None
        pcm = b""
        while pos + 8 <= len(data):
            cid = data[pos:pos + 4]
            (csize,) = struct.unpack("<I", data[pos + 4:pos + 8])
            body = data[pos + 8:pos + 8 + csize]
            if cid == b"fmt ":
                fmt = struct.unpack("<HHIIHH", body[:16])
            elif cid == b"data":
                pcm = body
            pos += 8 + csize + (csize & 1)
        tag, ch, sr, _br, _ba, bits = fmt
        if tag == 3 and bits == 32:
            x = np.frombuffer(pcm, dtype="<f4").astype(np.float32)
        elif tag == 1 and bits == 16:
            x = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        else:
            raise RuntimeError("没见过的 WAV 格式: tag=%s bits=%s" % (tag, bits))
        if ch > 1:
            x = x.reshape(-1, ch)[:, 0]
        return x, sr
    with wave.open(io.BytesIO(data), "rb") as w:
        sr = w.getframerate()
        n = w.getnframes()
        ch = w.getnchannels()
        width = w.getsampwidth()
        raw = w.readframes(n)
    dt = {1: np.uint8, 2: np.int16, 4: np.int32}.get(width, np.float32)
    x = np.frombuffer(raw, dtype=dt).astype(np.float32)
    if width == 1:
        x = (x - 128.0) / 128.0
    elif width == 2:
        x = x / 32768.0
    elif width == 4:
        x = x / 2147483648.0
    if ch > 1:
        x = x.reshape(-1, ch)[:, 0]
    return x, sr


def cluster(idx, max_gap=8):
    """把相邻的下标聚成事件（返回 (起点, 峰值) 列表）。"""
    out = []
    for i in idx:
        if out and i - out[-1][1] <= max_gap:
            if i > out[-1][0]:
                out[-1] = (out[-1][0], int(i))
            continue
        out.append((int(i), int(i)))
    return out


def main() -> int:
    print("=== 合成（play_to=browser ⇒ 板载喇叭不响）===")
    t0 = time.time()
    st = api("POST", "/api/tts/start", {
        "text": TEXT,
        "language": "chinese",
        "play_to": "browser",
        "volume": 0.01,
        # ⚠️ **不传 output** ⇒ 用服务自己的默认落盘路径
        # （`/api/tts/output` 会校验路径必须在允许的根目录下 ⇒
        #   自己指定 /tmp 会被 400 拒掉）。
    })
    out = (st.get("request") or {}).get("output") or ""
    first_frame = None
    deadline = time.time() + 300
    while time.time() < deadline:
        snap = api("GET", "/api/tts/")
        if first_frame is None and int(snap.get("frames") or 0) > 0:
            first_frame = time.time() - t0
        if not (snap.get("running") or snap.get("loading")):
            break
        time.sleep(0.05)
    total = time.time() - t0
    frames = int(snap.get("frames") or 0)
    audio_sec = float(snap.get("audio_sec") or 0.0)
    print(f"  首帧 {first_frame:.2f} s · 总耗时 {total:.2f} s · "
          f"帧 {frames} · 音频 {audio_sec:.2f} s")
    if audio_sec > 0:
        print(f"  **合成速率 = {audio_sec / total:.3f}×**"
              f"（< 1 ⇒ 管道会周期性见底 ⇒ 卡顿）")
    print(f"  产出块数 ≈ {frames / 25:.1f} 块 × {CHUNK_SEC} s "
          f"⇒ 供给粒度 {CHUNK_SEC} s")

    wav = fetch("/api/tts/output?path=" + urllib.parse.quote(out))
    x, sr = read_wav(wav)
    print(f"\n=== 波形分析（{len(x)} 采样 @ {sr} Hz = {len(x)/sr:.2f} s）===")

    # ---------------- ① 咔哒声（切音）---------------- #
    d = np.abs(np.diff(x))
    med = float(np.median(d))
    mad = float(np.median(np.abs(d - med)))
    sigma = 1.4826 * mad
    thr = med + 12.0 * sigma
    hits = np.where(d > thr)[0]
    events = cluster(hits)
    print(f"\n① 咔哒声：相邻样本跳变 > {thr:.5f}（中位 {med:.6f} + 12σ，"
          f"σ={sigma:.6f}）⇒ **{len(events)} 个事件**")
    if events:
        near_seam = 0
        for (a, b) in events[:25]:
            t = a / sr
            off = t % CHUNK_SEC
            seam = min(off, CHUNK_SEC - off)          # 离 2 s 接缝多近
            tag = "★接缝" if seam < 0.02 else ""
            if seam < 0.02:
                near_seam += 1
            print(f"   t={t:7.3f} s  跳变={float(d[a]):.4f}  "
                  f"离{CHUNK_SEC}s接缝 {off:+.3f} s {tag}")
        near = sum(1 for (a, b) in events
                   if min((a / sr) % CHUNK_SEC,
                          CHUNK_SEC - (a / sr) % CHUNK_SEC) < 0.02)
        print(f"   ⇒ 落在 **{CHUNK_SEC} s 窗口接缝**上的：{near} / {len(events)}")
        print("   ⇒ 判读：接缝上的咔哒 = **SDK 解码窗口接缝**；"
              "散布在别处的 = 模型/采样噪声")

    # ---------------- ② 断音（卡顿）---------------- #
    frame = max(1, int(0.02 * sr))
    n = len(x) // frame
    rms = np.sqrt((x[:n * frame].reshape(n, frame) ** 2).mean(axis=1))
    floor = float(np.percentile(rms, 5))
    thr2 = max(floor * 3.0, float(rms.max()) * 0.02)
    quiet = rms < thr2
    gaps = []
    i = 0
    while i < n:
        if quiet[i]:
            j = i
            while j < n and quiet[j]:
                j += 1
            gaps.append((i * frame / sr, (j - i) * frame / sr))
            i = j
        else:
            i += 1
    long_gaps = [g for g in gaps if g[1] >= 0.2]
    print(f"\n② 断音：噪声底 {floor:.6f} RMS · 门限 {thr2:.6f}")
    print(f"   静音段 ≥ 0.2 s 的有 **{len(long_gaps)}** 段：")
    for (s, dur) in long_gaps[:15]:
        print(f"     t={s:7.2f} s  时长 {dur:.2f} s")

    # ---------------- ③ 噪声底 vs 峰值（1% 音量下的听感）---------------- #
    peak = float(np.max(np.abs(x)))
    noise = float(np.percentile(np.abs(x), 20))
    print(f"\n③ 电平：峰值 {peak:.5f}（{20*np.log10(max(peak,1e-9)):.1f} dBFS）"
          f" · 噪声底 p20 {noise:.7f}"
          f"（{20*np.log10(max(noise,1e-9)):.1f} dBFS）"
          f" ⇒ **SNR ≈ {20*np.log10(max(peak,1e-9)/max(noise,1e-9)):.1f} dB**")
    print(f"   ⇒ 1% 音量后再乘 0.01 ⇒ 峰值 ≈ "
          f"{20*np.log10(max(peak*0.01,1e-9)):.1f} dBFS"
          f"（板载 codec 自身的噪声底就在这个量级附近 ⇒ **听得见的杂音**）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
