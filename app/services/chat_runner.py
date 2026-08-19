from __future__ import annotations

import re
from typing import Any, List, Optional, Tuple

MAX_NEW_TOKENS = 2048

_THINK_RE = re.compile(r" thinking(.*?) response", re.DOTALL)


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


def build_chatml_prompt(history: List[dict], user_text: str) -> str:
    parts = ["<|im_start|>system\nYou are a helpful assistant.<|im_end|>\n"]
    for msg in history:
        if msg.get("role") == "user":
            parts.append(f"<|im_start|>user\n{msg.get('content') or ''}<|im_end|>\n")
        elif msg.get("role") == "assistant":
            parts.append(f"<|im_start|>assistant\n{msg.get('content') or ''}<|im_end|>\n")
    parts.append(f"<|im_start|>user\n{user_text}<|im_end|>\n")
    parts.append("<|im_start|>assistant\n")
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
