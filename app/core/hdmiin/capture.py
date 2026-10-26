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
* V4L2 hands us a **YUV** frame (rk_hdmirx reports ``NV24``, which on
  this driver is 8-bit **4:4:4** — see :meth:`HdmiInCapture._frame_bgr`).
  That gets turned into BGR888 by the RGA, and the RGA then converts it
  straight to an ``NV12`` plane which is fed to
  ``MppEncoder(input_format=NV12)`` — so MPP encodes from YUV and PyAV
  never has to do its own BGR→NV12 step in libyuv (soft, ~10 ms per
  1920×1080) before the encode even starts.
* Throughput ceiling observed empirically (1080p, q=80, **worst-case
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

Why output is at the **native** resolution, q=80:
* The preview used to be hardcoded to 1280×720 and that is what made
  the operator's view look *blurry*. The page renders the frame into a
  tile **wider than 1280** (``object-fit: contain`` over a full-width
  stage), so the browser had to *upscale* the 720p JPEG by 1.4~1.5×.
  Measured against an X-screenshot ground truth, the perceived
  sharpness of the 720p preview is 82.6% / 77.4% / 74.4% / 56.7% at a
  display width of 1400 / 1600 / 1800 / 1920 px — the wider the
  window, the blurrier it looks. The very same frame at native 1080p
  holds 108.8% / 104.5% / 103.3% / 101.6%.
* Native is *not* the expensive option. The 1080p path replaces RGA's
  scale+convert with a plain colour convert (3.96 ms) and the MPP
  encode measured **5.7 ms** at 1080p vs **7.3 ms** at 720p — 11.5 ms
  vs 12.4 ms per frame end to end (medians over 30 frames). So the
  preview got sharper *and* slightly cheaper.
* Bytes: frame size scales with *screen content*. A flat desktop
  measures 41 KB/frame at 720p q80 and 80 KB at 1080p q80 (1.2 → 2.4
  MB/s at 30 fps). Do not trust the much smaller number you get from
  an idle / black HDMI source (~15 KB, because a black frame
  compresses to almost nothing); sizing the transport off a black
  source underestimates by ~18x. A photo-rich desktop measured
  ~270 KB/frame at 720p q80 — that case goes to ~600 KB at 1080p,
  i.e. ~18 MB/s at 30 fps, which is still fine on a LAN but is the
  number to watch if the preview is ever tunnelled.
* The *browser's* decode budget is the one number that used to argue
  for 720p (30 fps × 720p = 27.6 MPix/s of libjpeg vs 62 MPix/s at
  1080p, against a ~200 MPix/s single-core ceiling). In this
  deployment the viewer is a **separate host** (board 172.16.9.82,
  viewer 172.16.9.18), so that cost is not paid by this board at
  all. If you *do* run the browser on the board itself, drop
  ``target_fps`` to ~15 to keep the decode near the old 720p@30
  budget — the preview then still carries full 1080p detail.
* JPEG quality is *not* where the softness came from: on the same
  frame q=80 → 94.9% and q=95 → 95.0% of the reference sharpness.
  Buying quality there is close to a no-op; buying resolution is not.

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

Neither change alters the bytes produced at a 16-aligned height (verified:
md5 of a gradient frame is unchanged), since 720 is already 16-aligned —
and the preview now runs at 1080, where the MCU alignment *does* apply
(1080 → ver_stride 1088, handled by the cshim's padded copy).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

import numpy as np

from . import load_v4l2_capture
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

#: HDMI 链路送来的 YUV 用的色彩空间：**BT.709 限幅**。
#:
#: 依据：rk_hdmirx 的 ``VIDIOC_G_FMT`` 报 "YCbCr/HSV Encoding: xvYCC
#: 709" + "Quantization: Limited Range"；实测同一帧（HDMI-RX 回环本板
#: HDMI-OUT，X 抓屏当地面真相）用 BT.601 解与真相差 MAE 4.16，用
#: BT.709 差 2.39。RGA 默认按 BT.601 解，不声明就会整体偏色。
_SRC_COLOR_SPACE_MODE = "IM_YUV_BT709_LIMIT_RANGE"


class HdmiInCapture:
    """Owns the V4L2 device, the dequeue thread, and the latest frame.

    :meth:`start` is idempotent — re-calling after a stop is fine.
    :meth:`get_latest_jpeg` returns the most recently encoded JPEG
    bytes, or ``None`` if no frame has been captured yet.
    """

    def __init__(self, device: str = "", target_fps: float = 30.0,
                 out_w: int = 0, out_h: int = 0, q_factor: int = 80):
        """
        :param target_fps: how many JPEG frames to produce per second.
            The encode loop enforces this *rate* on top of the
            device's natural rate, so a 60 fps source still
            only burns the CPU for the target_fps we ship to the
            browser.
        :param out_w/out_h: JPEG output size. **0 (the default) means
            "native"** — the preview is encoded at exactly the size
            the driver negotiated (1920×1080 on this board), with no
            downscale at all.

            The preview used to be hardcoded to 1280×720, and that
            was the reason the operator's view looked *blurry*:
            the page renders the image into a tile wider than 1280
            (``object-fit: contain`` over a full-width stage), so the
            browser had to **upscale** the 720p JPEG by 1.4~1.5×.
            Measured against an X-screenshot ground truth, the
            perceived sharpness of the 720p preview collapses to
            82.6% / 77.4% / 74.4% / 56.7% at a display width of
            1400 / 1600 / 1800 / 1920 px — the wider the window, the
            blurrier it looks. The same frame at native 1080p holds
            108.8% / 104.5% / 103.3% / 101.6% at the same widths.

            Native is also *cheaper* than it looks: the 1080p path
            replaces RGA's scale+convert with a plain colour convert
            (3.96 ms) and the MPP encode measured **5.7 ms** at
            1080p vs **7.3 ms** at 720p, i.e. 11.5 ms vs 12.4 ms per
            frame end to end. Bytes go 41 KB → 80 KB per frame; at
            30 fps that is 1.2 MB/s → 2.4 MB/s, which is nothing on
            the LAN (the viewer is a separate host, so the browser's
            JPEG decode does not cost this board anything).

            Pass explicit values to force a smaller preview.
        :param q_factor: MPP MJPEG quality factor (1..99). 80 is
            "visually-lossless on a live preview" — about 270 KB per
            1280×720 frame of real desktop content (a *black* frame
            measures only ~15 KB, which badly understates it). Lower
            q_factor → smaller bytes, more visible blocking; higher →
            bigger bytes, sharper. Note this is *not* where the
            preview's softness came from: measured on the same
            frame, q=80 → 94.9% and q=95 → 95.0% of the reference
            sharpness, so buying quality here is close to a no-op.
        """
        self._device = device or (discover_device() or "/dev/video0")
        self._target_fps = target_fps
        # 0 = 跟随驱动协商出的原生分辨率（start() 里解析）。
        self._out_w = int(out_w)
        self._out_h = int(out_h)
        self._q_factor = int(q_factor)
        self._latest_jpeg: Optional[bytes] = None
        self._latest_size: tuple[int, int] = (0, 0)
        self._captured: int = 0
        self._encode_errors: int = 0
        self._started_at: float = 0.0
        self._lock = threading.Lock()
        # 帧的真实布局（wdnav 解析驱动报的四cc + 驱动 quirks 得到）。
        # start() 之后才有值。
        self._layout = None
        # YUV 源用的复用缓冲（避免每帧分配 6MB）。
        self._bgr_scratch: Optional[np.ndarray] = None
        self._yuv422_scratch: Optional[np.ndarray] = None
        # ---- AI-control side channel ---------------------------------- #
        # ``latest_jpeg`` is all the browser preview needs, but the
        # 智能电脑控制 agent has to hand a *pixel* frame to the VLM and
        # must see the exact same frame the operator is looking at.
        # Keeping every frame would cost a 6 MB copy × 30 fps forever,
        # so this is a strict one-shot request channel: the agent sets
        # ``_bgr_wanted``, the capture thread copies the *next* dequeued
        # frame (the same one it is about to JPEG-encode) into
        # ``_latest_bgr`` and bumps ``_bgr_seq``. No requester → the
        # flag stays False and the hot path is untouched.
        self._bgr_wanted: bool = False
        self._latest_bgr: Optional[np.ndarray] = None
        self._bgr_seq: int = 0
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
    def running(self) -> bool:
        """Is the capture thread actually dequeuing frames?

        The AI-control agent uses this to tell "the capture isn't up"
        apart from "the PC sent black frames", which are two very
        different user-facing errors.
        """
        return self._thread is not None and self._thread.is_alive()

    @property
    def latest_jpeg(self) -> Optional[bytes]:
        with self._lock:
            return self._latest_jpeg

    def grab_bgr(self, *, timeout: float = 5.0) -> Optional[np.ndarray]:
        """Block until a frame captured **after this call** is available.

        Returns a private ``(h, w, 3)`` BGR ndarray at the V4L2 native
        resolution (1920×1080 on this board), or ``None`` when the
        capture isn't running / nothing arrives within ``timeout``.

        Why native resolution and not the preview JPEG:

        * No JPEG decode, no second downscale — the agent hands the
          array straight to ``wdnai.utils.letterbox_resize_with_meta``,
          which produces the square canvas the vision RKNN wants.
          Downscaling here would throw away exactly the small-UI-element
          detail that GUI grounding lives or dies on, for nothing.
        * The frame is the *same* dequeue the preview JPEG comes from,
          so the coordinates the model outputs always refer to what the
          operator is looking at.

        The returned array is freshly allocated and never mutated after
        publication (``_latest_bgr`` is replaced, not written into), so
        the caller can hold it for the whole inference without copying.
        """
        if not (self._thread and self._thread.is_alive()):
            return None
        with self._lock:
            start_seq = self._bgr_seq
            self._bgr_wanted = True
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            with self._lock:
                if self._bgr_seq != start_seq and self._latest_bgr is not None:
                    return self._latest_bgr
            if time.monotonic() >= deadline:
                # Give the request back so a dead capture thread can't
                # leave the flag armed forever.
                with self._lock:
                    self._bgr_wanted = False
                return None
            # ``time.sleep`` and not ``Event.wait`` — see the wait_idle()
            # note in AGENTS.md: the capture thread's dequeue + memcpy
            # needs the GIL to make progress.
            time.sleep(0.01)

    def _maybe_snapshot_bgr(self, bgr: np.ndarray) -> None:
        """One-shot private copy of the BGR frame for :meth:`grab_bgr`.

        Called from :meth:`_run` with the **already normalised** BGR frame
        (see :meth:`_frame_bgr`) — so the AI agent and the operator look at
        one identical picture, and a YUV source is converted once per frame
        instead of once per consumer.

        No-op unless :meth:`grab_bgr` has armed the request — so the 30 fps
        preview loop pays nothing when no agent is running.
        """
        with self._lock:
            if not self._bgr_wanted:
                return
            self._bgr_wanted = False
        try:
            # copy, not a view: for a YUV source ``bgr`` is a scratch buffer
            # that gets overwritten next frame; for a BGR24 source it is a
            # view onto the mmap'd V4L2 buffer, which the driver overwrites
            # the moment we requeue it.
            #
            # ``.copy()`` and NOT ``np.ascontiguousarray(plane)``: the
            # frame is already C-contiguous, and ascontiguousarray documents
            # "no copy is performed if the array is already C-contiguous" —
            # it would hand back the *same* aliasing view. The agent would
            # then be looking at memory the driver is free to overwrite
            # mid-inference.
            frame = bgr.copy()
        except Exception as e:  # noqa: BLE001
            _LOG.warning("hdmiin bgr snapshot failed: %s", e)
            return
        with self._lock:
            self._latest_bgr = frame
            self._bgr_seq += 1

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
        cap_cls = load_v4l2_capture()
        if cap_cls is None:
            return False
        try:
            # 请求"最好的"格式，实际拿驱动肯给的。
            #
            # rk_hdmirx 只接受 type=CAPTURE_MPLANE + 'NV24'（本机实测：
            # bgr3 / nv12 / rgb3 / yuyv / uyvy 一律
            # ``OSError: [Errno 22] Invalid argument``），所以**不要**在这里
            # 猜驱动要什么 —— wdnav 的 V4L2Capture 带 **G_FMT 兜底**：
            # S_FMT 失败就 G_FMT 问驱动当前格式再重试一次，请求什么都能
            # 打开。最终格式看 ``cap.pixel_format``（**协商结果**，不是
            # 请求值）。
            cap = cap_cls(self._device, width=1920, height=1080,
                          pixel_format="bgr3", num_buffers=4)
        except (OSError, RuntimeError) as e:
            _LOG.warning("v4l2 open %s failed: %s", self._device, e)
            return False
        cap.start()
        # 驱动报的四cc可能"撒谎"（rk_hdmirx 报 NV24，按 V4L2 规范是 12bit
        # YUV420SP，实际写的是 8bit **YUV444**）—— 缓冲区怎么解读以
        # wdnav 的 frame_layout 为准，别按 fourcc 猜。
        self._layout = cap.frame_layout
        info = {"driver": cap.driver, "card": cap.card,
                "capabilities": getattr(cap, "_caps", 0),
                "width": cap.width, "height": cap.height,
                "pixfmt": cap.pixel_format}
        # out_w/out_h == 0 → 预览用**原生分辨率**（不降采样）。
        # 必须在建 MPP 编码器之前定下来 —— 编码器的宽高在
        # _ensure_encoder() 里锁定。
        if self._out_w <= 0 or self._out_h <= 0:
            self._out_w = int(cap.width)
            self._out_h = int(cap.height)
            _LOG.info("hdmiin preview at native resolution %dx%d",
                      self._out_w, self._out_h)
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
                # dedicated EPU — the RGA colour convert is ~4 ms and
                # the MPP encode ~5 ms at the native 1080p preview
                # size (measured medians over 30 frames).
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
            # 源格式可能是 BGR24，也可能是 YUV（rk_hdmirx 报 NV24、实际是
            # 8bit YUV444）—— 统一在 _frame_bgr 里归一化成全分辨率
            # BGR888，预览和 VLM 截图**共用同一份**，别每帧转两遍。
            try:
                bgr = self._frame_bgr(buf.plane_arrays[0])
            except Exception as e:  # noqa: BLE001
                _LOG.warning("hdmiin frame convert failed: %s", e)
                cap.requeue(buf)
                next_t = time.time()
                continue
            # Same frame the preview JPEG is about to encode, so the AI
            # and the operator are looking at one identical picture.
            # No-op unless grab_bgr() armed the one-shot request.
            self._maybe_snapshot_bgr(bgr)
            try:
                jpeg = self._encode_jpeg(bgr)
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
        cap_cls = load_v4l2_capture()
        if cap_cls is None:
            return False
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
            # Same request as start() — the G_FMT fallback inside
            # V4L2Capture takes whatever the driver is willing to give.
            cap = cap_cls(self._device, width=1920, height=1080,
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
        # 重开后的格式可能变（信号模式变了），布局要跟着更新。
        self._layout = cap.frame_layout
        _LOG.info("hdmiin capture reopened on %s %sx%s %s",
                  self._device, cap.width, cap.height, cap.pixel_format)
        return True

    def _frame_bgr(self, plane: np.ndarray) -> np.ndarray:
        """把一帧 V4L2 平面转成**全分辨率** BGR888。

        * ``bgr24`` 源：零拷贝视图（reshape），调用方要长期持有必须 copy。
        * ``yuv444sp`` 源（本机 rk_hdmirx 报的 NV24 就是它）：色度按 2:1
          抽成 4:2:2 半平面后，RGA 一次转成 BGR888。
        * ``yuv422sp`` / ``yuv420sp`` 源：直接 RGA 转 BGR888。

        为什么要按 payload 分支，而不是"默认按 BGR 读"：

        1. 驱动报的 fourcc 未必是真布局。rk_hdmirx 报 'NV24'，但按 V4L2
           规范那是 12bit YUV420SP；**实测缓冲区里躺的是 8bit YUV444**
           （Y 平面 W*H 字节 + 全宽交错 CbCr 平面 2*W*H 字节，合计正好
           sizeimage = 3*W*H）。按 fourcc 猜会把色度读错 → 整幅画面偏色。
           布局一律问 :func:`wdnav.v4l2.frame_layout`。
        2. 色彩空间要显式声明。HDMI 链路（TX 端 RGB→YUV）用的是
           **BT.709 限幅**（驱动的 G_FMT 也报 "YCbCr/HSV Encoding:
           xvYCC 709" / "Quantization: Limited Range"），而 RGA 默认按
           BT.601 解 —— 不声明就会整体偏色。实测同一帧：默认 BT.601 与
           地面真相差 MAE 4.16，声明 BT.709 后 2.39。

        三段转换全在 RGA 硬件里跑，CPU 只碰 numpy 的指针（外加 4:4:4
        → 4:2:2 的纯字节搬运，见 :meth:`_pack_yuv422`）。
        """
        h, w = self._cap.height, self._cap.width
        layout = self._layout
        payload = getattr(layout, "payload", "") or ""

        if payload == "bgr24":
            if plane.ndim == 1:
                return plane.reshape(h, w, 3)
            return plane

        if payload not in ("yuv444sp", "yuv422sp", "yuv420sp"):
            raise RuntimeError(
                f"unsupported capture payload {payload!r} "
                f"(fourcc={getattr(layout, 'fourcc', '?')})"
            )

        import wdnav.rga

        if self._bgr_scratch is None or self._bgr_scratch.shape != (h, w, 3):
            self._bgr_scratch = np.empty((h, w, 3), dtype=np.uint8)
            self._yuv422_scratch = np.empty(h * w * 2, dtype=np.uint8)

        if payload == "yuv444sp":
            src = self._pack_yuv422(plane, h, w)
            src_fmt = wdnav.rga.RgaFormat.YCbCr_422_SP
        else:
            src = plane
            src_fmt = getattr(
                wdnav.rga.RgaFormat,
                "YCbCr_422_SP" if payload == "yuv422sp" else "YCbCr_420_SP",
            )

        # YUV → BGR888：一次 RGA 转换。
        # （本机实测：422SP→BGR 直转与 numpy 参考差 MAE 0.07；
        #   420SP→BGR 直转 0.55。没必要再绕 NV12 中转。）
        wdnav.rga.cvtcolor(
            wdnav.rga.wrap_virtual(
                src.ctypes.data, w, h, src_fmt,
                color_space_mode=getattr(
                    wdnav.rga, _SRC_COLOR_SPACE_MODE),
            ),
            wdnav.rga.wrap_virtual(
                self._bgr_scratch.ctypes.data, w, h,
                wdnav.rga.RgaFormat.BGR_888,
            ),
        )
        return self._bgr_scratch

    def _pack_yuv422(self, plane: np.ndarray, h: int, w: int) -> np.ndarray:
        """把 8bit **4:4:4** 缓冲打包成 4:2:2 半平面（Y + 交错 CbCr）。

        为什么需要这一步：RGA（librga 1.10.5）**没有 4:4:4 输入路径**
        —— ``imcvtcolor`` / ``imresize`` 的源格式只支持
        420/422/YUYV/400，实测传 ``YCbCr_444_SP`` 会报
        ``src unsupported YUV444 semi-planner 8bit format``。而
        rk_hdmirx 只肯给 NV24（=4:4:4），别的 fourcc 一律 EINVAL，
        所以 4:4:4 → 4:2:2 这一步只能在这里做。

        这一步是**纯字节搬运**：Y 平面原样搬过去，色度平面横向按 2:1
        抽点（等价于标准 4:2:2 抽点），**不做任何颜色计算、不做缩放**。
        真正的 YUV→RGB 转换和最终降采样仍然全部在 RGA 硬件里完成。

        写法上有个大坑：别用 ``out.reshape(h, w//2, 2)[:] =
        src.reshape(h, w, 2)[:, ::2, :]`` 这种三维花式赋值 —— numpy
        对它是逐元素的 gather，本机实测 **9.97 ms/帧**（30 fps 就是
        30% 单核）。把色度看成 **uint16**（一个 CbCr 像素正好 2 字节）
        再取步长 2，numpy 走的是按行 memcpy，实测 **0.57 ms/帧**，
        快 17 倍，字节完全一致。
        """
        flat = plane.reshape(-1)
        out = self._yuv422_scratch
        out[: h * w] = flat[: h * w]
        # 色度：全宽交错 CbCr (h, w) 个 uint16 像素 → 半宽 (h, w//2)。
        # h*w 必为偶数（4:4:4 的 CbCr 是 2 字节/像素），所以 uint16
        # 视图的字节偏移是对齐的。
        src_u16 = flat[h * w:].view(np.uint16).reshape(h, w)
        out[h * w:].view(np.uint16).reshape(h, w // 2)[:] = src_u16[:, 0::2]
        return out

    def _encode_jpeg(self, bgr: np.ndarray) -> bytes:
        """Full-resolution BGR888 frame → JPEG bytes via :class:`wdnav.mpp.MppEncoder`.

        ``bgr`` is what :meth:`_frame_bgr` produced — either a zero-copy
        view onto the mmap'd V4L2 buffer (BGR24 source) or an RGA-converted
        buffer (YUV source). We:
        2. Allocate an NV12 destination buffer
           ``(self._out_h * 3 // 2, self._out_w)``;
        3. Run one RGA pass from BGR-888 → YCbCr-420-SP: a plain colour
           conversion when the preview size equals the source size (the
           default, native 1920×1080), or a scale+convert when the
           caller asked for a smaller preview;
        4. Hand the resulting ``(out_h * 3 // 2, out_w)`` NV12 plane to
           MPP for JPEG encode on the encoder EPU.

        Routing through RGA avoids an MPP-firmware edge case where
        the BGR888-input path hangs on the first ``encode_put_frame``
        when the input plane comes from a live V4L2 mmap'd buffer
        (synthetic numpy inputs work fine — verified at 320+ fps in
        isolation). The RGA pass is ~4 ms per frame and the MPP
        encode itself stays on the dedicated EPU at ~5 ms/frame, so
        30 fps (one encode per 33 ms) leaves ~2.5× margin.

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

        # RGA: BGR_888 → NV12 (色彩转换；同尺寸时不再重采样）。
        #
        # The destination is declared **BT.601 full range** on purpose.
        # JPEG has no range signalling that browsers honour — every
        # decoder (libjpeg included) reconstructs RGB from the stored
        # YCbCr with the *full-range* BT.601 matrix, i.e.
        #     R = Y + 1.402*(Cr-128)
        # RGA's default is BT.601 **limited** range (Y ∈ [16,235]), so
        # feeding that to the encoder makes the browser stretch the
        # luma by 255/219 and the chroma by 255/224 → the picture comes
        # out brighter and over-saturated (measured on this board:
        # R +11.4 / G +4.5 / B +2.0 against an X-screenshot ground
        # truth, exactly the 1.164/1.596/2.072 mismatch).
        #
        # Declaring the destination as BT.601 full range makes RGA emit
        # Y ∈ [0,255] with the *same* matrix the browser will use, so
        # the round trip is exact: the same frame measures MAE 8.215
        # with the default and **1.174** with the explicit mode.
        #
        # BT.709 would be wrong here even though the HDMI link itself
        # is 709: the browser only ever applies 601 to a JPEG, so
        # converting RGB → 709 YCbCr would introduce a hue shift.
        nv12 = np.zeros(self._out_h * 3 // 2 * self._out_w, dtype=np.uint8) \
                  .reshape(self._out_h * 3 // 2, self._out_w)
        src = wdnav.rga.wrap_virtual(
            bgr.ctypes.data, w, h, wdnav.rga.RgaFormat.BGR_888,
        )
        dst = wdnav.rga.wrap_virtual(
            nv12.ctypes.data, self._out_w, self._out_h,
            wdnav.rga.RgaFormat.YCbCr_420_SP,
            color_space_mode=wdnav.rga.IM_YUV_BT601_FULL_RANGE,
        )
        # 同尺寸 = 只换色彩空间，不必让 RGA 再重采样一遍。
        # （原生 1080p 预览走的就是这条：cvtcolor 3.96 ms，
        #   比 resize 少一次无谓的插值。）
        if (self._out_w, self._out_h) == (w, h):
            wdnav.rga.cvtcolor(src, dst)
        else:
            wdnav.rga.resize(src, dst)

        enc = self._ensure_encoder()
        packet = enc.encode(nv12)
        return packet.data


# --------------------------------------------------------------------------- #
# Module-level singleton
# --------------------------------------------------------------------------- #
_INSTANCE: Optional[HdmiInCapture] = None
_INSTANCE_LOCK = threading.Lock()


def _preview_size_from_env() -> tuple[int, int]:
    """Read the operator's preview-size override from the environment.

    ``WDNWEBUI_HDMIIN_PREVIEW_SIZE`` accepts ``"<w>x<h>"`` (e.g.
    ``1280x720``) or ``native`` / empty for the default — which is the
    **source's own resolution** (1920×1080 on this board).

    Exists because the preview is a bandwidth trade the operator may
    want to make themselves: native is the sharpest (the page tile is
    wider than 720p, so a smaller preview gets *upscaled* by the
    browser and looks soft), but it is also ~2× the bytes per frame.
    Anything unparseable falls back to native and logs a warning —
    a typo in a unit file must not take the preview down.
    """
    raw = os.environ.get("WDNWEBUI_HDMIIN_PREVIEW_SIZE", "").strip().lower()
    if not raw or raw in ("native", "auto", "0"):
        return 0, 0
    try:
        parts = raw.replace("×", "x").split("x")
        if len(parts) != 2:
            raise ValueError(raw)
        w, h = int(parts[0]), int(parts[1])
        if w <= 0 or h <= 0:
            raise ValueError(raw)
    except Exception:  # noqa: BLE001 — env var, any parse failure
        _LOG.warning(
            "WDNWEBUI_HDMIIN_PREVIEW_SIZE=%r is not '<w>x<h>' — falling "
            "back to the native capture resolution", raw,
        )
        return 0, 0
    return w, h


def get_capture() -> HdmiInCapture:
    """Return the process-wide HdmiInCapture (lazily created)."""
    global _INSTANCE
    if _INSTANCE is None:
        with _INSTANCE_LOCK:
            if _INSTANCE is None:
                out_w, out_h = _preview_size_from_env()
                _INSTANCE = HdmiInCapture(out_w=out_w, out_h=out_h)
    return _INSTANCE
