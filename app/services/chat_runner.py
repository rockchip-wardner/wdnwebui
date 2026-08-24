from __future__ import annotations

import re
from typing import Any, List, Optional, Tuple

MAX_NEW_TOKENS = 2048

_THINK_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL)


_TOOL_CALL_INSTRUCTION = (
    "When you decide to call a tool, output **exactly one** JSON object "
    "matching the format above. Do not output multiple JSON objects, "
    "placeholder/example JSONs, or any text before/after the JSON."
)

_TOOL_CALL_TAG_RE = re.compile(r"</?tool_call>", re.IGNORECASE)
_MD_JSON_FENCE_RE = re.compile(
    r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def strip_tool_call_tags(text: str) -> str:
    """从 answer 文本中剥离工具调用残留（B1 兜底）。

    模型发起工具调用时 answer 可能是 ``<tool_call>{...}</tool_call>``、
    裸 JSON 或 markdown ```json 围栏。chat_agent 正常路径下工具轮
    不会直接把 answer 发给用户（走 wrap-up / 回喂），但解析失败、
    半截输出等极端情况下仍可能有残留 —— 发送给用户前统一清洗。
    """
    if not text:
        return ""
    out = _TOOL_CALL_TAG_RE.sub("", text)
    out = _MD_JSON_FENCE_RE.sub("", out)
    # 剥离"整个字符串就是一个 JSON 对象"的情况（裸 JSON 兜底）
    s = out.strip()
    if s.startswith("{") and s.endswith("}"):
        try:
            import json as _json
            obj = _json.loads(s)
            if isinstance(obj, dict) and (
                    "name" in obj or "arguments" in obj or "tool_call" in obj):
                return ""
        except Exception:  # noqa: BLE001
            pass
    return out.strip()


def split_thinking(text: str) -> Tuple[str, str]:
    m = _THINK_RE.search(text)
    if not m:
        return "", text.strip()
    thinking = m.group(1).strip()
    answer = (text[:m.start()] + text[m.end():]).strip()
    return thinking, answer


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
