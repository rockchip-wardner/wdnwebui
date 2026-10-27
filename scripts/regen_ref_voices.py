#!/usr/bin/env python3
"""在板子上提取参考音色的 x-vector（2048 维）。

为什么在板子上做
----------------
参考音色和「用户上传音色」走**同一个** ECAPA-TDNN 提取器（同一个
``speaker_encoder.1828.rknn``），所以直接在板子上跑：

* 不用下载 x86 checkpoint、不需要 torch；
* 出来的音色和用户现场上传的音色同源、可比。

预处理（顺序很重要）
--------------------
1. **裁掉起始静音** —— 提取器只取 5 秒窗口，且「帧轴前面补零最差」。
2. **挑最稳的 5 秒窗口**（不是死用前 5 秒）—— 见下面「为什么挑窗口」。
3. **RMS 归一化到 0.05**（增益上限 20x）—— 与 ``app/apps/tts/api.py``
   的 ``_normalize_wav`` 完全一致，用户音色和参考音色就站在同一条
   起跑线上。

为什么挑窗口
------------
提取器只吃 **5 秒**（``encode_pcm`` 里 ``pcm[:MAX_SAMPLES]``），但参考
音频的前 5 秒不一定是最能代表说话人的 5 秒。

实测反例（本板的「男播音员音」）：文件第 4 秒起有一段 141~164Hz 的
音高漂移，正好落在「前 5 秒」里。用前 5 秒提出来的 x-vector 是个
「95Hz + 150Hz」的混合体 —— 合成时基频在 119~193Hz 之间乱飘，真机
回归实测极差 **8.3 半音（超标）**。换成同一文件里音高最稳的窗口
（起点 2.88s）后，极差降到 **3.44 半音**。

选窗规则（在有声帧率 ≥ 50% 的候选里）
------------------------------------
取 **基频四分位距（半音）最小** 的窗口 —— 音高最稳 = 最能代表这个
说话人；并列时取有声帧率更高的，再并列取最早的。

用法
----
    PYTHONPATH=. .venv/bin/python -u scripts/regen_ref_voices.py \
        --wav-dir /userdata/ref_voices/参考音色 \
        --out-dir  /userdata/ref_voices/out \
        --model-dir /userdata/models/Qwen3-TTS
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np

SR = 24000                 # speaker encoder 输入采样率（硬要求）
WINDOW_SECONDS = 5.0       # 提取窗口
MAX_SAMPLES = int(SR * WINDOW_SECONDS)
WINDOW_STEP = 0.25        # 选窗扫描步长

#: 静音判定：帧 RMS 低于本文件 P95 帧 RMS 的这个比例就算静音帧（尺度无关）
SILENCE_REL = 0.15
#: 裁掉起始静音时用的相对阈值
LEAD_SILENCE_REL = 0.02
#: 静音判定的绝对下限（防止全静音文件被判成"全是语音"）
SILENCE_ABS = 10 ** (-55 / 20)
#: 裁掉静音后保留多少前置余量（保住爆破音/辅音起始）
PREROLL_SECONDS = 0.05
#: 选窗时要求的有声帧率下限
MIN_VOICED_RATIO = 0.5
#: 归一化目标 RMS —— 与 app/apps/tts/api.py 的 _normalize_wav 一致
TARGET_RMS = 0.05
#: 归一化增益上限
MAX_GAIN = 20.0


# --------------------------------------------------------------------------- #
# 帧级分析
# --------------------------------------------------------------------------- #
def _frame_rms(pcm: np.ndarray, frame: int) -> np.ndarray:
    n = len(pcm) // frame
    if n == 0:
        return np.array([float(np.sqrt((pcm.astype(np.float64) ** 2).mean()))])
    seg = pcm[: n * frame].reshape(n, frame).astype(np.float64)
    return np.sqrt((seg ** 2).mean(axis=1))


def _frame_f0(pcm: np.ndarray, sr: int = SR,
              rel_gate: float = SILENCE_REL) -> "tuple[np.ndarray, np.ndarray]":
    """逐帧 (f0, voiced)。

    静音门限用**相对量**（本文件 P95 帧 RMS 的 ``rel_gate`` 倍）——
    绝对门限（0.02）会让同一段音频在归一化前后判出不同的有声帧，
    选窗就没法复现。

    f0 用自相关法：40ms 帧 / 20ms 跳，搜索 70~400Hz，自相关峰 < 0.3
    视为非浊音。**不用 NSDF** —— 它的「首个 ≥ 0.9·最大峰」选峰规则在
    高次谐波被共振峰放大的男声上会锁到谐波上（实测把 100Hz 的
    「磁性大叔音」测成 507Hz），而自相关与板端验收脚本
    ``verify_tts_voices_real.py::median_f0`` 逐行等价。
    """
    frame = int(sr * 0.04)
    hop = int(sr * 0.02)
    lo, hi = int(sr / 400), int(sr / 70)
    env = _frame_rms(pcm, max(1, frame))
    p95 = float(np.percentile(env, 95)) if env.size else 0.0
    gate = max(p95 * rel_gate, SILENCE_ABS)
    f0s = np.zeros(0, dtype=np.float64)
    voiced = np.zeros(0, dtype=bool)
    if len(pcm) < frame:
        return f0s, voiced
    f0s, voiced = [], []
    for i in range(0, max(0, len(pcm) - frame), hop):
        seg = pcm[i:i + frame].astype(np.float64)
        if float(np.sqrt(np.mean(seg ** 2))) < gate:
            f0s.append(0.0)
            voiced.append(False)
            continue
        s = seg - seg.mean()
        ac = np.correlate(s, s, mode="full")[frame - 1:]
        if ac[0] <= 0:
            f0s.append(0.0)
            voiced.append(False)
            continue
        ac = ac / ac[0]
        w = ac[lo:hi]
        if w.size == 0:
            f0s.append(0.0)
            voiced.append(False)
            continue
        k = int(np.argmax(w)) + lo
        if ac[k] < 0.3:
            f0s.append(0.0)
            voiced.append(False)
            continue
        f0s.append(sr / k)
        voiced.append(True)
    return np.asarray(f0s), np.asarray(voiced)


def trim_leading_silence(pcm: np.ndarray, sr: int = SR) -> tuple:
    """裁掉起始静音。返回 ``(裁剪后的 pcm, 裁掉秒数)``。

    帧长 20ms，阈值取「峰值相对阈值」与「绝对阈值」的较大者。
    裁剪点往前留 50ms 余量 —— 直接切在语音起始边界上会削掉辅音起始。
    """
    if pcm.size == 0:
        return pcm, 0.0
    frame = max(1, int(sr * 0.02))
    env = _frame_rms(pcm, frame)
    peak = float(env.max()) if env.size else 0.0
    thr = max(peak * LEAD_SILENCE_REL, SILENCE_ABS)
    voiced = np.nonzero(env >= thr)[0]
    if voiced.size == 0:
        return pcm, 0.0          # 全静音，原样返回
    preroll = max(0, int(voiced[0]) - int(round(PREROLL_SECONDS / 0.02)))
    cut = preroll * frame
    return pcm[cut:], cut / float(sr)


def pick_window(pcm: np.ndarray, sr: int = SR) -> tuple:
    """挑最稳的 5 秒窗口。返回 ``(起点秒, 详情 dict)``。

    规则：候选窗口（有声帧率 ≥ 50%）里，取基频四分位距（半音）最小的；
    并列时取有声帧率更高的；再并列取最早的。
    """
    f0s, voiced = _frame_f0(pcm, sr)
    hop = int(sr * 0.02)
    n_frames = int(WINDOW_SECONDS * sr) // hop
    best = None
    candidates = []
    if n_frames > 0 and len(f0s) >= n_frames:
        step = max(1, int(WINDOW_STEP * sr) // hop)
        for start in range(0, len(f0s) - n_frames + 1, step):
            seg_f0 = f0s[start:start + n_frames]
            seg_v = voiced[start:start + n_frames]
            rate = float(seg_v.mean())
            if rate < MIN_VOICED_RATIO:
                continue
            v = seg_f0[seg_f0 > 0]
            if v.size < 20:
                continue
            med = float(np.median(v))
            keep = v[(v >= 0.7 * med) & (v <= 1.4 * med)]   # 八度纠正
            if keep.size < 20:
                continue
            p25, p75 = np.percentile(keep, 25), np.percentile(keep, 75)
            iqr = float(12 * np.log2(max(p75, 1e-9) / max(p25, 1e-9)))
            item = {
                "start_s": round(start * hop / sr, 3),
                "iqr_semitones": round(iqr, 2),
                "f0_median": round(float(np.median(keep)), 1),
                "voiced_ratio": round(rate, 3),
            }
            candidates.append(item)
            key = (iqr, -rate, item["start_s"])
            if best is None or key < best[0]:
                best = (key, item)
    if best is None:
        return 0.0, {"start_s": 0.0, "iqr_semitones": 0.0,
                     "f0_median": 0.0, "voiced_ratio": 0.0,
                     "candidates": 0, "fallback": "no_candidate"}
    item = dict(best[1])
    item["candidates"] = len(candidates)
    return item["start_s"], item


def normalize_rms(pcm: np.ndarray) -> tuple:
    """RMS 归一化到 :data:`TARGET_RMS`。返回 ``(pcm, gain, rms_before)``。

    和 ``app/apps/tts/api.py`` 的 ``_normalize_wav`` 同规则：
    增益上限 :data:`MAX_GAIN`，|gain-1|<0.2 时视为不动。
    """
    if pcm.size == 0:
        return pcm, 1.0, 0.0
    rms = float(np.sqrt((pcm.astype(np.float64) ** 2).mean()))
    if rms <= 1e-6:
        return pcm, 1.0, rms
    gain = min(TARGET_RMS / rms, MAX_GAIN)
    if abs(gain - 1.0) < 0.2:
        return pcm, 1.0, rms
    return np.clip(pcm * gain, -1.0, 1.0).astype(np.float32), gain, rms


def measure_f0(pcm: np.ndarray, sr: int = SR) -> dict:
    """参考音频的基频统计（板端验收脚本同款口径）。

    与 ``scripts/verify_tts_voices_real.py::median_f0`` 逐行等价：
    40ms 帧 / 20ms 跳 / 搜索 70~400Hz / 自相关峰 ≥ 0.3 / 八度纠正。
    """
    frame = int(sr * 0.04)
    hop = int(sr * 0.02)
    lo, hi = int(sr / 400), int(sr / 70)
    f0s: list[float] = []
    n_frames = 0
    for i in range(0, max(0, len(pcm) - frame), hop):
        seg = pcm[i:i + frame].astype(np.float64)
        n_frames += 1
        if float(np.sqrt(np.mean(seg ** 2))) < 0.02:    # 静音段跳过
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
        if ac[k] < 0.3:                               # 自相关不够高 → 非浊音
            continue
        f0s.append(sr / k)
    out = {"f0_median": 0.0, "f0_p10": 0.0, "f0_p90": 0.0,
           "voiced_ratio": 0.0, "frames": n_frames}
    if not f0s:
        return out
    arr = np.asarray(f0s)
    med = float(np.median(arr))
    kept = arr[(arr >= 0.7 * med) & (arr <= 1.4 * med)]      # 八度纠正
    inl = kept if kept.size else arr
    out.update({
        "f0_median": round(float(np.median(inl)), 1),
        "f0_p10": round(float(np.percentile(inl, 10)), 1),
        "f0_p90": round(float(np.percentile(inl, 90)), 1),
        "voiced_ratio": round(len(inl) / max(n_frames, 1), 3),
    })
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--wav-dir", required=True, help="参考音频目录")
    ap.add_argument("--out-dir", required=True, help="x-vector 输出目录")
    ap.add_argument("--model-dir", default="/userdata/models/Qwen3-TTS",
                    help="speaker encoder 模型目录")
    ap.add_argument("--names", default="",
                    help="逗号分隔的 id=文件名 映射；不给则用文件名(去扩展名)")
    ap.add_argument("--window", choices=("auto", "first"), default="auto",
                    help="auto=自动挑最稳的 5 秒窗口；first=死用前 5 秒")
    args = ap.parse_args()

    wav_dir = Path(args.wav_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    mapping: dict[str, str] = {}
    if args.names:
        for item in args.names.split(","):
            item = item.strip()
            if not item:
                continue
            vid, _, fname = item.partition("=")
            mapping[vid.strip()] = (fname.strip() or vid.strip() + ".wav")
    if not mapping:
        for p in sorted(wav_dir.glob("*.wav")):
            mapping[p.stem] = p.name

    # 懒加载 RKNN —— 顶层 import 会让非 RK 机器 segfault
    try:
        from wdnai.backends.speaker_encode import (
            SpeakerEncoder, read_wav, resample_to_24k,
        )
    except ImportError:
        # 没装进 venv 时，回退到源码树（板端是 editable 安装）
        sys.path.insert(0, os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", ".venv",
            "src", "wdnai", "src"))
        from wdnai.backends.speaker_encode import (
            SpeakerEncoder, read_wav, resample_to_24k,
        )

    print(f"[refgen] 模型目录 {args.model_dir}")
    print(f"[refgen] 参考音频 {wav_dir}  共 {len(mapping)} 条  "
          f"选窗={args.window}")
    enc = SpeakerEncoder(args.model_dir)
    try:
        for vid, fname in mapping.items():
            path = wav_dir / fname
            if not path.is_file():
                print(f"❌ {vid}: 找不到 {path}")
                continue
            pcm, sr = read_wav(str(path))
            dur = len(pcm) / float(sr) if sr else 0.0
            pcm24 = resample_to_24k(pcm, sr)
            trimmed, cut = trim_leading_silence(pcm24, SR)

            if args.window == "auto":
                start, win = pick_window(trimmed, SR)
            else:
                start, win = 0.0, {"start_s": 0.0, "iqr_semitones": 0.0,
                                  "f0_median": 0.0, "voiced_ratio": 0.0,
                                  "candidates": 0, "fallback": "first"}
            a = int(round(start * SR))
            window_pcm = trimmed[a:a + MAX_SAMPLES]
            if window_pcm.size < MAX_SAMPLES:
                # 不够 5 秒就在**尾部**补零（头部补零最差）
                window_pcm = np.concatenate(
                    [window_pcm,
                     np.zeros(MAX_SAMPLES - window_pcm.size, dtype=np.float32)])
                padded = True
            else:
                padded = False

            norm, gain, rms_before = normalize_rms(window_pcm)
            rms_after = float(np.sqrt((norm.astype(np.float64) ** 2).mean())) \
                if norm.size else 0.0
            emb = enc.encode_pcm(norm)
            npy_path = out_dir / f"{vid}.npy"
            np.save(str(npy_path), emb.astype(np.float32))
            f0 = measure_f0(norm)
            meta = {
                "id": vid,
                "file": str(path),
                "sample_rate": sr,
                "channels": 1,
                "duration_s": round(dur, 2),
                "lead_silence_trimmed_s": round(cut, 3),
                "window_start_s": round(start, 3),
                "window_mode": args.window,
                "window_iqr_semitones": win.get("iqr_semitones"),
                "window_candidates": win.get("candidates"),
                "rms_before": round(rms_before, 5),
                "rms_after": round(rms_after, 5),
                "gain": round(gain, 3),
                "padded_tail": bool(padded),
                "embed_dim": int(emb.shape[0]),
                "embed_norm": round(float(np.linalg.norm(emb)), 4),
                "f0_median": f0["f0_median"],
                "f0_p10": f0["f0_p10"],
                "f0_p90": f0["f0_p90"],
            }
            (out_dir / f"{vid}.meta.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"✅ {vid}: {meta['duration_s']}s 裁静音 {cut:.2f}s "
                  f"窗口[{start:.2f}s 起 IQR {win.get('iqr_semitones')} 半音 "
                  f"候选 {win.get('candidates')}] "
                  f"RMS {rms_before:.4f}→{rms_after:.4f} (×{gain:.1f}) "
                  f"F0 {meta['f0_median']}Hz dim={meta['embed_dim']}")
    finally:
        enc.release()
    print(f"\n[refgen] 输出目录 {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
