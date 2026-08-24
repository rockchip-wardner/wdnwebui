"""Qwen3-8B 文本对话服务：单例 + worker + RPC 流式拉取。

设计要点
========

* **LLMSession 必须从 ComputeRegistry 取**（``get_session("swcap_text_chat")``）；
  拿到的是 ``RpcSessionProxy``，找不到抛
  ``RuntimeError("功能未启用: swcap_text_chat")``，调用方决定如何上报。
* **推理在 compute_worker 子进程里跑**：``session.chat_run(...)`` 通过 wdndds
  RPC 把 ``{history, text, context_limit, max_new_tokens}`` 发给 worker；
  worker 后台线程用 tokenizer 精确裁剪历史、拼 ChatML、跑
  ``_backend.run``，边跑边把 ``sink.text`` 增量收进 chat state。
  主进程 ``chat_poll`` 轮询拉增量，``on_delta`` 喂给 ``_ThinkingRouter``
  流式切分 thinking / answer。
* **thinking 与 answer 分离**：每个 delta 增量先过一道 ``_ThinkingRouter``
  状态机，按 `` thinking`` / `` response`` 推到对应频道；``done`` 时再
  用 ``split_thinking`` 兜底切一次（处理 token 边界切碎等极端情况）。
* **多轮上下文**：app 端消息 deque 全量发给 worker；worker 用真实 tokenizer
  从最旧历史滚动裁剪（超 ``context_limit - max_new_tokens`` 就丢更早的），
  拼 ChatML 后 ``keep_history=0`` 跑 —— 历史只写 ``content:answer``，
  不写 thinking，下一轮看不到。
* **context_limit 可配置**：``config.json["compute"]["text_chat"]["context_limit"]``，
  默认 32768；每次推理前热读。超限自动裁剪最旧历史，仅单条消息超限才报错。
* **停止 = 软停**：``stop()`` 置 ``_worker_stop_event``；proxy 的 ``chat_run``
  轮询循环感知后调 ``chat_stop`` RPC，worker 跑完返回 stopped，主进程丢本轮。
* **broadcast pattern**：worker 线程 push 到 ``asyncio.Queue`` 列表；
  WS handler 在主 event loop 里 ``drain``。

状态机
======

::

    idle ──(send)──> loading ──(session ready)──> running ──(run returns)──> done
                                            └─(RuntimeError)──> error
    running ──(stop)──> stopped   # 当前轮丢弃，不写历史
    done ──(reset)──> idle        # 清空 history + history_tokens
    error ──(send)──> loading     # 重试

WS 帧类型
=========

* ``snapshot`` —— 连接建立后立即推一次（与 wechat 一致）。
* ``state`` —— 状态字段 diff。
* ``user_echo`` —— 用户消息 echo 回前端（前端用它来立即渲染 user 气泡）。
* ``thinking`` —— `` thinking... response`` 段增量。
* ``answer`` —— `` response`` 之后的回答增量。
* ``stopped`` —— 用户中途停止。
* ``done`` —— 一轮推理结束，附完整 thinking/answer + 性能数据。
* ``error`` —— 推理异常或 capability 未启用。

单例：``get_chat_service()``，lazy 构造，与 wdnwebui 其它服务风格一致。
"""
from __future__ import annotations

import asyncio
import logging
import queue as _queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, List, Optional

from app.services.chat_runner import split_thinking
from app.services.config import get_config


_LOG = logging.getLogger("text_chat")


# Max buffered frames per WS subscriber before the oldest is dropped.
_SUB_QUEUE_MAX = 500


# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Request dataclass
# --------------------------------------------------------------------------- #
@dataclass
class ChatRequest:
    """单条用户请求；worker 串行消费。"""

    id:        int   = 0
    text:      str   = ""
    ts:        float = 0.0


# --------------------------------------------------------------------------- #
# Service singleton
# --------------------------------------------------------------------------- #
class _TextChatManager:
    """文本对话服务；单例（``get_chat_service()``）。"""

    # ---- 状态常量 ----
    STATE_IDLE         = "idle"
    STATE_LOADING      = "loading"
    STATE_RUNNING      = "running"
    STATE_DONE         = "done"
    STATE_ERROR        = "error"
    STATE_STOPPED      = "stopped"

    # ---- 限制 ----
    HISTORY_MAX        = 200          # deque maxlen
    MAX_NEW_TOKENS     = 2048         # spec max_new_tokens
    CONTEXT_LIMIT      = 32768        # spec max_context_len

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._state: str = self.STATE_IDLE
        self._error: str = ""
        self._history: Deque[dict] = deque(maxlen=self.HISTORY_MAX)
        self._history_tokens: int = 0

        self._current_request_id: Optional[int] = None
        self._last_result: Optional[dict] = None     # {thinking, answer, elapsed_sec, ts}
        self._last_started: float = 0.0

        # 单 worker 线程，串行消费请求
        self._worker_thread: Optional[threading.Thread] = None
        self._worker_stop_event = threading.Event()
        self._request_queue: "_queue.Queue[Optional[ChatRequest]]" = _queue.Queue()
        self._wake_event = threading.Event()

        # WS broadcaster
        self._subs_lock = threading.Lock()
        self._subs: List[asyncio.Queue] = []

        # 启动 worker
        self._worker_thread = threading.Thread(
            target=self._worker_loop, name="text-chat-worker", daemon=True,
        )
        self._worker_thread.start()

    # -------------------------------------------------------------- WS
    def register_subscriber(self, q: asyncio.Queue) -> None:
        with self._subs_lock:
            self._subs.append(q)

    def unregister_subscriber(self, q: asyncio.Queue) -> None:
        with self._subs_lock:
            try:
                self._subs.remove(q)
            except ValueError:
                pass

    def _broadcast(self, payload: dict) -> None:
        """跨线程 push；每个 subscriber 一个 asyncio.Queue，溢出丢最旧。"""
        with self._subs_lock:
            queues = list(self._subs)
        if not queues:
            return
        for q in queues:
            try:
                if q.full():
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                q.put_nowait(payload)
            except Exception as e:  # noqa: BLE001
                _LOG.warning("broadcast failed: %s", e)

    # -------------------------------------------------------------- 状态
    def snapshot(self) -> dict:
        with self._lock:
            return {
                "state":             self._state,
                "busy":              self._state in (self.STATE_LOADING, self.STATE_RUNNING),
                "error":             self._error,
                "history_count":     len(self._history),
                "history_tokens":    self._history_tokens,
                "context_limit":     self._resolve_context_limit(),
                "max_new_tokens":    self.MAX_NEW_TOKENS,
                "current_request_id": self._current_request_id,
                "last_result":       self._last_result,
                "last_started":      self._last_started,
            }

    def _set_state(self, state: str, *, error: str = "") -> None:
        with self._lock:
            self._state = state
            self._error = error

    # -------------------------------------------------------------- REST API
    def send(self, text: str) -> int:
        """发起一轮推理；返回 request_id；上一轮还在跑抛 ValueError。"""
        text = (text or "").strip()
        if not text:
            raise ValueError("text 不能为空")
        with self._lock:
            if self._state in (self.STATE_LOADING, self.STATE_RUNNING):
                raise ValueError("上一条请求还在生成中")
            rid = self._next_request_id()
            self._current_request_id = rid
            self._last_started = time.time()
            self._set_state(self.STATE_LOADING)
        req = ChatRequest(id=rid, text=text, ts=self._last_started)
        # 立即 echo 给前端（不用等 worker 排到队首）
        self._broadcast({"type": "user_echo", "request_id": rid, "text": text})
        self._broadcast({"type": "state", "state": self.snapshot()})
        self._request_queue.put(req)
        return rid

    def stop(self) -> None:
        """请求停止正在跑的推理。

        v0.4 起 worker 侧 ``sess.respond`` 是阻塞 + 不可中断的（cshim
        RPC 还没有 streaming/cancel 通路），主进程没有 cancel RPC 通道。
        本方法只翻状态 + 置位 ``_worker_stop_event``——worker 跑完
        那一轮 ``await sess.respond()`` 后看到 ``stopped`` 就丢弃本轮
        结果，不写历史。UI 端立即显示 "已停止"；用户体感是 "已停"，但
        下一轮 ``session.reset()`` 仍要等 worker 把当前一轮跑完。
        """
        with self._lock:
            if self._state != self.STATE_RUNNING:
                return
        self._worker_stop_event.set()
        _LOG.info("stop requested: worker will discard result after respond() returns")

    def reset(self) -> None:
        """清空 history + history_tokens，回到 idle。"""
        with self._lock:
            self._history.clear()
            self._history_tokens = 0
            self._current_request_id = None
            self._last_result = None
            self._set_state(self.STATE_IDLE)
        self._broadcast({"type": "state", "state": self.snapshot()})

    def _next_request_id(self) -> int:
        # 简单自增；自启动以来累加，重启清零
        if not hasattr(self, "_id_seq"):
            self._id_seq = 0
        self._id_seq += 1
        return self._id_seq

    # -------------------------------------------------------------- worker
    def _worker_loop(self) -> None:
        while True:
            try:
                req = self._request_queue.get(timeout=1.0)
            except _queue.Empty:
                continue
            if req is None:
                break
            self._worker_stop_event.clear()
            try:
                self._run_one(req)
            except Exception as e:  # noqa: BLE001
                _LOG.exception("worker _run_one crashed: %s", e)
                with self._lock:
                    self._set_state(self.STATE_ERROR, error=str(e))
                self._broadcast({
                    "type": "error",
                    "request_id": req.id,
                    "detail": f"worker 内部异常: {e}",
                })
                self._broadcast({"type": "state", "state": self.snapshot()})

    def _run_one(self, req: ChatRequest) -> None:
        """一轮推理：拿 proxy session → 拼 ChatML → 上下文检查 → reset+respond →
        切 thinking/answer → 推 WS → 写历史。"""
        from app.services.compute_registry import get_registry, get_session_call_lock

        # 1. 拿 _RpcSessionProxy（ComputeRegistry 不存在 / capability 未启用 → RuntimeError）
        try:
            session = get_registry().get_session("swcap_text_chat")
        except RuntimeError as e:
            with self._lock:
                self._set_state(self.STATE_ERROR, error=str(e))
            self._broadcast({
                "type": "error",
                "request_id": req.id,
                "detail": str(e),
            })
            self._broadcast({"type": "state", "state": self.snapshot()})
            return

        with self._lock:
            history_snapshot = list(self._history)
        context_limit = self._resolve_context_limit()

        router = _ThinkingRouter(
            emit_fn=lambda payload: self._broadcast(payload),
            request_id=req.id,
        )

        # 5. state → running
        with self._lock:
            self._set_state(self.STATE_RUNNING)
        self._broadcast({"type": "state", "state": self.snapshot()})

        # 6. RPC 推理（worker 端裁剪 + 跑；主进程轮询拉增量）。
        # chat_agent 承担工具执行与多轮回喂：模型发起 tool_call 时在
        # 主进程实例化 @Callable 并执行副作用，wrap-up / transcript
        # 回喂继续推理，直到给出自然语言回答或达到循环上限。
        t0 = time.perf_counter()
        try:
            from app.services.chat_agent import run_chat_with_tools
            from app.services.chat_serial import get_chat_run_lock
            with get_chat_run_lock():
                agent = run_chat_with_tools(
                    session,
                    history_snapshot,
                    req.text,
                    context_limit=context_limit,
                    max_new_tokens=self.MAX_NEW_TOKENS,
                    tool_names=self._resolve_tool_names(),
                    on_delta=lambda d: router.push(d),
                    stop_event=self._worker_stop_event,
                )
            elapsed = time.perf_counter() - t0
        except Exception as e:  # noqa: BLE001
            with self._lock:
                self._set_state(self.STATE_ERROR, error=str(e))
            self._broadcast({
                "type": "error",
                "request_id": req.id,
                "detail": str(e),
            })
            self._broadcast({"type": "state", "state": self.snapshot()})
            return

        # 7. 检查是否被 stop 中断 —— 丢弃本轮
        stopped = (
            self._worker_stop_event.is_set() or agent.get("interrupted"))

        # 8. thinking / answer：agent 返回最终轮输出（正常回答、
        # wrap-up 确认语或失败文案）。worker 已 split_thinking；
        # result.text 本地兜底仅在两者皆空时使用。
        thinking = agent.get("thinking") or ""
        answer = agent.get("answer") or ""
        if not thinking and not answer:
            full_text = ""
            thinking, answer = split_thinking(full_text)
        tool_call_name = agent.get("tool_call")
        tool_call_args = agent.get("tool_call_args")
        steps = agent.get("steps") or []

        if stopped:
            with self._lock:
                self._set_state(self.STATE_STOPPED)
            self._broadcast({
                "type": "stopped",
                "request_id": req.id,
                "thinking": thinking,
                "answer": answer,
                "elapsed_sec": elapsed,
            })
            self._broadcast({"type": "state", "state": self.snapshot()})
            return

        # 9. 写回 history。无工具轮：仅 answer 条目；工具轮：extend
        # chat_agent 返回的 history_append —— 含语义化 assistant 占位
        # （模型能看到自己调用过什么）、user 系统指令条目和最终回答，
        # 保证下轮 prompt 轮次节奏完整。
        prompt_tokens = int(agent.get("prompt_tokens") or 0)
        with self._lock:
            self._history.append({"role": "user", "content": req.text, "ts": req.ts})
            self._history_tokens = prompt_tokens
            appended = agent.get("history_append") or []
            for item in appended:
                self._history.append(dict(item))
            if not appended and answer:
                self._history.append({
                    "role": "assistant", "content": answer,
                    "thinking": thinking, "ts": time.time(),
                })
            self._last_result = {
                "thinking":   thinking,
                "answer":     answer,
                "elapsed_sec": elapsed,
                "ts":         time.time(),
                # tool_call：最后一次工具调用记录（None = 纯文本轮）；
                # steps 是每次执行的明细（args/outcome/error），前端
                # 工具徽章与调试用。
                "tool_call":     tool_call_name,
                "tool_call_args": tool_call_args,
                "steps":         steps,
            }
            self._set_state(self.STATE_DONE)

        self._broadcast({
            "type": "done",
            "request_id": req.id,
            "thinking":   thinking,
            "answer":     answer,
            "elapsed_sec": elapsed,
            "history_tokens": self._history_tokens,
            "tool_call":     tool_call_name,
            "tool_call_args": tool_call_args,
            "steps":         steps,
        })
        self._broadcast({"type": "state", "state": self.snapshot()})

    def _resolve_context_limit(self) -> int:
        """读 ``config.json["compute"]["text_chat"]["context_limit"]``。

        默认 32768；缺失 / 类型错 / <1 → 回退默认。每次推理前重读，配置热生效。
        """
        try:
            cfg = get_config().get_section("compute", {})
            if not isinstance(cfg, dict):
                return self.CONTEXT_LIMIT
            tc = cfg.get("text_chat")
            if not isinstance(tc, dict):
                return self.CONTEXT_LIMIT
            v = int(tc.get("context_limit") or self.CONTEXT_LIMIT)
            if v < 1:
                return self.CONTEXT_LIMIT
            return v
        except Exception:  # noqa: BLE001
            return self.CONTEXT_LIMIT

    def _resolve_tool_names(self) -> List[str]:
        """列出**当前可用**的 ``@Callable`` class 名（capability 过滤）。

        委托 ``resolve_available_tool_names()``：requires_capability
        对应模型未加载的工具直接从 schema 中排除，模型看不到就调不到。
        """
        try:
            from app.services.tools import resolve_available_tool_names
            return resolve_available_tool_names()
        except Exception as e:  # noqa: BLE001
            _LOG.warning("resolve_tool_names failed: %s", e)
            return []


# --------------------------------------------------------------------------- #
# ThinkingRouter：observer 每次 delta 进来调用，按  thinking 边界切频道
# --------------------------------------------------------------------------- #
class _ThinkingRouter:
    """增量状态机：维护一个游标决定下一段 delta 推 thinking 还是 answer。

    进入点固定假设模型先输出 thinking（用户已确认 Qwen3-8B 走 enable_thinking
    必输出 ``<think>...</think>``）；首次见到 ``</think>`` 后切到 answer。
    """

    def __init__(self, emit_fn: Callable[[dict], None], request_id: int) -> None:
        self._emit = emit_fn
        self._request_id = request_id
        self._mode = "thinking"          # thinking / answer
        self._sent_start_marker = False

    def push(self, delta: str) -> None:
        if not delta:
            return
        if self._mode == "thinking":
            end_idx = delta.find("</think>")
            if end_idx >= 0:
                head = delta[:end_idx]
                if head:
                    self._emit({
                        "type": "thinking",
                        "request_id": self._request_id,
                        "delta": head,
                    })
                self._mode = "answer"
                rest = delta[end_idx + len("</think>"):]
                if rest:
                    self._emit({
                        "type": "answer",
                        "request_id": self._request_id,
                        "delta": rest,
                    })
            else:
                # 整个 delta 还在 thinking 段
                self._emit({
                    "type": "thinking",
                    "request_id": self._request_id,
                    "delta": delta,
                })
        else:
            self._emit({
                "type": "answer",
                "request_id": self._request_id,
                "delta": delta,
            })


# --------------------------------------------------------------------------- #
# Singleton
# --------------------------------------------------------------------------- #
_service: Optional[_TextChatManager] = None
_service_lock = threading.Lock()


def get_chat_service() -> _TextChatManager:
    """lazy singleton。线程安全。"""
    global _service
    with _service_lock:
        if _service is None:
            _service = _TextChatManager()
        return _service


def close_chat() -> None:
    """app shutdown：停 worker（无原生句柄可释放，ComputeRegistry 负责）。"""
    global _service
    with _service_lock:
        if _service is None:
            return
        # wake worker 让其退出
        _service._request_queue.put(None)       # type: ignore[union-attr]
        _service._worker_stop_event.set()
        t = _service._worker_thread
        if t is not None and t.is_alive():
            t.join(timeout=2.0)
        _service = None
        _LOG.info("text-chat service closed")