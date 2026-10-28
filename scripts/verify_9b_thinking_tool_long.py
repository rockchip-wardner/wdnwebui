#!/usr/bin/env python3
"""Qwen3.5-VL-9B thinking / tool-call / long-prompt acceptance."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from typing import Annotated

import cv2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wdnai import (  # noqa: E402
    Attachment,
    Callable as WdnaiCallable,
    Field as WdnaiField,
    LLMLocalSession,
    SystemLanguageModel,
)


@WdnaiCallable
class Echo:
    """Test-only tool used to exercise the 9B tool-call path."""

    text: Annotated[str, "要回显的文本"] = WdnaiField(
        default="", description="要回显的文本",
    )

    def __call__(self, frame_bgr, ctx):
        return {"text": self.text or "", "echoed": True}


def _register_echo() -> None:
    from app.core.tools.registry import get_tool_registry

    get_tool_registry().register(
        key="verify-9b-echo",
        name="Echo",
        description="测试用回显工具",
        requires_capability=None,
        tool_class=Echo,
    )


def _text(value) -> str:
    return value if isinstance(value, str) else str(value or "")


def _check(name: str, ok: bool, detail: str = "") -> bool:
    print(("PASS " if ok else "FAIL ") + name + (f" :: {detail}" if detail else ""))
    return ok


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--npu", default="0,1")
    ap.add_argument("--image", default="/userdata/test/bus.jpg")
    ap.add_argument("--long-tokens", type=int, default=16384)
    ap.add_argument("--skip-image", action="store_true")
    args = ap.parse_args()

    cards = [int(x) for x in args.npu.split(",") if x.strip()]
    model = SystemLanguageModel.qwen3_5_vl_9b()
    sess = LLMLocalSession(model=model, npu_indices=cards)
    passed = True
    try:
        print("== thinking ==")
        sess.reset()
        t0 = time.perf_counter()
        think_on = await sess.respond(
            prompt="请用一句话说明 1+1 为什么等于 2。",
            enable_thinking=True,
            max_new_tokens=128,
        )
        print(f"thinking_on elapsed={time.perf_counter() - t0:.2f}s text={_text(think_on)[:300]!r}")
        passed &= _check("thinking=True 有输出", bool(_text(think_on).strip()))

        sess.reset()
        t0 = time.perf_counter()
        think_off = await sess.respond(
            prompt="请直接回答：1+1 等于几？只回答数字。",
            enable_thinking=False,
            max_new_tokens=32,
        )
        print(f"thinking_off elapsed={time.perf_counter() - t0:.2f}s text={_text(think_off)[:300]!r}")
        passed &= _check("thinking=False 有输出", bool(_text(think_off).strip()))

        print("== tool ==")
        _register_echo()
        from app.core.chat.agent import run_chat_with_tools

        sess.reset()
        t0 = time.perf_counter()
        tool_result = run_chat_with_tools(
            sess,
            [],
            "请调用 Echo 工具，参数 text 设为 hello。请只调用工具，不要直接回答。",
            context_limit=8192,
            max_new_tokens=128,
            tool_names=["Echo"],
            max_iters=2,
            channel="chat_web",
        )
        print(f"tool elapsed={time.perf_counter() - t0:.2f}s result={tool_result}")
        passed &= _check(
            "工具调用被执行",
            bool(tool_result.get("steps")),
            f"steps={tool_result.get('steps')}",
        )
        passed &= _check(
            "工具轮仍有最终回答或明确工具记录",
            bool(tool_result.get("answer") or tool_result.get("tool_call")),
            f"answer={tool_result.get('answer')!r} tool_call={tool_result.get('tool_call')!r}",
        )

        print("== long prompt ==")
        sess.reset()
        tokenizer = sess._backend.tokenizer
        unit = "这是一个用于长上下文验收的中文句子。"
        target = int(args.long_tokens)
        prompt = ""
        while len(tokenizer.encode(prompt)) < target:
            prompt += unit
        prompt_tokens = len(tokenizer.encode(prompt))
        t0 = time.perf_counter()
        long_out = await sess.respond(prompt=prompt, max_new_tokens=16)
        elapsed = time.perf_counter() - t0
        print(
            f"long tokens={prompt_tokens} elapsed={elapsed:.2f}s "
            f"text={_text(long_out)[:200]!r}"
        )
        passed &= _check(
            f"长 prompt({prompt_tokens} tokens) 正常返回",
            bool(_text(long_out).strip()),
        )

        if not args.skip_image:
            print("== image ==")
            img = cv2.imread(args.image)
            passed &= _check("测试图片可读", img is not None, args.image)
            if img is not None:
                sess.reset()
                t0 = time.perf_counter()
                image_out = await sess.respond(
                    prompt="请用一句话描述这张图片。",
                    attachment=[Attachment(img)],
                    max_new_tokens=64,
                )
                print(
                    f"image elapsed={time.perf_counter() - t0:.2f}s "
                    f"text={_text(image_out)[:300]!r}"
                )
                passed &= _check("图片推理有输出", bool(_text(image_out).strip()))
    finally:
        sess.close()

    print("RESULT", "PASS" if passed else "FAIL")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
