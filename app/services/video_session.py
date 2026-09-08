"""Single-process capture session backed by ``wdnav.VideoCapture``.

Owns one ``VideoCapture``, a background decode thread, and a frame queue
that the WebSocket handler drains. Designed to keep ``import wdnav.video``
lazy — the wdnav/AGENTS.md gotcha about non-Rockchip dev boxes still applies
(a missing native lib sets ``VideoCapture`` to ``None`` in
``wdnav/__init__.py``).
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Optional

import cv2


try:
    from wdnav import VideoCapture as _WdnavVideoCapture
except Exception:  # pragma: no cover - platform-specific load failure
    _WdnavVideoCapture = None


LOG = logging.getLogger("video_session")


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
        self._loop = False
        # Per-frame sinks (e.g. the wdnai detector). Notified on each
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
                "loop": self._loop,
                "last_frame_ts": self._last_frame_ts,
            }

    def open(self, source: str, timeout: float = 10.0, loop: bool = False) -> dict:
        """Tear down any active source and open a new one.

        ``loop``: when True the decode thread restarts the same source on
        EOF instead of emitting EOF and dying — useful for replaying
        short demo clips without the user clicking 重播. Live sources
        (rtsp/rtmp/http/device nodes) are unaffected since they don't
        EOF.
        """
        if _WdnavVideoCapture is None:
            with self._lock:
                self._state = self.STATE_ERROR
                self._error = "wdnav.VideoCapture 不可用：原生库缺失"
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
            self._loop = bool(loop)
            self._frame_pos = 0
            self._last_frame_ts = 0.0
            self._width = 0
            self._height = 0
            self._fps = 0.0
            self._frame_queue = queue.Queue(maxsize=self.FRAME_QUEUE_MAX)
            self._cap = None  # cleared until init succeeds

        try:
            cap = _WdnavVideoCapture(source, timeout=timeout)
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
        """Read frames until the source is exhausted or the user stops.

        Owns a local ``cap`` reference so the EOF + loop path can
        release the old capture and reopen the same source without
        going through ``open()`` (which would try to join the thread
        we're currently running on).

        Frame pacing uses presentation timestamps (PTS) rather than a
        fixed ``1/fps`` interval: ``cap.read()`` returns frames as fast
        as the MPP hardware decoder produces them (typically 100-500
        fps for local files) AND the JPEG encode can easily take
        50-100 ms per frame on RK3588 — a fixed-sleep pacing drifts
        whenever the loop period exceeds ``1/fps`` and produces
        visible stutter. PTS-based pacing instead computes each
        frame's target release wall-clock time from
        ``frame.pts * stream.time_base`` (relative to the first
        decoded frame), so playback honours the file's actual
        timestamps regardless of decode/encode jitter.

        Falls back to ``1/fps`` pacing if ``time_base`` is unavailable
        (V4L2 sources don't expose it). Live sources (``self._is_live``)
        skip pacing entirely — the producer is already real-time, just
        relay. Pause resets the schedule so the first frame after
        resume plays immediately instead of catching up on missed
        intervals.
        """
        cap: Optional[object] = None
        OPEN_TIMEOUT = 10.0
        # Wall-clock (monotonic) anchor for PTS-based pacing, plus the
        # matching pts value at that anchor. Both 0 = unanchored; the
        # first decoded frame sets both, and every subsequent frame's
        # target is ``play_start + (pts - pts_start) * time_base``.
        play_start_wall: float = 0.0
        pts_start: int = 0
        # Fallback pacing state used when PTS / time_base are unavailable.
        interval: float = 0.0
        next_frame_wall: float = 0.0
        while True:
            # Block here whenever _play_event is clear (paused).
            # Returns immediately when play() lifts the event, so resume
            # latency is bounded only by cap.read0() / queue dispatch.
            # Timeout just lets us recheck _stop_event during long pauses.
            self._play_event.wait(timeout=0.5)
            if self._stop_event.is_set():
                if cap is not None:
                    try:
                        cap.release()
                    except Exception:
                        pass
                return
            if not self._play_event.is_set():
                # Entering pause — drop the schedule so resume doesn't
                # fast-forward to catch up on missed intervals.
                play_start_wall = 0.0
                pts_start = 0
                next_frame_wall = 0.0
                continue

            # (Re-)open the capture if we don't have one yet — either
            # fresh start, or we just looped back after EOF.
            if cap is None:
                with self._lock:
                    source = self._source
                if not source:
                    return
                try:
                    new_cap = _WdnavVideoCapture(source, timeout=OPEN_TIMEOUT)
                except Exception as e:
                    self._fail(f"打开失败: {e}")
                    return
                if not new_cap.isOpened():
                    try:
                        new_cap.release()
                    except Exception:
                        pass
                    self._fail("VideoCapture.isOpened() = False")
                    return
                cap = new_cap
                with self._lock:
                    self._cap = cap
                    self._width = cap.width
                    self._height = cap.height
                    self._fps = float(getattr(cap, "fps", 0.0) or 0.0)
                    self._frame_pos = 0
                    self._state = self.STATE_PLAYING
                # Pre-compute the fallback interval (PTS path overrides
                # it at decode time when time_base is available).
                if self._fps > 0:
                    interval = 1.0 / self._fps
                # First frame after (re-)open fires immediately.
                play_start_wall = 0.0
                pts_start = 0
                next_frame_wall = 0.0
                continue

            try:
                ret, frame0 = cap.read0()
            except Exception as e:  # pragma: no cover - depends on source
                self._fail(f"read0() 异常: {e}")
                return

            if not ret or frame0 is None:
                # EOF. Decide whether to loop or to die and notify.
                with self._lock:
                    if self._state in (
                        self.STATE_PLAYING,
                        self.STATE_PAUSED,
                    ):
                        self._state = self.STATE_EOF
                    should_loop = self._loop
                try:
                    cap.release()
                except Exception:
                    pass
                cap = None
                with self._lock:
                    self._cap = None
                if should_loop:
                    # Reset frame counter; next iteration reopens the cap.
                    with self._lock:
                        self._frame_pos = 0
                        self._state = self.STATE_PLAYING
                    play_start_wall = 0.0
                    pts_start = 0
                    next_frame_wall = 0.0
                    continue
                try:
                    self._frame_queue.put_nowait(self._EOF)
                except queue.Full:
                    pass
                return

            # Grab pts from the zero-copy frame BEFORE materialising
            # the BGR ndarray — cheaper, and we need the value for the
            # pacing calculation that runs after encode.
            try:
                pts = int(getattr(frame0, "pts", 0) or 0)
            except Exception:
                pts = 0

            try:
                frame = frame0.numpy()
            except Exception as e:
                # RGA conversion failure (e.g. unsupported size on this
                # board). Skip this frame rather than crashing the loop.
                continue

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
                is_live = self._is_live
            # Per-frame sinks get the raw BGR. Sinks must not raise —
            # a misbehaving one shouldn't take down the decode loop.
            # 诊断 (v0.13): sink 抛错不再静默吞,LOG.warning 记录 (sink
            # 是 lambda 包 detector.submit_frame,理论上不会抛错;但
            # 一旦抛错会导致 detector 永远拿不到帧,warning 让上游能
            # 立即察觉)。
            sinks = self._current_sinks()
            for sink in sinks:
                try:
                    sink(frame, cur_frame_pos)
                except Exception as e:  # noqa: BLE001
                    LOG.warning(
                        "video_session: frame sink raised (frame_pos=%d): "
                        "%s: %s — sink will be skipped this frame",
                        cur_frame_pos, type(e).__name__, e,
                    )
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

            # Pacing — PTS-driven when available, else 1/fps, else
            # pass through. Live sources skip pacing (already real-time).
            if is_live:
                continue

            # Resolve time_base from the PyAV stream. Falls back to
            # fixed-interval pacing when PTS / time_base are missing
            # (V4L2 cameras, malformed streams, etc.).
            time_base = 0.0
            stream_obj = getattr(cap, "_stream", None)
            if stream_obj is not None and pts > 0:
                tb = getattr(stream_obj, "time_base", None)
                if tb is not None:
                    try:
                        time_base = float(tb.numerator) / float(tb.denominator)
                    except Exception:
                        time_base = 0.0

            now = time.monotonic()

            if time_base > 0 and pts > 0:
                # Anchor on the first decoded frame so target_wall
                # references ``time_base`` seconds before play_start.
                if play_start_wall <= 0.0:
                    play_start_wall = now
                    pts_start = pts
                target_wall = play_start_wall + (pts - pts_start) * time_base
                wait = target_wall - now
                if wait < -0.5:
                    # Fell badly behind (decoder stalled or we just
                    # resumed after a long pause) — re-anchor instead
                    # of spamming catch-up frames.
                    play_start_wall = now
                    pts_start = pts
            elif interval > 0:
                # Fallback: fixed 1/fps pacing (may drift under heavy
                # encode load — better than nothing).
                if next_frame_wall <= 0.0:
                    next_frame_wall = now + interval
                else:
                    next_frame_wall += interval
                    if next_frame_wall < now - 0.5:
                        next_frame_wall = now + interval
                wait = next_frame_wall - now
            else:
                # Unknown fps / no PTS — pass through.
                continue

            # Sleep in short slices so pause / stop stay responsive.
            while wait > 0:
                if self._stop_event.wait(timeout=min(wait, 0.05)):
                    if cap is not None:
                        try:
                            cap.release()
                        except Exception:
                            pass
                    return
                if not self._play_event.is_set():
                    # Paused mid-sleep — outer loop will block on
                    # _play_event and reset play_start_wall = 0.0.
                    play_start_wall = 0.0
                    pts_start = 0
                    next_frame_wall = 0.0
                    break
                now = time.monotonic()
                if time_base > 0 and pts > 0:
                    target_wall = (
                        play_start_wall + (pts - pts_start) * time_base
                    )
                else:
                    target_wall = next_frame_wall
                wait = target_wall - now


_session: Optional[VideoSession] = None
_session_lock = threading.Lock()


def get_session() -> VideoSession:
    global _session
    with _session_lock:
        if _session is None:
            _session = VideoSession()
        return _session
