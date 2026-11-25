#!/usr/bin/env python3
"""回归：同传的**快档切句器**（``_FastCommittedSegmenter``）。

为什么换掉了老切句器
====================
老切句器 ``StablePrefixSegmenter`` 看的是 ``unfixed`` —— 那是 SDK 回退窗口的
**滑动尾巴**（``wdnai/backends/asr.py``：``new_unfixed =
round_tokens[stable_count:]`` ⇒ 每轮只保留最后 ``rollback_tokens`` 个 token）
⇒ **连续说话时每轮都在滑** ⇒ 「这一轮文本没变」的判据**永远不成立**
⇒ 连续讲话时**一句都切不出来**（实测 2026-11-25：15 s 连续讲话里前 14.34 s
零句 ⇒ 首声 17.77 s，比稳档的 8.6 s 慢一倍）。

⇒ 新切句器看 **committed 增量**（已确认文本，单调增长）：
「这一轮 committed 没变长」= **模型停了**（停顿）⇒ 切尾巴；
「增长到某个标点」= 子句完整 ⇒ 立即切。

契约
====
1. **子句标点**（，；：,;:）⇒ 立即成句（**比稳档早** ⇒ 快档的意义）；
2. **句末标点** ⇒ 立即成句；
3. **顿号「、」不切**（并列成分不能拆成独立句子）；
4. 碎片（< ``min_chars``）⇒ **不切、不丢** ⇒ 等下一轮带长后整体成句；
5. **停顿**（连续 ``pause_rounds`` 轮 committed 没增长）⇒ 切尾巴；
6. committed 重新增长 ⇒ 停顿计数清零；
7. **长度兜底** ⇒ 切在 ≤ ``max_chars`` 且**不劈词**（英文按词边界）；
8. **epoch 切换**（committed 变短）⇒ 旧尾巴被 flush，**不丢内容**；
9. 一轮里攒下好几句 ⇒ **循环**切出多句（不是一刀切一句）；
10. ``flush()`` ⇒ 尾巴交出。

跑法::

    .venv/bin/python scripts/verify_interp_segmenter.py
"""
from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from wdnapps_audio_chat.cards.qwen3_asr.service import (  # noqa: E402
    _FAST_CLAUSE_END,
    _FAST_MAX_CHARS,
    _FAST_MIN_CHARS,
    _FAST_PAUSE_ROUNDS,
    _FastCommittedSegmenter,
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


def collect(rounds, seg=None):
    """喂若干轮 ``committed``，返回切出来的句子列表。"""
    seg = seg or _FastCommittedSegmenter()
    out: list[tuple[int, str]] = []
    for c in rounds:
        seg.feed(c, lambda t, i, c0, c1: out.append((i, t)))
    return out


def main() -> int:
    print("== 快档切句器（committed 增量） ==")

    # ------------------------------------------------------------------ #
    # ① 核心修复：连续说话（committed 每轮增长）⇒ **不等停顿就出句**
    # ------------------------------------------------------------------ #
    en = [
        "He wasn't even that big when I started listening to him,",
        "He wasn't even that big when I started listening to him, but he didn't do",
        "He wasn't even that big when I started listening to him, but he didn't do "
        "overly well in his solo music,",
        "He wasn't even that big when I started listening to him, but he didn't do "
        "overly well in his solo music, didn't he?",
    ]
    out = collect(en)
    check("连续增长 ⇒ 第 1 轮就出句（不等停顿）", len(out) >= 1, f"得到 {out!r}")
    check("连续增长 ⇒ 出的是**子句**（不是整段）",
          bool(out) and len(out[0][1]) < len(en[0]) + 5, f"得到 {out!r}")
    check("英文逗号被切成子句",
          bool(out) and out[0][1].rstrip().endswith(","), f"得到 {out!r}")

    # ------------------------------------------------------------------ #
    # ② 中文子句标点
    # ------------------------------------------------------------------ #
    out = collect(["今天天气真的不错，", "今天天气真的不错，我们出去走走吧。"])
    check("中文逗号 ⇒ 成句", len(out) >= 1, f"得到 {out!r}")
    check("中文成句内容正确",
          bool(out) and out[0][1] == "今天天气真的不错，", f"得到 {out!r}")

    # ------------------------------------------------------------------ #
    # ③ 句末标点
    # ------------------------------------------------------------------ #
    out = collect(["这是一句完整的话。", "这是一句完整的话。后面还有。"])
    check("句末标点 ⇒ 成句", bool(out), f"得到 {out!r}")

    # ------------------------------------------------------------------ #
    # ④ 顿号不切（并列成分不能拆）
    # ------------------------------------------------------------------ #
    out = collect(["苹果、香蕉、橘子都很好吃。", "苹果、香蕉、橘子都很好吃。"])
    check("顿号「、」**不是**切点（并列成分不拆）",
          bool(out) and out[0][1] == "苹果、香蕉、橘子都很好吃。", f"得到 {out!r}")
    check("顿号集合不含「、」", "、" not in _FAST_CLAUSE_END)

    # ------------------------------------------------------------------ #
    # ⑤ 碎片不切、不丢
    # ------------------------------------------------------------------ #
    out = collect(["Hi,", "Hi, ok", "Hi, ok, that is fine. And we are done."])
    check(f"碎片（< {_FAST_MIN_CHARS} 字）⇒ 不单独成句",
          all(len(t) >= _FAST_MIN_CHARS for _, t in out), f"得到 {out!r}")
    check("碎片被**并进**下一句（不单独出、也不丢）",
          bool(out) and out[0][1] == "Hi, ok, that is fine.", f"得到 {out!r}")

    # ------------------------------------------------------------------ #
    # ⑥ 停顿 ⇒ 切尾巴
    # ------------------------------------------------------------------ #
    seg = _FastCommittedSegmenter()
    out = collect(["他说了很长一句话但是没有标点", ] * 3, seg)
    check(f"连续 {_FAST_PAUSE_ROUNDS} 轮 committed 无增长 ⇒ 切尾巴",
          bool(out) and out[0][1] == "他说了很长一句话但是没有标点", f"得到 {out!r}")

    # ⑦ 重新增长 ⇒ 停顿计数清零（不该在增长中切尾巴）
    seg = _FastCommittedSegmenter()
    out = collect([
        "他说了很长一句话但是没有标点",
        "他说了很长一句话但是没有标点",
        "他说了很长一句话但是没有标点又继续说了下去",
        "他说了很长一句话但是没有标点又继续说了下去",
    ], seg)
    check("committed 重新增长 ⇒ 停顿计数清零（增长中不切）",
          not out, f"得到 {out!r}")

    # ------------------------------------------------------------------ #
    # ⑧ 长度兜底 ⇒ 不劈词
    # ------------------------------------------------------------------ #
    words = " ".join(f"word{i:02d}" for i in range(20))
    out = collect([words], _FastCommittedSegmenter())
    check(f"无标点超长 ⇒ 按长度兜底切（≤ {_FAST_MAX_CHARS}）",
          bool(out) and len(out[0][1]) <= _FAST_MAX_CHARS, f"得到 {out!r}")
    check("长度兜底**不劈词**（切点在词边界）",
          bool(out) and out[0][1].split()[-1].startswith("word"),
          f"得到 {out!r}")

    # ------------------------------------------------------------------ #
    # ⑨ epoch 切换 ⇒ 旧尾巴被 flush，不丢
    # ------------------------------------------------------------------ #
    seg = _FastCommittedSegmenter()
    out = collect(["第一段说的话还没有结束", "新的一段内容"], seg)
    check("epoch 切换 ⇒ 旧尾巴被 flush（不丢）",
          bool(out) and out[0][1] == "第一段说的话还没有结束", f"得到 {out!r}")

    # ------------------------------------------------------------------ #
    # ⑩ 一轮多句 ⇒ 循环切出
    # ------------------------------------------------------------------ #
    out = collect(["第一句。第二句。第三句。"])
    check("一轮里三句 ⇒ 切出三句", len(out) == 3, f"得到 {out!r}")
    check("句子序号连续",
          [i for i, _ in out] == list(range(1, len(out) + 1)), f"得到 {out!r}")

    # ------------------------------------------------------------------ #
    # ⑪ flush ⇒ 尾巴交出
    # ------------------------------------------------------------------ #
    seg = _FastCommittedSegmenter()
    out = []
    seg.feed("没有标点的最后一句", lambda t, i, c0, c1: out.append((i, t)))
    seg.flush(lambda t, i, c0, c1: out.append((i, t)))
    check("flush ⇒ 尾巴交出", bool(out), f"得到 {out!r}")

    # ------------------------------------------------------------------ #
    # ⑫ 不重复出句（同一 committed 再喂一次不重出）
    # ------------------------------------------------------------------ #
    seg = _FastCommittedSegmenter()
    out = []
    emit = lambda t, i, c0, c1: out.append((i, t))  # noqa: E731
    seg.feed("他说了一句话。", emit)
    seg.feed("他说了一句话。", emit)
    check("同一 committed 再喂一次 ⇒ 不重复成句", len(out) == 1, f"得到 {out!r}")

    print(f"\n通过 {_PASS} / 失败 {_FAIL}")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
