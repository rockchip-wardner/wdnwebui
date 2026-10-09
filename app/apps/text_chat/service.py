"""文本对话服务：单例 + worker + RPC 流式。

设计要点
========

* **LLMSession 必须从 ComputeRegistry 取**（``get_session("swcap_text_chat")``）；
  拿到的是 ``wdndds.LLMRPCSession``，找不到抛
  ``RuntimeError("功能未启用: swcap_text_chat")``，调用方决定如何上报。
* **推理在 compute_worker 子进程里跑**：``session.respond(...)`` 经 wdndds
  RPC 发出 ``{prompt, history, tools, options}``；worker 跑完
  ``LLMLocalSession`` 并把**原始文本**回传。ChatML 拼接在调用方完成
  （``wdnai.build_chatml_prompt``），工具 JSON 的解析也在调用方完成 ——
  worker 不认识任何 schema 或工具。
* **流式走 ``on_delta`` 回调**：delta 从一条独立的 RELIABLE 旁路 topic
  回来（reply 通道在推理期间是聋的），喂给 ``_ThinkingRouter`` 流式切分
  thinking / answer。没有轮询，也没有 chat state 机。
* **thinking 与 answer 分离**：每个 delta 增量先过一道 ``_ThinkingRouter``
  状态机，按 `` thinking`` / `` response`` 推到对应频道；``done`` 时再
  用 ``split_thinking`` 兜底切一次（处理 token 边界切碎等极端情况）。
* **多轮上下文**：app 端消息 deque 全量交给 agent；agent 用 worker 的
  tokenizer 精确计数（一次无推理的 ``count_tokens`` 控制调用）从最旧历史
  滚动裁剪（超 ``context_limit - max_new_tokens`` 就丢更早的）。历史只写
  ``content:answer``，不写 thinking，下一轮看不到。
* **context_limit 可配置**：``config.jsonc["compute"]["text_chat"]["context_limit"]``，
  默认 4096（= qwen3.5-vl-4b 的 max_context_len）；每次推理前热读。超限自动裁剪最旧历史，仅单条消息超限才报错。
* **停止 = 软停**：``stop()`` 置 ``_worker_stop_event`` 并调 ``session.stop()``；
  后者把取消标志发到旁路 topic，worker 跑完本轮看到后回报 stopped，本轮丢弃。
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

from app.core.chat.runner import split_thinking
from app.core.config import get_config


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
    # 对话窗口上限。**必须 ≤ 实际服务模型的 max_context_len**，否则
    # 裁剪会放进一个装不下的历史 —— 症状不是报错，而是 rknn3 在
    # prefill 阶段长挂（见 VAS ``_call_llm`` 的同款诊断注释）。
    #
    # 4096 是 ``swcap_text_chat`` 唯一 provider
    # ``qwen3.5-vl-4b`` 的真实窗口（原 qwen3-4b 专用文本 worker 已
    # 删除，它的 16384 不再适用）。换 provider 时必须同步改这里 ——
    # 这是 manifest 与 spec 之间唯一需要手工对齐的常量。
    CONTEXT_LIMIT      = 4096

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
        # Handle on the in-flight session, so ``stop()`` can publish a
        # cancel out-of-band while a turn is blocked in inference.
        self._session = None
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

        rknn3 没有协作式取消，in-flight 的 ``session_run`` 中断不了。
        所以停止是"软停"：``session.stop()`` 把取消标志发布到一条旁路
        DDS topic（不等任何 RPC 应答），worker 跑完那一轮推理后看到标志、
        回报 ``stopped``，``respond()`` 随即抛错，本轮结果被丢弃、不写
        历史。

        另有一道本地 ``_worker_stop_event``：工具轮之间 agent 会检查它，
        能在下一轮开始前就退出，不必等当前推理结束。

        用户体感和 UI 都是"已停"；下一轮 ``session.reset()`` 仍要等
        worker 把当前这一轮跑完。
        """
        with self._lock:
            if self._state != self.STATE_RUNNING:
                return
            session = self._session
        self._worker_stop_event.set()
        if session is not None:
            try:
                session.stop()
            except Exception as e:  # noqa: BLE001 — cancel is best-effort
                _LOG.debug("session.stop() failed (best-effort): %s", e)
        _LOG.info("stop requested: worker discards this turn once inference returns")

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
        from app.core.compute.registry import get_registry

        # 1. 拿 LLMRPCSession（ComputeRegistry 不存在 / capability 未启用 → RuntimeError）
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
            self._session = session

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
            from app.core.chat.agent import run_chat_with_tools
            from app.core.chat.serial import get_chat_run_lock
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
                    channel="web",
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
        """读 ``config.jsonc["compute"]["text_chat"]["context_limit"]``。

        缺失 / 类型错 / <1 → 回退 :attr:`CONTEXT_LIMIT`（4096，即 qwen3.5-vl-4b
        的真实窗口）。每次推理前重读，配置热生效 —— 但用户把它调大到
        模型装不下的值时不会被拦，只会变成 prefill 长挂。
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
        """列出**当前可用**的 ``@Callable`` class 名（capability + 渠道过滤）。

        委托 ``resolve_available_tool_names(channel="chat_web")``：
        ``requires_capability`` 对应模型未加载的工具直接从 schema 中
        排除，模型看不到就调不到；同时工具 ``channels`` 限定只在微信
        渠道生效的（如 ``VideoSearch`` —— 依赖 ``ctx.user_id`` 关联
        用户视频,web 端 ``user_id`` 始终空,工具必败）也不会注入,
        避免模型误调浪费一轮推理。
        """
        try:
            from app.core.tools.registry import resolve_available_tool_names
            return resolve_available_tool_names(channel="chat_web")
        except Exception as e:  # noqa: BLE001
            _LOG.warning("resolve_tool_names failed: %s", e)
            return []


# --------------------------------------------------------------------------- #
# ThinkingRouter：observer 每次 delta 进来调用，按  thinking 边界切频道
# --------------------------------------------------------------------------- #
class _ThinkingRouter:
    """增量状态机：维护一个游标决定下一段 delta 推 thinking 还是 answer。

    进入点固定假设模型先输出 thinking（用户已确认对话模型走 enable_thinking
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