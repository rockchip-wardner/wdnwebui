"""Video surveillance service: 16 channels, sharing one ``rkai.LLMSession``.

The ``SurveillanceManager`` owns 16 :class:`Channel` instances, each of
which runs its own decoder / encoder pipeline:

  rkv.VideoCapture.read0(fmt="NV12")                  # native NV12 DMA-BUF
       |
       +--> rkv.rga.resize_bgr(frame0, native_w, native_h)   # stream MJPEG path
       |        |
       |        v
       |    MppEncoder(MJPEG, fixqp, native)  -- every ~5 frames
       |        |                                 (MPP queue leak workaround)
       |        v
       |    /stream WS  ->  native-res JPEG -> browser <canvas>
       |
       +--> rkv.rga.resize_bgr(frame0, detect_w, detect_h)   # thumbnail MJPEG path
                |
                v
            MppEncoder(MJPEG, fixqp, detect)    -- every ~5 frames
                |
                v
            /thumb WS   ->  detection-res JPEG -> browser <canvas> (grid + modal)

  SurveillanceManager.submit_inference(...)   # enqueue, non-blocking
       |
       v
  _InferenceWorker (single thread, persistent asyncio loop)
       |   session.reset()  +  await session.respond(prompt, attachment=[...])
       v
  concurrent.futures.Future resolves; channel records event async

Why a shared session
====================

Loading one ``rkai.LLMSession`` materialises two RKNN contexts
(vision + LLM) totalling ~3.5 GB of resident weights. Spinning up 16
separate sessions would require ~56 GB of unified memory — physically
impossible on a single RK3588 board (8 GB total). So all 16 channels
funnel into ONE shared session, dispatched by a single inference
worker thread. The worker is the only thread that ever calls
``session.respond()``, which keeps the C-level RKNN context from
being touched by multiple threads simultaneously (the rkai backend
is not re-entrant — see ``rkv.AGENTS.md`` "不直接调用 rknntoolkit
通过 rkai 调用").

Inference latency is now serialised: with N channels each wanting
one inference every ``every_n`` frames at 25 fps, the worker queue
depth grows roughly as ``N * inference_seconds * fps / every_n``.
At one second per inference (the typical qwen3-vl-4b warm inference)
and ``every_n=30`` (~1.2 s/channel), all 16 channels together fit
in ~20 s of queue backlog — acceptable for a live-monitoring UI
where the events timestamp reflects when the inference actually
completed, not when the frame was captured.

The user spec ("避免相互PIL锁干扰") is satisfied by serialising
access in a single worker rather than spawning 16 competing sessions
that each grab the RKNN lock independently.

``rkv`` and ``rkai`` are imported lazily — non-Rockchip dev boxes can
still boot the web shell and report ``unavailable`` for the channels.
"""
from __future__ import annotations

import asyncio
import os
import queue
import threading
import time
from collections import deque
from concurrent.futures import Future
from pathlib import Path
from typing import Any, List, Optional, Tuple


# --------------------------------------------------------------------------- #
# Module-level constants                                                     #
# --------------------------------------------------------------------------- #
CHANNEL_COUNT = 16

# Detection size — only two valid values. The qwen3-vl-4b model
# internally resizes to 384; 768 is the higher-fidelity preset.
# Detection is forced square; ``detect_w == detect_h`` is enforced
# wherever the value flows through. Per-channel override via
# ``detect_w/h``.
DETECT_SIZE_CHOICES = (384, 768)
DEFAULT_DETECT_W = 384
DEFAULT_DETECT_H = 384

# Detection size — only two valid values. The qwen3-vl-4b model
# internally resizes to 384; 768 is the higher-fidelity preset.
# Detection is forced square; ``detect_w == detect_h`` is enforced
# wherever the value flows through. Per-channel override via
# ``detect_w/h``.
DETECT_SIZE_CHOICES = (384, 768)
DEFAULT_DETECT_W = 384
DEFAULT_DETECT_H = 384

# Thumbnail size + rate for the grid cells. With the move to MJPEG
# (see below) the thumbnail is a detection-resolution JPEG sent at
# ~5fps; THUMB_W/H constants are kept for backward compat but no
# longer drive the resize (we resize to detect_w/detect_h instead).
THUMB_W = 240
THUMB_H = 135
THUMB_INTERVAL_S = 0.2  # ~5 fps

# MJPEG encoder settings (replaces the old H.264 path).
ENCODE_FPS = 25
STREAM_JPEG_QUALITY = 80    # 1..99; native-resolution frames
THUMB_JPEG_QUALITY = 70     # 1..99; detection-resolution frames (smaller)
# Workaround interval for the MPP MJPEG HAL input-queue leak (see
# ``.venv/src/rkv/src/rkv/encoder.py`` ``_MPP_MJPEG_RECYCLE_EVERY``
# comment). After ~6 encode() calls the queue fills up; closing and
# recreating the encoder clears it. We recycle every 5 frames as a
# safety margin (1 frame headroom under the observed deadlock).
_MPP_MJPEG_RECYCLE_EVERY = 5

# VLM cadence: one inference every N frames. Bounded so a slow channel
# doesn't burn the NPU; user-tunable via the channel config.
DEFAULT_EVERY_N = 30

# Inference worker queue: drop oldest on overflow. The decode threads
# fire-and-forget; a backed-up queue just means older frames get
# skipped, which is the right behaviour for live monitoring.
INFERENCE_QUEUE_MAX = 8

# How long a single inference may take before we mark the request as
# timed out. The C-level RKNN call is bounded by max_new_tokens (1024
# in the qwen3_vl_4b preset) but on a stuck NPU we still want to give
# up rather than block forever.
INFERENCE_TIMEOUT_S = 60.0

# Lazy rkai import cache — shared with :mod:`app.services.detector`.
# We import ``from app.services.detector import _load_rkai`` to avoid
# duplicating the cache; if the detector module hasn't been imported
# yet, this still triggers the import (Python's module cache makes
# that O(1)).
_RKAI_CACHE: dict = {}


def _load_rkai():
    """Lazy rkai import. Same cache key as :mod:`app.services.detector`
    so the import only happens once across the whole process. Returns
    ``(ok, Attachment, LLMSession, SystemLanguageModel, error_str)``.

    Why duplicate :func:`app.services.detector._load_rkai`? Because
    detector's helper returns the ``@Generable`` extras we don't need
    here. We use the *same* module-level cache (``_RKAI_CACHE``) so
    the actual rkai import only runs once regardless of which entry
    point hits first.
    """
    if "_ok_surv" in _RKAI_CACHE:
        return (
            _RKAI_CACHE["_ok_surv"],
            _RKAI_CACHE["_Attachment_surv"],
            _RKAI_CACHE["_LLMSession_surv"],
            _RKAI_CACHE["_SystemLanguageModel_surv"],
            _RKAI_CACHE["_error_surv"],
        )
    try:
        from rkai import (
            Attachment,
            LLMSession,
            SystemLanguageModel,
        )
        _RKAI_CACHE.update(
            _ok_surv=True,
            _Attachment_surv=Attachment,
            _LLMSession_surv=LLMSession,
            _SystemLanguageModel_surv=SystemLanguageModel,
            _error_surv="",
        )
        return True, Attachment, LLMSession, SystemLanguageModel, ""
    except Exception as e:
        _RKAI_CACHE.update(
            _ok_surv=False,
            _Attachment_surv=None,
            _LLMSession_surv=None,
            _SystemLanguageModel_surv=None,
            _error_surv=str(e),
        )
        return False, None, None, None, str(e)


def _load_rkv():
    """Lazy rkv import — :class:`rkv.VideoCapture` and the MPP
    encoder/decoder handles are heavy native libs; the rkv package
    already gates them behind a try/except so a non-Rockchip dev box
    still boots. Returns the symbols the surveillance path needs:

    * ``VideoCapture`` — per-channel decode
    * ``MppEncoder`` — MJPEG hardware encoder (one instance per
      encode path; recycled every ``_MPP_MJPEG_RECYCLE_EVERY``
      frames to dodge the queue leak)
    * ``MppCodingType.MJPEG`` — fixedqp MJPEG coding
    * ``MppFrameFormat.BGR888`` — input format the encoder consumes
      (we resize NV12 → BGR via RGA)
    """
    try:
        from rkv import VideoCapture
        from rkv.mpp import MppEncoder, MppCodingType, MppFrameFormat
        return (
            True,
            VideoCapture,
            MppEncoder,
            int(MppCodingType.MJPEG),
            int(MppFrameFormat.BGR888),
            "",
        )
    except Exception as e:
        return False, None, None, None, None, str(e)


# --------------------------------------------------------------------------- #
# Source classification & playlist building                                   #
# --------------------------------------------------------------------------- #
# Recognised video extensions — used both when classifying a source string
# and when listing folder contents. Matches ``VIDEO_EXTS`` in app.js so
# the in-page file picker and the server agree on what's a video.
_VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".ts",
    ".m4v", ".3gp", ".wmv", ".mpg", ".mpeg", ".rmvb",
    ".h264", ".h265", ".hevc", ".m2ts", ".mts",
}


def _classify_source(source: str) -> str:
    """Classify the configured source string into one of:
      * ``"live"``     — RTSP / RTMP / HLS / ``/dev/video*`` URL.
      * ``"file"``     — regular file on disk; single-file loop mode.
      * ``"playlist"`` — directory on disk; play all videos in
                          alphabetical order and loop.
      * ``"missing"``  — neither a live URL nor a valid path.
    The check is cheap (``stat`` syscall) so we run it on every
    ``_decode_loop`` start, including after a playlist wrap, so a
    folder that gains new files mid-session picks them up.
    """
    if not isinstance(source, str):
        return "missing"
    s = source.strip()
    if not s:
        return "missing"
    if "://" in s or s.startswith("/dev/video"):
        return "live"
    try:
        p = Path(s)
        if p.is_dir():
            return "playlist"
        if p.is_file():
            return "file"
    except (OSError, RuntimeError, ValueError):
        pass
    return "missing"


def _build_playlist(folder: str) -> List[str]:
    """Return the absolute paths of all video files in ``folder``,
    sorted alphabetically. Hidden / dotfiles are skipped. Returns an
    empty list on any filesystem error (so the caller can surface a
    cleaner error than ``OSError``)."""
    try:
        out: List[str] = []
        for entry in os.scandir(folder):
            if not entry.is_file():
                continue
            name = entry.name
            if name.startswith("."):
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext in _VIDEO_EXTS:
                out.append(entry.path)
        out.sort()
        return out
    except (OSError, RuntimeError, ValueError):
        return []


# --------------------------------------------------------------------------- #
# Default channel scaffold                                                    #
# --------------------------------------------------------------------------- #
def _default_channels() -> List[dict]:
    """Build a 16-slot default channel list. The ``id`` field is added
    at read time from the list index so callers can't accidentally
    re-number things."""
    out: List[dict] = []
    for i in range(CHANNEL_COUNT):
        out.append({
            "name": f"通道 {i + 1}",
            "source": "",
            "prompt": (
                "请用一句话描述当前画面中发生的事件，并标注是否有异常"
            ),
            "playback_enabled": False,
            "detection_enabled": False,
            "detect_w": DEFAULT_DETECT_W,
            "detect_h": DEFAULT_DETECT_H,
        })
    return out


def _merge_default(stored: dict, idx: int) -> dict:
    """Merge a stored channel dict with defaults so missing keys are
    filled in without losing unknown fields (forward-compat).

    Old configs stored a single ``enabled`` field. Map it to
    ``playback_enabled`` when present so older saved states still
    light up after the toggle split.

    Migrate ``detect_w`` / ``detect_h`` to one of the supported
    detection sizes (currently ``{384, 768}``): any stored value
    outside the set falls back to the default. The two fields are
    also forced equal (square) — anything else is silently snapped
    to the smaller of the two.
    """
    defaults = _default_channels()[idx]
    merged = dict(defaults)
    if isinstance(stored, dict):
        for k, v in stored.items():
            merged[k] = v
        if "enabled" in stored and "playback_enabled" not in stored:
            merged["playback_enabled"] = bool(stored["enabled"])
            merged.pop("enabled", None)
        if (merged.get("detect_w") not in DETECT_SIZE_CHOICES
                or merged.get("detect_h") not in DETECT_SIZE_CHOICES):
            # Pre-migration dimensions (e.g. 480×270). Reset to default.
            merged["detect_w"] = defaults["detect_w"]
            merged["detect_h"] = defaults["detect_h"]
        elif merged["detect_w"] != merged["detect_h"]:
            # Stored sizes are valid but not equal (would only happen
            # if someone hand-edited config.json). Snap to square.
            sq = min(merged["detect_w"], merged["detect_h"])
            merged["detect_w"] = sq
            merged["detect_h"] = sq
    return merged


# --------------------------------------------------------------------------- #
# Channel                                                                    #
# --------------------------------------------------------------------------- #
class Channel:
    """One surveillance channel. State, decode thread, packet fan-out."""

    STATE_IDLE = "idle"
    STATE_OPENING = "opening"
    STATE_RUNNING = "running"
    STATE_PAUSED = "paused"
    STATE_EOF = "eof"
    STATE_ERROR = "error"

    def __init__(self, index: int, manager: "SurveillanceManager") -> None:
        self.id: int = index + 1
        self._manager = manager

        # ----- config (mutable from REST) -----
        self._config_lock = threading.Lock()
        defaults = _default_channels()[index]
        self._name: str = defaults["name"]
        self._source: str = defaults["source"]
        self._prompt: str = defaults["prompt"]
        # Two independent toggles: ``playback_enabled`` controls the
        # decode / encode pipeline, ``detection_enabled`` controls
        # whether the shared VLM worker gets frames. Playback can
        # run without detection (e.g. you want to watch 16 streams
        # without burning the NPU).
        self._playback_enabled: bool = defaults["playback_enabled"]
        self._detection_enabled: bool = defaults["detection_enabled"]
        self._detect_w: int = defaults["detect_w"]
        self._detect_h: int = defaults["detect_h"]

        # ----- runtime state -----
        self._lock = threading.Lock()
        self._state: str = self.STATE_IDLE
        self._error: str = ""
        self._frame_pos: int = 0
        self._fps_window_start: float = 0.0
        self._fps_window_count: int = 0
        self._fps: float = 0.0
        # ``_current_source_label`` is the basename of whichever file
        # in the playlist is currently being decoded; ``""`` when
        # the thread isn't running. Lets the UI show "playing cam2.mp4"
        # when a folder source is on the third file of its loop.
        self._current_source_label: str = ""

        # ----- lifecycle -----
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # ----- rkai session is owned by the manager (ComputeRegistry); this
        # channel only records session-level state for snapshot() -----
        self._session_error: str = ""

        # ----- VLM event history (in-memory, max 100 per channel) -----
        self._events_lock = threading.Lock()
        self._events: deque = deque(maxlen=100)
        self._next_event_id: int = 1

        # ----- native MJPEG stream fan-out (raw JPEG bytes) -----
        # Subscribers are server-side WS clients (the browser). Each
        # WS holds its own queue + event so a slow subscriber doesn't
        # back-pressure the decode thread — the queue's
        # put_nowait-with-drop-oldest behaviour favours latency.
        self._stream_lock = threading.Lock()
        self._stream_jpeg: Optional[bytes] = None
        self._stream_subs_lock = threading.Lock()
        self._stream_subs: List[Tuple[queue.Queue, threading.Event]] = []

        # ----- detection-resolution MJPEG thumb fan-out -----
        self._thumb_lock = threading.Lock()
        self._thumb_jpeg: Optional[bytes] = None
        self._thumb_last_ts: float = 0.0
        self._thumb_subs_lock = threading.Lock()
        self._thumb_subs: List[Tuple[queue.Queue, threading.Event]] = []

    # ------------------------------------------------------------------ #
    # Config                                                              #
    # ------------------------------------------------------------------ #
    def config_dict(self) -> dict:
        with self._config_lock:
            return {
                "id": self.id,
                "name": self._name,
                "source": self._source,
                "prompt": self._prompt,
                "playback_enabled": self._playback_enabled,
                "detection_enabled": self._detection_enabled,
                "detect_w": self._detect_w,
                "detect_h": self._detect_h,
            }

    def apply_config(self, *, name=None, source=None, prompt=None,
                      playback_enabled=None, detection_enabled=None,
                      enabled=None,  # legacy alias for playback_enabled
                      detect_w=None, detect_h=None) -> dict:
        # Backward-compat: old configs stored ``enabled`` instead of
        # ``playback_enabled``. If the caller passes ``enabled`` but
        # not ``playback_enabled``, treat the legacy field as the new
        # playback toggle.
        if playback_enabled is None and enabled is not None:
            playback_enabled = enabled
        with self._config_lock:
            if name is not None:
                v = str(name).strip()
                if v:
                    self._name = v
            if source is not None:
                self._source = str(source).strip()
            if prompt is not None:
                self._prompt = str(prompt)
            if playback_enabled is not None:
                self._playback_enabled = bool(playback_enabled)
            if detection_enabled is not None:
                self._detection_enabled = bool(detection_enabled)
            if detect_w is not None:
                try:
                    w = int(detect_w)
                    if w in DETECT_SIZE_CHOICES:
                        self._detect_w = w
                except (TypeError, ValueError):
                    pass
            if detect_h is not None:
                try:
                    h = int(detect_h)
                    if h in DETECT_SIZE_CHOICES:
                        self._detect_h = h
                except (TypeError, ValueError):
                    pass
            # Force square: if the caller sets only one dimension we
            # mirror it, ensuring ``cap.read0(detect_w, detect_h)`` is
            # always square.
            if (detect_w is not None or detect_h is not None
                    and self._detect_w != self._detect_h):
                sq = min(self._detect_w, self._detect_h)
                self._detect_w = sq
                self._detect_h = sq
            snap = {
                "name": self._name,
                "source": self._source,
                "prompt": self._prompt,
                "playback_enabled": self._playback_enabled,
                "detection_enabled": self._detection_enabled,
                "detect_w": self._detect_w,
                "detect_h": self._detect_h,
            }
        # Side effect: start/stop the decode thread if either toggle
        # or the source / size changed.
        self._maybe_restart()
        return snap

    def _maybe_restart(self) -> None:
        with self._config_lock:
            playback = self._playback_enabled
            detection = self._detection_enabled
            source = self._source
            detect_w = self._detect_w
            detect_h = self._detect_h
        # The decode thread runs whenever ``playback_enabled`` is on
        # AND we have a source. Detection is a no-op when the thread
        # isn't running (handled inside the loop), so toggling it
        # alone doesn't need to start / stop anything.
        if playback and source:
            with self._lock:
                cur_source = getattr(self, "_active_source", "")
                cur_size = getattr(self, "_active_size", (0, 0))
            if (cur_source != source
                    or cur_size != (detect_w, detect_h)
                    or self._state in (self.STATE_IDLE,
                                       self.STATE_ERROR,
                                       self.STATE_EOF)):
                self.start()
        else:
            if self._state != self.STATE_IDLE:
                self.stop()

    # ------------------------------------------------------------------ #
    # Status                                                              #
    # ------------------------------------------------------------------ #
    def snapshot(self) -> dict:
        with self._lock:
            return {
                "id": self.id,
                "state": self._state,
                "error": self._error,
                "frame_pos": self._frame_pos,
                "fps": round(self._fps, 1),
                "current_file": self._current_source_label,
                "thread_alive":
                    self._thread is not None and self._thread.is_alive(),
                "session_ready": self._manager.session_ready(),
                "session_error": self._manager.session_error(),
                "events_count": len(self._events),
                # Latest bytes available for new subscribers; surfaces
                # in the UI as a "frame available" hint so the cell
                # can render immediately rather than waiting up to
                # one frame interval.
                "thumb_ready": self.get_thumb() is not None,
                "stream_ready": self.get_stream() is not None,
            }

    def events_since(self, since_id: int = 0) -> List[dict]:
        with self._events_lock:
            return [
                dict(ev) for ev in self._events
                if int(ev.get("id", 0)) > since_id
            ]

    def latest_event(self) -> Optional[dict]:
        with self._events_lock:
            if not self._events:
                return None
            return dict(self._events[-1])

    def clear_events(self) -> None:
        with self._events_lock:
            self._events.clear()

    # ------------------------------------------------------------------ #
    # Lifecycle                                                           #
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        with self._config_lock:
            source = self._source
            detect_w = self._detect_w
            detect_h = self._detect_h
        if not source:
            with self._lock:
                if self._state != self.STATE_IDLE:
                    self._state = self.STATE_IDLE
                self._error = ""
            return
        # Tear down any prior session first so the new thread sees a
        # clean stop_event state (mirrors the VideoSession pattern).
        self._teardown()
        with self._lock:
            self._state = self.STATE_OPENING
            self._error = ""
            self._frame_pos = 0
            self._fps_window_start = 0.0
            self._fps_window_count = 0
            self._fps = 0.0
            self._active_source = source
            self._active_size = (detect_w, detect_h)
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._decode_loop,
            args=(source, detect_w, detect_h),
            name=f"surveillance-{self.id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._teardown()

    def _teardown(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.5)
            self._thread = None
        # The shared ``LLMSession`` is owned by the manager and outlives
        # any single channel — we never close it here.
        with self._lock:
            if self._state not in (self.STATE_IDLE,):
                self._state = self.STATE_IDLE
            self._active_source = ""
            self._active_size = (0, 0)

    def _set_error(self, msg: str) -> None:
        with self._lock:
            if self._state == self.STATE_IDLE:
                return
            self._state = self.STATE_ERROR
            self._error = msg

    # ------------------------------------------------------------------ #
    # Decode loop                                                         #
    # ------------------------------------------------------------------ #
    def _decode_loop(self, source: str, detect_w: int, detect_h: int) -> None:
        ok, VideoCapture, MppEncoder, MJPEG_FMT, BGR_FMT, err = _load_rkv()
        if not ok:
            self._set_error(f"rkv 未加载: {err}")
            return

        # ``resize_bgr`` does NV12→BGR + resize in a single RGA pass.
        # Imported lazily so a non-Rockchip dev box that never calls
        # the encode path still boots.
        try:
            from rkv import rga as _rkv_rga
            resize_bgr = _rkv_rga.resize_bgr
        except Exception:
            resize_bgr = None  # type: ignore[assignment]

        # Encoder lifecycle state. We keep one MppEncoder instance per
        # MJPEG output (stream at native res, thumb at detect res) and
        # recycle it every ``_MPP_MJPEG_RECYCLE_EVERY`` frames to dodge
        # the MPP input-queue leak (see ``_MPP_MJPEG_RECYCLE_EVERY``
        # comment at the top of the file). Both encoders follow the
        # same lifecycle as ``cap`` — created on file open, destroyed
        # on file close (or on stop).
        self._stream_enc = None
        self._stream_enc_count = 0
        self._thumb_enc = None
        self._thumb_enc_count = 0

        def _open_enc(width: int, height: int, q: int):
            return MppEncoder(
                MJPEG_FMT,
                width=width,
                height=height,
                fps=ENCODE_FPS,
                gop=1,                # MJPEG: every frame is independent
                input_format=BGR_FMT,
                rc_mode="fixqp",      # MJPEG is fixed-quality per frame
                q_factor=q,
            )

        kind = _classify_source(source)
        # ``playlist`` is None for live / missing / single-file sources
        # (single-file still loops, but there's only one entry so the
        # index advancement is a no-op — see EOF handling below).
        if kind == "playlist":
            playlist = _build_playlist(source)
            if not playlist:
                self._set_error(f"文件夹中没有视频文件: {source}")
                return
        elif kind == "file":
            playlist = [source]
        else:
            playlist = None
        playlist_idx = 0
        # Force a re-scan of the folder at every wrap so newly-dropped
        # files get picked up without restarting the channel.

        while not self._stop_event.is_set():
            # Pick the current source — playlist or single source.
            if playlist is not None and playlist:
                current_source = playlist[playlist_idx % len(playlist)]
            else:
                current_source = source

            # Open VideoCapture for the current source.
            try:
                cap = VideoCapture(current_source, timeout=10.0)
            except Exception as e:
                # If we have other files in the playlist, skip ahead.
                if playlist is not None and len(playlist) > 1:
                    playlist_idx = (playlist_idx + 1) % len(playlist)
                    continue
                self._set_error(f"打开失败: {e}")
                return
            if not cap.isOpened():
                try:
                    cap.release()
                except Exception:
                    pass
                if playlist is not None and len(playlist) > 1:
                    playlist_idx = (playlist_idx + 1) % len(playlist)
                    continue
                self._set_error("VideoCapture.isOpened() = False")
                return

            fps = max(1, int(getattr(cap, "fps", 0.0) or 0.0) or ENCODE_FPS)
            native_w = int(getattr(cap, "width", 0) or 0)
            native_h = int(getattr(cap, "height", 0) or 0)
            if native_w <= 0 or native_h <= 0:
                try:
                    cap.release()
                except Exception:
                    pass
                if playlist is not None and len(playlist) > 1:
                    playlist_idx = (playlist_idx + 1) % len(playlist)
                    continue
                self._set_error(f"无法获取源尺寸: {current_source}")
                return

            # Open both MJPEG encoders (native + detection). If either
            # fails we drop this file from the playlist and move on.
            try:
                self._stream_enc = _open_enc(
                    native_w, native_h, STREAM_JPEG_QUALITY,
                )
                self._thumb_enc = _open_enc(
                    detect_w, detect_h, THUMB_JPEG_QUALITY,
                )
                self._stream_enc_count = 0
                self._thumb_enc_count = 0
            except Exception as e:
                self._set_error(f"MppEncoder 初始化失败: {e}")
                try:
                    cap.release()
                except Exception:
                    pass
                if playlist is not None and len(playlist) > 1:
                    playlist_idx = (playlist_idx + 1) % len(playlist)
                    continue
                return

            with self._lock:
                self._state = self.STATE_RUNNING
                self._error = ""
                self._current_source_label = os.path.basename(current_source)

            # ---- inner decode loop for this one file ----
            hit_eof = False
            next_frame_at = time.monotonic()
            target_interval = 1.0 / float(fps)
            while not self._stop_event.is_set():
                try:
                    ret, frame0 = cap.read0(fmt="NV12")
                except Exception as e:
                    self._set_error(f"read0 异常: {e}")
                    break
                if not ret or frame0 is None:
                    hit_eof = True
                    break

                with self._lock:
                    self._frame_pos += 1
                    now = time.time()
                    if self._fps_window_start == 0.0:
                        self._fps_window_start = now
                        self._fps_window_count = 1
                    else:
                        self._fps_window_count += 1
                        elapsed = now - self._fps_window_start
                        if elapsed >= 1.0:
                            self._fps = self._fps_window_count / elapsed
                            self._fps_window_start = now
                            self._fps_window_count = 0

                # VLM inference every Nth frame (routed through the
                # manager's shared ``LLMSession`` via the inference
                # worker — see ``SurveillanceManager.submit_inference``).
                with self._config_lock:
                    detection_on = self._detection_enabled
                    prompt = self._prompt
                    every_n = DEFAULT_EVERY_N
                if (detection_on and prompt
                        and every_n > 0
                        and self._frame_pos % every_n == 0):
                    self._submit_vlm(frame0, prompt, detect_w, detect_h)

                # Native-res MJPEG stream (full source fps). Recycle
                # the encoder before it deadlocks on the MPP queue
                # leak. On close / re-open failure we null the slot
                # so subsequent encode attempts skip silently
                # instead of pounding on a closed handle.
                if (
                    self._stream_enc is not None
                    and self._stream_enc_count >= _MPP_MJPEG_RECYCLE_EVERY
                ):
                    try:
                        self._stream_enc.close()
                    except Exception:
                        pass
                    self._stream_enc = None
                if self._stream_enc is None and resize_bgr is not None:
                    try:
                        self._stream_enc = _open_enc(
                            native_w, native_h, STREAM_JPEG_QUALITY,
                        )
                        self._stream_enc_count = 0
                    except Exception:
                        pass  # leave None; next frame will retry
                if resize_bgr is not None and self._stream_enc is not None:
                    try:
                        native_bgr = resize_bgr(frame0, native_w, native_h)
                        pkt = self._stream_enc.encode(native_bgr)
                        if pkt and pkt.data:
                            self._stream_enc_count += 1
                            self._push_stream(pkt.data)
                    except Exception:
                        pass

                # Detection-res MJPEG thumb (~5 fps, gated by
                # ``_thumb_last_ts``). Same recycle logic as the
                # stream encoder — thumb frame count is much lower
                # per second but the queue leak still applies.
                now_mono = time.monotonic()
                with self._thumb_lock:
                    thumb_due = (now_mono - self._thumb_last_ts) >= THUMB_INTERVAL_S
                if thumb_due and self._thumb_enc is not None:
                    if self._thumb_enc_count >= _MPP_MJPEG_RECYCLE_EVERY:
                        try:
                            self._thumb_enc.close()
                        except Exception:
                            pass
                        self._thumb_enc = None
                if (
                    thumb_due
                    and self._thumb_enc is None
                    and resize_bgr is not None
                ):
                    try:
                        self._thumb_enc = _open_enc(
                            detect_w, detect_h, THUMB_JPEG_QUALITY,
                        )
                        self._thumb_enc_count = 0
                    except Exception:
                        pass
                if thumb_due and self._thumb_enc is not None and resize_bgr is not None:
                    try:
                        detect_bgr = resize_bgr(frame0, detect_w, detect_h)
                        pkt = self._thumb_enc.encode(detect_bgr)
                        if pkt and pkt.data:
                            self._thumb_enc_count += 1
                            with self._thumb_lock:
                                self._thumb_jpeg = pkt.data
                                self._thumb_last_ts = now_mono
                            self._push_thumb(pkt.data)
                    except Exception:
                        pass

                # Server-side pacing. ``read0`` returns frames as
                # fast as the decoder can produce them; without this
                # we'd blast frames through the WS at hundreds of
                # fps for a local file. Live streams (RTSP/RTMP/
                # V4L2) block on the upstream so this sleep reduces
                # to ~0 naturally.
                now_mono = time.monotonic()
                delay = next_frame_at - now_mono
                if delay > 0:
                    time.sleep(delay)
                next_frame_at += target_interval
                if next_frame_at < now_mono:
                    # Loop iteration overran (encode stall, GC,
                    # etc.). Resync rather than play catch-up by
                    # bursting.
                    next_frame_at = now_mono + target_interval

            # ---- cleanup this file ----
            for enc_attr in ("_stream_enc", "_thumb_enc"):
                enc = getattr(self, enc_attr, None)
                if enc is not None:
                    try:
                        enc.close()
                    except Exception:
                        pass
                    setattr(self, enc_attr, None)
            try:
                cap.release()
            except Exception:
                pass

            if self._stop_event.is_set():
                break

            if not hit_eof:
                # We exited the inner loop via an error / read0 failure;
                # don't try to advance the playlist, just bail out.
                break

            # EOF reached — figure out what to do next.
            if playlist is None:
                # Live stream that somehow hit EOF (rare). Mark EOF
                # and stop — looping a live stream doesn't make sense.
                with self._lock:
                    if self._state not in (self.STATE_IDLE, self.STATE_ERROR):
                        self._state = self.STATE_EOF
                break

            # Single-file or playlist: advance (and wrap around).
            playlist_idx = (playlist_idx + 1) % max(len(playlist), 1)

            if kind == "playlist":
                # Re-scan the folder so newly-dropped files join the
                # loop without restarting the channel.
                new_playlist = _build_playlist(source)
                if new_playlist:
                    playlist = new_playlist
                    if playlist_idx >= len(playlist):
                        playlist_idx = 0

            # Reset per-file counters and fall back into the outer loop
            # to reopen the next file. Keep ``STATE_RUNNING`` (not
            # EOF) so the UI doesn't flicker to "已结束" between files.
            with self._lock:
                self._state = self.STATE_RUNNING
                self._error = ""
                self._frame_pos = 0
                self._fps_window_start = 0.0
                self._fps_window_count = 0
                self._fps = 0.0

        # Stop event fired (or fatal error) — drop back to idle.
        with self._lock:
            if self._state == self.STATE_RUNNING:
                self._state = self.STATE_IDLE
            self._current_source_label = ""

    # ------------------------------------------------------------------ #
    # JPEG fan-out (native stream + detection thumb)                     #
    # ------------------------------------------------------------------ #
    # Both the enlarge-modal WS and the per-cell thumbnail WS deliver
    # raw JPEG bytes (no header / framing) — the browser just hands
    # them to ``<img>`` via a Blob URL. The decode thread pushes each
    # encoded JPEG into every subscriber queue with a non-blocking
    # ``put_nowait``; full queues drop the oldest frame so a stalled
    # subscriber doesn't back-pressure the encode pipeline.

    def _push_stream(self, jpg: bytes) -> None:
        with self._stream_lock:
            self._stream_jpeg = jpg
        with self._stream_subs_lock:
            subs = list(self._stream_subs)
        for q, evt in subs:
            try:
                q.put_nowait(jpg)
                evt.set()
            except queue.Full:
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(jpg)
                    evt.set()
                except queue.Full:
                    pass

    def _push_thumb(self, jpg: bytes) -> None:
        with self._thumb_subs_lock:
            subs = list(self._thumb_subs)
        for q, evt in subs:
            try:
                q.put_nowait(jpg)
                evt.set()
            except queue.Full:
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(jpg)
                    evt.set()
                except queue.Full:
                    pass

    def get_thumb(self) -> Optional[bytes]:
        with self._thumb_lock:
            return self._thumb_jpeg

    def get_stream(self) -> Optional[bytes]:
        with self._stream_lock:
            return self._stream_jpeg

    def register_thumb_subscriber(self) -> Tuple[queue.Queue, threading.Event]:
        q = queue.Queue(maxsize=2)
        evt = threading.Event()
        with self._thumb_subs_lock:
            self._thumb_subs.append((q, evt))
        # Immediately deliver the latest thumb so a new viewer doesn't
        # stare at an empty box for up to THUMB_INTERVAL_S.
        with self._thumb_lock:
            cur = self._thumb_jpeg
        if cur is not None:
            try:
                q.put_nowait(cur)
                evt.set()
            except queue.Full:
                pass
        return q, evt

    def unregister_thumb_subscriber(
        self, q: queue.Queue
    ) -> None:
        with self._thumb_subs_lock:
            self._thumb_subs = [
                (qq, e) for (qq, e) in self._thumb_subs if qq is not q
            ]

    def register_stream_subscriber(self) -> Tuple[queue.Queue, threading.Event]:
        q = queue.Queue(maxsize=2)
        evt = threading.Event()
        with self._stream_subs_lock:
            self._stream_subs.append((q, evt))
        with self._stream_lock:
            cur = self._stream_jpeg
        if cur is not None:
            try:
                q.put_nowait(cur)
                evt.set()
            except queue.Full:
                pass
        return q, evt

    def unregister_stream_subscriber(self, q: queue.Queue) -> None:
        with self._stream_subs_lock:
            self._stream_subs = [
                (qq, e) for (qq, e) in self._stream_subs if qq is not q
            ]

    # ------------------------------------------------------------------ #
    # VLM inference (routed through the manager's shared session)       #
    # ------------------------------------------------------------------ #
    def _submit_vlm(self, frame0, prompt: str, detect_w: int, detect_h: int) -> None:
        """Fire-and-forget VLM inference. The channel's decode thread
        never blocks — the manager's worker thread consumes the
        request, calls ``session.respond()`` on the shared
        ``LLMSession``, and resolves a :class:`concurrent.futures.Future`.
        We attach a done-callback that records the result in
        ``self._events`` so the UI sees it next time it polls.

        ``frame0`` is the decoder's native NV12 DMA-BUF — we resize
        + colour-convert to BGR at ``(detect_w, detect_h)`` in a
        single RGA pass via ``rkv.rga.resize_bgr`` (no NV12→RGB→BGR
        CPU round-trip). The encode paths keep ``frame0`` untouched
        so the encoder sees the same DMA-BUF the decoder handed us.

        Returns immediately so the decode loop can keep pumping frames
        regardless of how long the inference queue happens to be.
        """
        try:
            from rkv import rga as _rkv_rga
            bgr = _rkv_rga.resize_bgr(frame0, detect_w, detect_h)
        except Exception:
            return
        future = self._manager.submit_inference(
            self.id, bgr, prompt, frame_pos=self._frame_pos,
        )
        future.add_done_callback(self._on_inference_done)

    def _on_inference_done(self, future: "Future") -> None:
        """Future completion callback. Runs on the inference worker's
        asyncio loop thread; only touches thread-safe state."""
        try:
            text, error = future.result()
        except Exception as e:  # pragma: no cover
            text, error = "", f"(future exception: {e})"
        if error:
            text = f"(推理失败: {error})"
        try:
            frame_pos = int(future.frame_pos)  # type: ignore[attr-defined]
        except Exception:
            frame_pos = -1
        try:
            prompt = str(future.prompt)  # type: ignore[attr-defined]
        except Exception:
            prompt = ""
        with self._events_lock:
            ev_id = self._next_event_id
            self._next_event_id += 1
            ev = {
                "id": ev_id,
                "frame_pos": frame_pos,
                "ts": time.time(),
                "prompt": prompt,
                "text": text,
            }
            self._events.append(ev)

    # ------------------------------------------------------------------ #
    # End of Channel                                                      #
    # ------------------------------------------------------------------ #


# --------------------------------------------------------------------------- #
# Inference worker (shared LLMSession + single asyncio loop + queue)        #
# --------------------------------------------------------------------------- #
class _InferenceWorker:
    """Owns the ONE shared ``rkai.LLMSession`` and dispatches inference
    requests from any of the 16 channels serially.

    Threading contract:

    * **Producer side (any of the 16 channel decode threads)** calls
      :meth:`submit_inference`. This pushes a ``_Request`` onto an
      ``asyncio.Queue`` via ``loop.call_soon_threadsafe`` and returns
      a :class:`concurrent.futures.Future` immediately.
    * **Consumer side (this worker's asyncio loop)** pulls items off
      the queue one at a time. For each item it calls
      ``session.reset()`` then ``await session.respond(prompt,
      attachment=[...])`` — both of these are rkai API calls, never
      direct rknn3lite. The result / exception resolves the future.
    * The worker's persistent loop is started in
      :meth:`start` and stopped in :meth:`close`; ``close`` also frees
      the RKNN session via ``LLMSession.close()``.
    """

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_ready = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # LLMSession 由 ComputeRegistry 持有 —— 不再自己持 _session。
        self._session_lock = threading.Lock()
        # Queue is asyncio.Queue so the worker can ``await`` on it from
        # inside its loop; producers put items in via
        # ``call_soon_threadsafe``.
        self._queue: Optional[asyncio.Queue] = None
        self._closed = False

    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """Boot the worker thread + persistent asyncio loop."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._closed = False
        self._loop_ready.clear()
        self._thread = threading.Thread(
            target=self._run_loop, name="vlm-inference", daemon=True,
        )
        self._thread.start()
        # Wait until the loop is up so the first ``submit_inference``
        # call doesn't race the thread.
        self._loop_ready.wait(timeout=5.0)

    def close(self) -> None:
        """Stop accepting new requests, drain the queue, close the
        session, and join the worker thread."""
        self._closed = True
        loop = self._loop
        if loop is None:
            return
        # Schedule the shutdown coroutine on the loop's own thread.
        # ``call_soon_threadsafe`` is the safe bridge from another
        # thread into the loop. The coroutine below is responsible
        # for sending the sentinel, awaiting the consumer task, and
        # stopping the loop — so the consumer never gets destroyed
        # while awaiting on the queue.
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._graceful_shutdown(), loop
            )
        except Exception:
            return
        # Wait for the shutdown coroutine to finish (with a hard cap).
        try:
            future.result(timeout=5.0)
        except Exception:
            pass
        if self._thread is not None:
            self._thread.join(timeout=3.0)

    async def _graceful_shutdown(self) -> None:
        """Run on the worker's loop. Cancels the consumer, drains
        pending requests, closes the session, then stops the loop."""
        # Cancel any pending tasks (the consumer is awaiting on the
        # queue — we want it to wake up and exit cleanly).
        for task in list(asyncio.all_tasks(self._loop)):
            if task is not asyncio.current_task():
                task.cancel()
        # Wake the consumer with a sentinel so its ``await`` returns.
        if self._queue is not None:
            try:
                self._queue.put_nowait(None)
            except Exception:
                pass
        # Give cancelled tasks a chance to settle.
        await asyncio.sleep(0)
        # Drain the queue. LLMSession is owned by ComputeRegistry, so
        # we don't close it here — registry.close_all() handles that on
        # app shutdown.
        if self._queue is not None:
            while True:
                try:
                    item = self._queue.get_nowait()
                except Exception:
                    break
                if item is None:
                    continue
                if not item.future.done():
                    item.future.set_result(("", "(vlm worker 已关闭)"))
        self._loop.stop()

    def session_ready(self) -> bool:
        """是否已就绪 video_query capability（由 ComputeRegistry 提供）。"""
        try:
            from app.services.compute_registry import get_registry
            get_registry().get_session("video_query")
            return True
        except RuntimeError:
            return False

    def session_error(self) -> str:
        """video_query capability 不可用时的错误信息（UI 提示）。"""
        try:
            from app.services.compute_registry import get_registry
            get_registry().get_session("video_query")
            return ""
        except RuntimeError as e:
            return str(e)

    # ------------------------------------------------------------------ #
    # Producer API (called from any thread)                               #
    # ------------------------------------------------------------------ #
    def submit_inference(
        self,
        channel_id: int,
        frame_bgr: "np.ndarray",
        prompt: str,
        frame_pos: int = -1,
    ) -> Future:
        """Enqueue an inference request and return a Future that
        resolves to ``(text: str, error: str_or_None)``.

        If the worker is closed or hasn't started yet, the future
        resolves immediately with an error string.
        """
        future: Future = Future()
        future.frame_pos = frame_pos  # type: ignore[attr-defined]
        future.prompt = prompt        # type: ignore[attr-defined]
        future.channel_id = channel_id  # type: ignore[attr-defined]
        loop = self._loop
        if loop is None or self._closed:
            future.set_result(("", "vlm worker not running"))
            return future
        try:
            loop.call_soon_threadsafe(
                self._enqueue, channel_id, frame_bgr, prompt, future,
            )
        except Exception as e:
            future.set_result(("", f"submit failed: {e}"))
        return future

    def _enqueue(
        self,
        channel_id: int,
        frame_bgr: "np.ndarray",
        prompt: str,
        future: Future,
    ) -> None:
        assert self._queue is not None
        # Drop oldest on overflow so a backed-up producer doesn't
        # blow memory — the user spec accepts stale frames.
        while True:
            try:
                self._queue.put_nowait(
                    _Request(channel_id, frame_bgr, prompt, future)
                )
                break
            except asyncio.QueueFull:
                try:
                    dropped = self._queue.get_nowait()
                    if dropped is not None and not dropped.future.done():
                        dropped.future.set_result(
                            ("", "(推理队列已满, 跳过该帧)")
                        )
                except Exception:
                    break

    # ------------------------------------------------------------------ #
    # Worker loop (runs on the persistent asyncio thread)                 #
    # ------------------------------------------------------------------ #
    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._queue = asyncio.Queue(maxsize=INFERENCE_QUEUE_MAX)
        # Schedule the consumer coroutine that drains the queue
        # forever. ``run_forever`` keeps the loop alive until
        # ``loop.stop()`` is called from ``close()``.
        self._loop.create_task(self._consume())
        self._loop_ready.set()
        try:
            self._loop.run_forever()
        finally:
            # Belt-and-suspenders: ``close()`` already ran
            # ``_graceful_shutdown`` which closed the session and
            # drained the queue. Just close the loop now.
            try:
                self._loop.close()
            except Exception:
                pass
            self._loop = None
            self._queue = None

    async def _consume(self) -> None:
        """The coroutine that drains the queue forever. Lives on the
        worker's persistent asyncio loop."""
        assert self._queue is not None
        while True:
            req = await self._queue.get()
            if req is None:  # sentinel from _schedule_stop
                return
            try:
                text, error = await asyncio.wait_for(
                    self._infer_one(req),
                    timeout=INFERENCE_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                text, error = "", f"推理超时 ({INFERENCE_TIMEOUT_S}s)"
            except Exception as e:
                # VLM capability 不可用时（"功能未启用"）算正常业务流：
                # 把异常落到 error 字段，future 正常 set_result，channel
                # event 流照样走一行 error，**不**让 worker 协程挂掉。
                text, error = "", f"推理异常: {e}"
            if not req.future.done():
                req.future.set_result((text, error))

    async def _infer_one(self, req: "_Request") -> Tuple[str, Optional[str]]:
        # LLMSession 由 ComputeRegistry 统一管理（"算力配置"页面启用）。
        # 找不到 video_query capability → "功能未启用: video_query" —— 跟
        # detector / tts_session 一致；surveillance 的 16 路解码 / 缩略图
        # / WS 推送完全不动，只是 VLM 这一步报"功能未启用"。
        from app.services.compute_registry import get_registry
        try:
            sess = get_registry().get_session("video_query")
        except RuntimeError as e:
            return "", str(e)
        # ``session.reset()`` clears the C-level KV cache so the next
        # inference starts from a clean state. We call it between every
        # request — yes, it loses the rolling context, but each
        # surveillance call is a one-shot image+prompt, not a multi-turn
        # conversation. Trading context for determinism is the right
        # call here (see rkai/session.py:470 docstring).
        try:
            sess.reset()
        except Exception:
            pass
        try:
            ok, Attachment, *_ = _load_rkai()
            if not ok:
                return "", "rkai 不可用"
            result = await sess.respond(
                prompt=req.prompt,
                attachment=[Attachment(req.frame_bgr)],
            )
            text = str(getattr(result, "text", "") or "")
            return text, None
        except Exception as e:
            return "", str(e)


class _Request:
    """Tuple-like record queued by the producer and consumed by the
    worker. Keeping it as a class lets the Future carry typed fields
    without dict overhead on the hot path."""

    __slots__ = ("channel_id", "frame_bgr", "prompt", "future")

    def __init__(
        self,
        channel_id: int,
        frame_bgr: "np.ndarray",
        prompt: str,
        future: Future,
    ) -> None:
        self.channel_id = channel_id
        self.frame_bgr = frame_bgr
        self.prompt = prompt
        self.future = future


# --------------------------------------------------------------------------- #
# SurveillanceManager                                                       #
# --------------------------------------------------------------------------- #
class SurveillanceManager:
    """Owns 16 :class:`Channel` instances plus the shared VLM inference
    worker. Config lives under ``config.json`` -> ``surveillance.channels[i]``;
    missing fields are filled in from defaults so older configs keep
    working after a schema bump.
    """

    def __init__(self, config_store) -> None:
        self._store = config_store
        self._lock = threading.Lock()
        # The shared RKNN interpreter goes here — ONE for all 16
        # channels, dispatched by ``_InferenceWorker``.
        self._worker = _InferenceWorker()
        self._channels: List[Channel] = [
            Channel(i, self) for i in range(CHANNEL_COUNT)
        ]
        self._hydrate_from_config()
        self._worker.start()

    # ------------------------------------------------------------------ #
    # Inference fan-out                                                   #
    # ------------------------------------------------------------------ #
    def submit_inference(
        self,
        channel_id: int,
        frame_bgr: "np.ndarray",
        prompt: str,
        frame_pos: int = -1,
    ) -> Future:
        return self._worker.submit_inference(
            channel_id, frame_bgr, prompt, frame_pos=frame_pos,
        )

    def session_ready(self) -> bool:
        return self._worker.session_ready()

    def session_error(self) -> str:
        return self._worker.session_error()

    # ------------------------------------------------------------------ #
    # Config persistence                                                  #
    # ------------------------------------------------------------------ #
    def _hydrate_from_config(self) -> None:
        stored_list = self._read_channels_list()
        for idx, ch in enumerate(self._channels):
            merged = _merge_default(
                stored_list[idx] if idx < len(stored_list) else {},
                idx,
            )
            ch.apply_config(
                name=merged["name"],
                source=merged["source"],
                prompt=merged["prompt"],
                playback_enabled=merged.get("playback_enabled", False),
                detection_enabled=merged.get("detection_enabled", False),
                detect_w=merged["detect_w"],
                detect_h=merged["detect_h"],
            )

    def _read_channels_list(self) -> List[dict]:
        section = self._store.get_section("surveillance", {}) or {}
        chans = section.get("channels", [])
        if not isinstance(chans, list):
            return []
        # Trim / extend to CHANNEL_COUNT entries.
        out: List[dict] = []
        for i in range(CHANNEL_COUNT):
            if i < len(chans) and isinstance(chans[i], dict):
                out.append(chans[i])
            else:
                out.append({})
        return out

    def _write_channels_list(self, channels: List[dict]) -> None:
        section = self._store.get_section("surveillance", {}) or {}
        section["channels"] = channels
        # ``set_section`` does an atomic write and returns the new
        # full snapshot — caller may want to display the on-disk path.
        self._store.set_section("surveillance", section)

    def list_configs(self) -> List[dict]:
        return [ch.config_dict() for ch in self._channels]

    def update_config(self, idx: int, **fields) -> dict:
        if not (0 <= idx < CHANNEL_COUNT):
            raise ValueError(f"channel index out of range: {idx}")
        ch = self._channels[idx]
        new_section = ch.apply_config(**fields)
        # Persist the whole channels list (one atomic write).
        self._write_channels_list([c.config_dict() for c in self._channels])
        return new_section

    def bulk_update(self, channels: List[dict]) -> List[dict]:
        """Apply a list of channel dicts (each may be partial)."""
        out: List[dict] = []
        for idx, payload in enumerate(channels):
            if not (0 <= idx < CHANNEL_COUNT):
                continue
            if not isinstance(payload, dict):
                out.append(self._channels[idx].config_dict())
                continue
            out.append(self._channels[idx].apply_config(**payload))
        self._write_channels_list([c.config_dict() for c in self._channels])
        return out

    def reset_to_defaults(self) -> List[dict]:
        for ch in self._channels:
            defaults = _default_channels()[ch.id - 1]
            ch.apply_config(
                name=defaults["name"],
                source=defaults["source"],
                prompt=defaults["prompt"],
                playback_enabled=defaults["playback_enabled"],
                detection_enabled=defaults["detection_enabled"],
                detect_w=defaults["detect_w"],
                detect_h=defaults["detect_h"],
            )
        self._write_channels_list([c.config_dict() for c in self._channels])
        return self.list_configs()

    # ------------------------------------------------------------------ #
    # Channel access                                                      #
    # ------------------------------------------------------------------ #
    def get_channel(self, idx: int) -> Channel:
        if not (0 <= idx < CHANNEL_COUNT):
            raise IndexError(f"channel index out of range: {idx}")
        return self._channels[idx]

    def list_snapshots(self) -> List[dict]:
        return [ch.snapshot() for ch in self._channels]

    def close_all(self) -> None:
        """Tear down every channel + the shared inference worker.
        Called on app shutdown."""
        for ch in self._channels:
            try:
                ch.stop()
            except Exception:
                pass
        try:
            self._worker.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Singleton                                                                  #
# --------------------------------------------------------------------------- #
_manager: Optional[SurveillanceManager] = None
_manager_lock = threading.Lock()


def get_manager() -> SurveillanceManager:
    """Lazy singleton accessor. First call hydrates from config.json."""
    global _manager
    with _manager_lock:
        if _manager is None:
            # Local import — config.py imports get_config which is the
            # canonical accessor.
            from app.services.config import get_config

            _manager = SurveillanceManager(get_config())
        return _manager