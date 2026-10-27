#!/usr/bin/env python3
# coding=utf-8
"""只替换 SDK 里那张参考音色表（其余改动已就位）。

用法: python replace_sdk_table.py --sdk <tts.py> --blob <blob.py>
"""
import argparse
import base64
import re
import sys
import zlib
from pathlib import Path

import numpy as np

START = "_BUNDLED_SPEAKER_EMBEDDINGS_B64: dict[str, tuple[str, ...]] = {"
DEFAULT_RE = re.compile(
    r"DEFAULT_REF_SPEAKER: str = next\(iter\(_BUNDLED_SPEAKER_EMBEDDINGS_B64\), \"\"\)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sdk", required=True)
    ap.add_argument("--blob", required=True)
    args = ap.parse_args()

    sdk = Path(args.sdk)
    src = sdk.read_text(encoding="utf-8")
    blob = Path(args.blob).read_text(encoding="utf-8").rstrip("\n")
    i = src.index(START)
    j = src.index("\n}\n", i) + len("\n}")
    new = blob          # blob 本身就是完整的字典块（含闭合括号）
    backup = sdk.with_suffix(sdk.suffix + ".bak_table")
    backup.write_text(src, encoding="utf-8")
    sdk.write_text(src[:i] + new + src[j:], encoding="utf-8")

    # 写坏 SDK 文件是最坏的结果 —— 下面任何一项不过关就回滚。
    import py_compile
    try:
        py_compile.compile(str(sdk), doraise=True)
    except py_compile.PyCompileError as e:
        backup.replace(sdk)
        print(f"❌ 替换后语法错误，已回滚：{e}", file=sys.stderr)
        return 1

    try:
        # 自检：新表能解码、维度和条数对得上、默认音色从表里派生
        ns: dict = {}
        exec(new, ns)
        tbl = ns["_BUNDLED_SPEAKER_EMBEDDINGS_B64"]
        assert len(tbl) == 6, f"条目数应为 6，实得 {len(tbl)}"
        for name, chunks in tbl.items():
            v = np.frombuffer(zlib.decompress(base64.b64decode("".join(chunks))),
                              dtype=np.float16).astype(np.float32)
            assert v.shape[0] == 2048, f"{name} 维度 {v.shape[0]}"
            assert bool(np.all(np.isfinite(v))), f"{name} 含 NaN/Inf"
        text = sdk.read_text(encoding="utf-8")
        m = DEFAULT_RE.search(text)
        assert m, "SDK 里没有「从表里派生」的 DEFAULT_REF_SPEAKER"
    except Exception as e:
        backup.replace(sdk)
        print(f"❌ 自检不过，已回滚：{e}", file=sys.stderr)
        return 1
    print(f"✅ 已替换 SDK 表：{len(tbl)} 条 → {', '.join(tbl)}")
    print(f"   默认音色 = 表的第一条 = {next(iter(tbl))}")
    print(f"   norm: " + ", ".join(
        f"{k}={float(np.linalg.norm(np.frombuffer(zlib.decompress(base64.b64decode(''.join(v))), dtype=np.float16).astype(np.float32))):.4f}"
        for k, v in tbl.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
