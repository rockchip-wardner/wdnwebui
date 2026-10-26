#!/usr/bin/env python3
"""真机回归: 语音生成的音色**真的生效** —— 同一个音色,多次生成必须是同一个声音。

``verify_tts_voice_catalog.py`` 断言的是参数分流(发出去的 speaker /
ref_speaker 对不对)。本脚本补另一半: 选一个音色连续合成几次,**基频(F0)
必须稳**。音色条件没生效时模型会自由发挥 —— 同一个「女声」时而 115Hz
(男声)时而 233Hz(女声)。

量法: 自相关法,取 40ms 帧 / 20ms 跳,算自相关找峰(70~400Hz),只留自相关
> 0.3 的浊音帧,再做**八度纠正**(丢掉偏离中位数 0.7~1.4 倍的帧,免得
2x/0.5x 的误判混进来),取中位数。

判据: 同一音色 6 次生成的 F0 极差 **< 8 个半音**(约 50%)。阈值标定自
2026-10-26 实测(本板 Base 版权重):

============================  ==========================
音色                          组内 F0 极差
============================  ==========================
girl_base(参考/有效)          4.1 半音 ✅
ahu(参考/有效)                2.8 半音 ✅
serena(内置/失效对照)         9.6 ~ 12.4 半音 ❌
============================  ==========================

失效对照组的构造: 本板是 Base 版,内置音色行是噪声 —— 直接 POST
speaker=serena 就能复现(UI 不露、API 也拒了,标定时是在加校验之前跑的)。

本脚本**只发合成请求,不启停模型** —— 启停会打断同板子上的其他人。
"""
import json
import struct
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np

API = "http://127.0.0.1:8000"
TIMEOUT_S = 180
REPEAT = 6
# 同一音色的 F0 极差上限(半音)。8 半音 ≈ 50%。
# 标定: 有效音色 2.8~4.1 半音, 失效音色 9.6~12.4 半音。
MAX_SPREAD_SEMITONES = 8.0

ok_count = 0
failed: list = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global ok_count
    if cond:
        ok_count += 1
        print(f"  ✅ {name}" + (f"  ({detail})" if detail else ""))
    else:
        failed.append(name)
        print(f"  ❌ {name}" + (f"  ({detail})" if detail else ""))


def _get(path: str) -> dict:
    with urllib.request.urlopen(f"{API}{path}", timeout=15) as r:
        return json.loads(r.read().decode())


def _post(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def read_wav_f32(path: Path):
    """解析本项目写的 WAV: RIFF/WAVE, format_tag=3(IEEE_FLOAT),
    1 声道, 32-bit LE float。返回 (采样率, float32 数组)。"""
    raw = path.read_bytes()
    if raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise ValueError("不是 RIFF/WAVE")
    pos, fmt, data = 12, None, None
    while pos + 8 <= len(raw):
        cid = raw[pos:pos + 4]
        size = struct.unpack("<I", raw[pos + 4:pos + 8])[0]
        body = raw[pos + 8:pos + 8 + size]
        if cid == b"fmt ":
            fmt = struct.unpack("<HHIIHH", body[:16])
        elif cid == b"data":
            data = body
        pos += 8 + size + (size & 1)
    if fmt is None or data is None:
        raise ValueError("缺 fmt/data 块")
    tag, channels, sr = fmt[0], fmt[1], fmt[2]
    if tag != 3:
        raise ValueError(f"format_tag={tag},期望 3(IEEE_FLOAT)")
    pcm = np.frombuffer(data, dtype="<f4")
    if channels > 1:
        pcm = pcm.reshape(-1, channels)[:, 0]
    return sr, pcm


def median_f0(sr: int, pcm) -> float:
    """自相关法估基频, 返回中位数(Hz);估不出返回 0。

    估完做一次**八度纠正**: 自相关有时会锁到 2x / 0.5x 的峰, 把偏离
    中位数 0.7~1.4 倍的帧剔掉再取中位数。
    """
    frame = int(sr * 0.04)
    hop = int(sr * 0.02)
    lo, hi = int(sr / 400), int(sr / 70)          # 70~400 Hz
    f0s = []
    for i in range(0, max(0, len(pcm) - frame), hop):
        seg = pcm[i:i + frame].astype(np.float64)
        if np.sqrt(np.mean(seg ** 2)) < 0.02:     # 静音段跳过
            continue
        seg = seg - seg.mean()
        ac = np.correlate(seg, seg, mode="full")[frame - 1:]
        if ac[0] <= 0:
            continue
        ac = ac / ac[0]
        window = ac[lo:hi]
        if window.size == 0:
            continue
        k = int(np.argmax(window)) + lo
        if ac[k] < 0.3:                            # 自相关不够高 → 非浊音
            continue
        f0s.append(sr / k)
    if not f0s:
        return 0.0
    med = float(np.median(f0s))
    kept = [f for f in f0s if 0.7 * med <= f <= 1.4 * med]
    return float(np.median(kept or f0s))


def synth(text: str, **kw) -> dict:
    """发一次合成, 等到 done/error。返回本次的统计。"""
    body = {"text": text, "play_to": "browser", "volume": 0.1}
    body.update(kw)
    snap = _post("/api/tts/start", body)
    if snap.get("state") == "error":
        raise RuntimeError(f"start 直接报错: {snap.get('error')}")

    t0 = time.time()
    last = ""
    while time.time() - t0 < TIMEOUT_S:
        snap = _get("/api/tts/")
        last = snap.get("state", "")
        if last in ("done", "error", "stopped", "idle"):
            break
        time.sleep(0.8)
    if last == "error":
        raise RuntimeError(f"合成失败: {snap.get('error')}")
    if last not in ("done", "idle"):
        raise TimeoutError(f"{TIMEOUT_S}s 内没跑完,最后状态 {last!r}")

    out = snap.get("output") or ""
    if not out:
        raise RuntimeError("没有产出文件")
    p = Path(out)
    sr, pcm = read_wav_f32(p)
    return {
        "path": str(p),
        "sr": sr,
        "sec": round(pcm.size / sr, 2) if sr else 0.0,
        "nonzero": int(np.count_nonzero(pcm)),
        "rms": float(np.sqrt(np.mean(pcm.astype(np.float64) ** 2))) if pcm.size else 0.0,
        "f0": median_f0(sr, pcm),
        "request": snap.get("request") or {},
    }


def main() -> int:
    print("=== 真机: TTS 音色生效性(基频稳定性) ===")

    comp = _get("/api/compute")
    tts_inst = [i for i in comp.get("instances", [])
                if i.get("model_id") == "qwen3-tts"]
    if not tts_inst:
        print("❌ 本机没有 qwen3-tts 实例,先在算力页启用再跑")
        return 1
    st = tts_inst[0].get("status")
    print(f"qwen3-tts: status={st} source={tts_inst[0].get('_source')}")
    if st != "running":
        print("❌ qwen3-tts 没在 running")
        return 1

    snap = _get("/api/tts/")
    if snap.get("busy"):
        print("❌ TTS 会话正忙(有人在用),稍后再跑")
        return 1

    # 清单 = 端点给的(已按本模型的有效性过滤过)
    catalog = _get("/api/tts/voices")
    voices = catalog.get("voices") or []
    print(f"清单音色: {[v['id'] + '(' + v['kind'] + ')' for v in voices]}")
    if not voices:
        print("❌ 清单为空 —— 音色探测有问题")
        return 1

    text = "今天天气不错,适合出去走一走。"

    for v in voices:
        vid, kind = v["id"], v["kind"]
        kw = {"ref_speaker": vid} if kind == "ref" else {"speaker": vid}
        print(f"\n--- {vid} ({kind}) ---")
        f0s, reqs = [], []
        for _ in range(REPEAT):
            try:
                r = synth(text, **kw)
            except Exception as e:  # noqa: BLE001
                check(f"{vid} 合成成功", False, str(e))
                break
            f0s.append(r["f0"])
            reqs.append(r["request"])
            print(f"  F0={r['f0']:7.1f}Hz  {r['sec']:5.2f}s  非零 {r['nonzero']:6d}  "
                  f"RMS {r['rms']:.5f}")
        if not f0s:
            continue

        # 参数确实按说的送到了后端
        req = reqs[0]
        if kind == "builtin":
            check(f"{vid} 后端收到 speaker", req.get("speaker") == vid,
                  repr(req.get("speaker")))
            check(f"{vid} 后端没收到 ref_speaker", not req.get("ref_speaker"),
                  repr(req.get("ref_speaker")))
        else:
            check(f"{vid} 后端收到 ref_speaker",
                  req.get("ref_speaker") == vid, repr(req.get("ref_speaker")))
            check(f"{vid} 后端没收到 speaker", not req.get("speaker"),
                  repr(req.get("speaker")))

        vals = [f for f in f0s if f > 0]
        if len(vals) < 2:
            check(f"{vid} F0 稳定(音色生效)", False,
                  f"只估出 {len(vals)} 个 F0: {f0s}")
            continue
        med = float(np.median(vals))
        spread_semi = 12 * np.log2(max(vals) / min(vals))
        print(f"  → 中位 {med:.1f}Hz  极差 {max(vals) - min(vals):.1f}Hz "
              f"= {spread_semi:.1f} 半音")
        check(f"{vid} F0 稳定(音色生效)",
              spread_semi < MAX_SPREAD_SEMITONES,
              (f"极差 {spread_semi:.1f} 半音" if spread_semi < MAX_SPREAD_SEMITONES
               else f"极差 {spread_semi:.1f} 半音 > {MAX_SPREAD_SEMITONES} —— "
                    f"音色条件没生效,模型在自由发挥"))

    total = ok_count + len(failed)
    print(f"\n=== 总结: {ok_count}/{total} ===")
    if failed:
        print(f"❌ 失败: {failed}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
