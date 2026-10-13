#!/usr/bin/env python3
"""实测：**1080p 实时监控流**上的 conc 分数分布（阈值标定的关键缺口）。

背景：阈值 15 是在 `carplant_29132868.mp4`（960x540 **文件**）上标的。
用户实际跑的是 `rtsp://...192.168.169.25/...`（**1920x1080 实时流**），
配 conc/阈值 10/帧间隔 300，结果 4.78% 的帧被判为"变化"、检测事件几秒刷一次。
文件视频的标定值能不能直接搬到 1080p 实时监控流？本脚本给出答案。

走**与服务完全相同的路径**（wdnav.VideoCapture → plan_thumb_size(long_side=160)
→ luma_of_nv12 → frame_judge.conc），不另起炉灶。

用法::

    LD_LIBRARY_PATH=.venv/src/wdndds/native/lib \\
      .venv/bin/python scripts/measure_live_conc.py --rtsp <url> --frames 900
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from app.core.video.frame_judge import (  # noqa: E402
    _CONC_BLOCK,
    _CONC_FLOOR,
    _CONC_PCTL,
    conc,
)


def load_thumbs(source: str, n: int, long_side: int = 160):
    from wdnav import VideoCapture
    from wdnav.scene import luma_of_nv12, plan_thumb_size

    cap = VideoCapture(source, timeout=8)
    try:
        dw, dh = plan_thumb_size(cap.width, cap.height, long_side)
        print(f"源 {cap.width}x{cap.height} → 缩略图 {dw}x{dh} "
              f"(long_side={long_side}, 块 {_CONC_BLOCK}px)")
        thumbs, fails = [], 0
        while len(thumbs) < n and fails < 600:
            try:
                ok, buf = cap.read(dw, dh, "NV12")
            except Exception as exc:  # noqa: BLE001
                if "im2d" not in str(exc):
                    raise
                fails += 1
                continue
            if not ok or buf is None:
                break
            thumbs.append(luma_of_nv12(buf, dh).copy())
        return np.stack(thumbs) if thumbs else None
    finally:
        try:
            cap.release()
        except Exception:  # noqa: BLE001
            pass


def blocks(a, b, bs=_CONC_BLOCK):
    h, w = a.shape
    hh, ww = (h // bs) * bs, (w // bs) * bs
    A = a[:hh, :ww].reshape(hh // bs, bs, ww // bs, bs).mean(axis=(1, 3))
    B = b[:hh, :ww].reshape(hh // bs, bs, ww // bs, bs).mean(axis=(1, 3))
    return A, B


# 直接调**产品实现**，不复刻。
#   踩过的坑：先前这里复刻成了 ``|块均值A - 块均值B|``，而 frame_judge.conc
#   算的是**块内像素级** ``mean(|a-b|)``。两者只差"块内纹理/噪声"这一项 ——
#   静止画面亮度不变时前者恒接近 0、后者照样有噪声底，于是量出来的分数比线上
#   低一个量级，差点得出"判据误报"的结论。凡是量判据分数，一律调真函数。
def conc_score(a, b):
    return float(conc(a, b))


def pct(xs, p):
    return float(np.percentile(xs, p)) if len(xs) else float("nan")


def report(label, xs):
    if not len(xs):
        print(f"{label}: 无数据")
        return
    print(f"  {label}: n={len(xs)} min={min(xs):.2f} p50={pct(xs,50):.2f} "
          f"p95={pct(xs,95):.2f} p99={pct(xs,99):.2f} max={max(xs):.2f}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rtsp", required=True)
    ap.add_argument("--file", dest="file")
    ap.add_argument("--frames", type=int, default=900)
    ap.add_argument("--every-n", type=int, default=300)
    ap.add_argument("--thresholds", default="6,10,15,20,30,40")
    a = ap.parse_args()

    source = a.file or a.rtsp
    th = load_thumbs(source, a.frames)
    if th is None or len(th) < 60:
        print("解码失败 / 帧数不足")
        return 1
    print(f"取到 {len(th)} 帧\n")

    # ---- 1. 相邻帧（标定脚本用的就是这一档）----
    adj = [conc_score(th[i], th[i - 1]) for i in range(1, len(th))]
    print("【相邻帧 vs 上一帧】← verify_frame_judge_metrics.py 标定用的就是这一档")
    report("分数", adj)

    # ---- 2. 相隔 d 帧：自动模式下参照帧只在送检时推进，
    #         所以实际比较距离通常 >1 帧，这是标定没覆盖的那一档 ----
    print("\n【相隔 d 帧】（自动模式真实比较距离）")
    by_d = {}
    for d in (1, 2, 5, 10, 20, 40, 80):
        xs = [conc_score(th[i], th[i - d]) for i in range(d, len(th))]
        by_d[d] = xs
        report(f"d={d:<3}", xs)

    # ---- 3. 按阈值算触发率 ----
    print("\n【触发率 = 分数 > 阈值的帧占比】")
    thr = [float(x) for x in a.thresholds.split(",")]
    print(f"  {'阈值':>6} {'相邻帧':>10} " +
          " ".join(f"d={d:<6}" for d in (5, 20, 40)))
    for t in thr:
        row = [f"{100.0 * sum(1 for x in adj if x > t) / len(adj):9.3f}%"]
        for d in (5, 20, 40):
            xs = by_d[d]
            row.append(f"{100.0 * sum(1 for x in xs if x > t) / len(xs):8.3f}%")
        print(f"  {t:>6.1f} " + " ".join(row))

    # ---- 4. 完整跑一遍 FrameJudge 语义（参照帧只在送检时推进）----
    print(f"\n【完整 auto 语义模拟：参照帧只在送检时推进，every_n={a.every_n}】")
    for t in thr:
        ref, gaps, taken = None, [], []
        last = -1
        for i in range(len(th)):
            if ref is None:
                take = True
            else:
                s = conc_score(th[i], ref)
                gap = i - last
                take = bool(s > t or (gap >= a.every_n and s >= t * 0.5))
            if take:
                ref = th[i]
                last = i
                taken.append(i)
                gaps.append(i)
        gaps = [gaps[k] - gaps[k - 1] for k in range(1, len(gaps))]
        rate = 100.0 * len(taken) / len(th)
        g_desc = (f"p50={pct(gaps,50):.0f} p90={pct(gaps,90):.0f} max={max(gaps)}"
                  if gaps else "n/a")
        print(f"  阈值 {t:>5.1f}: 送检 {len(taken):>3}/{len(th)} = {rate:6.3f}%  "
              f"送检间隔帧 {g_desc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
