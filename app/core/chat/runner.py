"""文本后处理 —— thinking/answer 切分 + 输出清洗。

**不再拼 prompt**。ChatML 的拼装归 ``wdnai.build_chatml_prompt``（单一
真源，``LLMSession.respond(history=…)`` 内部就是调它）；历史裁剪归
``chat_agent._trim``（它需要向 worker 的 tokenizer 提问）。本模块曾经
各留了一份副本，两份迟早会漂 —— 现已删除。

``MAX_NEW_TOKENS`` / ``sanitize_chat_output`` / ``split_thinking`` 三者
都是纯文本处理，与 transport 无关，留在这里。
"""
from __future__ import annotations

import re
from typing import Tuple

MAX_NEW_TOKENS = 2048

# 严格配对：``<think>...</think>``。Qwen3.5-VL 在某些输出
# 里也会丢开口只写 ``</think>``（实测 Qwen3.5-VL 自报家门那段就漏了
# ``<think>``），所以还要有 ``_THINK_TAIL_RE`` 兜底。
_THINK_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL)
_THINK_TAIL_RE = re.compile(r"^(.*?)</think>", re.DOTALL)


_CHATML_STRAY_RE = re.compile(r"<\|im_end\|>")


def sanitize_chat_output(text: str) -> str:
    """清理模型越界生成的 ChatML 标记，并截断自造的"下一回合"。

    wdnai 的 GGUF tokenizer 只把 eos/pad + multimodal 占位符注册为
    special token —— ``<|im_start|>`` / ``<think>`` / ``</think>`` 不在
    其中，``decode(skip_special_tokens=True)`` 跳不过去，模型一旦生成
    这些 token 就会以**字面文本**泄漏到最终回复（真机实测：微信视频
    检索回复里出现大量 ``<|im_start|>user`` / ``<|im_end|>`` 等）。

    触发这类越界生成的上游原因是 C 侧 KV cache 在轮次间未清理
    （wdnai ``reset()`` 已在 SDK 侧修好，worker 不再自己清）。修复后
    正常单轮输出**不应**含 ``<|im_end|>``，因此：从首个 ``<|im_end|>``
    起整段丢弃（其后内容是模型幻觉的新回合，不可信），再删掉残留的
    ``<|im_end|>``。

    ``<think>`` / ``</think>`` **刻意保留** —— 上层 ``split_thinking``
    依赖它们做思维链切分。
    """
    idx = text.find("<|im_start|>")
    if idx >= 0:
        text = text[:idx]
    text = _CHATML_STRAY_RE.sub("", text)
    return text.strip()


def split_thinking(text: str) -> Tuple[str, str]:
    """把 ``text`` 切成 ``(thinking, answer)``。

    三档兜底（从最严到最松）：
      1. ``<think>...</think>`` 严格配对（多数 Qwen3-VL 都
         走这条路径）→ 取出 group(1) 当 thinking，其余当 answer。
      2. 只有 ``</think>`` 没 ``<think>`` 开头（实测 Qwen3.5-VL
         在 ``enable_thinking=True`` 时的偶发输出）：
         ``<think>`` 之前的内容当 thinking，`` 之后当 answer。
         修这个之前 ``answer`` 字段会被 ``split_thinking`` 整段
         吞掉，前端 ``_finalizeBubble`` 用 done 帧覆盖流式结果时
         ``xxxxx</think>`` 直接漏到 answer 气泡。
      3. 都没有 → 整段当 answer（``thinking=""``）。

    同一段文本里多次 ``</think>`` 时取最早出现的那次 —— 第二个
    ``</think>`` 之后的字符仍按 answer 处理（典型 degenerate 输
    出在 answer 里复读 ``</think>``，不该再往 thinking 塞）。
    """
    m = _THINK_RE.search(text)
    if m:
        thinking = m.group(1).strip()
        answer = (text[:m.start()] + text[m.end():]).strip()
        return thinking, answer
    # 只有尾部 ``</think>`` 的兜底：thinking = 开头到第一个 ``</think>`` 之前。
    # 注意：用 strict 的 ``re.search`` 不开 DOTALL 也可以，但 thinking 段
    # 里可能有换行，所以 ``(.*?)`` + DOTALL 更稳。
    m = _THINK_TAIL_RE.search(text)
    if m:
        thinking = m.group(1).strip()
        answer = text[m.end():].strip()
        return thinking, answer
    # Only an opening marker, never closed (non-thinking models still emit
    # ``<think>`` sometimes): strip it so it cannot leak into the answer.
    if "<think>" in text:
        return "", text.replace("<think>", "", 1).strip()
    return "", text.strip()
