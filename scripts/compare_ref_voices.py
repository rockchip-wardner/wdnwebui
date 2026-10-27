#!/usr/bin/env python3
# coding=utf-8
"""参考音色两两对比：判断「有没有两个名字一个人」。

不能只看一个 cosine：ECAPA-TDNN 的 x_vector 有巨大的公共分量，单维可以支配
整体范数与 cosine（实测 d1897 一维占两个参考音色整体 cosine 的 84.1%）。

所以每个 pair 报三项：
  ① 剔除主导维后的**逐维符号一致率**（不同说话人的典型值 ≈ 55%，随机基线 50%）
  ② 剔除主导维后的**余弦**
  ③ **基频**差（最硬的证据）

用法:
    .venv/bin/python -u scripts/compare_ref_voices.py --dir /home/rockchip/ref_voices/out
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

#: 主导维判定：|值| 超过该维绝对值中位数的这个倍数就算「主导维」
DOMINANT_RATIO = 5.0


def load_vectors(d: Path) -> dict:
    out = {}
    for p in sorted(d.glob("*.npy")):
        out[p.stem] = np.load(str(p)).astype(np.float32).reshape(-1)
    return out


def dominant_dim(v: np.ndarray) -> int:
    """返回「主导维」下标（|值| 远超其他维的那一维）。

    主导维的典型特征：绝对值远大于中位数 —— 说话人无关的系统性偏置。
    """
    a = np.abs(v)
    med = float(np.median(a))
    thr = med * DOMINANT_RATIO
    idx = int(np.argmax(a))
    if a[idx] < thr:
        return -1
    return idx


def pair_report(name_a: str, a: np.ndarray, name_b: str, b: np.ndarray) -> dict:
    da, db = dominant_dim(a), dominant_dim(b)
    keep = np.ones(a.shape[0], dtype=bool)
    drop = []
    for d in (da, db):
        if d >= 0:
            keep[d] = False
            drop.append(d)
    ka, kb = a[keep], b[keep]
    # 逐维符号一致率（符号为 0 的维按 + 算，x_vector 里几乎没有精确 0）
    sign_agree = float(np.mean(np.sign(ka) == np.sign(kb)))
    cos_all = float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-12))
    cos_kept = float(np.dot(ka, kb) / (np.linalg.norm(ka) * np.linalg.norm(kb) + 1e-12))
    rel = np.abs(ka - kb) / (np.abs(ka) + np.abs(kb) + 1e-9)
    return {
        "a": name_a,
        "b": name_b,
        "cosine_all": round(cos_all, 4),
        "cosine_kept": round(cos_kept, 4),
        "sign_agreement_kept": round(sign_agree, 4),
        "rel_diff_median_kept": round(float(np.median(rel)), 4),
        "dropped_dims": drop,
        "dominant_a": {"dim": da, "val": round(float(a[da]), 3) if da >= 0 else None},
        "dominant_b": {"dim": db, "val": round(float(b[db]), 3) if db >= 0 else None},
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dir", required=True, help="x-vector 目录")
    ap.add_argument("--json-out", default="", help="把结果写成 JSON")
    args = ap.parse_args()

    d = Path(args.dir)
    vecs = load_vectors(d)
    if not vecs:
        print(f"❌ {d} 里没有 .npy")
        return 1

    print(f"读到 {len(vecs)} 个音色: {', '.join(vecs)}\n")

    metas = {}
    for name in vecs:
        mp = d / f"{name}.meta.json"
        if mp.is_file():
            m = json.loads(mp.read_text(encoding="utf-8"))
            metas[name] = m
            print(f"  {name:<16} F0 {m.get('f0_median', 0):>6.1f} Hz  "
                  f"norm {m.get('embed_norm', 0):>7.3f}  "
                  f"时长 {m.get('duration_s', 0):>5.2f}s  "
                  f"裁静音 {m.get('lead_silence_trimmed_s', 0):.2f}s")

    names = list(vecs)
    print("\n成对对比（剔除主导维后的符号一致率 / 余弦）")
    print(f"{'A':<16}{'B':<16}{'cos全部':>9}{'cos剔除':>9}{'符号一致':>10}"
          f"{'ΔF0':>9}{'判定':>10}")
    rows = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            na, nb = names[i], names[j]
            r = pair_report(na, vecs[na], nb, vecs[nb])
            fa = metas.get(na, {}).get("f0_median", 0.0)
            fb = metas.get(nb, {}).get("f0_median", 0.0)
            df0 = abs(fa - fb) if (fa and fb) else 0.0
            # §6.3：F0 中位数差 < 10 Hz = 疑似同一人
            verdict = "疑似同一人" if df0 < 10.0 else "可分"
            r["f0_a"] = fa
            r["f0_b"] = fb
            r["f0_diff"] = round(df0, 1)
            r["verdict"] = verdict
            rows.append(r)
            print(f"{na:<16}{nb:<16}{r['cosine_all']:>9.4f}{r['cosine_kept']:>9.4f}"
                  f"{r['sign_agreement_kept']*100:>9.1f}%{df0:>9.1f}{verdict:>12}")

    # 按符号一致率排序：最低的几个 pair 最可能是「同一个人」
    rows.sort(key=lambda r: r["sign_agreement_kept"])
    print("\n最像「同一人」的 3 对（符号一致率最低）：")
    for r in rows[:3]:
        print(f"  {r['a']} ↔ {r['b']}: 符号一致 {r['sign_agreement_kept']*100:.1f}%  "
              f"cos(剔除) {r['cosine_kept']:.4f}  ΔF0 {r['f0_diff']:.1f} Hz  "
              f"→ {r['verdict']}")

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写入 {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
