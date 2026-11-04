#!/usr/bin/env python3
"""解析 rknn-toolkit2 构建日志里的**逐算子表**，统计 CPU 回落 / NPU 算力分配。

rknn-toolkit2 2.5.0 在 build() 末尾会打一张算子表（每行一个算子）::

    D RKNN: [00:27:15.483] 21   Transpose  FLOAT16  CPU    (1,3456,1,1024)  (1,1,1024,3456)  0/0/0  6912   Transpose:node_permute_1
    ^                                     ^          ^        ^   ^                            ^        ^      ^
    日志前缀                               序号       算子类型  dtype  target                      形状      周期    名字

「周期」这一列是工具链自己的**成本模型**（不是实测 ns），
用来比较不同图 / 不同配置的**相对**开销。

用法
----
    # 解析一份构建日志
    python3 op_table.py build_c2_ah09999.log

    # 对比两份（手术前 vs 手术后）
    python3 op_table.py --diff old.log new.log

日志可以是从 119 拉回来的，也可以直接在 119 上跑::

    ssh toybrick@10.10.10.119 'cd /datasets/Qwen-Image-2.1/rknn2_vae && \\
        tr "\\r" "\\n" < logs/build_c2_ah09999.log' > build_c2_ah09999.log
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict

#: 一行算子表：前缀 + 序号 + 算子类型 + dtype + target(CPU/NPU) + 中间列 + 周期 + 名字
_ROW = re.compile(
    r"RKNN: \[[0-9:.]+\]\s+(\d+)\s+(\S+)\s+(\S+)\s+(CPU|NPU)\b(.*?)(\d+)\s+(\S+)\s*$"
)


def parse(path: str) -> list[tuple[int, str, str, str, int, str]]:
    """返回 [(序号, 算子类型, dtype, target, 周期, 名字), ...]"""
    rows = []
    with open(path, errors="ignore") as f:
        for line in f:
            m = _ROW.search(line.rstrip("\n"))
            if not m:
                continue
            idx, op, dtype, target, _mid, cyc, name = m.groups()
            rows.append((int(idx), op, dtype, target, int(cyc), name.strip()))
    return rows


def report(tag: str, rows: list) -> dict:
    by_target = defaultdict(list)
    for r in rows:
        by_target[r[3]].append(r)

    cpu_cycles = sum(r[4] for r in by_target.get("CPU", []))
    total = sum(r[4] for r in rows)

    print("=== %s：%d 个算子（CPU %d / NPU %d）===" %
          (tag, len(rows), len(by_target.get("CPU", [])), len(by_target.get("NPU", []))))
    print("    CPU 回落周期 %.3f M / 全图 %.3f M  = %.1f%%" %
          (cpu_cycles / 1e6, total / 1e6, 100.0 * cpu_cycles / total if total else 0.0))

    print("    CPU 回落算子（按周期降序）：")
    for r in sorted(by_target.get("CPU", []), key=lambda x: -x[4]):
        if r[4] == 0 and r[1] in ("InputOperator", "OutputOperator"):
            continue
        print("      #%-4d %-16s cycles=%-8d %s" % (r[0], r[1], r[4], r[5]))

    kinds = defaultdict(lambda: [0, 0])
    for r in by_target.get("CPU", []):
        kinds[r[1]][0] += 1
        kinds[r[1]][1] += r[4]
    print("    CPU 算子类型统计：")
    for k, (n, cy) in sorted(kinds.items(), key=lambda kv: -kv[1][1]):
        print("      %-18s %2d 个  %9d 周期 (%.3f M)" % (k, n, cy, cy / 1e6))
    print()

    return {
        "tag": tag,
        "n_ops": len(rows),
        "n_cpu": len(by_target.get("CPU", [])),
        "n_npu": len(by_target.get("NPU", [])),
        "cpu_cycles": cpu_cycles,
        "total_cycles": total,
        "cpu_share_pct": 100.0 * cpu_cycles / total if total else 0.0,
    }


def scaling_fit(t1: float, t2: float, t3: float) -> None:
    """两参数标度拟合 t(k) = A/k + B —— 回答「为什么多核给不了线性加速」。

    A = 可被多核并行的部分（1 核口径）；B = 不随核数变的固定开销。
    """
    a = 2.0 * (t1 - t2)
    b = t1 - a
    pred3 = a / 3.0 + b
    print("=== 核数标度拟合 t(k) = A/k + B ===")
    print("    实测：1 核 %.1f ms / 2 核 %.1f ms / 3 核 %.1f ms" % (t1, t2, t3))
    print("    A（可并行部分，1 核口径）= %.0f ms（占 %.1f%%）" % (a, 100.0 * a / t1))
    print("    B（固定开销）            = %.0f ms（占 %.1f%%）" % (b, 100.0 * b / t1))
    print("    预测 t(3) = %.0f ms vs 实测 %.0f ms，偏差 %.1f%%" %
          (pred3, t3, 100.0 * abs(pred3 - t3) / t3))
    print("    3 核加速比 = %.3fx（理论上限 3.0x）" % (t1 / t3))
    print("    ⇒ 固定开销的那部分才是瓶颈：NPU 算子间的串行依赖，"
          "或 CPU 回落算子前后的数据搬运停顿。")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("log", nargs="+", help="构建日志（可给多份做对比）")
    ap.add_argument("--diff", action="store_true",
                    help="把两份日志并排对比（手术前 vs 手术后）")
    ap.add_argument("--scaling", nargs=3, type=float, metavar=("T1", "T2", "T3"),
                    help="板端 1/2/3 核的单次耗时（ms），做标度拟合")
    args = ap.parse_args()

    summaries = []
    for path in args.log:
        rows = parse(path)
        if not rows:
            print("⚠️ %s 里没解析出算子表（是不是把 \\r 换行去掉了？）" % path,
                  file=sys.stderr)
            continue
        summaries.append(report(path, rows))

    if args.diff and len(summaries) >= 2:
        a, b = summaries[0], summaries[1]
        print("=== 对比：%s → %s ===" % (a["tag"], b["tag"]))
        print("    算子总数   %d → %d   (%+d)" % (a["n_ops"], b["n_ops"], b["n_ops"] - a["n_ops"]))
        print("    CPU 回落   %d → %d   (%+d)" % (a["n_cpu"], b["n_cpu"], b["n_cpu"] - a["n_cpu"]))
        print("    CPU 周期   %.3f M → %.3f M  (%.1f%%)" %
              (a["cpu_cycles"] / 1e6, b["cpu_cycles"] / 1e6,
               100.0 * (b["cpu_cycles"] - a["cpu_cycles"]) / a["cpu_cycles"]))
        print("    CPU 占比   %.1f%% → %.1f%%" % (a["cpu_share_pct"], b["cpu_share_pct"]))
        print()

    if args.scaling:
        scaling_fit(*args.scaling)

    return 0


if __name__ == "__main__":
    sys.exit(main())
