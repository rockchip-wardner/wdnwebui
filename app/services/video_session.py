"""Single-process capture session backed by ``rkv.VideoCapture``.

Owns one ``VideoCapture``, a background decode thread, and a frame queue
that the WebSocket handler drains. Designed to keep ``import rkv.video``
lazy — the rkv/AGENTS.md gotcha about non-Rockchip dev boxes still applies
(a missing native lib sets ``VideoCapture`` to ``None`` in
``rkv/__init__.py``).
"""
from __future__ import annotations

import queue
import threading
import time
from typing import Optional

import cv2


try:
    from rkv import VideoCapture as _RkvVideoCapture
except Exception:  # pragma: no cover - platform-specific load failure
    _RkvVideoCapture = None


class VideoSession:
    """Single-shot capture session. Re-entrant ``open()`` tears down the
    previous source first so the UI can switch streams without racing.
    """

    STATE_IDLE = "idle"
    STATE_OPENING = "opening"
    STATE_PLAYING = "playing"
    STATE_PAUSED = "paused"
    STATE_EOF = "eof"
    STATE_ERROR = "error"

    FRAME_QUEUE_MAX = 1
    JPEG_QUALITY = 75

    _EOF = object()

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cap: Optional[object] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._play_event = threading.Event()
        self._frame_queue: "queue.Queue[object]" = queue.Queue(
            maxsize=self.FRAME_QUEUE_MAX,
        )
        self._state = self.STATE_IDLE
        self._error = ""
        self._source = ""
        self._is_live = False
        self._width = 0
        self._height = 0
        self._fps = 0.0
        self._frame_pos = 0
        self._last_frame_ts = 0.0
        # Per-frame sinks (e.g. the rkai detector). Notified on each
        # decoded BGR frame; called from the decode thread so sinks must
        # be cheap or marshal work elsewhere themselves.
        self._frame_sinks: list = []
        self._sinks_lock = threading.Lock()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "state": self._state,
                "error": self._error,
                "source": self._source,
                "width": self._width,
                "height": self._height,
                "fps": self._fps,
                "frame_pos": self._frame_pos,
                "is_live": self._is_live,
                "last_frame_ts": self._last_frame_ts,
            }

    def open(self, source: str, timeout: float = 10.0) -> dict:
        """Tear down any active source and open a new one."""
        if _RkvVideoCapture is None:
            with self._lock:
                self._state = self.STATE_ERROR
                self._error = "rkv.VideoCapture 不可用：原生库缺失"
            raise RuntimeError(self._error)

        # Tear down any prior session first; this also flips both events
        # into the "stopped" state so the old thread exits cleanly.
        self._teardown()

        with self._lock:
            self._state = self.STATE_OPENING
            self._error = ""
            self._source = source
            self._is_live = (
                "://" in source or source.startswith("/dev/video")
            )
            self._frame_pos = 0
            self._last_frame_ts = 0.0
            self._width = 0
            self._height = 0
            self._fps = 0.0
            self._frame_queue = queue.Queue(maxsize=self.FRAME_QUEUE_MAX)
            self._cap = None  # cleared until init succeeds

        try:
            cap = _RkvVideoCapture(source, timeout=timeout)
        except Exception as e:
            self._fail(f"打开失败: {e}")
            raise

        if not cap.isOpened():
            try:
                cap.release()
            except Exception:
                pass
            self._fail("VideoCapture.isOpened() = False")
            raise RuntimeError(f"cannot open {source!r}")

        with self._lock:
            self._cap = cap
            self._width = cap.width
            self._height = cap.height
            self._fps = float(getattr(cap, "fps", 0.0) or 0.0)
            self._state = self.STATE_PLAYING

        # Reset events AFTER teardown but BEFORE the new thread spins up,
        # so the loop sees "go" on its first iteration.
        self._stop_event.clear()
        self._play_event.set()

        self._thread = threading.Thread(
            target=self._decode_loop,
            name="video-decode",
            daemon=True,
        )
        self._thread.start()
        return self.snapshot()

    def play(self) -> dict:
        """Resume decode from any state.

        - PAUSED + live thread → set play_event, mark PLAYING.
        - PAUSED + dead thread (EOF race) / EOF / ERROR / IDLE →
          re-open the last known source so the user recovers with one
          click instead of having to retype the path.
        - PLAYING → no-op success (idempotent).
        """
        with self._lock:
            cur_state = self._state
            cur_source = self._source
            cur_error = self._error
            thread_alive = (
                self._thread is not None and self._thread.is_alive()
            )

        if cur_state == self.STATE_PLAYING and thread_alive:
            return {"ok": True, "state": cur_state}

        if cur_state == self.STATE_PAUSED and thread_alive:
            with self._lock:
                self._play_event.set()
                self._state = self.STATE_PLAYING
            return {"ok": True, "state": self.STATE_PLAYING}

        # Anything else — EOF, dead thread, ERROR, IDLE — re-open.
        if not cur_source:
            return {
                "ok": False,
                "state": cur_state,
                "reason": cur_error or "no source",
            }
        try:
            return self.open(cur_source)
        except Exception as e:
            return {"ok": False, "state": self._state, "reason": str(e)}

    def pause(self) -> dict:
        with self._lock:
            if self._state in (self.STATE_PLAYING,):
                self._play_event.clear()
                self._state = self.STATE_PAUSED
                return {"ok": True, "state": self._state}
            return {"ok": False, "state": self._state, "reason": "no active session"}

    def close(self) -> dict:
        self._teardown()
        with self._lock:
            return {"ok": True, "state": self._state}

    # ------------------------------------------------------------------ #
    # Frame sinks — see ``_decode_loop`` for the call site.
    # ------------------------------------------------------------------ #
    def add_frame_sink(self, sink) -> None:
        """Register ``sink(frame_bgr, frame_pos)`` to fire on each decoded
        frame. Sinks persist across ``open()`` calls so the detector can
        keep registering once at startup and forget about it.
        """
        with self._sinks_lock:
            if sink not in self._frame_sinks:
                self._frame_sinks.append(sink)

    def remove_frame_sink(self, sink) -> None:
        with self._sinks_lock:
            try:
                self._frame_sinks.remove(sink)
            except ValueError:
                pass

    def _current_sinks(self) -> list:
        # Cheap copy under lock so the decode loop can call without holding it.
        with self._sinks_lock:
            return list(self._frame_sinks)

    def fetch_frame(self, timeout: float = 0.1):
        """Pull the next event for the WS loop."""
        try:
            item = self._frame_queue.get(timeout=timeout)
        except queue.Empty:
            return ("empty", None)
        if item is self._EOF:
            return ("eof", None)
        return ("frame", item)

    def _fail(self, msg: str) -> None:
        with self._lock:
            # A dying decode thread (one whose cap was already released
            # by _teardown) may still surface an exception in read()
            # before it exits. If we let that flip the snapshot to
            # ERROR after the user's already moved on — or worse, after
            # we've already reopened a fresh session — the UI would
            # display a stale "MppDecoder is closed" forever. Drop the
            # call silently when we're already idle / already in error.
            if self._state in (
                self.STATE_IDLE,
                self.STATE_ERROR,
            ):
                return
            self._state = self.STATE_ERROR
            self._error = msg

    def _teardown(self) -> None:
        with self._lock:
            cap = self._cap
            self._cap = None
            was_open = self._state not in (
                self.STATE_IDLE,
                self.STATE_ERROR,
            )
            self._state = self.STATE_IDLE
        self._stop_event.set()
        self._play_event.set()  # release any waiters
        if self._thread is not None:
            self._thread.join(timeout=1.5)
            self._thread = None
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass
        # drain queue (avoid leaking previous frames into next session)
        while True:
            try:
                self._frame_queue.get_nowait()
            except queue.Empty:
                break

    def _decode_loop(self) -> None:
        with self._lock:
            cap = self._cap
        if cap is None:
            return
        while True:
            # Block here whenever _play_event is clear (paused).
            # Returns immediately when play() lifts the event, so resume
            # latency is bounded only by cap.read() / queue dispatch.
            # Timeout just lets us recheck _stop_event during long pauses.
            self._play_event.wait(timeout=0.5)
            if self._stop_event.is_set():
                return
            if not self._play_event.is_set():
                continue
            try:
                ret, frame = cap.read()
            except Exception as e:  # pragma: no cover - depends on source
                self._fail(f"read() 异常: {e}")
                return
            if not ret or frame is None:
                with self._lock:
                    # EOF regardless of whether we were paused — file
                    # finished during the previous frame's read. Without
                    # this, state would stay stuck at PAUSED with a dead
                    # thread, and play() couldn't recover.
                    if self._state in (
                        self.STATE_PLAYING,
                        self.STATE_PAUSED,
                    ):
                        self._state = self.STATE_EOF
                try:
                    self._frame_queue.put_nowait(self._EOF)
                except queue.Full:
                    pass
                return
            ok, buf = cv2.imencode(
                ".jpg",
                frame,
                [int(cv2.IMWRITE_JPEG_QUALITY), self.JPEG_QUALITY],
            )
            if not ok:
                continue
            jpg = bytes(buf)
            with self._lock:
                self._frame_pos = cap.frame_pos
                self._last_frame_ts = time.time()
                cur_frame_pos = cap.frame_pos
            # Per-frame sinks get the raw BGR. Sinks must not raise —
            # a misbehaving one shouldn't take down the decode loop.
            sinks = self._current_sinks()
            for sink in sinks:
                try:
                    sink(frame, cur_frame_pos)
                except Exception:
                    pass
            # Drop oldest on overflow — favor low latency over completeness.
            try:
                self._frame_queue.put_nowait(jpg)
            except queue.Full:
                try:
                    self._frame_queue.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self._frame_queue.put_nowait(jpg)
                except queue.Full:
                    pass


_session: Optional[VideoSession] = None
_session_lock = threading.Lock()


def get_session() -> VideoSession:
    global _session
    with _session_lock:
        if _session is None:
            _session = VideoSession()
        return _session
