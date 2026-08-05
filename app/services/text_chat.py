"""Qwen3-8B 文本对话服务：单例 + worker + 流式 observer。

设计要点
========

* **LLMSession 必须从 ComputeRegistry 取**（``get_session("swcap_text_chat")``）；
  找不到抛 ``RuntimeError("功能未启用: swcap_text_chat")``，调用方决定如何上报。
* **rkai 导入懒加载**：模块顶部只 import ``os / re / queue / threading / asyncio``；
  rkai / rknn3lite 只在第一次 ``_TextChatManager`` 实例化时 import。
* **流式回调 = monkey-patch**：模块加载时把
  ``rkai.backends._common.make_result_callback`` 替换成包装版（仅在 chat 路径
  注册了 observer 才推送，未注册的 sink 走原 callback，零开销）。
* **thinking 与 answer 分离**：observer 拿到的每个 delta 增量先过一道
  ``_ThinkingRouter`` 状态机，按 ``<think>`` / ``</think>`` 推到对应频道；
  ``done`` 时再用正则兜底切一次（处理 token 边界切碎等极端情况）。
* **多轮上下文**：app 端消息 deque + 每轮 ``session.reset()`` 清 C 端 KV，
  再用 ChatML 拼整段 prompt 喂进 ``session._backend.run``。``keep_history=0``
  透传到 ``rknn.session_run`` —— KV 在单次 run 期间保留，跨轮由 app 重 prompt
  重建。注意：历史中**只写 ``content:answer``**，不写 thinking，下一轮看不到。
* **32K token 限制**：每收到 ``done`` 用 ``tokenizer.encode(content)`` 累加
  ``history_tokens``；超过 ``context_limit - max_new_tokens`` 拒绝发新请求。
* **停止 = 真中断**：worker 在 ``session_run`` 阻塞期间监听 ``_stop_event``；
  ``stop()`` 把 ``_stop_event`` 置位并调 ``session._backend.rknn.session_stop()``，
  C 库收到 stop 信号后回调走 FINISH 状态（state=3），``session_run`` 返回。
  当前轮丢弃，不写历史。
* **broadcast pattern**：worker 线程 push 到 ``asyncio.Queue`` 列表；
  WS handler 在主 event loop 里 ``drain``。observer 也走同一条路径，避免
  引入第二条 channel。

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
* ``thinking`` —— ``<think>...</think>`` 段增量。
* ``answer`` —— ``</think>`` 之后的回答增量。
* ``stopped`` —— 用户中途停止。
* ``done`` —— 一轮推理结束，附完整 thinking/answer + 性能数据。
* ``error`` —— 推理异常或 capability 未启用。

单例：``get_chat_service()``，lazy 构造，与 pyrkaiweb 其它服务风格一致。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import queue as _queue
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Optional


_LOG = logging.getLogger("text_chat")


# Max buffered frames per WS subscriber before the oldest is dropped.
_SUB_QUEUE_MAX = 500


# --------------------------------------------------------------------------- #
# Monkey-patch: 流式 observer 注入
# --------------------------------------------------------------------------- #
# **关键时序**：必须在本模块被 import 时立即安装（模块顶层）—— 而不是
# 推迟到 ``_TextChatManager.__init__``。原因：``LLMSession.__init__`` 内部
# 调 ``_common.make_result_callback(sink)`` **一次性**把 callback 绑进
# ``RKNN3Lite.init_runtime(..., llm_callback=cb, ...)``。如果 patch 在
# ``LLMSession`` 构造**之后**才装，新 callback 不会生效，C 库触发的是旧
# callback（不通知 observer），用户看到的就是 "模型没开始推理 / 一直
# running 没结果 / 直接 done 但内容空"。
#
# 模块顶层 ``import rkai.backends._common`` 不触发 RKNN3 native lib 加载
# （_common 只 import dataclasses / typing / time，没有 rknn3lite），所以
# 这里 import 安全。
#
# 其它业务（detector / tts / vlm）未注册 observer 时走原 callback，零开销。
_OBSERVERS: Dict[int, Callable[[str], None]] = {}
_OBSERVER_LOCK = threading.Lock()
_PATCH_DONE = False


def _install_patches() -> None:
    """（兼容旧调用点）模块顶层已经装过一次，这里仅做幂等保护。"""
    global _PATCH_DONE
    if _PATCH_DONE:
        return
    _do_install_patches()


def _do_install_patches() -> None:
    """真正执行 monkey-patch；模块顶层 import 时直接调，不走 __init__ 懒加载。"""
    global _PATCH_DONE
    if _PATCH_DONE:
        return
    import rkai.backends._common as _common
    _original_factory = _common.make_result_callback

    # 内部状态常量（与 _common._STATE_NORMAL / _STATE_FINISH 对齐）
    _STATE_NORMAL = 0
    _STATE_FINISH_STATES = (2, 3, 4)

    def _patched(sink):
        inner_cb = _original_factory(sink)
        sink_id = id(sink)
        last_text_ref = [""]            # closure-private；不动 sink.text 避免冲突

        def _cb_with_observer(userdata, result_ptr, state):
            # **必须显式 return** —— ``LLMResultCallback = CFUNCTYPE(c_int, ...)``
            # 要求返回值是 ``c_int``；不 return 会隐式 None，C 端转 int 抛
            # ``TypeError: 'NoneType' object cannot be interpreted as an integer``，
            # 然后 ctypes 打印 "Exception ignored on converting result of ctypes
            # callback function" 并**吞掉**这个错误，导致原 callback 的副作用
            # （tokens 累积、state=normal 时的 sink.text 更新）实际上没有发生。
            ret = inner_cb(userdata, result_ptr, state)
            if state == _STATE_NORMAL or state in _STATE_FINISH_STATES:
                with _OBSERVER_LOCK:
                    obs = _OBSERVERS.get(sink_id)
                if obs is not None:
                    new_text = sink.text
                    old_text = last_text_ref[0]
                    if new_text != old_text:
                        delta = new_text[len(old_text):]
                        last_text_ref[0] = new_text
                        try:
                            obs(delta)
                        except Exception as e:  # noqa: BLE001
                            _LOG.warning("observer raised: %s", e)
            return ret

        from rknn3lite.api import LLMResultCallback
        return LLMResultCallback(_cb_with_observer)

    _common.make_result_callback = _patched
    _PATCH_DONE = True
    _LOG.info("monkey-patched rkai.backends._common.make_result_callback")


# **模块顶层立即安装** —— 在 ``_TextChatManager.__init__`` 之前，确保
# 任何后续 ``LLMSession(model=qwen3_8b_spec())`` 构造都能拿到 patched callback。
_do_install_patches()


def _register_observer(sink: Any, callback: Callable[[str], None]) -> None:
    """chat worker 在 session_run 之前调；observer 拿 sink.text 的增量。"""
    with _OBSERVER_LOCK:
        _OBSERVERS[id(sink)] = callback


def _unregister_observer(sink: Any) -> None:
    with _OBSERVER_LOCK:
        _OBSERVERS.pop(id(sink), None)


# --------------------------------------------------------------------------- #
# thinking 切分
# --------------------------------------------------------------------------- #
_THINK_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL)


def _split_thinking(text: str) -> tuple[str, str]:
    """一次性切分完整回复（done 时兜底切）。

    返回 ``(thinking, answer)``；若没找到 ``<think>``，thinking 为空。
    """
    m = _THINK_RE.search(text)
    if not m:
        return "", text.strip()
    thinking = m.group(1).strip()
    answer = (text[:m.start()] + text[m.end():]).strip()
    return thinking, answer


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

        # 安装 patch（一次性）
        _install_patches()

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
                "context_limit":     self.CONTEXT_LIMIT,
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
        """立即停止正在跑的推理。worker 排空当前 sink.text 后丢本轮，不写历史。"""
        with self._lock:
            if self._state != self.STATE_RUNNING:
                return
            rid = self._current_request_id
        # 触发 C 库中断 —— 必须在 session_run 阻塞的同一个线程里调；
        # worker 在 polling _stop_event 时发现置位就调 session_stop()。
        self._worker_stop_event.set()

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
        """一轮推理：拿 session → 注册 observer → 拼 prompt → run → 切分 → 写历史。"""
        # 1. 拿 LLMSession（从 ComputeRegistry）
        from app.services.compute_registry import get_registry
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

        sink = session._backend.sink
        tokenizer = session._backend.tokenizer
        rknn = session._backend.rknn

        # 2. 拼 ChatML（仅含 user + assistant(answer)，不含 thinking）
        with self._lock:
            history_snapshot = list(self._history)
        full_prompt = self._build_chatml_prompt(history_snapshot, req.text)

        # 3. token 估算 + 上下文检查
        prompt_tokens = self._count_tokens(tokenizer, full_prompt)
        if prompt_tokens + self.MAX_NEW_TOKENS > self.CONTEXT_LIMIT:
            detail = (
                f"对话过长（prompt≈{prompt_tokens} tokens + 预留 "
                f"{self.MAX_NEW_TOKENS} > {self.CONTEXT_LIMIT}），"
                f"请清空对话或缩短上下文"
            )
            with self._lock:
                self._set_state(self.STATE_ERROR, error=detail)
            self._broadcast({
                "type": "error",
                "request_id": req.id,
                "detail": detail,
            })
            self._broadcast({"type": "state", "state": self.snapshot()})
            return

        # 4. 注册 observer（按 thinking/answer 频道分发）
        router = _ThinkingRouter(
            emit_fn=lambda payload: self._broadcast(payload),
            request_id=req.id,
        )

        def _on_delta(delta: str) -> None:
            router.push(delta)

        _register_observer(sink, _on_delta)

        # 5. state → running
        with self._lock:
            self._set_state(self.STATE_RUNNING)
        self._broadcast({"type": "state", "state": self.snapshot()})

        # 6. session.reset() 清 KV + 跑
        try:
            try:
                session.reset()
            except Exception as e:  # noqa: BLE001
                _LOG.warning("session.reset() raised: %s (非致命)", e)
            t0 = time.perf_counter()
            full_text = session._backend.run(
                full_prompt,
                keep_history=0,            # app 端 ChatML 拼整段，KV 不续接
                enable_thinking=True,
            )
            elapsed = time.perf_counter() - t0
            # **诊断日志**：打印模型原始输出 + 切分结果，方便确认 thinking
            # 是否被模型 / C 库剥离、标签是否一致。日志级别 INFO，正常
            # 使用时噪音大；调试完把开关关掉即可（grep _DEBUG_THINKING_LOG）。
            _LOG.info(
                "[text-chat] req=%d raw_len=%d raw_head=%r",
                req.id, len(full_text), full_text[:200],
            )
            _dbg_think, _dbg_answer = _split_thinking(full_text)
            _LOG.info(
                "[text-chat] req=%d split: thinking=%d chars, answer=%d chars; "
                "thinking_head=%r, answer_head=%r",
                req.id,
                len(_dbg_think), len(_dbg_answer),
                _dbg_think[:120], _dbg_answer[:120],
            )
        except Exception as e:  # noqa: BLE001
            _unregister_observer(sink)
            with self._lock:
                self._set_state(self.STATE_ERROR, error=str(e))
            self._broadcast({
                "type": "error",
                "request_id": req.id,
                "detail": str(e),
            })
            self._broadcast({"type": "state", "state": self.snapshot()})
            return
        finally:
            _unregister_observer(sink)

        # 7. 检查是否被 stop 中断 —— 若 stop_event 置位且 full_text 不含
        #    完整 ``</think>``，丢弃本轮
        stopped = self._worker_stop_event.is_set()

        # 8. 切 thinking / answer（兜底正则）
        thinking, answer = _split_thinking(full_text)

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

        # 9. 写回 history（仅 answer，不含 thinking）
        user_tokens = self._count_tokens(tokenizer, req.text)
        answer_tokens = self._count_tokens(tokenizer, answer) if answer else 0
        with self._lock:
            self._history.append({"role": "user",      "content": req.text, "ts": req.ts})
            self._history_tokens += user_tokens
            if answer:
                self._history.append({
                    "role": "assistant", "content": answer,
                    "thinking": thinking, "ts": time.time(),
                })
                self._history_tokens += answer_tokens
            self._last_result = {
                "thinking":   thinking,
                "answer":     answer,
                "elapsed_sec": elapsed,
                "ts":         time.time(),
            }
            self._set_state(self.STATE_DONE)

        self._broadcast({
            "type": "done",
            "request_id": req.id,
            "thinking":   thinking,
            "answer":     answer,
            "elapsed_sec": elapsed,
            "history_tokens": self._history_tokens,
        })
        self._broadcast({"type": "state", "state": self.snapshot()})

    # -------------------------------------------------------------- helpers
    @staticmethod
    def _build_chatml_prompt(history: List[dict], user_text: str) -> str:
        """构造喂给 SDK 的裸 prompt；LLMSession.respond 不参与，
        所以直接拼 ``<|im_start|>`` 系列 ChatML 模板（与 Qwen3 训练一致）。

        历史只含 ``role:user`` + ``role:assistant``，不含 thinking。
        """
        parts = ["<|im_start|>system\nYou are a helpful assistant.<|im_end|>\n"]
        for msg in history:
            if msg["role"] == "user":
                parts.append(f"<|im_start|>user\n{msg['content']}<|im_end|>\n")
            elif msg["role"] == "assistant":
                parts.append(f"<|im_start|>assistant\n{msg['content']}<|im_end|>\n")
        parts.append(f"<|im_start|>user\n{user_text}<|im_end|>\n")
        parts.append("<|im_start|>assistant\n")
        return "".join(parts)

    @staticmethod
    def _count_tokens(tokenizer: Any, text: str) -> int:
        if not text:
            return 0
        try:
            return len(tokenizer.encode(text, add_special_tokens=False))
        except Exception:
            return len(text) // 2          # 兜底：粗估 2 char/token


# --------------------------------------------------------------------------- #
# ThinkingRouter：observer 每次 delta 进来调用，按 <think> 边界切频道
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