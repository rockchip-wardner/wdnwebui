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
* **只回 answer 不回 thinking**：最终轮输出已是面向用户的自然语言
  （正常回答 / wrap-up 确认语 / 失败回喂后的解释），直接发微信。
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

from app.services.tools import resolve_available_tool_names

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
        elif kind == "video":
            # 视频消息 → 微信视频管理器确定性分流：落盘 + 自动后台提取。
            # 不走 LLM 工具链路（提取是确定性事件无需智能决策，且不能
            # 让 CDN 下载阻塞消息回调线程 —— enqueue 纯内存入队毫秒级）。
            from app.services.wechat_video_manager import (
                get_wechat_video_manager,
            )
            try:
                get_wechat_video_manager().enqueue_video(user_id, msg)
            except Exception as exc:  # noqa: BLE001
                _LOG.exception("wechat video enqueue failed for %s", user_id)
                self._reply_no_context(user_id, f"视频接收失败：{exc}")
        else:
            self._reply_no_context(user_id, "当前仅支持文字与视频消息（图片 / 语音暂不支持）。")

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
        from app.services.chat_agent import run_chat_with_tools
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
        # 工具执行与 wrap-up / 回喂 / 失败告知循环由主进程侧 chat_agent 完成。
        try:
            with get_chat_run_lock():
                agent = run_chat_with_tools(
                    session,
                    history_snapshot,
                    text,
                    context_limit=context_limit,
                    max_new_tokens=self.MAX_NEW_TOKENS,
                    tool_names=self._resolve_tool_names(),
                    user_id=user_id,
                    channel="wechat",
                )
        except Exception as exc:  # noqa: BLE001
            _LOG.exception("chat_run failed for %s", user_id)
            self._reply_no_context(user_id, f"推理失败：{exc}")
            return

        if agent.get("interrupted"):
            return

        # 最终轮输出：正常回答 / wrap-up 确认语 / 失败回喂后的自然语言
        # 解释 —— 三种路径都已是面向用户的文本，直接发送。
        answer = (agent.get("answer") or "").strip()
        answer = answer or "（模型未输出有效内容）"

        steps = agent.get("steps") or []
        if steps:
            executed = "；".join(
                f"{s.get('tool_call')}({'成功' if not s.get('error') else '失败'})"
                for s in steps)
            _llm_log(f"agent-loop for {user_id}: {executed}")

        with self._lock:
            hist = self.histories.setdefault(user_id, deque(maxlen=_HISTORY_MAX))
            hist.append({"role": "user", "content": text, "ts": time.time()})
            appended = agent.get("history_append") or []
            for item in appended:
                hist.append(dict(item))
            if not appended and answer != "（模型未输出有效内容）":
                hist.append({
                    "role": "assistant", "content": answer,
                    "thinking": agent.get("thinking") or "",
                    "ts": time.time(),
                })

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
        """列出**当前可用**的 ``@Callable`` class 名（capability 过滤）。

        委托 ``resolve_available_tool_names()``：qwen3-tts 未加载时
        tts_alert 直接从 schema 排除，模型看不到就调不到，避免必败
        调用。新工具在 tools 包内 register 一行即自动纳入。
        """
        try:
            from app.services.tools import resolve_available_tool_names
            return resolve_available_tool_names()
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
