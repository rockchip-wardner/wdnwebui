"""文本对话服务：单例 + worker + RPC 流式。

设计要点
========

* **LLMSession 必须从 ComputeRegistry 取**：优先
  ``get_session_for_model("swcap_text_chat", "qwen3.5-vl-9b")``，找不到再回退
  ``qwen3.5-vl-4b``；拿到的是 ``wdndds.LLMRPCSession``，都找不到抛
  ``RuntimeError("功能未启用: swcap_text_chat")``。
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
* **上下文跟随模型**：``context_limit`` 从实际 session 的
  ``spec.max_context_len`` 读取；``max_new_tokens`` 固定使用共享
  ``chat_runner.MAX_NEW_TOKENS``（2048）。超限自动裁剪最旧历史，仅单条
  消息超限才报错。
* **停止 = 真中断**：``stop()`` 置 ``_worker_stop_event`` 并调 ``session.stop()``；
  标志经旁路 topic 送到 worker，``ServedSession.request_stop()`` 转发给
  ``LLMLocalSession.stop()``，由 rknn3 侧真正中止本轮并释放 NPU。
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
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, List, Optional

from app.core.chat.runner import MAX_NEW_TOKENS, split_thinking
from app.apps.text_chat.thinking_9b import Thinking9BParser, split_9b_thinking


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
    enable_thinking: bool = False


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
    #: 文本对话优先使用的模型。9B 占两张卡；4B 是回退（手动启用时）。
    PREFERRED_MODELS   = ("qwen3.5-vl-9b", "qwen3.5-vl-4b")

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
        self._active_model_id: Optional[str] = None
        self._context_limit: Optional[int] = None
        self._max_new_tokens: Optional[int] = None
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
    def _ensure_model_meta(self) -> None:
        """Populate model_id/context before the first request.

        Opening the page without sending (e.g. skipping the wizard) used to
        leave these fields at ``None`` even when a model was already
        running, so the token counter showed "模型未就绪". Enumeration via
        ``list_sessions`` does not invoke inference or emit the
        ``get_session_for_model`` info log on every 1 Hz snapshot.
        """
        from app.core.compute.registry import get_registry

        try:
            handles = get_registry().list_sessions(
                "swcap_text_chat", include_remote=True,
            )
        except Exception:  # noqa: BLE001
            handles = []
        selected = None
        for model_id in self.PREFERRED_MODELS:
            selected = next(
                (h for h in handles if h.model_id == model_id), None,
            )
            if selected is not None:
                break
        if selected is None:
            with self._lock:
                self._session = None
                self._active_model_id = None
                self._context_limit = None
                self._max_new_tokens = None
            return
        try:
            context_limit, max_new_tokens = self._limits_for_session(
                selected.session,
            )
        except RuntimeError:
            return
        with self._lock:
            self._session = selected.session
            self._active_model_id = selected.model_id
            self._context_limit = context_limit
            self._max_new_tokens = max_new_tokens

    def snapshot(self) -> dict:
        self._ensure_model_meta()
        with self._lock:
            return {
                "state":             self._state,
                "busy":              self._state in (self.STATE_LOADING, self.STATE_RUNNING),
                "error":             self._error,
                "history_count":     len(self._history),
                "history_tokens":    self._history_tokens,
                "model_id":          self._active_model_id,
                "context_limit":     self._context_limit,
                "max_new_tokens":    self._max_new_tokens,
                "current_request_id": self._current_request_id,
                "last_result":       self._last_result,
                "last_started":      self._last_started,
            }

    def history(self) -> list[dict]:
        """User-visible conversation history for a page reload.

        ``_history`` also carries internal tool transcript entries
        (``_system_note`` / ``_placeholder``); those are prompt plumbing, not
        chat bubbles. Only normal user/assistant turns are exposed here.
        """
        with self._lock:
            out: list[dict] = []
            for msg in self._history:
                if msg.get("_system_note") or msg.get("_placeholder"):
                    continue
                role = msg.get("role")
                content = msg.get("content")
                if role not in ("user", "assistant") or not content:
                    continue
                out.append({
                    "role": role,
                    "content": str(content),
                    "ts": float(msg.get("ts") or 0.0),
                })
            return out

    def _set_state(self, state: str, *, error: str = "") -> None:
        with self._lock:
            self._state = state
            self._error = error

    # -------------------------------------------------------------- REST API
    def send(self, text: str, *, enable_thinking: bool = False) -> int:
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
        req = ChatRequest(id=rid, text=text, ts=self._last_started,
                          enable_thinking=bool(enable_thinking))
        # 立即 echo 给前端（不用等 worker 排到队首）
        self._broadcast({"type": "user_echo", "request_id": rid, "text": text})
        self._broadcast({"type": "state", "state": self.snapshot()})
        self._request_queue.put(req)
        return rid

    def stop(self) -> None:
        """停止正在跑的推理；当前轮丢弃，不写历史。

        rknn3 支持中途取消：``session.stop()`` 把标志经旁路 DDS topic 送到
        worker，worker 侧 ``ServedSession.request_stop()`` 转发给
        ``LLMLocalSession.stop()``，最终由 ``rknn3_session_stop()`` 真正中止
        本轮。停止消息带 stream_id，上一轮的残留重发不会误杀下一轮。

        ``_worker_stop_event`` 是本地的一道：agent 在工具轮之间检查它，能在下一轮
        开始前就退出，不必等当前推理结束。
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
        # 1. 拿 LLMRPCSession：9B 优先，4B 回退。实际用哪个以运行中的实例为准。
        try:
            model_id, session = self._select_session()
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
        context_limit, max_new_tokens = self._limits_for_session(session)
        with self._lock:
            self._session = session
            self._active_model_id = model_id
            self._context_limit = context_limit
            self._max_new_tokens = max_new_tokens

        with self._lock:
            history_snapshot = list(self._history)

        if model_id == "qwen3.5-vl-9b":
            router = Thinking9BParser(
                emit_fn=lambda payload: self._broadcast(payload),
                request_id=req.id,
                enabled=req.enable_thinking,
            )
        else:
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
                    max_new_tokens=max_new_tokens,
                    enable_thinking=req.enable_thinking,
                    tool_names=self._resolve_tool_names(),
                    on_delta=lambda d: router.push(d),
                    retry_empty_answer=True,
                    on_retry=router.begin_retry,
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

        # Flush whatever the router still holds (a short answer with no
        # closing marker never crosses the tail buffer otherwise).
        router.flush()

        # 7. 检查是否被 stop 中断 —— 丢弃本轮
        stopped = (
            self._worker_stop_event.is_set() or agent.get("interrupted"))

        # 8. thinking / answer：agent 返回最终轮输出（正常回答、
        # wrap-up 确认语或失败文案）。worker 已 split_thinking；
        # result.text 本地兜底仅在两者皆空时使用。
        thinking = agent.get("thinking") or ""
        answer = agent.get("answer") or ""
        if self._active_model_id == "qwen3.5-vl-9b":
            thinking, answer = split_9b_thinking(answer, req.enable_thinking)
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

    def _select_session(self):
        from app.core.compute.registry import get_registry

        reg = get_registry()
        last_error: Optional[RuntimeError] = None
        for model_id in self.PREFERRED_MODELS:
            try:
                return model_id, reg.get_session_for_model(
                    "swcap_text_chat", model_id,
                )
            except RuntimeError as e:
                last_error = e
        raise RuntimeError("功能未启用: swcap_text_chat") from last_error

    @staticmethod
    def _limits_for_session(session):
        spec = getattr(session, "spec", None)
        context_limit = int(getattr(spec, "max_context_len", 0) or 0)
        if context_limit <= 0:
            raise RuntimeError("模型未提供 max_context_len，无法计算上下文窗口")
        return context_limit, MAX_NEW_TOKENS

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
    """Split streamed deltas into the thinking / answer channels.

    Two output shapes have to work, and both were measured on Qwen3.5-4B:

    * thinking **on** — the C side injects ``<think>`` into the assistant
      prefix, so the stream starts with the reasoning text and only the
      closing ``</think>`` appears in the deltas;
    * thinking **off** — the model emits an empty ``<think>\n\n</think>``
      block, then the answer.

    Deltas can split either tag across calls (``<th`` + ``ink>``), so the
    router buffers instead of using ``str.find`` on a single delta. An
    empty thinking segment is not emitted at all: the page hides the
    thought block when there is nothing to show, and a transient empty
    block is exactly the bug this replaces.
    """

    _OPEN = "<think>"
    _CLOSE = "</think>"
    _PREFIX_LIMIT = 64          # give up tag detection on pathological whitespace
    # 模型偶发模仿 <answer> / </answer>（重试时尤其常见）。done 帧由
    # sanitize_chat_output 清理；流式这里也过滤，并保留尾部 8 字符，
    # 避免标签被 delta 切开时漏半截。
    _ANSWER_TAG_RE = re.compile(r"</?answer>", re.IGNORECASE)
    # 保留**完整标签长度**（9），而不是 len-1：标签可能整段落在
    # 下一段 delta 的开头，只留 8 个字符会缺首字符而识别不出来。
    _ANSWER_KEEP = max(len("<answer>"), len("</answer>"))

    def __init__(
        self,
        emit_fn: Callable[[dict], None],
        request_id: int,
        *,
        start_mode: str = "thinking",
        boundary: str = "</think>",
        start_seen: bool = False,
        saw_open: bool = False,
    ) -> None:
        self._emit = emit_fn
        self._request_id = request_id
        self._start_mode = start_mode
        self._boundary = boundary
        self._start_seen_default = start_seen
        self._saw_open_default = saw_open
        self._mode = start_mode          # thinking / answer
        self._buf = ""
        self._start_seen = start_seen
        self._saw_open = saw_open
        self._saw_close = False
        self._suppress_thinking = False
        self._answer_buf = ""

    def begin_retry(self) -> None:
        """切换为「只流式答案」：重试的思考丢弃，第一次的思考已展示过。"""
        # 先把第一次残留的尾部补发，否则 done 帧的 thinking 会比流式
        # 拼出来的多几个字符。
        self.flush()
        self._mode = self._start_mode
        self._buf = ""
        self._start_seen = self._start_seen_default
        self._saw_open = self._saw_open_default
        self._saw_close = False
        self._suppress_thinking = True
        self._answer_buf = ""

    def _emit_thinking(self, text: str) -> None:
        if self._suppress_thinking:
            return
        if text and text.strip():
            self._emit({
                "type": "thinking",
                "request_id": self._request_id,
                "delta": text,
            })

    def _emit_answer(self, text: str) -> None:
        if not text:
            return
        # 先拼接、再对整段过滤：标签可能被 delta 切成 <ans + wer>，
        # 单段过滤会漏掉（Real bug 实测：跨 delta 时 <answer> 漏进答案）。
        # 尾部保留完整标签长度，保证任何未完成的标签都还在缓冲里。
        self._answer_buf += text
        self._answer_buf = self._ANSWER_TAG_RE.sub("", self._answer_buf)
        keep = self._ANSWER_KEEP
        if len(self._answer_buf) > keep:
            out = self._answer_buf[:-keep]
            self._answer_buf = self._answer_buf[-keep:]
            self._emit({
                "type": "answer",
                "request_id": self._request_id,
                "delta": out,
            })

    def push(self, delta: str) -> None:
        if not delta:
            return
        self._buf += delta

        if not self._start_seen:
            stripped = self._buf.lstrip()
            if len(stripped) < len(self._OPEN) and self._OPEN.startswith(stripped):
                # Still a possible ``<think>`` prefix (or only whitespace).
                if len(self._buf) <= self._PREFIX_LIMIT:
                    return
                self._start_seen = True           # pathological: give up
            elif stripped.startswith(self._OPEN):
                self._buf = stripped[len(self._OPEN):]
                self._start_seen = True
                self._saw_open = True
            else:
                self._start_seen = True

        if self._mode == "thinking":
            boundary = self._boundary or self._CLOSE
            idx = self._buf.find(boundary)
            if idx >= 0:
                head = self._buf[:idx]
                rest = self._buf[idx + len(boundary):]
                self._saw_close = True
                self._emit_thinking(head)
                self._mode = "answer"
                self._buf = ""
                self._emit_answer(rest)
                return
            keep = len(boundary) - 1
            if len(self._buf) > keep:
                emit = self._buf[:-keep]
                self._buf = self._buf[-keep:]
                self._emit_thinking(emit)
        else:
            emit = self._buf
            self._buf = ""
            self._emit_answer(emit)

    def flush(self) -> None:
        """Emit the tail buffer when the turn ends (called after the agent)."""
        rest = self._buf
        self._buf = ""
        if rest:
            if self._mode == "thinking":
                if not self._saw_open and not self._saw_close:
                    # Plain answer with no markers at all (model ignored the
                    # template): do not mislabel it as thinking.
                    self._emit_answer(rest)
                else:
                    # An unterminated thought block is still thinking.
                    self._emit_thinking(rest)
            else:
                self._emit_answer(rest)
        if self._answer_buf:
            out = self._answer_buf
            self._answer_buf = ""
            self._emit({
                "type": "answer",
                "request_id": self._request_id,
                "delta": out,
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