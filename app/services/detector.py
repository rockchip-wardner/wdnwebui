"""``rkai``-backed video detector.

Owns one ``LLMSession`` + a worker thread. Frames arrive from
``VideoSession`` via a frame-sink callback; every Nth frame is fed to the
session with the user-supplied prompt. Completed inferences are kept in an
in-memory ring buffer and pushed to a list of registered broadcasters.

``rkai`` is heavy (RKNN model load is several seconds) and the inference is
single-shot — one worker, one session, queue maxsize=1 (latest-only). On
boxes without the model files ``start()`` records an error in
``snapshot()["init_error"]`` and the worker exits cleanly without blocking
the request handler.
"""
from __future__ import annotations

import asyncio
import queue
import threading
import time
from collections import deque
from typing import Callable, List, Optional

import numpy as np


_RKAI_IMPORT_ERROR = ""
# rkai is imported LAZILY (at start() time, not at module load) — RKNN native
# libraries can race with rkv's mpp init or fork-unsafe state if loaded too
# eagerly, and a missing wheel on dev boxes would otherwise crash app startup
# even for users who never touch the detection panel.
_RKAI_CACHE: dict = {}


def _load_rkai():
    """Lazily import rkai; return a tuple of (ok, Attachment, LLMSession,
    SystemLanguageModel, error_str). Safe to call repeatedly — only the
    first call hits Python's import machinery."""
    if "_Attachment" in _RKAI_CACHE:
        return _RKAI_CACHE["_ok"], _RKAI_CACHE["_Attachment"], _RKAI_CACHE["_LLMSession"], _RKAI_CACHE["_SystemLanguageModel"], _RKAI_CACHE["_error"]
    try:
        from rkai import Attachment, LLMSession, SystemLanguageModel
        _RKAI_CACHE.update(_ok=True, _Attachment=Attachment, _LLMSession=LLMSession, _SystemLanguageModel=SystemLanguageModel, _error="")
        return True, Attachment, LLMSession, SystemLanguageModel, ""
    except Exception as e:
        _RKAI_CACHE.update(_ok=False, _Attachment=None, _LLMSession=None, _SystemLanguageModel=None, _error=str(e))
        return False, None, None, None, str(e)


class RkaiDetector:
    """Single VLM session shared by the video-detection page."""

    HISTORY_MAX = 200
    ITEM_QUEUE_MAX = 1  # latest-only — older frames are dropped

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._enabled = False
        self._prompt = ""
        self._every_n = 30
        self._frame_count = 0
        self._model_name = "qwen3-vl-4b"
        self._session = None
        self._init_error = ""
        self._events: deque = deque(maxlen=self.HISTORY_MAX)
        self._events_lock = threading.Lock()
        self._latest = None
        self._next_event_id = 1
        self._item_q: "queue.Queue" = queue.Queue(maxsize=self.ITEM_QUEUE_MAX)
        self._worker: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._broadcaster_lock = threading.Lock()
        self._broadcasters: List[Callable[[dict], None]] = []

    # ------------------------------------------------------------------ #
    @property
    def available(self) -> bool:
        ok, *_ = _load_rkai()
        return ok

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "available": self.available,
                "init_error": self._init_error,
                "enabled": self._enabled,
                "prompt": self._prompt,
                "every_n": self._every_n,
                "model": self._model_name,
                "session_ready": self._session is not None,
                "worker_alive": (
                    self._worker is not None and self._worker.is_alive()
                ),
                "queue_size": self._item_q.qsize(),
            }

    # ------------------------------------------------------------------ #
    def start(self, prompt: str, every_n: int = 30) -> dict:
        if not self.available:
            raise RuntimeError(f"rkai 不可用: {self._init_error}")
        if not (prompt or "").strip():
            raise ValueError("prompt 不能为空")
        with self._lock:
            self._enabled = True
            self._prompt = prompt
            self._every_n = max(1, int(every_n))
            self._frame_count = 0
        if self._worker is None or not self._worker.is_alive():
            self._stop_event.clear()
            self._worker = threading.Thread(
                target=self._worker_loop,
                name="rkai-detect",
                daemon=True,
            )
            self._worker.start()
        return self.snapshot()

    def stop(self) -> dict:
        with self._lock:
            self._enabled = False
        # Wake the worker in case it's blocking on item_q.get().
        try:
            self._item_q.put_nowait(None)
        except queue.Full:
            pass
        return self.snapshot()

    def close(self) -> None:
        """Fully tear down (worker + session) — used on app shutdown."""
        self.stop()
        with self._lock:
            sess = self._session
            self._session = None
        if sess is not None:
            try:
                sess.close()
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    def submit_frame(self, frame_bgr: "np.ndarray", frame_pos: int) -> None:
        """Called from the decode-loop thread.

        Walks the frame counter, drops the frame if it doesn't match the
        cadence, and otherwise hands it to the worker. The queue is size 1
        so a backlog never builds up — if the worker is still busy on the
        previous frame, this one replaces it.
        """
        with self._lock:
            if not (self._enabled and self._prompt):
                return
            self._frame_count += 1
            if self._frame_count % self._every_n != 0:
                return
            prompt = self._prompt
        # Drop any backlog, then enqueue. The queue is size 1 and we
        # just emptied it, so put_nowait always succeeds.
        try:
            while True:
                self._item_q.get_nowait()
        except queue.Empty:
            pass
        try:
            self._item_q.put_nowait((frame_bgr, frame_pos, prompt))
        except queue.Full:  # pragma: no cover - shouldn't happen
            pass

    # ------------------------------------------------------------------ #
    def events(self) -> list:
        with self._events_lock:
            return list(self._events)

    def latest(self) -> Optional[dict]:
        with self._events_lock:
            return dict(self._latest) if self._latest else None

    def clear(self) -> None:
        with self._events_lock:
            self._events.clear()
            self._latest = None

    def events_since(self, since_id: int) -> list:
        """Snapshot the events with id > ``since_id`` (for WS incremental push)."""
        with self._events_lock:
            return [dict(ev) for ev in self._events if ev["id"] > since_id]

    def register_broadcaster(self, fn: Callable[[dict], None]) -> None:
        with self._broadcaster_lock:
            self._broadcasters.append(fn)

    def unregister_broadcaster(self, fn: Callable[[dict], None]) -> None:
        with self._broadcaster_lock:
            try:
                self._broadcasters.remove(fn)
            except ValueError:
                pass

    def _broadcast(self, event: dict) -> None:
        with self._broadcaster_lock:
            bs = list(self._broadcasters)
        for fn in bs:
            try:
                fn(event)
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    def _ensure_session(self) -> None:
        if self._session is not None:
            return
        ok, _Attachment, LLMSession, SystemLanguageModel, err = _load_rkai()
        if not ok:
            with self._lock:
                self._init_error = f"rkai 不可用: {err}"
            raise RuntimeError(self._init_error)
        try:
            model = SystemLanguageModel.qwen3_vl_4b()
        except Exception as e:
            with self._lock:
                self._init_error = f"无法加载模型配置: {e}"
            raise
        try:
            sess = LLMSession(model=model)
        except Exception as e:
            with self._lock:
                self._init_error = f"LLMSession 初始化失败: {e}"
            raise
        with self._lock:
            self._session = sess
            self._init_error = ""

    def _infer_sync(self, frame_bgr: "np.ndarray", prompt: str) -> str:
        """One synchronous inference call. Wraps ``respond`` (a coroutine)
        with ``asyncio.run`` because the SDK was originally designed for
        ad-hoc scripts and the underlying C ``session_run`` is itself
        blocking — the asyncio layer is just bookkeeping.
        """
        try:
            self._ensure_session()
            assert self._session is not None
            _, Attachment, *_ = _load_rkai()
            self._session.reset()
            coro = self._session.respond(
                msg=prompt,
                attachment=Attachment(frame_bgr),
            )
            return str(asyncio.run(coro))
        except Exception as e:  # pragma: no cover - depends on model
            return f"(推理失败: {e})"

    # ------------------------------------------------------------------ #
    def _worker_loop(self) -> None:
        """Daemon worker. One loop, one session, sequential inferences."""
        while not self._stop_event.is_set():
            try:
                item = self._item_q.get(timeout=1.0)
            except queue.Empty:
                continue
            if item is None:
                return
            frame_bgr, frame_pos, prompt = item
            text = self._infer_sync(frame_bgr, prompt)
            with self._events_lock:
                event = {
                    "id": self._next_event_id,
                    "frame_pos": frame_pos,
                    "ts": time.time(),
                    "prompt": prompt,
                    "text": text,
                }
                self._next_event_id += 1
                self._events.append(event)
                self._latest = event
            self._broadcast(event)


_detector: Optional[RkaiDetector] = None
_detector_lock = threading.Lock()


def get_detector() -> RkaiDetector:
    global _detector
    with _detector_lock:
        if _detector is None:
            _detector = RkaiDetector()
        return _detector
