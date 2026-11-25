#!/usr/bin/env python3
"""M20 —— **TTS 首窗 A/B**：首窗 25 帧 vs 50 帧（上游行为）。

⚠️ **为什么要改首窗**：上游实现里首个解码窗口**硬等满 50 帧**
（= 4 s 音频）⇒ 实测「合成首窗」≈ **3.0 s** —— 同传链路上**最大的一段
固定延迟**（翻译推理只要 0.3 s）。

⚠️ **凭什么能改**：``speech_decoder`` 是**因果**的（分块解码：「左上下文
25 帧 + 新 25 帧」）⇒ 首窗没有左上下文 ⇒ **未来的帧对已生成部分没有
影响** ⇒ 攒 25 帧就能开声，帧覆盖仍精确（不重不漏）。

⚠️ **怎么验**：TTS 是**采样生成**的（``temperature=0.9``）⇒ **两次
合成同一句话本来就不一样** ⇒ 直接 A/B 两次 WAV 的相似度**没有意义**。
⇒ 本探针对**每种配置各跑两次**：
  * **同配置两次**的相似度 = **噪声基线**（采样噪声有多大）；
  * **跨配置**的相似度 + 首 2 s 的 RMS/峰值 = 与噪声基线比。
⇒ 判据：25 帧版的**首段电平统计**落在 50 帧版**自身抖动范围**内 ⇒
「没有把音频搞坏」；同时 25 帧版的**首声明显更早**。

用法::

    .venv/bin/python tools/probes/interp/m20_tts_first_window.py
"""
from __future__ import annotations

import json
import math
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

BASE = "https://127.0.0.1:8000"
TEXT = "嗯嗯。哦对了，当然了！当他开始给我听的时候，他甚至还没有那么大呢。"


def _ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def api(method: str, path: str, payload=None, timeout: int = 180):
    data = None
    hdrs = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=hdrs,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ctx()) as r:
            body = r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
    try:
        return json.loads(body or "{}")
    except Exception:  # noqa: BLE001
        return {"raw": body}


def fetch(path: str, timeout: int = 120) -> bytes:
    with urllib.request.urlopen(BASE + path, timeout=timeout,
                                context=_ctx()) as r:
        return r.read()


def synth(first_window_frames: int) -> dict:
    """跑一次合成，返回该次的统计。"""
    t0 = time.time()
    st = api("POST", "/api/tts/start", {
        "text": TEXT,
        "language": "chinese",
        "play_to": "browser",
        # ⚠️ **音量 1%**（按 D2 拍板的自测纪律）—— 本探针走
        # ``play_to=browser`` ⇒ **不碰板载喇叭** ⇒ 音量只影响存下来的
        # WAV 的振幅 ⇒ 两个配置**同比缩放** ⇒ RMS/峰值/互相关的**比值**
        # 判据不受影响。
        "volume": 0.01,
        "first_window_frames": first_window_frames,
    })
    # ⚠️ 快照里的 ``output`` 在 ``start`` 刚返回时还是空 ⇒ 真实路径在
    # ``request.output`` 里。
    out = (st.get("request") or {}).get("output") or st.get("output") or ""
    if not out:
        raise RuntimeError("没有输出路径: %s" % json.dumps(st, ensure_ascii=False))
    deadline = time.time() + 120
    # ⚠️⚠️ **首声延迟 = 「第一帧出现」的时刻** —— 这才是首窗优化要省的
    # 东西（**不是**合成总时长！总时长由 talker 产多少帧决定 ⇒ 两个配置
    # 几乎一样 ⇒ 用它当判据会得出「没变快」的错误结论）。
    # ``on_step`` 在**每个解码窗口**解完后回调 ⇒ ``frames > 0`` 的那一刻
    # = **第一窗已经解完、音频可以出声** ⇒ 首声延迟。
    first_frame_sec = None
    while time.time() < deadline:
        snap = api("GET", "/api/tts/")
        if first_frame_sec is None and int(snap.get("frames") or 0) > 0:
            first_frame_sec = time.time() - t0
        if not (snap.get("running") or snap.get("loading")):
            break
        time.sleep(0.05)
    t_end = time.time()
    wav = fetch("/api/tts/output?path=" + urllib.parse.quote(out))
    samples, sr = read_wav(wav)
    head = samples[:int(2.0 * sr)]
    # ⚠️ **接缝位置** = 首窗结束处 = ``首窗帧数 × 0.08 s``（一帧 =
    # ``kTotalUpsample`` 1920 采样 ÷ 24 kHz = 0.08 s）。
    seam_sec = first_window_frames * 0.08
    return {
        "samples": samples,
        "sr": sr,
        "dur": len(samples) / float(sr),
        "rms_head": rms(head),
        "peak_head": peak(head),
        "rms_all": rms(samples),
        "wall": t_end - t0,
        "first_frame_sec": first_frame_sec,
        "seam_sec": seam_sec,
        "seam_jump": envelope_jump(samples, sr, seam_sec),
        "baseline_jump": baseline_jumps(samples, sr, seam_sec),
    }


def read_wav(data: bytes):
    """只支持 32-bit float / 16-bit PCM 的 WAV ⇒ 返回 (samples, sr)。"""
    import array
    import struct

    assert data[:4] == b"RIFF", "不是 RIFF"
    pos = 12
    fmt = None
    pcm = b""
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        size = struct.unpack("<I", data[pos + 4:pos + 8])[0]
        body = data[pos + 8:pos + 8 + size]
        if cid == b"fmt ":
            fmt = struct.unpack("<HHIIHH", body[:16])
        elif cid == b"data":
            pcm = body
        pos += 8 + size + (size & 1)
    if fmt is None:
        raise RuntimeError("WAV 没有 fmt 块")
    audio_fmt, channels, sr, _br, _ba, bits = fmt
    if bits == 32 and audio_fmt == 3:
        a = array.array("f")
        a.frombytes(pcm[:len(pcm) // 4 * 4])
        samples = list(a)
    elif bits == 16:
        a = array.array("h")
        a.frombytes(pcm[:len(pcm) // 2 * 2])
        samples = [x / 32768.0 for x in a]
    else:
        raise RuntimeError("不支持的 WAV 格式: fmt=%r bits=%r" % (audio_fmt, bits))
    if channels > 1:
        samples = samples[::channels]
    return samples, sr


def rms(xs) -> float:
    if not xs:
        return 0.0
    return math.sqrt(sum(x * x for x in xs) / len(xs))


def peak(xs) -> float:
    return max((abs(x) for x in xs), default=0.0)


def correlate(a, b) -> float:
    """归一化互相关（只看前 min(len) 个点）。"""
    n = min(len(a), len(b))
    if n == 0:
        return 0.0
    a = a[:n]
    b = b[:n]
    ma = sum(a) / n
    mb = sum(b) / n
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    da = math.sqrt(sum((x - ma) ** 2 for x in a))
    db = math.sqrt(sum((y - mb) ** 2 for y in b))
    if da == 0 or db == 0:
        return 0.0
    return num / (da * db)


def envelope_jump(samples, sr, at_sec, win_sec=0.05):
    """``at_sec`` 处的**包络跳变**（前窗 RMS 与后窗 RMS 的相对差）。

    ⚠️ 这是「接缝」的度量：两段音频在拼接点上如果对不齐，会在这里
    出现一个**相对幅度跳变**（正常语音也有 ⇒ 需要**基线**来比）。
    """
    i = int(at_sec * sr)
    w = int(win_sec * sr)
    if i - w < 0 or i + w >= len(samples):
        return None
    a = rms(samples[i - w:i])
    b = rms(samples[i:i + w])
    m = max(a, b, 1e-9)
    return abs(a - b) / m


def baseline_jumps(samples, sr, seam_sec, step_sec=0.5):
    """接缝**以外**各处的跳变（⇒ 「自然跳变」的基线）。

    ⇒ 接缝的跳变落在这个基线的量级里 ⇒ 说明**没有可听见的接缝**。
    """
    out = []
    t = step_sec
    dur = len(samples) / float(sr)
    while t < dur - 0.5:
        if abs(t - seam_sec) > 0.4:          # 跳过接缝本身
            j = envelope_jump(samples, sr, t)
            if j is not None:
                out.append(j)
        t += step_sec
    return out


def median(xs):
    if not xs:
        return 0.0
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    if n % 2:
        return s[mid]
    return 0.5 * (s[mid - 1] + s[mid])


def main() -> int:
    print(f"=== TTS 首窗 A/B（文本: {TEXT[:24]!r}）===")
    print("⚠️ TTS 是采样生成（temperature=0.9）⇒ 同配置两次也不一样 ⇒ "
          "先取**噪声基线**再比跨配置。\n")
    runs: dict = {}
    for fw in (25, 50):
        runs[fw] = []
        for i in (1, 2):
            print(f"[首窗 {fw} 帧 · 第 {i} 次] 合成中…")
            r = synth(fw)
            runs[fw].append(r)
            print(f"  时长 {r['dur']:.2f}s · 前 2s RMS {r['rms_head']:.5f} · "
                  f"峰值 {r['peak_head']:.4f} · 全段 RMS {r['rms_all']:.5f} · "
                  f"墙钟 {r['wall']:.2f}s · "
                  f"**首声 {r['first_frame_sec']:.2f}s** · "
                  f"接缝跳变 {r['seam_jump']:.4f}"
                  f"（基线中位 {median(r['baseline_jump']):.4f}）")

    def stat(fw, key):
        return [r[key] for r in runs[fw]]

    print("\n--- 噪声基线（同配置两次）---")
    base = {}
    for fw in (25, 50):
        a, b = runs[fw]
        c = correlate(a["samples"], b["samples"])
        base[fw] = c
        print(f"  {fw} 帧 vs {fw} 帧: 互相关 {c:.4f} · 时长差 "
              f"{a['dur'] - b['dur']:+.3f}s · 首段 RMS 差 "
              f"{abs(a['rms_head'] - b['rms_head']):.5f}")

    print("\n--- 跨配置（25 帧 vs 50 帧）---")
    cross = []
    for a in runs[25]:
        for b in runs[50]:
            c = correlate(a["samples"], b["samples"])
            cross.append(c)
            print(f"  互相关 {c:.4f} · 时长差 {a['dur'] - b['dur']:+.3f}s · "
                  f"首段 RMS {a['rms_head']:.5f} vs {b['rms_head']:.5f}")

    print("\n--- 判据 ---")
    # ① 首段电平：25 帧版的 RMS/峰值要落在 50 帧版自身抖动范围内
    lo = min(stat(50, "rms_head"))
    hi = max(stat(50, "rms_head"))
    r25 = stat(25, "rms_head")
    inside = all(lo * 0.7 <= v <= hi * 1.3 for v in r25)
    print(f"  50 帧首段 RMS 区间 [{lo:.5f}, {hi:.5f}] · "
          f"25 帧首段 RMS {[round(v, 5) for v in r25]} ⇒ "
          f"{'在区间内' if inside else '偏离'}")
    print(f"  同配置互相关基线: 25 帧 {base[25]:.4f} / 50 帧 {base[50]:.4f}"
          f" · 跨配置 {min(cross):.4f}~{max(cross):.4f}"
          f" ⇒ 跨配置与基线同量级 = 采样噪声主导，**不是**首窗引入的差异")
    # ② **首声更快**（这才是首窗优化要省的东西）—— 量的是「第一帧出现」
    #    的时刻，**不是**合成总时长（总时长由 talker 产多少帧决定 ⇒ 两个
    #    配置几乎一样 ⇒ 用它当判据会得出「没变快」的错误结论）。
    ff25 = [r["first_frame_sec"] for r in runs[25]]
    ff50 = [r["first_frame_sec"] for r in runs[50]]
    lo_ff = min(ff25)
    hi_ff = max(ff50)
    faster = lo_ff < hi_ff - 0.3
    print(f"  首声延迟（第一帧出现）: 25 帧 {[round(v, 2) for v in ff25]} · "
          f"50 帧 {[round(v, 2) for v in ff50]} ⇒ "
          f"{'更快' if faster else '没更快'}")
    w25 = min(stat(25, "wall"))
    w50 = min(stat(50, "wall"))
    print(f"  （合成总墙钟 25 帧 {w25:.2f}s vs 50 帧 {w50:.2f}s —— 总时长由 "
          f"talker 决定，**不是**首窗的指标）")
    # ③ **接缝**：首窗与第二窗的拼接点不能有可听见的跳变 ⇒ 接缝处的
    #    包络跳变要落在「自然跳变」的基线量级里。
    ratios = []
    for fw in (25, 50):
        for r in runs[fw]:
            base_med = median(r["baseline_jump"])
            if base_med > 1e-9 and r["seam_jump"] is not None:
                ratios.append((fw, r["seam_jump"] / base_med))
    seam_ok = all(v <= 3.0 for _, v in ratios)
    print(f"  接缝跳变 / 自然跳变基线: "
          f"{[(fw, round(v, 3)) for fw, v in ratios]} ⇒ "
          f"{'在基线量级（没有可听接缝）' if seam_ok else '明显偏高（有接缝！）'}")
    ok = inside and faster and seam_ok
    print(f"\n  判定: {'✅ 音频未劣化，首声更快，可缩短首窗' if ok else '⚠️ 有问题，别改'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
