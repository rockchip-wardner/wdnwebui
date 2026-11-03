"""Qwen3.5-VL-9B thinking output adapter.

9B's segmented ctypes pipeline emits a plain ``Thinking Process:`` block
instead of the ``<think>...</think>`` markers used by the 4B C template.
Keep this parser app-local so the 4B / WeChat paths and the shared
``chat_runner.split_thinking`` remain unchanged.
"""
from __future__ import annotations

import re
from typing import Callable, Tuple

_PREFIX = "Thinking Process:"
_BOUNDARY = "\n\n\n"
_ANSWER_TAG_RE = re.compile(r"</?answer>", re.IGNORECASE)


def split_9b_thinking(text: str, enabled: bool = True) -> Tuple[str, str]:
    """Split 9B's raw ``Thinking Process:`` output.

    A completed generation separates the final answer with a blank line
    run (observed as ``\\n\\n\\n``). If no such boundary exists, keep the
    generated reasoning as ``thinking`` and do not leak it into answer.
    """
    raw = (text or "").strip()
    if not raw:
        return "", ""
    has_prefix = raw.startswith(_PREFIX)
    body = raw[len(_PREFIX):].lstrip("\r\n") if has_prefix else raw
    if not enabled:
        if not has_prefix:
            return "", raw
        idx = body.rfind(_BOUNDARY)
        if idx >= 0:
            return "", body[idx + len(_BOUNDARY):].strip()
        return "", ""
    if has_prefix or enabled:
        idx = body.rfind(_BOUNDARY)
        if idx >= 0:
            thinking = body[:idx].strip()
            answer = body[idx + len(_BOUNDARY):].strip()
            return thinking, answer
        return body.strip(), ""
    return "", raw


class Thinking9BParser:
    """Stream 9B's ``Thinking Process:`` output into two channels.

    The boundary is intentional: the parser buffers only the tail needed
    to recognize ``\\n\\n\\n`` and otherwise forwards deltas immediately.
    When thinking is disabled the reasoning segment is consumed but never
    emitted, so it cannot leak into the answer bubble.
    """

    def __init__(
        self,
        emit_fn: Callable[[dict], None],
        request_id: int,
        *,
        enabled: bool,
    ) -> None:
        self._emit = emit_fn
        self._request_id = request_id
        self._enabled = bool(enabled)
        self._buf = ""
        self._started = False
        self._mode = "thinking" if self._enabled else "answer"

    def _emit_event(self, kind: str, text: str) -> None:
        if not text:
            return
        if kind == "thinking" and not self._enabled:
            return
        self._emit({
            "type": kind,
            "request_id": self._request_id,
            "delta": text,
        })

    def _drain(self) -> None:
        while True:
            if self._mode == "thinking":
                idx = self._buf.find(_BOUNDARY)
                if idx >= 0:
                    self._emit_event("thinking", self._buf[:idx])
                    self._buf = self._buf[idx + len(_BOUNDARY):]
                    self._mode = "answer"
                    self._emit_event("answer", _ANSWER_TAG_RE.sub("", self._buf))
                    self._buf = ""
                    return
                keep = len(_BOUNDARY) - 1
                if len(self._buf) > keep:
                    self._emit_event("thinking", self._buf[:-keep])
                    self._buf = self._buf[-keep:]
                return
            self._emit_event("answer", _ANSWER_TAG_RE.sub("", self._buf))
            self._buf = ""
            return

    def push(self, delta: str) -> None:
        if not delta:
            return
        self._buf += delta
        if not self._started:
            stripped = self._buf.lstrip()
            if stripped.startswith(_PREFIX):
                self._started = True
                self._buf = stripped[len(_PREFIX):]
                self._mode = "thinking"
            elif len(stripped) >= len(_PREFIX):
                self._started = True
                # With thinking enabled, 9B may omit the text prefix but
                # still emit reasoning before the answer boundary.
                self._mode = "thinking" if self._enabled else "answer"
                self._buf = stripped
            else:
                return
        self._drain()

    def flush(self) -> None:
        if not self._started:
            text = self._buf
            self._buf = ""
            self._emit_event("answer", _ANSWER_TAG_RE.sub("", text))
            return
        self._drain()
        text = self._buf
        self._buf = ""
        self._emit_event("thinking" if self._mode == "thinking" else "answer",
                         _ANSWER_TAG_RE.sub("", text))

    def begin_retry(self) -> None:
        self.flush()
        self._buf = ""
        self._started = False
        self._mode = "thinking" if self._enabled else "answer"
