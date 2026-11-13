#!/usr/bin/env python3
"""回归：``_ThinkingRouter`` 的分频与标签剥离（纯逻辑，不需要 NPU）。

真机实测的两种输出形态都要覆盖：

* thinking 开启 —— C 侧把 ``<think>`` 注入 assistant 前缀，流里只有
  思考内容 + ``</think>``；
* thinking 关闭 —— 模型仍输出空的 ``<think>\\n\\n</think>`` 块。

标签可能被 token 流切开（``<th`` + ``ink>``），所以这里全部按多段 delta
喂入。空思考段不得产生 thinking 事件；关闭思考时页面上不应出现任何
``<think>`` 字样。

卡片位置：``_ThinkingRouter`` 是 **text-chat 卡的内部类**。该卡已于
v0.26 从宿主仓 ``app/apps/text_chat/`` 迁到 ``wdnapps_audio_chat`` 插件包，
所以类的新路径是 ``wdnapps_audio_chat.cards.text_chat.service``。
本脚本同时接受旧路径（宿主仓还没删干净、或插件没装上的过渡期）。
"""
from __future__ import annotations

import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def _import_router():
    """取 ``_ThinkingRouter`` —— 插件路径优先，旧的内置路径兜底。"""
    last_err: Exception | None = None
    for mod in (
        "wdnapps_audio_chat.cards.text_chat.service",   # 插件卡（现行）
        "app.apps.text_chat.service",                   # 宿主内置卡（过渡期）
    ):
        try:
            return getattr(__import__(mod, fromlist=["_ThinkingRouter"]),
                           "_ThinkingRouter")
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise RuntimeError(
        "找不到 _ThinkingRouter —— text-chat 卡既不在插件包"
        "（wdnapps_audio_chat 未安装？）也不在宿主内置路径下：%r" % (last_err,)
    )


def run(deltas):
    _ThinkingRouter = _import_router()

    events = []
    router = _ThinkingRouter(events.append, 1)
    for d in deltas:
        router.push(d)
    router.flush()
    thinking = "".join(e["delta"] for e in events if e["type"] == "thinking")
    answer = "".join(e["delta"] for e in events if e["type"] == "answer")
    return thinking, answer, events


CASES = []


def case(name, deltas, *, thinking="", answer_contains="", no_thinking_event=False):
    CASES.append((name, deltas, thinking, answer_contains, no_thinking_event))


# 关闭思考：空 <think></think>，标签跨 delta 切开
case("off: 空 think 块（跨 delta）",
     ["<th", "ink>", "\n\n</thi", "nk>\n\n9.9 大"],
     thinking="", answer_contains="9.9 大", no_thinking_event=True)

# 关闭思考：整块单 delta
case("off: 空 think 块（单 delta）",
     ["<think>\n\n</think>\n\n9.11 大"],
     thinking="", answer_contains="9.11 大", no_thinking_event=True)

# 开启思考：流里没有开标签（C 侧注入），只有内容 + </think>
case("on: 无开标签，只有闭合",
     ["用户问的是比较 9.11 和 9.9", "哪个大。\n", "</thi", "nk>", "\n\n9.9 大"],
     thinking="用户问的是比较 9.11 和 9.9哪个大。", answer_contains="9.9 大")

# 开启思考：开/闭标签都出现且跨 delta
case("on: 完整标签（跨 delta）",
     ["<thi", "nk>思考", "</th", "ink>答案"],
     thinking="思考", answer_contains="答案")

# 无任何标签：不能误当 thinking 显示
case("plain: 无标签答案",
     ["9.9 大"],
     thinking="", answer_contains="9.9 大", no_thinking_event=True)

# 只有空标签对
case("off: 只有空标签对",
     ["<think>", "</think>"],
     thinking="", answer_contains="", no_thinking_event=True)

# 重试时模型偶发模仿 <answer> 标签 —— 流式必须剥掉
case("answer 标签（单 delta）",
     ["<think>思考</think><answer>答案</answer>"],
     thinking="思考", answer_contains="答案")

case("answer 标签（跨 delta）",
     ["<think>思考</think><ans", "wer>答案</ans", "wer>"],
     thinking="思考", answer_contains="答案")

case("answer 标签跨缓冲边界",
     ["<think>思</think>", "<answ", "er>答", "案</answer>"],
     thinking="思", answer_contains="答案")


def main() -> int:
    failed = 0
    for name, deltas, want_thinking, want_answer, no_thinking_event in CASES:
        thinking, answer, events = run(deltas)
        ok = True
        reasons = []
        if thinking.strip() != want_thinking:
            ok = False
            reasons.append(f"thinking={thinking!r} want={want_thinking!r}")
        if want_answer and want_answer not in answer:
            ok = False
            reasons.append(f"answer={answer!r} missing={want_answer!r}")
        if any(tag in answer for tag in
               ("<think>", "</think>", "<answer>", "</answer>")):
            ok = False
            reasons.append(f"answer still contains tag: {answer!r}")
        if no_thinking_event and any(e["type"] == "thinking" for e in events):
            ok = False
            reasons.append("unexpected thinking event")
        print(("PASS " if ok else "FAIL ") + name)
        if not ok:
            failed += 1
            for r in reasons:
                print("     ", r)
    print(f"\n{len(CASES) - failed}/{len(CASES)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
