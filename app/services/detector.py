"""``rkai``-backed video detector (v2 API).

Frames arrive from ``VideoSession`` via a frame-sink callback; every
Nth frame is fed to a single ``LLMSession`` with the user prompt.
Completed inferences live in an in-memory ring buffer and are pushed
to the registered broadcasters.

The service drives the v2 ``LLMSession.respond`` API with a list-
shaped ``attachment`` (so the session can also carry text/audio
later) and an ``LMOutput``-derived ``generating`` schema; for VLM
``.attachment`` is just an empty list, but the slot exists so the
same schema could be reused with a TTS-style backend without
code changes here.

``rkai`` is heavy (RKNN model load is several seconds) and inference
is single-shot — one worker, one session, queue maxsize=1 (latest-only).
On boxes without the model files ``start()`` records an error in
``snapshot()["init_error"]`` and the worker exits cleanly.
"""
from __future__ import annotations

import asyncio
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, List, Optional

import numpy as np


_RKAI_IMPORT_ERROR = ""
# rkai is imported LAZILY (at start() time, not at module load) — RKNN native
# libraries can race with rkv's mpp init or fork-unsafe state if loaded too
# eagerly, and a missing wheel on dev boxes would otherwise crash app startup
# even for users who never touch the detection panel.
_RKAI_CACHE: dict = {}


def _load_rkai():
    """Lazily import rkai; cache the result so only the first call hits
    the import machinery. Returns ``(ok, Attachment, LLMSession,
    SystemLanguageModel, LMOutput, error_str)``."""
    if "_Attachment" in _RKAI_CACHE:
        return (
            _RKAI_CACHE["_ok"],
            _RKAI_CACHE["_Attachment"],
            _RKAI_CACHE["_LLMSession"],
            _RKAI_CACHE["_SystemLanguageModel"],
            _RKAI_CACHE["_LMOutput"],
            _RKAI_CACHE["_Generable"],
            _RKAI_CACHE["_Field"],
            _RKAI_CACHE["_error"],
        )
    try:
        from rkai import (
            Attachment,
            Field,
            Generable,
            LMOutput,
            LLMSession,
            SystemLanguageModel,
        )
        _RKAI_CACHE.update(
            _ok=True,
            _Attachment=Attachment,
            _LLMSession=LLMSession,
            _SystemLanguageModel=SystemLanguageModel,
            _LMOutput=LMOutput,
            _Generable=Generable,
            _Field=Field,
            _error="",
        )
        return True, Attachment, LLMSession, SystemLanguageModel, LMOutput, Generable, Field, ""
    except Exception as e:
        _RKAI_CACHE.update(
            _ok=False,
            _Attachment=None,
            _LLMSession=None,
            _SystemLanguageModel=None,
            _LMOutput=None,
            _Generable=None,
            _Field=None,
            _error=str(e),
        )
        return False, None, None, None, None, None, None, str(e)


# --------------------------------------------------------------------------- #
# Structured output schema — inherits LMOutput so the SDK can populate
# ``attachment`` automatically when a future backend produces binary
# outputs. Currently empty for VLM (text-only path).
# --------------------------------------------------------------------------- #
def _build_vlm_doc_class():
    """Build the ``VLMDoc`` schema lazily, once rkai is importable.

    The class is created with ``@Generable`` + ``LMOutput`` as the base so
    the SDK can attach the parsed fields into an instance that also
    carries the standard ``attachment`` slot.
    """
    # ``from typing import Optional`` *inside* this function so the name
    # shows up in the function's locals — ``pydantic`` resolves forward
    # refs by ``typing.get_type_hints``, which searches the enclosing
    # frame's namespace. Without this, the ``Optional[str]`` annotation
    # for ``level`` cannot be evaluated and instantiation fails.
    from typing import Optional

    _, _, _, _, LMOutput, Generable, Field, _ = _load_rkai()

    @Generable
    class VLMDoc(LMOutput):
        """Structured VLM output for pyrkaiweb's video-detection panel."""
        text: str = Field(description="The model's free-form description of the frame.")
        level: Optional[str] = Field(
            default=None,
            description="Optional severity tag (e.g. 'normal' / 'warning' / 'danger').",
        )

    return VLMDoc


# Cached after first build.
_VLM_DOC_CLS = None


def _get_vlm_doc_cls():
    global _VLM_DOC_CLS
    if _VLM_DOC_CLS is None:
        _VLM_DOC_CLS = _build_vlm_doc_class()
    return _VLM_DOC_CLS


@dataclass
class DetectorEvent:
    """Public-facing event payload pushed to WS subscribers.

    The shape is what the frontend already expects (id / frame_pos / ts
    / prompt / text); the LMOutput instance is also attached so future
    consumers can pull ``attachment`` or other structured fields.
    """
    id: int
    frame_pos: int
    ts: float
    prompt: str
    text: str
    level: Optional[str] = None
    attachment_count: int = 0

    def to_dict(self) -> dict:
        return {
            "id":               self.id,
            "frame_pos":        self.frame_pos,
            "ts":               self.ts,
            "prompt":           self.prompt,
            "text":             self.text,
            "level":            self.level,
            "attachment_count": self.attachment_count,
        }


# --------------------------------------------------------------------------- #
class RkaiDetector:
    """Single VLM session shared by the video-detection page.

    Uses the v2 SDK surface:

        result = await session.respond(
            msg=prompt,
            attachment=[Attachment(frame)],     # list
            generating=VLMDoc,                  # @Generable subclassing LMOutput
        )
        # result.text, result.level, result.attachment
    """

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
        self._latest: Optional[dict] = None
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
                "available":      self.available,
                "init_error":     self._init_error,
                "enabled":        self._enabled,
                "prompt":         self._prompt,
                "every_n":        self._every_n,
                "model":          self._model_name,
                "session_ready":  self._session is not None,
                "worker_alive":   self._worker is not None and self._worker.is_alive(),
                "queue_size":     self._item_q.qsize(),
            }

    # ------------------------------------------------------------------ #
    def start(self, prompt: str, every_n: int = 30) -> dict:
        if not self.available:
            raise RuntimeError(f"rkai 不可用: {self._init_error}")
        if not (prompt or "").strip():
            raise ValueError("prompt 不能为空")
        # Build the structured-output class now (lazy) so that any
        # import failure surfaces here instead of inside the worker.
        _get_vlm_doc_cls()
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
        with self._lock:
            if not (self._enabled and self._prompt):
                return
            self._frame_count += 1
            if self._frame_count % self._every_n != 0:
                return
            prompt = self._prompt
        try:
            while True:
                self._item_q.get_nowait()
        except queue.Empty:
            pass
        try:
            self._item_q.put_nowait((frame_bgr, frame_pos, prompt))
        except queue.Full:
            pass

    # ------------------------------------------------------------------ #
    def events(self) -> list:
        with self._events_lock:
            return [ev.to_dict() if hasattr(ev, "to_dict") else dict(ev) for ev in self._events]

    def latest(self) -> Optional[dict]:
        with self._events_lock:
            return dict(self._latest) if self._latest else None

    def clear(self) -> None:
        with self._events_lock:
            self._events.clear()
            self._latest = None

    def events_since(self, since_id: int) -> list:
        with self._events_lock:
            out = []
            for ev in self._events:
                d = ev.to_dict() if hasattr(ev, "to_dict") else dict(ev)
                if d["id"] > since_id:
                    out.append(d)
            return out

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
        ok, Attachment, LLMSession, SystemLanguageModel, *_ = _load_rkai()
        if not ok:
            with self._lock:
                self._init_error = "rkai 不可用"
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

    def _infer_sync(self, frame_bgr: "np.ndarray", prompt: str) -> DetectorEvent:
        """One synchronous inference call against the v2 SDK surface."""
        try:
            self._ensure_session()
            assert self._session is not None
            ok, Attachment, *_ = _load_rkai()
            VLMDoc = _get_vlm_doc_cls()

            self._session.reset()
            coro = self._session.respond(
                msg=prompt,
                attachment=[Attachment(frame_bgr)],     # v2: list of Attachments
                generating=VLMDoc,                      # v2: LMOutput subclass
            )
            result = asyncio.run(coro)
            # `result` is a ``VLMDoc`` (LMOutput subclass). Backend
            # currently produces no audio outputs, so ``attachment`` is [].
            att_count = len(getattr(result, "attachment", []) or [])
            return DetectorEvent(
                id=-1,                   # filled in by worker_loop
                frame_pos=-1,            # ditto
                ts=time.time(),
                prompt=prompt,
                text=str(getattr(result, "text", "") or ""),
                level=getattr(result, "level", None),
                attachment_count=att_count,
            )
        except Exception as e:  # pragma: no cover - depends on model
            return DetectorEvent(
                id=-1,
                frame_pos=-1,
                ts=time.time(),
                prompt=prompt,
                text=f"(推理失败: {e})",
                level=None,
                attachment_count=0,
            )

    # ------------------------------------------------------------------ #
    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                item = self._item_q.get(timeout=1.0)
            except queue.Empty:
                continue
            if item is None:
                return
            frame_bgr, frame_pos, prompt = item
            ev = self._infer_sync(frame_bgr, prompt)
            with self._events_lock:
                ev.id = self._next_event_id
                ev.frame_pos = frame_pos
                self._next_event_id += 1
                self._events.append(ev)
                self._latest = ev.to_dict()
            self._broadcast(ev.to_dict())


_detector: Optional[RkaiDetector] = None
_detector_lock = threading.Lock()


def get_detector() -> RkaiDetector:
    global _detector
    with _detector_lock:
        if _detector is None:
            _detector = RkaiDetector()
        return _detector
