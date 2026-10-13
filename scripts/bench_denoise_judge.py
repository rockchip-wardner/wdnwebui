#!/usr/bin/env python3
"""去噪预处理对帧判据的影响：卷积/滤波到底能不能"排除底噪"。

背景
----
上一轮实测里，梯度类卷积算子（LoG / Sobel / DoG）在这条监控流上**全线
失败**（几何平均分离度 2~3x，最差场景 0.5x，比现有 conc 还差）。这与
"卷积能去噪"的直觉相反，值得单独查清原因。

假设：LoG/Sobel 是**高通**滤波。本流的底噪是 H.264 压缩残差 + 传感器
噪声，主要落在**高频**，高通滤波不但压不住噪声，还把它放大 —— 所以信号
和噪声一起被抬高，比值反而被稀释。

若假设成立，能压底噪的应该是**低通 / 中值 / 下采样**（盒式卷积属于这一
类），而不是梯度算子。本脚本验证这一点。

判据（这是"排除底噪"是否成功的唯一标准）
------------------------------------------
  * 降噪比   = 底噪(前) / 底噪(后)          —— 底噪压下去了吗
  * 信号保留 = 场景响应(后) / 场景响应(前)    —— 真信号还在吗
  * **净收益 = 降噪比 / 信号保留**            —— >1 才算赚

只看"降噪比"会被形态学开运算之类的操作骗过去：它把底噪压到 0，同时把小
目标一起抹掉，净收益 <1。所以两个数必须一起报。

跑法::

    LD_LIBRARY_PATH=.venv/src/wdndds/native/lib \\
      .venv/bin/python scripts/bench_denoise_judge.py --rtsp <url> --frames 600
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from app.core.video.frame_judge import gscore  # noqa: E402
from scripts.bench_conv_judge import load, m_gscore, scenes, _blocks  # noqa: E402


# ------------------------------------------------------------ 去噪预处理链
def p_identity(x):
    return x


def p_median3(x):
    return cv2.medianBlur(_u8(x), 3)


def p_median5(x):
    return cv2.medianBlur(_u8(x), 5)


def p_bilateral(x):
    """双边滤波：抑制空间噪声同时保住边缘 —— 底噪多为高斯型时最对口。"""
    return cv2.bilateralFilter(_u8(x), 5, 40, 40)


def p_gauss1(x):
    return cv2.GaussianBlur(_u8(x), (0, 0), 1.0)


def p_box2(x):
    """2x 面积平均下采样 —— 教科书上的盒式卷积（低通），实现上是 INTER_AREA。"""
    return cv2.resize(_u8(x), (x.shape[1] // 2, x.shape[0] // 2),
                      interpolation=cv2.INTER_AREA)


def p_box2_med3(x):
    """下采样 + 中值：先砍掉一半高频，再打掉剩下的孤立脉冲（坏点）。"""
    y = p_box2(x)
    return cv2.medianBlur(y, 3)


def p_morph_open(x):
    """形态学开运算：清孤立前景点。经典去噪手段，但要盯紧信号损失。"""
    g = _u8(x)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    return cv2.morphologyEx(g, cv2.MORPH_OPEN, k)


PRE = [
    ("无处理", p_identity),
    ("中值3x3", p_median3),
    ("中值5x5", p_median5),
    ("双边5x5", p_bilateral),
    ("高斯σ=1", p_gauss1),
    ("盒式2x下采样", p_box2),
    ("下采样+中值3", p_box2_med3),
    ("形态学开", p_morph_open),
]


def _u8(x):
    x = np.asarray(x)
    if x.dtype == np.uint8:
        return x
    return np.clip(x, 0, 255).astype(np.uint8)


def _gray(x):
    x = np.asarray(x)
    return cv2.cvtColor(x, cv2.COLOR_BGR2GRAY) if x.ndim == 3 else x


# ---------------------------------------------------------------- 度量
def g_of(a, b):
    """仿射归一化后的全图残差（上一轮横评里最均衡的候选指标）。"""
    return m_gscore(a, b)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rtsp")
    ap.add_argument("--file", dest="file")
    ap.add_argument("--frames", type=int, default=600)
    ap.add_argument("--metric", default="gscore", choices=["gscore"])
    a = ap.parse_args()
    src = a.file or a.rtsp
    if not src:
        ap.error("需要 --rtsp 或 --file")
    M = g_of

    F = load(src, a.frames)
    if F is None or len(F) < 120:
        print("解码失败 / 帧数不足")
        return 1
    N = len(F)
    print(f"抓到 {N} 帧 {F.shape[1]}x{F.shape[2]}   指标={a.metric}\n")

    idx = list(range(1, min(N, 200)))
    refs = [F[int(N * r)] for r in (0.25, 0.45, 0.55, 0.75, 0.9)]
    sc = scenes(F[0])

    base_noise = base_scen = None
    print("== 降噪比 / 信号保留 / 净收益（净收益 >1 才算真压住了底噪）==\n")
    print(f"  {'预处理':<16}{'降噪比':>9}{'几何分离度':>11}{'净收益':>9}"
          f"{'最差场景':>10}{'耗时ms':>9}")
    print("  " + "-" * 64)
    for name, fn in PRE:
        xs = [M(fn(F[i]), fn(F[i - 1])) for i in idx]
        noise = max(float(np.percentile(xs, 95)), 1e-9)

        per = []
        for sname, fs in sc:
            v = float(np.median([M(fn(fs(r)), fn(r)) for r in refs]))
            per.append(v / noise)
        geo = float(np.exp(np.mean(np.log(np.maximum(per, 1e-6)))))

        if base_noise is None:
            base_noise, base_geo = noise, geo
            dn, keep, net = 1.0, 1.0, 1.0
        else:
            dn = base_noise / noise
            keep = geo / max(base_geo, 1e-9)
            net = dn / max(keep, 1e-9)

        import time
        t0 = time.perf_counter()
        for i in idx[:50]:
            M(fn(F[i]), fn(F[i - 1]))
        dt = (time.perf_counter() - t0) / 50 * 1000

        flag = "  <-- 有效" if net > 1.15 else ""
        print(f"  {name:<16}{dn:8.2f}x{geo:10.1f}x{net:8.2f}x"
              f"{min(per):9.1f}x{dt:8.2f}{flag}")

    print("\n== 逐场景分离度（原始 / 最佳预处理后）==")
    best = None
    for name, fn in PRE:
        xs = [M(fn(F[i]), fn(F[i - 1])) for i in idx]
        noise = max(float(np.percentile(xs, 95)), 1e-9)
        geo = float(np.exp(np.mean(np.log(np.maximum(
            [float(np.median([M(fn(fs(r)), fn(r)) for r in refs])) / noise
             for _, fs in sc], 1e-6)))))
        if best is None or geo > best[1]:
            best = (name, geo, fn, noise)

    xs0 = [M(F[i], F[i - 1]) for i in idx]
    n0 = max(float(np.percentile(xs0, 95)), 1e-9)
    print(f"  {'场景':<22}{'原始':>10}{'最佳(' + best[0] + ')':>22}")
    for sname, fs in sc:
        v0 = float(np.median([M(fs(r), r) for r in refs])) / n0
        v1 = float(np.median([M(best[2](fs(r)), best[2](r)) for r in refs])) / best[3]
        print(f"  {sname:<22}{v0:9.1f}x{v1:21.1f}x")
    return 0


if __name__ == "__main__":
    sys.exit(main())
