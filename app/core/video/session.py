"""Single-process capture session backed by ``wdnav.VideoCapture``.

Owns one ``VideoCapture``, a background decode thread, and a frame queue
that the WebSocket handler drains. Designed to keep ``import wdnav.video``
lazy — the wdnav/AGENTS.md gotcha about non-Rockchip dev boxes still applies
(a missing native lib sets ``VideoCapture`` to ``None`` in
``wdnav/__init__.py``).

Multi-consumer WS broadcast
----------------------------
The decode loop broadcasts each produced JPEG to every active subscriber
queue (``subscribe()`` returns a per-consumer ``queue.Queue(maxsize=1)``).
This is required because all three video WS handlers (``/video-detection``,
``/video-target-detection``, ``/video-target-localization``) share this
singleton — if they shared a single-slot queue they'd fight over frames
and each receive ~half the frames, causing visible playback stutter when
the user has more than one of these pages' WS connection alive
simultaneously (the typical trigger: card JS IIFE never re-runs because
``DemoGuard._loadedApps`` only adds — the original page's WS is
preserved across page navigation and competes with the new page's WS).

Legacy ``fetch_frame()`` is retained so any caller that hasn't migrated
to ``subscribe()`` continues to work — it drains the same default queue.
"""
from __future__ import annotations

import base64
import logging
import os
import queue
import sys
import threading
import time
from typing import List, Optional

import numpy as np

from app.core.video.frame_judge import (
    DEFAULT_EVERY_N,
    EXPOSED_METRICS,
    MODE_MANUAL,
    FrameJudge,
)

import cv2


try:
    from wdnav import VideoCapture as _WdnavVideoCapture
except Exception:  # pragma: no cover - platform-specific load failure
    _WdnavVideoCapture = None


LOG = logging.getLogger("video_session")


# 诊断埋点 (RTSP 卡顿定位) — 默认关闭,零行为变化。
# 设 `WDNWEBUI_VIDEO_SESSION_TIMING=1` 启动 run.py / uvicorn 后,
# 解码线程每 30 帧输出 1 行,同时写到 3 个通道:
#   1) LOG.info               — 标准 logging 通道
#   2) sys.stderr + 前缀       — 绕过 logging 配置,直接到 stderr
#   3) /tmp/wdnwebui_video_session_timing.log  — 独立文件
# 三通道任一可达即可;运维推荐直接 `tail -f` 文件。
_TIMING_ENABLED = os.getenv("WDNWEBUI_VIDEO_SESSION_TIMING", "0") == "1"
_TIMING_SAMPLE_WINDOW = 30
_TIMING_LOG_PATH = "/tmp/wdnwebui_video_session_timing.log"
_TIMING_FH = None


def _timing_open() -> None:
    global _TIMING_FH
    if _TIMING_FH is not None:
        return
    try:
        _TIMING_FH = open(_TIMING_LOG_PATH, "a", buffering=1)
    except OSError:
        _TIMING_FH = None


def _timing_emit(line: str) -> None:
    LOG.info(line)
    try:
        print(f"[VS-TIMING] {line}", file=sys.stderr, flush=True)
    except Exception:
        pass
    if _TIMING_FH is not None:
        try:
            _TIMING_FH.write(line + "\n")
        except Exception:
            pass


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

    FRAME_QUEUE_MAX = 8
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

        # Per-WS frame subscribers (broadcast pattern). Each WS handler
        # that wants pixel-depth playback calls ``subscribe()`` to get its
        # own ``queue.Queue(maxsize=1)``; the decode loop then broadcasts
        # every decoded JPEG to all registered subscribers. Multiple
        # consumers (vd / vtd / vtl all share this singleton) each
        # receive every frame independently — without this, they'd fight
        # over a single-slot queue and each get ~half the frames
        # (visible stutter when more than one of these WS is alive).
        self._subscribers: List[queue.Queue] = []
        self._subscribers_lock = threading.Lock()

        # 帧判据（app/core/video/frame_judge.py）：决定"这一帧要不要送给
        # frame sink"，并把逐帧分数留给 UI 画曲线 / 浮框。
        #
        # **判据是唯一的节流旋钮** —— 下游 detector 一律收到 every_n=1，
        # 否则两级节流相乘（间隔 30 撞上 30 = 900 帧才送一帧），卡片上
        # 写的"间隔 N 帧"就成了假话。默认值 30 与三张卡历史上 detector 的
        # every_n 默认值一致，所以接入判据**不改变**开箱行为。
        self._judge = FrameJudge(mode=EXPOSED_METRICS[0], every_n=DEFAULT_EVERY_N)
        self._judge_lock = threading.Lock()

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
                # 判据视图 —— 四张视频卡共用同名字段，前端一套组件直接读。
                "judge": self._judge.snapshot(),
            }

    # ------------------------------------------------------------------ #
    # 帧判据
    # ------------------------------------------------------------------ #
    def configure_judge(self, mode: Optional[str] = None,
                        every_n: Optional[int] = None,
                        threshold: Optional[float] = None) -> dict:
        """配置判据（算法 / 阈值 / 帧间隔），返回新的判据视图。

        ``mode`` 既可以是 ``"manual"``（手动跳帧），也可以是
        ``frame_judge.metrics()`` 暴露的任一度量名（当前只有 ``conc``）。
        未知名字回落 ``manual``；**已下架的 ``z_mad`` / ``auto`` 则迁移到
        ``conc`` 并重置阈值**（见 ``frame_judge._migrate``）。
        """
        with self._judge_lock:
            self._judge.reconfigure(mode=mode, every_n=every_n,
                                    threshold=threshold)
            return self._judge.snapshot()

    def judge_snapshot(self) -> dict:
        """判据只读视图（不夹 ``self._lock``，decode 热路径不碰它）。"""
        return self._judge.snapshot()

    def reset_judge(self) -> None:
        """新一段视频前清空参照帧 —— 循环重播 / 重开源时必须调用，
        否则上一段的尾帧会继续当参照帧，算出一个人为的巨大「变化」。"""
        self._judge.reset()

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
            # **buf0 / buf1 / buf2 三级 buffer 架构**（v0.19 用户拍板）：
            #
            # * **buf0** = mpp 出来的原始分辨率 Y 平面（1920×1080 灰度）—— 给
            #   **网页预览**用，让用户看清原始画面，不再是 160×90 缩略图拉伸。
            # * **buf1** = buf0 第一次 resize 到的**推理分辨率**（中间尺寸
            #   src//4，避开 librga 7x 缩放比坑）—— 给**模型推理**用，NPU
            #   看到的是中等分辨率（480×270 / 640×360），不是被压成 160×90
            #   的缩略图。
            # * **buf2** = buf1 第二次 resize 到 160×90 缩略图—— 给**帧判据
            #   gscore**用（gscore 期望 8x8 块网格在小图上跑，160×90 是标定
            #   过的尺寸）。
            #
            # 三级都**在 ndarray 阶段**走（mpp frame 一次性出原始 NV12
            # ndarray，wdnwebui 侧用 cv2.resize 缩两级）—— librga 7x 缩放
            # 比坑不踩，因为 mpp 那一次 read0 **不缩放**（src 尺寸直出）。
            long_side = self._judge.long_side
            src_w, src_h = cap.width, cap.height
            # ---- thumb 尺寸（buf2）: 160×90 缩略图给 gscore
            if src_w >= src_h and src_w > 0:
                self._thumb_w = long_side
                self._thumb_h = max(1, int(round(src_h * long_side / src_w)))
            elif src_h > 0:
                self._thumb_h = long_side
                self._thumb_w = max(1, int(round(src_w * long_side / src_h)))
            else:  # pragma: no cover - 不会到这里
                self._thumb_w, self._thumb_h = long_side, long_side
            # ---- mid 尺寸（buf1）: 推理分辨率
            # 设计：每级缩放比 ≤ 6x（librga 7x 边界留 1x 余量；实测 8x 失败
            # 6x 稳定）。buf0 → buf1 缩放比 4x，buf1 → buf2 缩放比 3x，
            # 都安全。
            mid_long = max(long_side * 4, max(src_w, src_h) // 4)
            mid_long = min(mid_long, max(src_w, src_h))
            mid_long = (mid_long + 15) & ~15
            if src_w >= src_h:
                self._mid_w = mid_long
                self._mid_h = max(1, int(round(src_h * mid_long / src_w)))
            else:
                self._mid_h = mid_long
                self._mid_w = max(1, int(round(src_w * mid_long / src_h)))
            self._mid_w = (self._mid_w + 15) & ~15
            self._mid_h = (self._mid_h + 15) & ~15
            # mid 必须**严格**比 thumb 大（buf1 → buf2 是真下采样，不能
            # 是放大）：mid_long 至少是 thumb_long * 2，否则直接缩到 thumb
            # 尺寸（avoid 两次 cv2.resize 走冤枉路）
            if self._mid_w < self._thumb_w * 2 or self._mid_h < self._thumb_h * 2:
                # src 太小（< 320 长边），直接一步到 thumb，不走 mid
                self._mid_w, self._mid_h = 0, 0
            self._fps = float(getattr(cap, "fps", 0.0) or 0.0)
            if self._fps <= 0 and not self._is_live:
                try:
                    import cv2
                    _probe = cv2.VideoCapture(source)
                    if _probe.isOpened():
                        self._fps = float(_probe.get(cv2.CAP_PROP_FPS) or 0.0) or 0.0
                    _probe.release()
                except Exception:  # noqa: BLE001
                    pass
            if self._fps <= 0:
                self._fps = 25.0
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
        """Pull the next event from the **legacy** single-consumer queue.

        .. deprecated::
            Use :meth:`subscribe` instead. Multiple WS handlers on the
            same singleton (``/video-detection``, ``/video-target-detection``,
            ``/video-target-localization`` all share this ``VideoSession``)
            would otherwise fight over this single-slot queue and each
            receive only every-other frame — visible playback stutter.

        New callers should ``subscribe()`` to get a private queue and
        read it independently.
        """
        try:
            item = self._frame_queue.get(timeout=timeout)
        except queue.Empty:
            return ("empty", None)
        if item is self._EOF:
            return ("eof", None)
        return ("frame", item)

    # ------------------------------------------------------------------ #
    # Multi-consumer WS broadcast (preferred API)
    # ------------------------------------------------------------------ #
    def subscribe(self) -> "queue.Queue":
        """Register a new per-consumer queue and return it.

        Each call returns a fresh, independent ``queue.Queue(maxsize=1)``.
        The decode loop broadcasts every decoded JPEG to all active
        subscribers in turn (drop-oldest per queue); each subscriber
        drains at its own pace without blocking the others. EOF events
        are also broadcast — a subscriber that is too slow to drain
        will simply skip EOF and pick up the next frame after the
        decoder has looped back to playing.

        Always pair with :meth:`unsubscribe` when the WS connection
        closes — otherwise the decode thread will keep waking on every
        frame push into the queue you no longer read.
        """
        q: "queue.Queue" = queue.Queue(maxsize=self.FRAME_QUEUE_MAX)
        with self._subscribers_lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: "queue.Queue") -> None:
        """Drop a previously registered subscriber queue. Idempotent."""
        with self._subscribers_lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    def _current_subscribers(self) -> list:
        # Cheap snapshot under lock so the broadcast call below doesn't
        # hold the lock during put_nowait (which is fine but keeps the
        # critical section tight and the lock-free read path hot).
        with self._subscribers_lock:
            return list(self._subscribers)

    def _broadcast_frame(self, jpg: bytes) -> None:
        """Push a JPEG to every subscriber queue (drop-oldest per queue).

        Slow / disconnected subscribers never block the decoder: their
        queue stays full at the latest frame and ``put_nowait`` on top
        of that drops the stale frame. EOF is broadcast separately by
        :meth:`_broadcast_eof` so each subscriber sees the end-of-file
        marker exactly once on its own queue.
        """
        for q in self._current_subscribers():
            try:
                q.put_nowait(jpg)
            except queue.Full:
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(jpg)
                except queue.Full:
                    # Queue vanished mid-flight (e.g. subscriber just
                    # unsubscribed); benign — skip.
                    pass

    def _broadcast_eof(self) -> None:
        """Push the EOF sentinel to every subscriber queue (best-effort).

        Drop-oldest on Full — a slow subscriber that hasn't drained its
        latest frame yet is happy to lose the EOF marker; the decoder
        will loop (or stay idle, depending on ``self._loop``) and the
        subscriber's NEXT ``get()`` will return either the new frame
        or a future EOF depending on whether the session is still in
        EOF state.
        """
        for q in self._current_subscribers():
            try:
                q.put_nowait(self._EOF)
            except queue.Full:
                pass

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
        for q in self._current_subscribers():
            try:
                while True:
                    q.get_nowait()
            except queue.Empty:
                pass

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
        if _TIMING_ENABLED:
            _t_frames = 0
            _t_drops = 0
            _t_acc_read0 = 0.0
            _t_acc_numpy = 0.0
            _t_acc_encode = 0.0
            _t_acc_sinks = 0.0
            _t_acc_put = 0.0
            _t_window_start = time.monotonic()
            _t_size_w = 0
            _t_size_h = 0
            _t_jpg_bytes = 0
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
                    long_side = self._judge.long_side
                    src_w, src_h = cap.width, cap.height
                    if src_w >= src_h and src_w > 0:
                        self._thumb_w = long_side
                        self._thumb_h = max(1, int(round(src_h * long_side / src_w)))
                    elif src_h > 0:
                        self._thumb_h = long_side
                        self._thumb_w = max(1, int(round(src_w * long_side / src_h)))
                    else:  # pragma: no cover
                        self._thumb_w, self._thumb_h = long_side, long_side
                    self._fps = float(getattr(cap, "fps", 0.0) or 0.0)
                    self._frame_pos = 0
                    self._state = self.STATE_PLAYING
                self._judge.reset()
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
                if _TIMING_ENABLED:
                    _t0_read0 = time.monotonic()
                # **mpp 内置 RGA 一次出原始 BGR**（buf0）—— read0 用
                # fmt='BGR' 让 wdnav 走 mpp DMA-BUF 路径（**物理连续内存**）
                # 调 RGA NV12→BGR。这是 wdnav 0.5.2 的"正确"彩色路径：
                # 同尺寸 1920×1080 NV12→BGR 不踩 librga 7x 缩放比上限
                # （实测 OK），且 librga 在 mpp DMA-BUF fd 上 map 成功。
                #
                # 之前一版曾尝试 read0(fmt='NV12') 拿 NV12 ndarray 再用
                # wdnav rga.cvtcolor 二次转换，但 `rga.wrap_virtual(numpy
                # heap ptr)` librga 在 Python heap 虚拟地址上 map 失败
                # （job buffer map failed）—— 静默失败，buf 是脏数据；
                # fallback 到灰度让用户看到"花屏"或单色。
                ret, frame0 = cap.read0(w=None, h=None, fmt="BGR")
                if _TIMING_ENABLED:
                    _t_acc_read0 += time.monotonic() - _t0_read0
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
                    # 循环重播：清掉判据参照帧，让新一段的第一帧重新当
                    # 基准。否则拿上一段尾帧做参照，算出的「变化」是人为的。
                    self._judge.reset()
                    play_start_wall = 0.0
                    pts_start = 0
                    next_frame_wall = 0.0
                    continue
                try:
                    self._frame_queue.put_nowait(self._EOF)
                except queue.Full:
                    pass
                self._broadcast_eof()
                return

            # Grab pts from the zero-copy frame BEFORE materialising
            # the BGR ndarray — cheaper, and we need the value for the
            # pacing calculation that runs after encode.
            try:
                pts = int(getattr(frame0, "pts", 0) or 0)
            except Exception:
                pts = 0

            try:
                if _TIMING_ENABLED:
                    _t0_numpy = time.monotonic()
                # **buf0 = 原始分辨率彩色 BGR ndarray**（v0.19 + 修正花屏）：
                # read0(fmt='BGR') 走 mpp 内置 RGA NV12→BGR（DMA-BUF
                # 路径），frame.numpy() 直接给 (h, w, 3) BGR ndarray。
                # **不要**再走 wdnav rga.cvtcolor —— numpy heap 虚拟地址上
                # librga map 失败，buf 是脏数据，让预览花屏。
                buf0_bgr = frame0.numpy()
                # **buf1 = 推理分辨率**（中间尺寸，src//4）BGR —— 给模型推理。
                if self._mid_w > 0:
                    buf1 = cv2.resize(
                        buf0_bgr,
                        (self._mid_w, self._mid_h),
                        interpolation=cv2.INTER_AREA,
                    )
                else:
                    buf1 = buf0_bgr
                # **buf2 = 160×90 灰度缩略图** —— 给 gscore 帧判据。
                if self._mid_w > 0 and (
                        buf1.shape[1] != self._thumb_w or
                        buf1.shape[0] != self._thumb_h):
                    buf1_thumb = cv2.resize(
                        buf1,
                        (self._thumb_w, self._thumb_h),
                        interpolation=cv2.INTER_AREA,
                    )
                else:
                    buf1_thumb = buf1
                buf2 = cv2.cvtColor(buf1_thumb, cv2.COLOR_BGR2GRAY)
                # sinks 拿 **buf1**（中间尺寸 BGR）做 NPU 推理 + base64 帧。
                frame = buf1
                if _TIMING_ENABLED:
                    _t_acc_numpy += time.monotonic() - _t0_numpy
                    _t_size_w = int(buf1.shape[1])
                    _t_size_h = int(buf1.shape[0])
            except Exception as e:
                # RGA conversion failure (e.g. unsupported size on this
                # board). Skip this frame rather than crashing the loop.
                continue

            if _TIMING_ENABLED:
                _t0_encode = time.monotonic()
            # **预览 JPEG 用 buf0_bgr**（原始分辨率彩色）—— 用户拍板 v0.19
            # 要看**彩色**原始画面。buf1/buf2 在 numpy 阶段已经算好，下
            # 面 sinks 拿 buf1 做 base64 帧（中间尺寸 NPU 推理），buf2 喂
            # gscore。
            ok, buf = cv2.imencode(
                ".jpg",
                buf0_bgr,
                [int(cv2.IMWRITE_JPEG_QUALITY), self.JPEG_QUALITY],
            )
            if _TIMING_ENABLED:
                _t_acc_encode += time.monotonic() - _t0_encode
            if not ok:
                continue
            jpg = bytes(buf)
            if _TIMING_ENABLED:
                _t_jpg_bytes = len(jpg)
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
            if _TIMING_ENABLED:
                _t0_sinks = time.monotonic()
            # 判帧先跑：它决定本帧要不要送给 sink，并顺手把分数记进
            # 环形缓冲（UI 曲线 / 浮框读它）。判为"没变化"的帧**照样
            # 广播给 WS 预览** —— 省的是推理算力，不是给人看的画面。
            # **buf2**（160×90 缩略图）喂 gscore：8x8 块网格在标定过的
            # 尺寸上跑，threshold=10 是 gscore 标定值。
            take = self._judge.observe(buf2, cur_frame_pos)
            if not take:
                sinks = []
            if sinks:
                # detector 拿 **buf1**（中间尺寸 BGR）的 base64 JPEG —— 与
                # ndarray 尺寸匹配。buf0_bgr 的 jpeg 给 WS 网页预览。
                try:
                    _ok1, _buf1 = cv2.imencode(
                        ".jpg",
                        buf1,
                        [int(cv2.IMWRITE_JPEG_QUALITY), self.JPEG_QUALITY],
                    )
                    frame_b64_for_detector = (
                        base64.b64encode(bytes(_buf1)).decode("ascii")
                        if _ok1 else ""
                    )
                except Exception as _enc_e:  # noqa: BLE001
                    LOG.debug(
                        "video_session: detector frame_b64 base64 encode "
                        "failed: %s", _enc_e,
                    )
                    frame_b64_for_detector = ""
            else:
                frame_b64_for_detector = ""
            # 本帧的判据分数**一并送给 sink**：下游要把"这一帧为什么被送检"
            # 写进检测事件，事件历史里没有这个数就分不清是阈值触发还是
            # 兜底触发（后者本来就允许阈值以下的帧送检）。只送给 take=True
            # 的帧，所以分数一定非空；手动跳帧模式下判据不算分，为 None。
            judge_score = self._judge.current_score if take else None
            for sink in sinks:
                try:
                    sink(frame, cur_frame_pos, frame_b64_for_detector,
                         judge_score)
                except TypeError as e:
                    # sink 还是老的 3 参签名 —— 判据分数是**附加**信息，
                    # 不该因为它把 sink 打挂。按老签名再调一次。
                    if "positional argument" not in str(e):
                        raise
                    try:
                        sink(frame, cur_frame_pos, frame_b64_for_detector)
                    except Exception as e2:  # noqa: BLE001
                        LOG.warning(
                            "video_session: frame sink raised "
                            "(frame_pos=%d): %s: %s",
                            cur_frame_pos, type(e2).__name__, e2,
                        )
                except Exception as e:  # noqa: BLE001
                    LOG.warning(
                        "video_session: frame sink raised (frame_pos=%d): "
                        "%s: %s — sink will be skipped this frame",
                        cur_frame_pos, type(e).__name__, e,
                    )
            if _TIMING_ENABLED:
                _t_acc_sinks += time.monotonic() - _t0_sinks
                _t0_put = time.monotonic()
# Drop oldest on overflow — favor low latency over completeness.
            self._broadcast_frame(jpg)
            try:
                self._frame_queue.put_nowait(jpg)
            except queue.Full:
                if _TIMING_ENABLED:
                    _t_drops += 1
                try:
                    self._frame_queue.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self._frame_queue.put_nowait(jpg)
                except queue.Full:
                    if _TIMING_ENABLED:
                        _t_drops += 1
            if _TIMING_ENABLED:
                _t_acc_put += time.monotonic() - _t0_put
                _t_frames += 1
                if _t_frames >= _TIMING_SAMPLE_WINDOW:
                    _t_window = time.monotonic() - _t_window_start
                    _t_fps = _t_frames / _t_window if _t_window > 0 else 0.0
                    _m_read0 = (_t_acc_read0 / _t_frames) * 1000
                    _m_numpy = (_t_acc_numpy / _t_frames) * 1000
                    _m_encode = (_t_acc_encode / _t_frames) * 1000
                    _m_sinks = (_t_acc_sinks / _t_frames) * 1000
                    _m_put = (_t_acc_put / _t_frames) * 1000
                    _line = (
                        f"video_session timing: {_t_frames} frames in "
                        f"{_t_window:.2f}s ({_t_fps:.1f} fps) "
                        f"read0={_m_read0:.1f}ms "
                        f"numpy={_m_numpy:.1f}ms "
                        f"imencode={_m_encode:.1f}ms "
                        f"sinks={_m_sinks:.1f}ms "
                        f"put={_m_put:.1f}ms "
                        f"jpg={_t_jpg_bytes // 1024}KB "
                        f"{_t_size_w}x{_t_size_h} "
                        f"q={self._frame_queue.qsize()} "
                        f"dropped={_t_drops} "
                        f"live={is_live} frame_pos={cur_frame_pos}"
                    )
                    _timing_open()
                    _timing_emit(_line)
                    _t_frames = 0
                    _t_drops = 0
                    _t_acc_read0 = 0.0
                    _t_acc_numpy = 0.0
                    _t_acc_encode = 0.0
                    _t_acc_sinks = 0.0
                    _t_acc_put = 0.0
                    _t_window_start = time.monotonic()

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
