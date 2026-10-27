#!/usr/bin/env python3
# coding=utf-8
"""校验板端 SDK 内嵌表 == ref_voices/out/*.npy（部署一致性自检）。"""
import sys

import numpy as np

sys.path.insert(0, "/home/rockchip/work/wdnwebui/.venv/src/wdnai/src")
from wdnai.backends.tts import _get_bundled_speaker_embeddings  # noqa: E402

OUT = "/home/rockchip/ref_voices/out"
sdk = _get_bundled_speaker_embeddings()
ok = True
for name, vec in sdk.items():
    npy = np.load(f"{OUT}/{name}.npy").astype(np.float32)
    d = float(np.abs(npy - vec.astype(np.float32)).max())
    mark = "OK" if d < 1e-6 else "MISMATCH"
    print(f"  {name:<16} maxdiff={d:.3e}  {mark}")
    ok = ok and d < 1e-6
print("  ->", "SDK 表与 out/ 完全一致" if ok else "不一致")
sys.exit(0 if ok else 1)
