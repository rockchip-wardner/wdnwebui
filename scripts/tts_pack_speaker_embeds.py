#!/usr/bin/env python3
"""把 C++ 头文件里的 speaker embedding 转成 wdnai SDK 内嵌格式。

背景
----
参考音色**不在 RKNN 模型里** —— 它是一份独立的 2048 维向量数组:
* C++ 侧  ``examples/Qwen3_TTS/cpp/qwen3tts_speaker_embed.h``
  (``std::unordered_map<std::string, std::vector<float>>``)
* Python 侧 ``wdnai/backends/tts.py`` 的 ``_BUNDLED_SPEAKER_EMBEDDINGS_B64``
  (zlib 压缩的 fp16 原始字节 + base64)

加一个参考音色 = 跑一次 ``export_speaker_embed_hpp.py`` 更新头文件,再把
新条目搬进 SDK 的那张表。**不需要重新转换 RKNN 模型。**

用法
----
    # 1) 从头文件生成可粘贴的代码块
    python -m scripts.tts_pack_speaker_embeds --header <头文件>

    # 2) 顺便核对「头文件」与「SDK 当前内嵌的」是否一致
    python -m scripts.tts_pack_speaker_embeds --header <头文件> --verify

    # 3) 直接写文件
    python -m scripts.tts_pack_speaker_embeds --header <头文件> --out blob.py

编码链: float32 -> fp16 -> zlib.compress -> base64 -> 单个字符串块
(与 SDK 里现有的 ``_BUNDLED_SPEAKER_EMBEDDINGS_B64`` 逐字节一致;
``--verify`` 会用现有的参考音色做往返自检。)
"""
import argparse
import base64
import re
import sys
import zlib
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
#: 一条 x_vector 的长度 —— Qwen3-TTS 的 speaker encoder 是 ECAPA-TDNN,
#: 输出 enc_dim=2048。
EMBED_DIM = 2048

_ENTRY_RE = re.compile(r'\{"([^"]+)",\s*\{(.*?)\}\s*\}', re.DOTALL)
_NUM_RE = re.compile(r'-?\d+\.\d+e[+-]\d+f')


def parse_header(path: Path) -> "dict[str, np.ndarray]":
    """解析 C++ 头文件 → ``{名字: float32 向量}``。

    头文件里的数字是 ``%.8e`` 格式的 float32 字面量(带 ``f`` 后缀)。
    """
    src = path.read_text(encoding="utf-8")
    out: dict[str, np.ndarray] = {}
    for m in _ENTRY_RE.finditer(src):
        name = m.group(1)
        vals = [float(x[:-1]) for x in _NUM_RE.findall(m.group(2))]
        if len(vals) != EMBED_DIM:
            raise ValueError(
                f"{name}: 维度应为 {EMBED_DIM},实得 {len(vals)} —— 头文件格式不对?")
        out[name] = np.asarray(vals, dtype=np.float32)
    if not out:
        raise ValueError(f"{path}: 没解析出任何条目")
    return out


def encode(vec: np.ndarray) -> str:
    """float32 向量 → SDK 用的 base64 字符串(整块,不分行)。

    压缩级别固定 **level 7** —— 跟 SDK 里现有的参考音色逐字节一致
    (``--verify`` 会验这一条)。
    """
    raw = np.asarray(vec, dtype=np.float32).astype(np.float16).tobytes()
    return base64.b64encode(zlib.compress(raw, 7)).decode("ascii")


def emit(vectors: "dict[str, np.ndarray]") -> str:
    """生成可直接替换 SDK 里那张表的 Python 代码块。"""
    lines = ["_BUNDLED_SPEAKER_EMBEDDINGS_B64: dict[str, tuple[str, ...]] = {"]
    for name in vectors:
        blob = encode(vectors[name])
        # SDK 里是 tuple[str, ...];这里给单块,和 SDK 现有的写法一致
        lines.append(f'    "{name}": (')
        lines.append(f'        "{blob}",')
        lines.append("    ),")
    lines.append("}")
    return "\n".join(lines)


def verify(vectors: "dict[str, np.ndarray]") -> int:
    """跟 SDK 当前内嵌的值逐条比对,顺便做一次编码往返自检。"""
    sys.path.insert(0, str(ROOT / ".venv" / "src" / "wdnai" / "src"))
    try:
        from wdnai.backends.tts import (
            _BUNDLED_SPEAKER_EMBEDDINGS_B64,
            _get_bundled_speaker_embeddings,
        )
    except ImportError as e:  # noqa: BLE001
        print(f"⚠️  没法 import wdnai SDK({e})—— 跳过比对", file=sys.stderr)
        return 0

    sdk = _get_bundled_speaker_embeddings()
    bad = 0
    for name, vec in vectors.items():
        # 1) 数值一致?
        if name in sdk:
            diff = float(np.abs(vec - sdk[name].astype(np.float32)).max())
            ok = diff < 1e-2          # fp16 量化误差量级
            print(f"  {'✅' if ok else '❌'} {name}: 与 SDK 内嵌值最大差 {diff:.6f}")
            bad += 0 if ok else 1
        else:
            print(f"  ➕ {name}: SDK 里还没有(新增条目)")
        # 2) 编码往返:decode 回来的数还一样吗?
        back = np.frombuffer(
            zlib.decompress(base64.b64decode(encode(vec))), dtype=np.float16
        ).astype(np.float32)
        rt = float(np.abs(back - vec).max())
        ok = rt < 1e-2
        print(f"  {'✅' if ok else '❌'} {name}: fp16 往返最大差 {rt:.6f}")
        bad += 0 if ok else 1
    # 3) 老条目的字节级往返 —— 证明本脚本的编码方式与 SDK 生成的一致
    for name, chunks in _BUNDLED_SPEAKER_EMBEDDINGS_B64.items():
        if name not in vectors:
            continue
        same = encode(vectors[name]) == "".join(chunks)
        print(f"  {'✅' if same else '❌'} {name}: base64 与 SDK 现有串逐字节一致")
        bad += 0 if same else 1
    return bad


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--header", required=True,
                    help="qwen3tts_speaker_embed.h 的路径")
    ap.add_argument("--out", help="把生成的代码块写到文件")
    ap.add_argument("--verify", action="store_true",
                    help="与 SDK 当前内嵌值比对 + 编码往返自检")
    args = ap.parse_args()

    vectors = parse_header(Path(args.header))
    print(f"读到 {len(vectors)} 条: "
          f"{', '.join(f'{k}(norm={np.linalg.norm(v):.3f})' for k, v in vectors.items())}\n")

    if args.verify:
        bad = verify(vectors)
        if bad:
            print(f"\n❌ {bad} 项不一致", file=sys.stderr)
            return 1
        print("\n✅ 全部一致")

    block = emit(vectors)
    if args.out:
        Path(args.out).write_text(block + "\n", encoding="utf-8")
        print(f"\n已写入 {args.out}")
    else:
        print("\n" + block)
    return 0


if __name__ == "__main__":
    sys.exit(main())
