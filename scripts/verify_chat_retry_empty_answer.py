#!/usr/bin/env python3
"""回归：多轮思考「只有 thinking、没有 answer」时的重试兜底（纯 mock）。

真机 Qwen3.5-4B 在多轮 history 下会偶发把完整答案写进 thinking，生成
``</think>`` 后直接 EOS，answer 为空。文本对话因此启用一次重试：
保留 history + 追加「请务必在思考后给出最终回答」，重试的思考丢弃
（``_ThinkingRouter.begin_retry``），只流式答案；仍为空则显示提示文案。

本脚本不加载 NPU，也不依赖 web 服务。
"""
from __future__ import annotations

import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


class FakeSession:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    async def respond(self, **kwargs):
        self.calls += 1
        return self.replies.pop(0)


def run(replies, retry=True):
    from app.core.chat.agent import _run_turn

    sess = FakeSession(replies)
    hits = {"n": 0}

    def on_retry():
        hits["n"] += 1

    out = _run_turn(
        sess,
        history=[],
        text="测试",
        tool_names=[],
        context_limit=4096,
        max_new_tokens=256,
        enable_thinking=True,
        retry_empty_answer=retry,
        on_retry=on_retry,
    )
    return out, sess, hits


CASES = [
    # (name, replies, retry, want_answer, want_thinking, want_calls, want_retry)
    ("首次无答案 → 重试成功",
     ["思考1</think>", "思考2</think>答案2"],
     True, "答案2", "思考1", 2, 1),
    ("首次无答案 → 重试仍空 → 提示文案",
     ["思考1</think>", "思考2</think>"],
     True, "模型未生成最终回答，请重试", "思考1", 2, 1),
    ("未启用重试 → 不重试、不加文案",
     ["思考1</think>"],
     False, "", "思考1", 1, 0),
    ("首次就有答案 → 不重试",
     ["思考1</think>答案1"],
     True, "答案1", "思考1", 1, 0),
]


def main() -> int:
    failed = 0
    for name, replies, retry, wa, wt, wc, wr in CASES:
        out, sess, hits = run(replies, retry=retry)
        ok = (
            out.get("answer") == wa
            and out.get("thinking") == wt
            and sess.calls == wc
            and hits["n"] == wr
        )
        print(("PASS " if ok else "FAIL ") + name)
        if not ok:
            failed += 1
            print("   got answer=%r thinking=%r calls=%d retries=%d"
                  % (out.get("answer"), out.get("thinking"), sess.calls, hits["n"]))
            print("   want answer=%r thinking=%r calls=%d retries=%d"
                  % (wa, wt, wc, wr))
    print(f"\n{len(CASES) - failed}/{len(CASES)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
