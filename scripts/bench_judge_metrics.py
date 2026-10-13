#!/usr/bin/env python3
"""在同一段真实监控流上，对比业界常用的几类帧级变化检测。

为什么跑这个而不是查文献：文献给的是"哪类方法好"，而阈值必须按**这一台
相机、这个场景**定。同一个算法换个相机的噪声底能差一个量级（本项目
AGENTS.md 记的"阈值标定不可跨素材迁移"就是踩出来的）。

对比对象（都是监控/视频里真正在用的）：
  * absdiff   —— 最朴素的帧差，取全图平均差
  * gscore    —— 仿射归一化后的块级 p99 残差
  * MOG2      —— 自适应高斯混合背景建模（OpenCV 官方样例用的那套）
  * KNN       —— 邻域背景建模，同上
  * gscore    —— 仿射归一化后的全图残差（本轮提出的候选）

每个算法都报两条：
  1. 噪声底：真实相邻帧的分数分布；
  2. 场景响应：对真实参考帧施加人工变换后的分数。
**分离度 = 场景响应 ÷ 噪声底 p95**，小于 1 就是"这个场景压根测不出来"。

跑法::

    LD_LIBRARY_PATH=.venv/src/wdndds/native/lib \\
      .venv/bin/python scripts/bench_judge_metrics.py --rtsp <url> --frames 600
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from app.core.video.frame_judge import gscore  # noqa: E402


def load(source: str, n: int, long_side: int = 160):
    from wdnav import VideoCapture
    from wdnav.scene import luma_of_nv12, plan_thumb_size

    cap = VideoCapture(source, timeout=8)
    try:
        dw, dh = plan_thumb_size(cap.width, cap.height, long_side)
        out, fails = [], 0
        while len(out) < n and fails < 600:
            try:
                ok, buf = cap.read(dw, dh, "NV12")
            except Exception:  # noqa: BLE001
                fails += 1
                continue
            if not ok or buf is None:
                break
            luma = luma_of_nv12(buf, dh)
            out.append(np.repeat(luma[:, :, None], 3, axis=2).copy())
        return np.stack(out) if out else None
    finally:
        try:
            cap.release()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- 度量实现
def _blocks(x, bs=_CONC_BLOCK):
    x = x.astype(np.float32)
    if x.ndim == 3:
        x = cv2.cvtColor(x, cv2.COLOR_BGR2GRAY)
    h, w = x.shape
    ph, pw = (-h) % bs, (-w) % bs
    if ph or pw:
        x = np.pad(x, ((0, ph), (0, pw)), mode="edge")
    hh, ww = x.shape[0] // bs, x.shape[1] // bs
    return x.reshape(hh, bs, ww, bs).mean(axis=(1, 3))


def m_absdiff(a, b):
    """最朴素的帧差：全图平均绝对差。"""
    return float(np.abs(_blocks(a) - _blocks(b)).mean())


def m_gscore(a, b):
    """仿射归一化后的全图残差：先吃掉整幅亮度/对比度变化，再看剩多少结构。"""
    A = _blocks(a).ravel().astype(np.float64)
    B = _blocks(b).ravel().astype(np.float64)
    if A.var() < 1.0:
        return float(np.abs(B - B.mean()).mean())
    g = ((A - A.mean()) * (B - B.mean())).mean() / A.var()
    return float(np.abs(B - (g * A + B.mean() - g * A.mean())).mean())


class _Mog:
    """MOG2 / KNN 都得**连续喂帧**才能建模，所以用可调用对象串起来。"""

    def __init__(self, kind="mog2"):
        self.kind = kind

    def __call__(self, a, b):
        sub = (cv2.createBackgroundSubtractorMOG2() if self.kind == "mog2"
               else cv2.createBackgroundSubtractorKNN())
        sub.apply(a.astype(np.uint8), learningRate=0)     # 先学背景
        sub.apply(a.astype(np.uint8), learningRate=0)
        fg = sub.apply(b.astype(np.uint8), learningRate=0)  # 再测 b
        return float((fg > 0).mean()) * 100.0             # 前景像素占比 %


# ---------------------------------------------------------------- 场景构造
def scenes(base):
    h, w = base.shape[:2]
    out = [
        ("全画面 -45 灰阶(曝光)",  lambda b: np.clip(b.astype(np.int16) - 45, 0, 255).astype(np.uint8)),
        ("全画面 +45 灰阶(曝光)",  lambda b: np.clip(b.astype(np.int16) + 45, 0, 255).astype(np.uint8)),
    ]

    def hand(b):
        c = b.copy()
        c[int(h * .25):int(h * .85), int(w * .30):int(w * .72)] = (150, 190, 220)
        return c
    out.append(("手掌块 25% 画面", hand))
    out.append(("整幅均匀暗色(全挡)", lambda b: np.full_like(b, 40)))
    out.append(("整幅右移 25px", lambda b: np.roll(b, 25, axis=1)))
    out.append(("右移 8px(轻微抖动)", lambda b: np.roll(b, 8, axis=1)))

    def half(b):
        c = b.copy()
        r0 = int(h * .45)
        c[r0:] = np.clip(c[:h - r0].astype(np.int16) * 16 // 10 + 40, 0, 255).astype(np.uint8)
        return c
    out.append(("下半部 50% 改变", half))

    def small(b):
        c = b.copy()
        c[int(h * .40):int(h * .68), int(w * .42):int(w * .50)] = 235
        return c
    out.append(("小目标 2.3% 面积(对照)", small))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rtsp")
    ap.add_argument("--file", dest="file")
    ap.add_argument("--frames", type=int, default=600)
    a = ap.parse_args()
    src = a.file or a.rtsp
    if not src:
        ap.error("需要 --rtsp 或 --file")

    F = load(src, a.frames)
    if F is None or len(F) < 80:
        print("解码失败 / 帧数不足")
        return 1
    N, H, W = F.shape[:3]
    print(f"抓到 {N} 帧 {H}x{W}\n")

    metrics = [("absdiff", m_absdiff), ("gscore", m_gscore),
               ("MOG2", _Mog("mog2")), ("KNN", _Mog("knn"))]

    # ---- 噪声底 ----
    idx = list(range(1, min(N, 120)))
    noise = {}
    for name, fn in metrics:
        xs = [fn(F[i], F[i - 1]) for i in idx]
        noise[name] = np.percentile(xs, 95)
        print(f"  噪声底 {name:<8} p50={np.percentile(xs,50):8.3f} "
              f"p95={noise[name]:8.3f} max={max(xs):8.3f}")
    print()

    refs = [F[int(N * r)] for r in (0.25, 0.45, 0.55, 0.75, 0.9)]
    sc = scenes(F[0])
    head = "  " + f"{'场景':<24}" + "".join(f"{n:>16}" for n, _ in metrics)
    print(head + "     （数字 = 场景响应 ÷ 该算法噪声底 p95）")
    for sname, fn_s in sc:
        row = "  " + f"{sname:<24}"
        for mname, fn_m in metrics:
            v = float(np.median([fn_m(fn_s(r), r) for r in refs]))
            row += f"{v / noise[mname]:15.2f}x"
        print(row)
    return 0


if __name__ == "__main__":
    sys.exit(main())
