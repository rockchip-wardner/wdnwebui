"""Qwen3-8B 文本对话服务：单例 + worker。

设计要点
========

* **LLMSession 必须从 ComputeRegistry 取**（``get_session("swcap_text_chat")``，
  返回 ``_RpcSessionProxy``）。Proxy 只暴露 ``reset()`` / ``respond()`` /
  ``embed_text()`` / ``embed_video()`` —— 这层抽象把 wdnai.LLMSession +
  RKNN3 runtime 隔在 compute_worker 子进程里，主进程**绝对不能**碰
  ``session._backend.sink / .tokenizer / .rknn / .run(...)`` 等私有属性
  （v0.4 之前的老代码会过；当前架构直接 ``AttributeError``）。
* **wdnai 导入懒加载**：模块顶部不 import wdnai。tokenizer 仅用于精确
  token 计数 —— 但 v0.4 之后主进程拿不到 tokenizer，token 预算改用
  ``len(text) // 3`` 粗估（中英混合场景 ±30% 误差，对 32K 上限留 6K
  buffer 够用）。
* **批模式推理（非流式）**：compute_worker 的 ``infer_handler`` 把
  ``sess.respond(prompt=...)`` 跑完后**一次性**把 ``result.text`` 整段
  通过 RPC reply 返回（见 ``app/services/compute_worker.py:infer_handler``）。
  主进程拿到完整文本后用 ``_split_thinking`` 正则切成
  ``thinking`` / ``answer`` 两段，再各推一个 single-frame delta 给前端。
  真正的 token 级流式得 cshim 加 streaming RPC 通路（v0.10 未实现）。
* **thinking 与 answer 分离**：主进程 ``_split_thinking`` 一刀切；前端
  ``TextChat._appendThinkingDelta`` / ``_appendAnswerDelta`` 接收
  ``delta`` 字段做累加，单次 delta = 整段，所以前端是一次性填满而不是
  字符级打字机效果。layout 不变。
* **多轮上下文**：app 端消息 deque + 每轮 ``session.reset()`` 清 C 端 KV，
  再用 ChatML 拼整段 prompt 喂进 ``await sess.respond(prompt=...)``。
  历史中**只写 ``content:answer``**，不写 thinking，下一轮看不到。
* **32K token 限制**：每轮用粗估 ``len(text)//3`` 算 ``history_tokens``；
  超过 ``context_limit - max_new_tokens`` 拒绝发新请求。
* **停止 = 状态翻转（best-effort）**：worker 侧 ``sess.respond`` 是阻塞
  + 不可中断的，主进程没有 cancel RPC 通路（v0.10 未实现）。``stop()``
  仅把状态翻到 ``stopped`` 并把 ``_worker_stop_event`` 置位，worker 跑完
  后看到 ``stopped`` 就丢弃本轮结果不写历史；用户体感是 "UI 立即停，
  worker 静默跑完"。下一轮 ``session.reset()`` 仍可用 —— proxy 内部
  ThreadPoolExecutor(max_workers=1) 串行，新请求会排队等 worker 空闲。
* **broadcast pattern**：worker 线程 push 到 ``asyncio.Queue`` 列表；
  WS handler 在主 event loop 里 ``drain``。
* **进程级串行**：通过 ``ComputeRegistry.get_session_call_lock()`` 串行化
  主进程侧 ``session.reset() + respond()``。这与 detector / VFA 共用同一
  qwen3-vl-4b 时锁的是同一把 —— 但 text_chat 走 qwen3-8b，不冲突；这里
  锁为防御性兜底（理论上单 instance 也只有 chat 这一个 consumer）。

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
from typing import Any, Deque, List, Optional


_LOG = logging.getLogger("text_chat")


# Max buffered frames per WS subscriber before the oldest is dropped.
_SUB_QUEUE_MAX = 500


# --------------------------------------------------------------------------- #
# thinking 切分（worker 返回的 result.text 仍可能含 <think>...</think> 标签；
# 在主进程侧用正则一次性切开）
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

        # 2. 拼 ChatML（仅含 user + assistant(answer)，不含 thinking）
        with self._lock:
            history_snapshot = list(self._history)
        full_prompt = self._build_chatml_prompt(history_snapshot, req.text)

        # 3. token 粗估 + 上下文检查（中英混合 ~3 char/token，留 ~6144 buffer）
        prompt_tokens = self._est_tokens(full_prompt)
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

        # 4. state → running
        with self._lock:
            self._set_state(self.STATE_RUNNING)
        self._broadcast({"type": "state", "state": self.snapshot()})

        # 5. session.reset() + await sess.respond() —— 串行锁与 detector / VFA 共享
        #    （text_chat 走 qwen3-8b，理论上不冲突；锁为防御性兜底）
        t0 = time.perf_counter()
        try:
            with get_session_call_lock():
                try:
                    session.reset()
                except Exception as e:  # noqa: BLE001
                    _LOG.warning("session.reset() raised: %s (非致命)", e)
                coro = session.respond(
                    prompt=full_prompt,
                    max_new_tokens=self.MAX_NEW_TOKENS,
                )
                # proxy.respond() 是 async，内部 run_in_executor → 同步阻塞跑完
                result = asyncio.run(coro)
            elapsed = time.perf_counter() - t0

            # 6. 抽 result.text → 切 thinking/answer
            full_text = getattr(result, "text", None) or ""
            if not full_text and isinstance(result, str):
                full_text = result
            _LOG.info(
                "[text-chat] req=%d raw_len=%d raw_head=%r",
                req.id, len(full_text), full_text[:200],
            )
            thinking, answer = _split_thinking(full_text)
            _LOG.info(
                "[text-chat] req=%d split: thinking=%d chars, answer=%d chars",
                req.id, len(thinking), len(answer),
            )
        except Exception as e:  # noqa: BLE001
            elapsed = time.perf_counter() - t0
            with self._lock:
                self._set_state(self.STATE_ERROR, error=str(e))
            self._broadcast({
                "type": "error",
                "request_id": req.id,
                "detail": str(e),
            })
            self._broadcast({"type": "state", "state": self.snapshot()})
            return

        # 推 WS thinking/answer（单次 delta = 整段，前端累加 = 一次性填满）
        if thinking:
            self._broadcast({
                "type": "thinking",
                "request_id": req.id,
                "delta": thinking,
            })
        if answer:
            self._broadcast({
                "type": "answer",
                "request_id": req.id,
                "delta": answer,
            })

        # cshim 没 cancel RPC，worker 实际跑完了；被 stop 时丢本轮不写历史
        stopped = self._worker_stop_event.is_set()

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

        # 9. 写回 history（仅 answer，不含 thinking —— 下轮 prompt 看不到 thinking）
        user_tokens = self._est_tokens(req.text)
        answer_tokens = self._est_tokens(answer) if answer else 0
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
    def _est_tokens(text: str) -> int:
        """粗估 token 数：中英混合 ~3 char/token（含 ChatML 标签 + 空格）。

        v0.4 主进程拿不到 tokenizer（tokenizer 只在 compute_worker 子进程里）；
        v0.5 之前是 ``tokenizer.encode(text)`` 精确数。3 char/token 对中英
        混合文本误差 ±30%，32K 上限预留 6K buffer 够用。返回 0 防除零。
        """
        if not text:
            return 0
        return max(1, len(text) // 3)


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