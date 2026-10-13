#!/usr/bin/env python3
"""卷积式帧判据横评：和现有 conc / gscore 在同一条真实监控流上对比。

问的是"有没有基于卷积的算法"，但结论必须落在**这台相机**上，所以实测。
分两类：

  A. 需要权重的 CNN 特征距离（VGG16 / AlexNet 中间层特征图距离）
     —— 业界最常见的"深度特征帧差"。**本机当前不可用**：整机没有任何
     .onnx/.tflite/.rknn 权重，也没装 onnxruntime / torch / rknn，
     网络也下不来。所以这里只能做**可行性判断**，不做数值对比。

  B. 不需要权重的固定卷积核（LoG / Sobel / DoG）
     —— 经典 blob 检测与高通滤波，纯 cv2 就能跑。**这一类现在就能实测。**

为什么值得专门测 B：conc 的结构性缺陷不是"线性"，而是**比值** ——
分子（块内平均差 max）和分母（块均值 90 分位）在**大范围变化**时同步抬升，
比值回到 1，于是"挡镜头""相机被动""下半屏改变"全部测不出来（实测 0.1~0.2 倍
噪声底）。固定卷积核给的是**绝对响应幅值**，天然没有这个归一化陷阱 ——
它恰好是 conc 缺的那一半。

本脚本报三个维度，缺一不可：
  1. **分离度** = 场景响应 ÷ 该算法自己的噪声底 p95（<1 = 压根测不出来）
  2. **噪声底稳定性** = 分窗重测的波动倍数（conc 实测三次抓取飘 735 倍，
     这是它在大阈值下仍然误报的根因）
  3. **单帧耗时** —— 热路径预算，必须 <1ms

跑法::

    LD_LIBRARY_PATH=.venv/src/wdndds/native/lib \\
      .venv/bin/python scripts/bench_conv_judge.py --rtsp <url> --frames 600
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

# conc 在 v0.21 已下架（对大范围变化结构性失明）—— 这个 bench 是历史
# 横向对比脚本，**不再**导入 conc（脚本里 ``m_conc`` 一并移除）。只留 gscore
# 与卷积候选的对比。运行只是看卷积类算子相对 gscore 的差距，跑一次
# 之后可删。
from app.core.video.frame_judge import gscore  # noqa: E402


# ---------------------------------------------------------------- 抓帧
def load(source: str, n: int, long_side: int = 160):
    """与线上一致：RGA 缩到长边 160 的真实缩略图，判据口径才对得上。"""
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


# ---------------------------------------------------------------- 基础
def _gray(x):
    x = np.asarray(x)
    return cv2.cvtColor(x, cv2.COLOR_BGR2GRAY) if x.ndim == 3 else x


def _blocks(x, bs=8):
    x = x.astype(np.float32)
    if x.ndim == 3:
        x = cv2.cvtColor(x, cv2.COLOR_BGR2GRAY)
    h, w = x.shape
    ph, pw = (-h) % bs, (-w) % bs
    if ph or pw:
        x = np.pad(x, ((0, ph), (0, pw)), mode="edge")
    return x.reshape(x.shape[0] // bs, bs, x.shape[1] // bs, bs).mean(axis=(1, 3))


# ---------------------------------------------------- 卷积算子（无需权重）
def _log(x, sigma=1.5):
    """尺度归一化 LoG：σ²·∇²(G_σ * x)。经典 blob 检测核。

    高斯平滑后求拉普拉斯，σ² 归一化保证不同尺度下响应幅度可比 ——
    少了这一项，σ 一大响应就被摊平，blob 反而检不出来。
    """
    g = cv2.GaussianBlur(x, (0, 0), sigma)
    return (sigma ** 2) * cv2.Laplacian(g, cv2.CV_32F, ksize=3)


def _dog(x, s1=1.0, s2=2.0):
    """高斯差分 = 尺度空间的拉普拉斯近似，两个 σ 的高斯卷积相减。"""
    return (cv2.GaussianBlur(x, (0, 0), s2)
            - cv2.GaussianBlur(x, (0, 0), s1))


def _sobel(x):
    gx = cv2.Sobel(x, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(x, cv2.CV_32F, 0, 1, ksize=3)
    return cv2.magnitude(gx, gy)


# ---------------------------------------------------------------- 度量
def m_gscore(a, b):
    """仿射归一化后的全图残差（上一轮横评里最均衡的候选）。"""
    A = _blocks(a).ravel().astype(np.float64)
    B = _blocks(b).ravel().astype(np.float64)
    if A.var() < 1.0:
        return float(np.abs(B - B.mean()).mean())
    g = ((A - A.mean()) * (B - B.mean())).mean() / A.var()
    return float(np.abs(B - (g * A + B.mean() - g * A.mean())).mean())


def m_logmax(a, b):
    """LoG 响应图之差的**峰值**：绝对量，不做比值归一化。"""
    A, B = _gray(a).astype(np.float32), _gray(b).astype(np.float32)
    return float(np.abs(_log(A) - _log(B)).max())


def m_logsum(a, b):
    """LoG 响应差的 99.5 分位：比 max 抗单点尖刺（坏点/编码残差）。"""
    A, B = _gray(a).astype(np.float32), _gray(b).astype(np.float32)
    return float(np.percentile(np.abs(_log(A) - _log(B)), 99.5))


def m_dogmax(a, b):
    """DoG 响应差的峰值。"""
    A, B = _gray(a).astype(np.float32), _gray(b).astype(np.float32)
    return float(np.abs(_dog(A) - _dog(B)).max())


def m_sobelmax(a, b):
    """Sobel 梯度幅值图之差的峰值。梯度域天然免疫亮度平移（曝光）。"""
    A, B = _gray(a).astype(np.float32), _gray(b).astype(np.float32)
    return float(np.abs(_sobel(A) - _sobel(B)).max())


def m_sobel_conc(a, b):
    """把 conc 的比值结构套到卷积梯度上：梯度域的集中度。

    这是"卷积 + 现有思路"的混合体 —— 想知道保住了 conc 的哪一半。
    """
    A, B = _gray(a).astype(np.float32), _gray(b).astype(np.float32)
    return conc(_blocks(_sobel(A)), _blocks(_sobel(B)))


# ---------------------------------------------------------------- 场景
def scenes(base):
    """重点覆盖 conc 实测失灵的三类：大范围变化 / 挡镜头 / 相机被动。"""
    h, w = base.shape[:2]
    out = [
        ("曝光 -45 灰阶", lambda b: np.clip(b.astype(np.int16) - 45, 0, 255).astype(np.uint8)),
        ("整幅均匀暗(全挡)", lambda b: np.full_like(b, 40)),
    ]

    def hand(b):
        """手挡镜头：大面积、**低对比**（皮肤透光）、带模糊、边界清晰。

        这是用户实际报过的现象（"手遮挡摄像头，浮框仍显示 1.7"），
        贴矩形测不出来 —— 真实手挡是半透的软边界。
        """
        c = b.copy()
        yy, xx = np.mgrid[0:h, 0:w]
        cy, cx = h * 0.45, w * 0.55
        mask = (((yy - cy) / (h * 0.42)) ** 2 + ((xx - cx) / (w * 0.30)) ** 2) < 1.0
        c[mask] = cv2.GaussianBlur((b[mask] * 0.35 + 25).astype(np.uint8), (0, 0), 3)
        return c

    out.append(("手挡镜头(半透软边)", hand))

    def blur(b):
        """相机失焦/模糊：整幅高频丢失，无位移无亮度变化。"""
        return cv2.GaussianBlur(b, (0, 0), 2.0)

    out.append(("整幅失焦(高频丢失)", blur))

    def pan(b):
        """相机被动：小角度摇，8px（在 180x320 缩略图上≈全分辨率 48px）。"""
        return np.roll(b, 8, axis=1)

    out.append(("相机右移 8px(被动)", pan))

    def half(b):
        c = b.copy()
        r0 = int(h * .45)
        c[r0:] = np.clip(c[:h - r0].astype(np.int16) * 16 // 10 + 40, 0, 255).astype(np.uint8)
        return c

    out.append(("下半部 50% 改变", half))

    def small(b):
        """小目标 2.3% 面积 —— conc 的强项，任何候选都不能丢。"""
        c = b.copy()
        c[int(h * .40):int(h * .68), int(w * .42):int(w * .50)] = 235
        return c

    out.append(("小目标 2.3%(必须保住)", small))
    return out


METRICS = [
    ("gscore", m_gscore),
    ("LoG-max", m_logmax),
    ("LoG-p99.5", m_logsum),
    ("DoG-max", m_dogmax),
    ("Sobel-max", m_sobelmax),
    ("Sobel-conc", m_sobel_conc),
]


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
    if F is None or len(F) < 120:
        print("解码失败 / 帧数不足")
        return 1
    N, H, W = F.shape[:3]
    print(f"抓到 {N} 帧 {H}x{W}\n")

    # ---------------------------------------------------------- 1. 噪声底
    idx = list(range(1, min(N, 200)))
    xs = {name: [fn(F[i], F[i - 1]) for i in idx] for name, fn in METRICS}
    noise = {n: float(np.percentile(v, 95)) for n, v in xs.items()}
    print("== 噪声底（真实相邻帧）==")
    for name, _ in METRICS:
        v = xs[name]
        print(f"  {name:<11} p50={np.percentile(v,50):9.4f} "
              f"p95={noise[name]:9.4f} max={max(v):9.4f}")
    print()

    # ------------------------------------------------ 2. 噪声底稳定性
    # conc 实测三次抓取飘 735 倍 —— 这是"阈值调高也没用"的根因。
    print("== 噪声底稳定性（把相邻帧对按时间切成 6 段，各段 p95 的波动）==")
    print(f"  {'算法':<11}{'各窗 p95':<44}{'max/min':>9}")
    for name, _ in METRICS:
        w = []
        for k in range(6):
            seg = xs[name][k * len(xs[name]) // 6:(k + 1) * len(xs[name]) // 6]
            if seg:
                w.append(max(float(np.percentile(seg, 95)), 1e-9))
        ratio = max(w) / max(min(w), 1e-9)
        print(f"  {name:<11}{' '.join(f'{x:6.3f}' for x in w):<44}{ratio:8.1f}x")
    print()

    # ---------------------------------------------------- 3. 场景分离度
    refs = [F[int(N * r)] for r in (0.25, 0.45, 0.55, 0.75, 0.9)]
    sc = scenes(F[0])
    print("== 场景响应 ÷ 该算法自己的噪声底 p95（<1 = 这个场景压根测不出来）==")
    print("  " + f"{'场景':<22}" + "".join(f"{n:>12}" for n, _ in METRICS))
    rows = []
    for sname, fn_s in sc:
        row = []
        for mname, fn_m in METRICS:
            v = float(np.median([fn_m(fn_s(r), r) for r in refs]))
            row.append(v / max(noise[mname], 1e-9))
        rows.append((sname, row))
        print("  " + f"{sname:<22}" + "".join(f"{v:11.1f}x" for v in row))
    print()

    # ---------------------------------------------------------- 4. 耗时
    print("== 单帧耗时（热路径预算 <1ms）==")
    for name, fn in METRICS:
        t0 = time.perf_counter()
        for i in idx[:60]:
            fn(F[i], F[i - 1])
        dt = (time.perf_counter() - t0) / 60 * 1000
        print(f"  {name:<11}{dt:7.3f} ms")

    print("\n== 汇总：几何平均分离度（几何平均惩罚『靠一两个场景刷分」的算法）==")
    for j, (name, _) in enumerate(METRICS):
        col = [r[1][j] for r in rows]
        print(f"  {name:<11}{np.exp(np.mean(np.log(np.maximum(col, 1e-6)))):9.1f}x"
              f"   最差场景 {min(col):8.1f}x")
    return 0


if __name__ == "__main__":
    sys.exit(main())
