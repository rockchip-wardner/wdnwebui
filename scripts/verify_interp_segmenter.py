#!/usr/bin/env python3
"""回归：同传的**快档切句器**（``StablePrefixSegmenter``）。

契约
====
1. LCP 变长 ⇒ 候选更新，稳定计数清零（**不成句**）；
2. LCP 连续 N 轮「长度与内容都不变」⇒ 成句；
3. LCP **变短**（模型改写）⇒ 作废候选，从头再来；
4. 低于 ``INTERP_MIN_CHARS`` 不成句；
5. 超过 ``INTERP_MAX_CHARS`` 的部分被截断。

跑法::

    .venv/bin/python scripts/verify_interp_segmenter.py
"""
from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from wdnapps_audio_chat.cards.qwen3_asr.interp import (  # noqa: E402
    INTERP_MAX_CHARS,
    INTERP_MIN_CHARS,
    INTERP_STABLE_ROUNDS,
    StablePrefixSegmenter,
)

_PASS = 0
_FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  ok   {name}")
    else:
        _FAIL += 1
        print(f"  FAIL {name} {extra}")


def main() -> int:
    print("== 快档切句器 ==")

    # 1. 一路增长 ⇒ 不成句
    seg = StablePrefixSegmenter()
    out = None
    for t in ("今天天气真的不错", "今天天气真的不错", "今天天气真的很好啊", "今天天气真的很好啊我们"):
        out = seg.feed(t)
        check(f"增长中不成句（{t}）", out is None, f"得到 {out!r}")

    # 2. 连续 N 轮不变 ⇒ 成句
    seg = StablePrefixSegmenter()
    seg.feed("今天天气真的很好啊")
    seg.feed("今天天气真的很好啊")
    got = seg.feed("今天天气真的很好啊")
    check(f"连续 {INTERP_STABLE_ROUNDS} 轮不变 ⇒ 成句",
          got == "今天天气真的很好啊", f"得到 {got!r}")

    # 3. 成句后候选清空 ⇒ 下一句要重新攒
    seg = StablePrefixSegmenter()
    seg.feed("今天天气真的很好啊")
    seg.feed("今天天气真的很好啊")
    got = seg.feed("今天天气真的很好啊")
    check("成句后不再重复成句", seg.feed("今天天气真的很好啊") is None)

    # 4. 模型改写（LCP 变短）⇒ 作废候选
    seg = StablePrefixSegmenter()
    seg.feed("今天天气真的很好啊")
    seg.feed("今天天气真的很好啊")
    got = seg.feed("今天天气不错呀天气不")     # 改写了
    check("改写 ⇒ 作废候选（不成句）", got is None, f"得到 {got!r}")

    # 5. 空文本（退化 / 回看）⇒ 作废
    seg = StablePrefixSegmenter()
    seg.feed("今天天气真的很好啊")
    check("空文本 ⇒ 作废候选", seg.feed("") is None)

    # 6. 太短 ⇒ 不成句
    seg = StablePrefixSegmenter()
    seg.feed("嗯")
    seg.feed("嗯")
    got = seg.feed("嗯")
    check(f"短于 {INTERP_MIN_CHARS} 字不成句", got is None, f"得到 {got!r}")

    # 7. 超长 ⇒ 截断到 INTERP_MAX_CHARS
    seg = StablePrefixSegmenter()
    long_text = "啊" * (INTERP_MAX_CHARS + 30)
    seg.feed(long_text)
    seg.feed(long_text)
    got = seg.feed(long_text)
    check(f"超长截断到 {INTERP_MAX_CHARS}",
          got is not None and len(got) == INTERP_MAX_CHARS,
          f"得到 {got!r}")

    # 8. 稳定计数：内容变了（变长）⇒ 计数清零
    seg = StablePrefixSegmenter()
    seg.feed("今天天气真的很好啊")
    seg.feed("今天天气真的很好啊")
    seg.feed("今天天气不错呀天气很好呀")     # 变长 ⇒ 计数清零
    got = seg.feed("今天天气不错呀天气很好呀")
    check("变长 ⇒ 稳定计数清零（还要再等一轮）", got is None, f"得到 {got!r}")

    print(f"\n通过 {_PASS} / 失败 {_FAIL}")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
