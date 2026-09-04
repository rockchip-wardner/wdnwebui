from __future__ import annotations

import re
from typing import Any, List, Optional, Tuple

MAX_NEW_TOKENS = 2048

# 严格配对：``<think>...</think>``。Qwen3-4B / Qwen3.5-VL 在某些输出
# 里也会丢开口只写 ``</think>``（实测 Qwen3.5-VL 自报家门那段就漏了
# ``<think>``），所以还要有 ``_THINK_TAIL_RE`` 兜底。
_THINK_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL)
_THINK_TAIL_RE = re.compile(r"^(.*?)</think>", re.DOTALL)


_TOOL_CALL_INSTRUCTION = (
    "When you decide to call a tool, output **exactly one** JSON object "
    "matching the format above. Do not output multiple JSON objects, "
    "placeholder/example JSONs, or any text before/after the JSON."
)


def split_thinking(text: str) -> Tuple[str, str]:
    """把 ``text`` 切成 ``(thinking, answer)``。

    三档兜底（从最严到最松）：
      1. ``<think>...</think>`` 严格配对（Qwen3-4B / 多数 Qwen3-VL 都
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
    return "", text.strip()


def count_tokens(tokenizer: Any, text: str) -> int:
    if not text:
        return 0
    try:
        return len(tokenizer.encode(text, add_special_tokens=False))
    except Exception:
        return len(text) // 2


def build_chatml_prompt(history: List[dict], user_text: str,
                        tool_specs: Optional[List[dict]] = None) -> str:
    """拼 ChatML 文本 prompt。

    ``tool_specs``（可选，``get_tool_specs(tools)`` 返回值）非空时，
    渲染 wdnai 的 ``render_tools_block`` 并以独立段形式追加到 system
    段之后、对话历史之前（与 wdnai 默认追加到末尾不同——chat_run 走
    独立段，结构清晰）。模型只需看到工具描述，无需被历史 / 用户问题
    注意力分散。

    同时追加 ``_TOOL_CALL_INSTRUCTION`` 强化指令（chat_run ↔ @Callable
    链路 双重保险之一），让 LLM 在 answer 段只输出**单个** tool_call
    JSON，避免"先示例后真实"的多 JSON 场景。解析层
    ``compute_worker._parse_qwen_tool_call`` 路径 2 提供兜底。
    """
    parts = ["system\nYou are a helpful assistant.\n"]
    if tool_specs:
        from wdnai._callable import render_tools_block
        parts.append("\n")
        parts.append(render_tools_block(tool_specs))
        parts.append("\n\n")
        parts.append(_TOOL_CALL_INSTRUCTION)
        parts.append("\n\n---\n\n")
    for msg in history:
        if msg.get("role") == "user":
            parts.append(f"user\n{msg.get('content') or ''}\n")
        elif msg.get("role") == "assistant":
            parts.append(f"assistant\n{msg.get('content') or ''}\n")
    parts.append(f"user\n{user_text}\n")
    parts.append("assistant\n")
    return "".join(parts)


def trim_history(
    history: List[dict],
    user_text: str,
    tokenizer: Any,
    context_limit: int,
    max_new_tokens: int = MAX_NEW_TOKENS,
) -> Tuple[List[dict], Optional[str]]:
    reserve = min(max_new_tokens, max(1, context_limit // 8))
    user_tokens = count_tokens(tokenizer, user_text)
    if user_tokens + reserve > context_limit:
        detail = (
            f"单条消息过长（{user_tokens} tokens + 预留 {reserve} > "
            f"{context_limit}），请缩短输入"
        )
        return [], detail
    selected: List[dict] = []
    total = user_tokens
    for msg in reversed(history):
        content = msg.get("content") or ""
        t = count_tokens(tokenizer, content)
        if total + t + reserve > context_limit:
            break
        selected.insert(0, msg)
        total += t
    return selected, None
