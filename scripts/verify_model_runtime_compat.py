#!/usr/bin/env python3
"""门禁：模型包用 **rknn3-toolkit X** 构建 ⇒ 本板 runtime 必须 ≥ X。

背景（2026-11-18 真机现场）
--------------------------
网页翻译的翻译模型 ``hy-mt-1-5`` 装不上：worker 起来就 abort，
``rc=-6``，日志里只有一句

    E RKNNAPI: MODEL_SETUP fail, ack = ACK_FAIL, expect = ACK_SUCC!

看起来像「显存不够」或「卡被占着」，其实**都不是**：

* 卡是空的（5120MB 只用了 36MB）；
* 单起一份也一样失败（不是并发抢卡）；
* 模型文件 sha256 与 HF 上游**逐字节一致**（没损坏）。

真正的原因是**版本错配**：

============================  ==========================
模型 ``HY-MT1.5-1.8B.1820.rknn``  rknn3-toolkit **1.2.0**（2026-11-02 构建）
本板 ``/usr/lib/librknn3_api.so``  librknn3 **1.1.0**（2026-08-13 构建）
============================  ==========================

⇒ 用 1.2.0 编译出来的模型，本板 1.1.0 的 runtime/固件不认 ⇒
MODEL_SETUP 失败。本板其它模型（toolkit 1.0.4 / 1.1.0）全都正常加载，
**只有 HY-MT 是 1.2.0** —— 它是唯一的例外，也就是唯一的受害者。

两条修法（本脚本只负责把话说清楚，不动系统）：

1. **升板端 runtime + EP 固件** 到 ≥ 1.2.0（Rockchip 的 whl / rknn3_rk1820.img）；
2. **用 ≤ 1.1.0 的 toolkit 重编** HY-MT 并重新分发。

⇒ 这条门禁的价值：把「起不来」在**装模型之前**就拦下来，别让用户对着
rc=-6 猜半天。

用法::

    .venv/bin/python scripts/verify_model_runtime_compat.py
    .venv/bin/python scripts/verify_model_runtime_compat.py --model hy-mt-1-5
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import ssl
import sys
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

#: 本板 rknn3 runtime 的候选路径（发行版不同位置不同）
RUNTIME_GLOBS = (
    "/usr/lib/librknn3_api.so",
    "/usr/lib64/librknn3_api.so",
    "/lib/librknn3_api.so",
    "/usr/local/lib/librknn3_api.so",
)

#: 模型文件里的 toolkit 版本标识
TOOLKIT_RE = re.compile(rb"rknn3-toolkit (\d+)\.(\d+)\.(\d+)")
#: runtime .so 里的版本标识
RUNTIME_RE = re.compile(rb"librknn3_api version: (\d+)\.(\d+)\.(\d+)")


def _ver(b: tuple) -> tuple:
    return tuple(int(x) for x in b)


def find_runtime() -> tuple:
    """本板 rknn3 runtime 版本（读不到返回 (0,0,0)）。"""
    for path in RUNTIME_GLOBS:
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "rb") as f:
                blob = f.read()
        except OSError:
            continue
        m = RUNTIME_RE.search(blob)
        if m:
            return _ver(m.groups()), path
    return (0, 0, 0), ""


def toolkit_of(path: str) -> tuple:
    """一个 .rknn 文件的构建 toolkit 版本。"""
    try:
        with open(path, "rb") as f:
            blob = f.read()
    except OSError:
        return (0, 0, 0)
    m = TOOLKIT_RE.search(blob)
    return _ver(m.groups()) if m else (0, 0, 0)


def models_root() -> str:
    """本板模型根（问后端拿，别写死）。"""
    base = os.environ.get("WDNWEBUI_BASE", "https://127.0.0.1:8000").rstrip("/")
    try:
        ctx = ssl._create_unverified_context()
        with urllib.request.urlopen(base + "/api/compute", timeout=15,
                                    context=ctx) as r:
            return json.loads(r.read().decode("utf-8")).get("models_root") or ""
    except Exception:  # noqa: BLE001
        return "/userdata/models"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="",
                    help="只看某个模型目录（默认全盘扫）")
    args = ap.parse_args()

    runtime, rpath = find_runtime()
    root = models_root()

    print("=" * 72)
    print("门禁：模型 toolkit 版本 ≤ 本板 rknn3 runtime 版本")
    print("=" * 72)
    print(f"  本板 runtime : {runtime[0]}.{runtime[1]}.{runtime[2]}"
          f"   ({rpath or '未找到'})")
    print(f"  模型根       : {root}")

    if runtime == (0, 0, 0):
        print("\n❌ 读不到本板 rknn3 runtime 版本 —— 无法判定")
        return 1

    bad = []
    rows = []
    for d in sorted(glob.glob(os.path.join(root, "*"))):
        if not os.path.isdir(d):
            continue
        name = os.path.basename(d)
        if args.model and args.model.lower() not in name.lower():
            continue
        vers = set()
        for rknn in glob.glob(os.path.join(d, "*.rknn")):
            v = toolkit_of(rknn)
            if v != (0, 0, 0):
                vers.add(v)
        if not vers:
            continue
        top = max(vers)
        ok = top <= runtime
        rows.append((name, top, ok))
        if not ok:
            bad.append((name, top))

    print()
    for name, top, ok in rows:
        mark = "ok  " if ok else "FAIL"
        print(f"  {mark} {name:28s} toolkit {top[0]}.{top[1]}.{top[2]}"
              f"  {'≤ runtime ✓' if ok else '> runtime ✗（装不上，会 rc=-6）'}")

    if not rows:
        print("  （没扫到 .rknn 文件）")

    print("\n" + "=" * 72)
    if bad:
        print("❌ 以下模型的构建 toolkit 比本板 runtime 新 —— 它们会 MODEL_SETUP fail：")
        for name, top in bad:
            print(f"   · {name}  (toolkit {top[0]}.{top[1]}.{top[2]} "
                  f"> runtime {runtime[0]}.{runtime[1]}.{runtime[2]})")
        print("\n   修法：① 升板端 rknn3 runtime + EP 固件；"
              "或 ② 用 ≤ 本板 runtime 的 toolkit 重编该模型再分发。")
        return 1
    print("✅ 所有模型的 toolkit 版本都不高于本板 runtime")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
