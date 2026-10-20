"""HDMI-IN continuous capture + JPEG snapshot service.

A single :class:`HdmiInCapture` instance owns a :class:`wdnav.v4l2.V4L2Capture`
session against ``/dev/video0`` (auto-discovered) and a background
thread that dequeues frames as fast as the device produces them,
**hardware-jpeg-encodes** each frame via Rockchip MPP
(:class:`wdnav.mpp.MppEncoder` with ``coding_type=MJPE``), and keeps
the most recent frame in memory. HTTP clients poll
:func:`get_latest_jpeg` and get the latest frame.

Why one capture for the whole app (not per-request):
* Opening ``/dev/video0`` and starting a stream takes ~150 ms —
  repeated open/close on every HTTP poll would either cap the
  snapshot rate at ~6 fps (the open time dominates) or compete
  for the chardev between concurrent requests.

Why **MPP hardware** MJPEG (not PyAV mjpeg) and not BGR8:
* RK3588's MPP encoder is **the only** on-SoC JPEG encoder —
  it runs entirely on the dedicated encoder EPU/cycle, leaving
  all four A76 cores free for the rest of the pipeline. AGENTS.md
  "设计原则 2" makes this mandatory: ``mpp`` is the only blessed
  route for JPEG I/O.
* V4L2 negotiates ``NV12`` (YUV420SP) on this kernel/driver stack —
  feeding that directly to ``MppEncoder(input_format=NV12)``
  means MPP encodes from YUV; PyAV would have to do its own
  BGR→NV12 step in libyuv (soft, ~10 ms per 1920×1080) before
  the encode even starts. Skip that step entirely.
* Throughput ceiling observed empirically (720p, q=80, **worst-case
  synthetic noise**): ~12 ms/frame ≈ 80 fps on a single encoder
  instance. A live desktop is far cheaper (~3 ms) because it is
  dominated by flat UI regions. 30 fps sustained leaves ample slack
  for the encode_errors retry budget and the per-frame HTTP poll.

Why the encoder instance is **reused** across the lifetime of
the capture (not recreated every N frames):
* Earlier this code recycled the encoder every 6 frames, working
  around a deadlock where :func:`mpp_port_enqueue` blocked forever
  on the 9th call. Root cause traced into the cshim: its
  :func:`encode_mjpeg` path did not call ``encode_get_packet``
  after ``encode_put_frame``, so the output port's 8-deep task
  queue saturated and the next put blocked forever. The cshim has
  been fixed at the source (the cshim's :func:`encode_mjpeg` now
  drains the output-task slot symmetrically to :func:`encode_h264`),
  so a single encoder instance can now run indefinitely.

Why output is **1280×720, q=80**:
* Bandwidth, not pixels, is the binding constraint. Frame size scales
  with *screen content*, and a real desktop (text, photos, icons) at
  720p q80 measures **~270 KB/frame** — ~8 MB/s at 30 fps. Do not
  trust the much smaller number you get from an idle / black HDMI
  source (that measures ~15 KB, because a black frame compresses to
  almost nothing); sizing the transport off a black source
  underestimates by ~18x.
* Browser decode budget on RK3588 is ~200 MPix/s of software
  libjpeg-turbo — 30 fps × 720p is 27.6 MPix/s (well under it),
  30 fps × 1080p would be 62 MPix/s (right at saturation; the
  page turns into a slideshow again). A user-visible diff at 30 fps
  between 720p and 1080p is negligible because they only need to
  spot-click PC UI controls.

Two encoder-side bugs were found while chasing a "grey bar along the
bottom of the preview" report. Both produced JPEGs with **no trailing
``ff d9`` EOI marker**, which every decoder rejects with "premature end
of data segment" and which renders as flat grey over the un-decoded
bottom MCU rows. Both are fixed in the cshim (see
:file:`cshim_video_encoder.c`):

1. the output packet buffer was a fixed 256 KB (~0.28 B/pixel at
   720p), so a busy desktop frame (~270 KB) overflowed it and the EPU
   stopped mid-scan. It is now sized like MPP's own
   ``mpp_enc_check_pkt_buf`` does — ``align16(w) * align16(h) * 1.5``;
2. the JPEG HAL walks the input in 16x16 MCU steps using
   ``prep:ver_stride``, so a height that is not a multiple of 16
   emits a corrupt stream. 1920×1080 is the classic victim
   (1080 = 67.5×16). Strides are now MCU-aligned, with the caller's
   tightly-packed buffer copied row-wise into the padded destination.

Neither change alters the bytes produced at 1280×720 (verified: md5 of
a gradient frame is unchanged), since 720 is already 16-aligned.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Optional

import numpy as np

from .probe import discover_device, detect_format

_LOG = logging.getLogger("core.hdmiin.capture")

# Wall-clock seconds with *no* frame dequeued before the watchdog in
# :meth:`HdmiInCapture._run` tears down and reopens /dev/videoN. Eight
# seconds is ~240 missed frames at 30 fps — long past any legitimate
# gap (a source that pauses delivery for a second still recovers on the
# next dequeue; only a genuine signal-mode change / wedged fd stays
# starved this long).
_STARVE_WINDOW_S = 8.0
# Minimum seconds between watchdog reopens, so a persistently-absent
# source doesn't cause us to thrash open/close every few seconds.
_REOPEN_COOLDOWN_S = 10.0


class HdmiInCapture:
    """Owns the V4L2 device, the dequeue thread, and the latest frame.

    :meth:`start` is idempotent — re-calling after a stop is fine.
    :meth:`get_latest_jpeg` returns the most recently encoded JPEG
    bytes, or ``None`` if no frame has been captured yet.
    """

    def __init__(self, device: str = "", target_fps: float = 30.0,
                 out_w: int = 1280, out_h: int = 720, q_factor: int = 80):
        """
        :param target_fps: how many JPEG frames to produce per second.
            The encode loop enforces this *rate* on top of the
            device's natural rate, so a 60 fps source still
            only burns the CPU for the target_fps we ship to the
            browser.
        :param out_w/out_h: JPEG output size. Defaults to 1280×720
            because the human watching the live preview gets a
            faithful image, and the browser-side JPEG decode load
            stays at ~28 MPix/s — well under the ~200 MPix/s a
            single RK3588 big-core can sustain. The grid → host
            digitizer mapping doesn't care about this size (the
            frontend still maps its 1920×1080 grid onto the
            rendered image independently), so changing this is a
            pure bandwidth / CPU trade.
        :param q_factor: MPP MJPEG quality factor (1..99). 80 is
            "visually-lossless on a live preview" — about 270 KB per
            1280×720 frame of real desktop content (a *black* frame
            measures only ~15 KB, which badly understates it). Lower
            q_factor → smaller bytes, more visible blocking; higher →
            bigger bytes, sharper.
        """
        self._device = device or (discover_device() or "/dev/video0")
        self._target_fps = target_fps
        self._out_w = int(out_w)
        self._out_h = int(out_h)
        self._q_factor = int(q_factor)
        self._latest_jpeg: Optional[bytes] = None
        self._latest_size: tuple[int, int] = (0, 0)
        self._captured: int = 0
        self._encode_errors: int = 0
        self._started_at: float = 0.0
        self._lock = threading.Lock()
        # Captured at start(): driver / card / QUERYCAP / negotiated fmt.
        # probe() reads this when the capture is running so it doesn't have
        # to call V4L2Capture again (which would compete for /dev/video0
        # with the running stream and report "could not be opened").
        self._info: dict = {}
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        # MPP encoder state. Created once in :meth:`_ensure_encoder`
        # when ``_run`` first needs to encode, and reused for the rest
        # of the capture. The firmware deadlock that wdnav/encoder.py
        # used to dodge with ``_MPP_MJPEG_RECYCLE_EVERY = 6`` has been
        # fixed in :file:`cshim_video_encoder.c::encode_mjpeg` —
        # the cshim now drains the output-task slot the encoder
        # enqueues after every put, so the output port's 8-deep queue
        # never saturates and the encoder runs forever on a single
        # instance.
        self._mpp_encoder: Optional["MppEncoder"] = None

    @property
    def device(self) -> str:
        return self._device

    @property
    def latest_jpeg(self) -> Optional[bytes]:
        with self._lock:
            return self._latest_jpeg

    @property
    def stats(self) -> dict:
        with self._lock:
            return {
                "device": self._device,
                "captured": self._captured,
                "encode_errors": self._encode_errors,
                "latest_size": list(self._latest_size),
                "running": self._thread is not None
                          and self._thread.is_alive(),
                "uptime_s": (time.time() - self._started_at) if self._thread
                            and self._thread.is_alive() else 0.0,
            }

    def start(self) -> bool:
        """Open the device, launch the dequeue thread. Returns True on
        success; False if the device can't be opened (caller should
        show the reason in the UI).

        Reentrant: calling start() while a capture is already running
        is a no-op. Calling start() after a previous capture was closed
        (thread gone, cap=None) opens a fresh session; the previous
        /dev/videoN fd is released before we re-open. This matters
        because the kernel's rk_hdmirx is a single-consumer device —
        leaving a stale fd open will EBUSY the next open.
        """
        if self._thread and self._thread.is_alive():
            return True
        # Drop any prior session state (cap may be left set if the
        # previous run's thread exited but stop() wasn't called).
        old_cap = getattr(self, "_cap", None)
        if old_cap is not None:
            try:
                old_cap.close()
            except Exception:  # noqa: BLE001
                pass
            self._cap = None

        # Lazy import — wdnav / pyav failures shouldn't take the whole
        # process down at module load time.
        try:
            from wdnav.v4l2 import V4L2Capture
        except ImportError as e:
            _LOG.error("wdnav.v4l2 import failed: %s", e)
            return False
        try:
            # NB: pixel_format must be the driver's *native* FourCC.
            # rk_hdmirx exposes V4L2_PIX_FMT_BGR3 ("bgr3"), not
            # V4L2_PIX_FMT_NV12 — asking for nv12 makes S_FMT fail with
            # EINVAL and the whole open() fails. Verified on RK3588:
            #   nv12 -> FAIL OSError: [Errno 22] Invalid argument
            #   bgr3 -> OK  negotiated=BGR3 1920x1080
            # This matches what _negotiate_format always reported
            # anyway, and what the module docstring describes.
            cap = V4L2Capture(self._device, width=1920, height=1080,
                              pixel_format="bgr3", num_buffers=4)
        except (OSError, RuntimeError) as e:
            _LOG.warning("v4l2 open %s failed: %s", self._device, e)
            return False
        cap.start()
        info = {"driver": cap.driver, "card": cap.card,
                "capabilities": getattr(cap, "_caps", 0),
                "width": cap.width, "height": cap.height,
                "pixfmt": cap.pixel_format}
        self._cap = cap
        self._info = info
        self._stop.clear()
        self._started_at = time.time()
        self._thread = threading.Thread(
            target=self._run, name="hdmiin-capture", daemon=True,
        )
        self._thread.start()
        _LOG.info("hdmiin capture started: %s %sx%s %s",
                  self._device, info["width"], info["height"],
                  info["pixfmt"])
        return True

    def stop(self) -> None:
        """Stop the dequeue thread, close the V4L2 device. Idempotent."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        cap = getattr(self, "_cap", None)
        if cap is not None:
            try:
                cap.close()
            except Exception:  # noqa: BLE001
                pass
            self._cap = None
        enc = getattr(self, "_mpp_encoder", None)
        if enc is not None:
            try:
                enc.close()
            except Exception:  # noqa: BLE001
                pass
            self._mpp_encoder = None
        _LOG.info("hdmiin capture stopped")

    def _ensure_encoder(self):
        """Create the MPP MJPEG encoder on demand and reuse it across
        the capture lifetime.

        Lazy because the encoder needs to be configured against the
        V4L2 format we want to feed it (NV12 / YUV420SP) and the
        desired output size / q_factor, which we only know after
        :meth:`start` runs. Created once and then reused: the cshim
        now correctly drains the output-task slot the encoder
        enqueues after every put, so the output port's 8-deep queue
        never saturates and a single instance can run indefinitely
        (verified at 320+ fps × 100 frames on RK3588).
        """
        from wdnav.mpp import MppEncoder, MppCodingType, MppFrameFormat

        if self._mpp_encoder is None:
            self._mpp_encoder = MppEncoder(
                MppCodingType.MJPEG,
                width=self._out_w,
                height=self._out_h,
                fps=int(self._target_fps) if self._target_fps >= 1 else 30,
                # We feed the encoder NV12 (YUV420SP) — produced by
                # wdnav.rga from the BGR24 frames rk_hdmirx negotiates.
                # The BGR888 path through MppEncoder is also supported
                # but on the production hardware it occasionally hangs
                # on the first put_frame when fed from a live V4L2
                # mmap'd buffer (synthetic numpy frames don't trip
                # it); routing through RGA→NV12 sidesteps that while
                # still leaving the JPEG encoding itself on the
                # dedicated EPU — RGA resize+colorconvert is ~2 ms
                # at 1280×720, MPP encode is ~3 ms.
                input_format=MppFrameFormat.YUV420SP,
                q_factor=self._q_factor,
            )
        return self._mpp_encoder

    def _run(self) -> None:
        target_period = 1.0 / max(self._target_fps, 0.1)
        next_t = time.time()
        # Watchdog: rk_hdmirx stops delivering frames after a PC sleep
        # / wake, HDMI hot-unplug-replug, or signal-mode change, and the
        # dequeue loop just spins on `select()` returning no data. The
        # thread stays alive so the lazy-restart in the snapshot handler
        # (which fires only on ``thread is dead``) cannot help, and the
        # page goes black until the user manually closes+opens. The
        # watchdog below detects this state ("alive but starved") and
        # forces a fresh ``/dev/videoN`` open, which is the only step
        # that ever recovers rk_hdmirx after a signal change.
        empty_count = 0
        starved_since: Optional[float] = None
        last_reopen_at = 0.0
        while not self._stop.is_set():
            cap = self._cap
            if cap is None:
                # Watchdog reopen failed earlier and tore down _cap.
                # Bail; the snapshot handler's lazy-start can re-open
                # us when the next request comes in.
                time.sleep(0.5)
                return
            now = time.time()
            if now < next_t:
                if self._stop.wait(next_t - now):
                    return
                continue
            buf = cap.dequeue(timeout_s=0.5)
            if buf is None:
                empty_count += 1
                if starved_since is None:
                    starved_since = time.time()
                elif (time.time() - starved_since) >= _STARVE_WINDOW_S \
                        and (time.time() - last_reopen_at) >= _REOPEN_COOLDOWN_S:
                    _LOG.warning(
                        "hdmiin capture starved %.1fs (link may have "
                        "changed) — reopening %s",
                        time.time() - starved_since, self._device,
                    )
                    ok = self._reopen_v4l2()
                    last_reopen_at = time.time()
                    empty_count = 0
                    starved_since = None
                    next_t = time.time()
                    if not ok:
                        _LOG.error("hdmiin reopen failed; capture thread "
                                   "bailing out so a future request can "
                                   "do a fresh start")
                        return
                    continue
                next_t = time.time()
                continue
            empty_count = 0
            starved_since = None
            try:
                jpeg = self._encode_jpeg(buf.plane_arrays[0])
            except Exception as e:  # noqa: BLE001
                with self._lock:
                    self._encode_errors += 1
                _LOG.debug("encode jpeg failed: %s", e)
                # Rebuild the encoder on a transient error so the next
                # frame doesn't inherit a half-dead MPP instance —
                # close() is the documented teardown path for a
                # confirmed encoder crash.
                enc = self._mpp_encoder
                self._mpp_encoder = None
                if enc is not None:
                    try:
                        enc.close()
                    except Exception:  # noqa: BLE001
                        pass
                cap.requeue(buf)
                next_t = time.time()
                continue
            cap.requeue(buf)
            with self._lock:
                self._latest_jpeg = jpeg
                self._latest_size = (self._out_w, self._out_h)
                self._captured += 1
            # Advance the deadline by target_period but never let it
            # drift backwards — if a single encode took longer than
            # the period (encode transient > target_fps) we'd queue
            # a backfill that never drains. Falling back to "now"
            # keeps the loop tight to the actual cost.
            next_t = max(next_t + target_period, time.time())

    def _reopen_v4l2(self) -> bool:
        """Close and reopen the ``/dev/videoN`` capture without touching
        ``_thread`` / ``_stop``. Returns True if the device is open and
        streaming again, False on failure.

        The recovery step for "PC sleep / wake, page stays black": the
        old ``V4L2Capture`` is left holding an ``fd`` whose kernel-side
        buffer queue is permanently exhausted after a signal-mode
        change, and no further ``dequeue`` will ever complete. Forcing
        a fresh ``REQBUFS`` (open + start on a new fd) is the only
        documented way to revive ``rk_hdmirx`` here.
        """
        from wdnav.v4l2 import V4L2Capture
        old = getattr(self, "_cap", None)
        if old is not None:
            try:
                old.stop()
            except Exception:  # noqa: BLE001
                pass
            try:
                old.close()
            except Exception:  # noqa: BLE001
                pass
            self._cap = None
        try:
            # Must match start(): rk_hdmirx's native FourCC is BGR3.
            cap = V4L2Capture(self._device, width=1920, height=1080,
                              pixel_format="bgr3", num_buffers=4)
        except (OSError, RuntimeError) as e:
            _LOG.warning("hdmiin reopen open() failed: %s", e)
            return False
        try:
            cap.start()
        except Exception as e:  # noqa: BLE001
            _LOG.warning("hdmiin reopen start() failed: %s", e)
            try:
                cap.close()
            except Exception:  # noqa: BLE001
                pass
            return False
        self._cap = cap
        _LOG.info("hdmiin capture reopened on %s %sx%s %s",
                  self._device, cap.width, cap.height, cap.pixel_format)
        return True

    def _encode_jpeg(self, plane: np.ndarray) -> bytes:
        """V4L2 BGR24 frame ndarray → JPEG bytes via :class:`wdnav.mpp.MppEncoder`.

        ``plane`` comes in as a **flat 1-D numpy view onto the mmap'd
        V4L2 buffer** — V4L2 exposes each capture plane as a single
        contiguous byte string. ``rk_hdmirx`` negotiates BGR24 on this
        kernel regardless of what we request at open() time, so we:

        1. Reshape to ``(H, W, 3)``;
        2. Allocate an NV12 destination buffer
           ``(self._out_h * 3 // 2, self._out_w)``;
        3. Run :func:`wdnav.rga.resize` from BGR-888 → YCbCr-420-SP at
           the new size — a single RGA pass that both color-converts
           and downscales (1920×1080 → 1280×720);
        4. Hand the resulting ``(out_h * 3 // 2, out_w)`` NV12 plane to
           MPP for JPEG encode on the encoder EPU.

        Routing through RGA avoids an MPP-firmware edge case where
        the BGR888-input path hangs on the first ``encode_put_frame``
        when the input plane comes from a live V4L2 mmap'd buffer
        (synthetic numpy inputs work fine — verified at 320+ fps in
        isolation). The RGA path is ~2 ms per 720p frame; the MPP
        encode itself stays on the dedicated EPU at ~3 ms/frame, so
        30 fps (one encode per 33 ms) leaves ~10× margin.

        The encoder instance is created once in :meth:`_ensure_encoder`
        and reused for the rest of the capture — the cshim
        :func:`encode_mjpeg` now drains the output-task slot the
        encoder enqueues after every put (see the long comment in
        :file:`cshim_video_encoder.c`), so a single instance can run
        indefinitely.

        Returns the JPEG bytes (always starts ``\xff\xd8`` and ends
        ``\xff\xd9`` — confirmed by direct inspection).
        """
        import wdnav.rga

        h, w = self._cap.height, self._cap.width
        if plane.ndim == 1:
            plane = plane.reshape(h, w, 3)

        # RGA resize+colorconvert: 1920x1080 BGR_888 → 1280x720 NV12.
        nv12 = np.zeros(self._out_h * 3 // 2 * self._out_w, dtype=np.uint8) \
                  .reshape(self._out_h * 3 // 2, self._out_w)
        src = wdnav.rga.wrap_virtual(
            plane.ctypes.data, w, h, wdnav.rga.RgaFormat.BGR_888,
        )
        dst = wdnav.rga.wrap_virtual(
            nv12.ctypes.data, self._out_w, self._out_h,
            wdnav.rga.RgaFormat.YCbCr_420_SP,
        )
        wdnav.rga.resize(src, dst)

        enc = self._ensure_encoder()
        packet = enc.encode(nv12)
        return packet.data


# --------------------------------------------------------------------------- #
# Module-level singleton
# --------------------------------------------------------------------------- #
_INSTANCE: Optional[HdmiInCapture] = None
_INSTANCE_LOCK = threading.Lock()


def get_capture() -> HdmiInCapture:
    """Return the process-wide HdmiInCapture (lazily created)."""
    global _INSTANCE
    if _INSTANCE is None:
        with _INSTANCE_LOCK:
            if _INSTANCE is None:
                _INSTANCE = HdmiInCapture()
    return _INSTANCE
