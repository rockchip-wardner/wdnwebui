"""微信对接文本对话模型桥接。

开启开关后，微信用户发来的文字消息自动进入一个串行 worker 队列，桥接
调用 ``swcap_text_chat``（qwen3-8b）的 ``chat_run`` 推理，把回答通过
``Wechat.send_text`` 发回给该微信用户。

设计要点
========

* **开关内存态**：``_enabled`` 只存内存，不落盘（重启即关）；开关可开性
  由 ``available()`` 决定 —— qwen3-8b 未加载时无法开启（后端校验兜底）。
* **per-user 上下文隔离**：``self.histories: dict[user_id, deque]`` 各自
  独立，与 web 文本对话的全局历史完全无关。上下文裁剪复用
  ``chat_runner.trim_history``。
* **串行保证**：桥接 worker 线程串行消费队列；``chat_run`` 用
  ``chat_serial.get_chat_run_lock()`` 与 text_chat worker 互斥。
* **只回 answer 不回 thinking**：``split_thinking`` 去掉思考段再发微信。
* **发送复用 wechat_bot**：回复走 ``Wechat.send_text``，自动出现在"调试
  收发"消息列表（out 方向），与手动发送共用同一发送通道、线程安全。
"""
from __future__ import annotations

import logging
import queue as _queue
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, Optional

from app.services.chat_runner import split_thinking
from app.services.tools import get_tool_registry

_LOG = logging.getLogger("wechat_llm")

_HISTORY_MAX = 200


def _llm_log(msg: str) -> None:
    print(f"[wechat-llm] {msg}", flush=True)


class _LlmBridge:
    """微信文字消息 → qwen3-8b → 自动回复 的串行桥接。"""

    MAX_NEW_TOKENS = 2048

    def __init__(self) -> None:
        self._enabled = False
        self._lock = threading.RLock()
        self.histories: Dict[str, Deque[dict]] = {}
        self._queue: "_queue.Queue[Optional[tuple]]" = _queue.Queue()
        self._wake = threading.Event()
        self._busy = False
        self._active_sessions = 0
        self._worker = threading.Thread(
            target=self._worker_loop, name="wechat-llm-bridge", daemon=True,
        )
        self._worker.start()

    # ------------------------------------------------------------- 状态
    def available(self) -> bool:
        """qwen3-8b 是否已加载、可对话（用于前端置灰 + 后端开启校验）。"""
        try:
            from app.services.compute_registry import get_registry
            get_registry().get_session("swcap_text_chat")
            return True
        except RuntimeError:
            return False

    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def busy(self) -> bool:
        with self._lock:
            return self._busy

    def active_sessions(self) -> int:
        with self._lock:
            return self._active_sessions

    def snapshot(self) -> dict:
        return {
            "enabled": self.enabled(),
            "available": self.available(),
            "busy": self.busy(),
            "active_sessions": self.active_sessions(),
        }

    # ------------------------------------------------------------- 开关
    def set_enabled(self, on: bool) -> dict:
        if on and not self.available():
            raise RuntimeError("文本对话模型未启用（请在算力配置页加载 qwen3-8b）")
        with self._lock:
            self._enabled = bool(on)
            if not self._enabled:
                self.histories.clear()
        _llm_log(f"llm bridge enabled={self._enabled}")
        return self.snapshot()

    # ------------------------------------------------------------- 入口
    def handle_incoming(self, msg: Any) -> None:
        """由 wechat 的消息处理器调用；开关开启时处理文字消息。"""
        if not self.enabled():
            return
        kind = getattr(msg, "type", None)
        text = (getattr(msg, "text", "") or "").strip()
        user_id = getattr(msg, "user_id", "") or ""
        if not user_id:
            return
        if kind == "text" and text:
            self._queue.put((user_id, text, time.time()))
            self._wake.set()
        else:
            self._reply_no_context(user_id, "当前仅支持文字对话（图片 / 语音 / 文件暂不支持）。")

    def _reply_no_context(self, user_id: str, text: str) -> None:
        try:
            from app.services.wechat import get_wechat
            get_wechat().send_text(user_id, text)
        except Exception as exc:  # noqa: BLE001
            _llm_log(f"reply error to {user_id}: {exc!r}")

    # ------------------------------------------------------------- worker
    def _worker_loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                break
            user_id, text, ts = item
            with self._lock:
                self._busy = True
                self._active_sessions += 1
            try:
                self._process(user_id, text)
            except Exception as exc:  # noqa: BLE001
                _LOG.exception("wechat-llm _process failed: %s", exc)
                self._reply_no_context(user_id, f"处理失败：{exc}")
            finally:
                with self._lock:
                    self._busy = False
                    self._active_sessions -= 1

    def _process(self, user_id: str, text: str) -> None:
        from app.services.chat_serial import get_chat_run_lock
        from app.services.compute_registry import get_registry

        try:
            session = get_registry().get_session("swcap_text_chat")
        except RuntimeError as e:
            self._reply_no_context(user_id, f"功能未启用: swcap_text_chat（{e}）")
            return

        # 读取该用户历史（惰性建 deque）
        with self._lock:
            history_snapshot = list(
                self.histories.setdefault(user_id, deque(maxlen=_HISTORY_MAX)))

        context_limit = self._resolve_context_limit()

        # 裁剪 / ChatML 拼接由 worker 端 _chat_run_loop 完成（真实 tokenizer）；
        # 主进程只透传 history，拿到完整文本后再切 thinking / answer。
        try:
            with get_chat_run_lock():
                result = session.chat_run(
                    history=history_snapshot,
                    text=text,
                    context_limit=context_limit,
                    max_new_tokens=self.MAX_NEW_TOKENS,
                    on_delta=None,
                    tool_names=self._resolve_tool_names(),
                )
        except Exception as exc:  # noqa: BLE001
            _LOG.exception("chat_run failed for %s", user_id)
            self._reply_no_context(user_id, f"推理失败：{exc}")
            return

        # worker 端已用 split_thinking 切好 + 仅对 answer 段解析 tool_call；
        # 兜底走本地 split（处理 worker 未拆分等极端情况）。
        full = result.get("text") or ""
        answer = (result.get("answer") or "").strip()
        if not answer:
            _, answer = split_thinking(full)
            answer = (answer or "").strip()
        answer = answer or "（模型未输出有效内容）"

        # 阶段1：tool_call 透传但**不执行 instance()**（避免 TTS 误播、
        # agent-loop 副作用未实现）。仅记日志提示该轮触发了工具调用。
        tool_call_name = result.get("tool_call")
        tool_call_args = result.get("tool_call_args")
        if tool_call_name:
            _llm_log(f"tool_call emitted (not executed, stage1): "
                     f"{tool_call_name} args={tool_call_args}")

        with self._lock:
            self.histories.setdefault(user_id, deque(maxlen=_HISTORY_MAX)).append(
                {"role": "user", "content": text, "ts": time.time()})
            if tool_call_name is not None:
                # 阶段1 不执行 tool_call —— 不写 JSON 字面量到 history 避免
                # 下轮 prompt 污染；用空 content + ``tool_call`` 标记占位。
                # 阶段2 引入 agent-loop + 结构化 history 后整体替换。
                self.histories[user_id].append({
                    "role": "assistant",
                    "content": "",
                    "ts": time.time(),
                    "tool_call": tool_call_name,
                    "_placeholder": True,
                })
            else:
                self.histories[user_id].append(
                    {"role": "assistant", "content": answer, "ts": time.time()})

        self._reply_no_context(user_id, answer)

    def _resolve_context_limit(self) -> int:
        try:
            from app.services.config import get_config
            cfg = get_config().get_section("compute", {})
            if isinstance(cfg, dict):
                tc = cfg.get("text_chat")
                if isinstance(tc, dict):
                    v = int(tc.get("context_limit") or 32768)
                    if v >= 1:
                        return v
        except Exception:  # noqa: BLE001
            pass
        return 32768

    def _resolve_tool_names(self) -> list:
        """列出来自 ``get_tool_registry()`` 的 ``@Callable`` class 名。

        阶段1：传所有已注册工具给 worker。微信用户当前主要场景是文字
        对话，但模型看到 ``tts_alert`` schema 后可能调它（参见
        text_chat._resolve_tool_names 注释）；阶段1 不执行 instance()，
        仅日志记录。阶段2 加白名单。
        """
        try:
            return [
                spec.tool_class.__name__
                for spec in get_tool_registry().list_specs()
            ]
        except Exception as exc:  # noqa: BLE001
            _llm_log(f"resolve_tool_names failed: {exc!r}")
            return []

    def close(self) -> None:
        self._queue.put(None)
        with self._lock:
            self._enabled = False
            self.histories.clear()


_bridge: Optional[_LlmBridge] = None
_bridge_lock = threading.Lock()


def get_llm_bridge() -> _LlmBridge:
    """lazy singleton。"""
    global _bridge
    with _bridge_lock:
        if _bridge is None:
            _bridge = _LlmBridge()
        return _bridge


def close_llm_bridge() -> None:
    """shutdown hook —— no-op if never created."""
    global _bridge
    with _bridge_lock:
        if _bridge is None:
            return
        _bridge.close()
        _bridge = None
