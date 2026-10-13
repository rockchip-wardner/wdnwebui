#!/usr/bin/env python3
"""自适应阈值 vs 固定阈值：用真实相邻帧测"底噪在飘"到底能不能治。

为什么做这个
------------
上一轮实测到 `conc` 的噪声底 p95 三次抓取分别是 0.159 / 11.65 / 116.8 ——
**波动 735 倍**。上一轮 `bench_denoise_judge.py` 又证明，空间滤波（中值 / 双边 /
高斯 / 形态学）压不动这个底噪：降噪比全部 ≤1，因为底噪不是空间相关的加性噪声，
而是 H.264 编码残差在**时间域**的抖动 + 场景内容本身在变（人走过、风扇、
光线）。空间滤波对这两类都无效。

所以"排除底噪"这条路已经被实测堵死。剩下唯一对症的是**不让阈值去追一个
会飘的底噪** —— 也就是把绝对阈值换成"当前滚动噪声底的 k 倍"。镜头切换检测
（shot boundary detection）领域用的 local adaptive threshold 就是这个思路。

本脚本**不需要真实闯入样本**：只测误报侧（真实相邻帧的触发率），而这恰恰是
用户现场"每 3.75 秒一条事件"的直接成因。判定逻辑与线上
`FrameJudge.observe` 的 auto 语义一致（参照帧只在送检时推进）。

判据
----
  * 触发率 = 相邻帧被判为"变了"的比例（越高误报越多）
  * **均匀性 = 最嘈杂窗 ÷ 最安静窗的触发率**（1.0 = 完全稳；这就是"在飘"）
  * 有效送检间隔 p50（与线上浮框显示的 `every_n_eff` 同口径）

跑法::

    LD_LIBRARY_PATH=.venv/src/wdndds/native/lib \\
      .venv/bin/python scripts/bench_adaptive_threshold.py --rtsp <url> --frames 900
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from scripts.bench_conv_judge import load, m_conc, m_gscore  # noqa: E402

SCENARIO = ("H.264 编码残差在时间域抖动 + 场景内容变化", )


def simulate(scores, every_n, fixed=None, adaptive_k=None, win=50):
    """复刻 FrameJudge.observe 的 auto 语义。

    fixed 非空  → 固定阈值；adaptive_k 非空 → score > k × 滚动 p95。
    两者都给时返回更松的那个（线上兜底语义），但本脚本只用其中一种。
    返回 (触发位置 list, 评估过的帧数 list)
    """
    take, evaluated = [], []
    n_fire, last_commit = 0, None  # last_commit=None → 首帧必送
    hist = []
    for i, s in enumerate(scores):
        if adaptive_k is not None:
            hist.append(s)
            thr = (np.percentile(hist[-win:], 95) * adaptive_k) if len(hist) >= 5 else np.inf
        else:
            thr = float(fixed)
        evaluated.append(i)
        gap = (i - last_commit) if last_commit is not None else every_n + 1
        fire = False
        if last_commit is None:
            fire = True                     # 首帧必送
        elif s > thr:
            fire = True                     # 超阈值
        elif gap >= every_n and s >= thr * 0.5:
            fire = True                     # 兜底：gap 满 且 分数过半
        if fire:
            take.append(i)
            last_commit = i
    return take, evaluated


def report(name, scores, every_n, **kw):
    take, _ = simulate(scores, every_n, **kw)
    n = len(scores)
    rate = len(take) / max(n, 1)
    # 均匀性：按时间切 6 窗，看最忙窗 / 最闲窗。
    # 分母不能直接用 min(w) —— 大部分窗是 0 会把比值炸成天文数字，而
    # "只有 1 帧触发" 本来就是好事。改用「最忙窗 vs 全局平均」来衡量，
    # 再单列「各窗触发数的极差」更直观。
    w = [len(take[k * n // 6:(k + 1) * n // 6]) / max(n // 6, 1)
         for k in range(6)]
    unif = max(w) / max(rate, 1e-9)          # 1.0 = 每窗都均匀
    counts = [int(round(x * n / 6)) for x in w]
    iv = [take[i + 1] - take[i] for i in range(len(take) - 1)]
    p50 = int(np.percentile(iv, 50)) if iv else -1
    return rate, unif, p50, counts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rtsp")
    ap.add_argument("--file", dest="file")
    ap.add_argument("--frames", type=int, default=900)
    ap.add_argument("--every-n", type=int, default=300)
    a = ap.parse_args()
    src = a.file or a.rtsp
    if not src:
        ap.error("需要 --rtsp 或 --file")

    F = load(src, a.frames)
    if F is None or len(F) < 200:
        print("解码失败 / 帧数不足")
        return 1
    N = len(F)
    print(f"抓到 {N} 帧 {F.shape[1]}x{F.shape[2]}  every_n={a.every_n}")
    print(f"底噪成因（推测）：{SCENARIO[0]}\n")

    S = {
        "conc": np.array([m_conc(F[i], F[i - 1]) for i in range(1, N)]),
        "gscore": np.array([m_gscore(F[i], F[i - 1]) for i in range(1, N)]),
    }

    for mname, sc in S.items():
        q = np.percentile(sc, [50, 95, 99])
        print(f"  {mname:<7} 真实相邻帧 p50={q[0]:.4f} p95={q[1]:.4f} p99={q[2]:.4f}")
        # 分 6 窗看 p95 —— 这是"在飘"的直接证据
        w95 = [float(np.percentile(sc[k * len(sc) // 6:(k + 1) * len(sc) // 6], 95))
               for k in range(6)]
        print(f"          各窗 p95 = {' '.join(f'{x:.4f}' for x in w95)}"
              f"   波动 {max(w95)/max(min(w95),1e-9):.1f}x")
    print()

    print(f"== 触发率对比（相邻帧被判'变了'的比例；真实相邻帧**全部**是误报）==\n")
    print(f"  {'配置':<26}{'触发率':>9}{'触发帧':>7}{'偏斜':>8}{'间隔p50':>9}   各窗触发帧数")
    print("  " + "-" * 84)
    for mname, sc in S.items():
        for T in (1.0, 2.0, 5.0, 10.0, 15.0, 30.0):
            r, u, p50, cnt = report(mname, sc, a.every_n, fixed=T)
            print(f"  {mname + ' 固定 ' + f'{T:g}':<26}{r*100:8.2f}%"
                  f"{len(cnt)*0+int(r*len(sc)):7d}{u:7.2f}x{p50:8d}   "
                  + " ".join(f"{x}" for x in cnt))
        for k in (1.5, 2.0, 3.0, 5.0):
            r, u, p50, cnt = report(mname, sc, a.every_n, adaptive_k=k)
            print(f"  {mname + ' 自适应 k=' + f'{k:g}':<26}{r*100:8.2f}%"
                  f"{int(r*len(sc)):7d}{u:7.2f}x{p50:8d}   "
                  + " ".join(f"{x}" for x in cnt))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
