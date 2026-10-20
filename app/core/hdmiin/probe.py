"""HDMI-IN capability / status probe.

All read-only. Doesn't open the device — it only inspects sysfs to
answer:

  * Is there a ``/dev/videoN`` node backed by ``rk_hdmirx``?
  * Is the upstream HDMI link connected (signal present)?
  * What does the driver currently advertise via ``VIDIOC_G_FMT``?
    (queried via :func:`wdnav.v4l2.V4L2Capture` only when the device
    can actually be opened — see :func:`detect_format`.)

If a board has multiple ``/dev/videoN`` nodes, the first rk_hdmirx
one wins. On the stock RK3588 board that's ``/dev/video0``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

LOG = logging.getLogger("core.hdmiin")


V4L2_ROOT = Path("/sys/class/video4linux")
HDMIRX_SYSFS = Path("/sys/class/hdmirx/hdmirx")


@dataclass
class HdmiInStatus:
    """Read-only snapshot of the HDMI-IN subsystem. ``ok`` is the only
    field the consumer should treat as boolean; everything else is
    presentational."""

    ok: bool
    reason: str = ""
    device: str = ""            # e.g. "/dev/video0"
    driver: str = ""            # e.g. "rk_hdmirx"
    card: str = ""              # e.g. "rk_hdmirx"
    hdmi_link: str = ""         # "connected" / "disconnected" / ""
    width: int = 0
    height: int = 0
    pixfmt: str = ""            # "BGR3" / "NV12" / ...
    capabilities: int = 0       # raw V4L2 caps bitmask from QUERYCAP

    def as_dict(self) -> dict:
        return asdict(self)


def discover_device() -> Optional[str]:
    """Return ``/dev/videoN`` whose driver is rk_hdmirx, or ``None``.

    Walks ``/sys/class/video4linux/video*`` and matches by ``name``
    ("stream_hdmirx" on RK3588 6.12) or by the parent device's driver
    path. Falls back to ``/dev/video0`` if the name heuristic doesn't
    match — that's the stock board's location and silent failure is
    worse than a wrong guess the caller can re-check via QUERYCAP.
    """
    if not V4L2_ROOT.is_dir():
        return None
    for entry in sorted(V4L2_ROOT.iterdir()):
        try:
            name = (entry / "name").read_text().strip()
        except OSError:
            continue
        if "hdmirx" in name.lower():
            return "/dev/" + entry.name
        # Fallback: parent driver's modalias ends in ...hdmirx...
        try:
            driver_link = (entry / "device" / "driver").resolve()
        except (OSError, ValueError):
            continue
        if "hdmirx" in str(driver_link).lower():
            return "/dev/" + entry.name
    return None


def read_hdmi_link() -> str:
    """Return ``/sys/class/hdmirx/hdmirx/status`` content if present.

    On RK3588 the value is either ``"connected"`` or ``"disconnected"``.
    An empty string means the sysfs entry isn't there (no HDMI-RX
    controller at all — board variant, kernel config, etc.)."""
    if not HDMIRX_SYSFS.is_dir():
        return ""
    try:
        return (HDMIRX_SYSFS / "status").read_text().strip()
    except OSError as e:
        LOG.debug("read hdmirx status failed: %s", e)
        return ""


def _read_capture_info() -> dict:
    """Return driver/card/capabilities/width/height/pixfmt from the
    currently-running :class:`HdmiInCapture`, or an empty dict if no
    capture is up.

    Used by :func:`probe` to avoid calling :class:`V4L2Capture` again
    just to learn what the running capture already knows. A second
    open on ``/dev/video0`` competes with the running stream (v4l2
    chardevs are typically exclusive) and would always fail — producing
    a misleading "device found but could not be opened" status.
    """
    try:
        from .capture import get_capture  # local import: avoid cycle
    except Exception:  # noqa: BLE001
        return {}
    inst = get_capture()
    info = getattr(inst, "_info", None)
    if not info:
        return {}
    return dict(info)


def detect_format(device: str) -> dict:
    """Open ``device`` with :class:`wdnav.v4l2.V4L2Capture` and read
    QUERYCAP + G_FMT. Returns a dict suitable for ``HdmiInStatus``.

    Heavy: this calls into wdnav (native lib, root-only on some
    boards). Caller is expected to gate behind the ``hwcap_hdmiin``
    capability flag.
    """
    out = {"driver": "", "card": "", "capabilities": 0,
           "width": 0, "height": 0, "pixfmt": ""}
    try:
        # Lazy import — wdnav on a dev box may not have its native .so.
        from wdnav.v4l2 import V4L2Capture
        # width/height/pixfmt are placeholders; the driver picks.
        cap = V4L2Capture(device, width=1920, height=1080,
                          pixel_format="nv12", num_buffers=2)
    except (ImportError, OSError, RuntimeError) as e:
        LOG.info("hdmiin detect_format(%s) failed: %s", device, e)
        return out
    try:
        out["driver"] = cap.driver
        out["card"] = cap.card
        out["capabilities"] = cap._caps   # noqa: SLF001 — internal use only
        out["width"] = cap.width
        out["height"] = cap.height
        out["pixfmt"] = cap.pixel_format
    finally:
        cap.close()
    return out


def probe() -> HdmiInStatus:
    """Cheap probe — sysfs only. Returns HdmiInStatus(ok=False) if
    the driver / sysfs entries aren't there."""
    device = discover_device()
    if not device:
        return HdmiInStatus(ok=False,
                            reason="no rk_hdmirx /dev/videoN node found")
    hdmi_link = read_hdmi_link()
    if hdmi_link == "disconnected":
        return HdmiInStatus(ok=False,
                            device=device,
                            hdmi_link="disconnected",
                            reason="HDMI link disconnected — "
                                   "plug a PC into the OTG-in port")
    # Best-effort: enrich with QUERYCAP. If a capture is running it
    # already holds /dev/video0 — calling V4L2Capture here would just
    # race with it and return "could not be opened" (v4l2 chardevs are
    # typically exclusive). Read the negotiated format from the
    # capture instance instead. If nothing is running we still want to
    # surface perms errors so the user knows to fix the udev rule —
    # only fall back to V4L2Capture then.
    info = _read_capture_info()
    if not info:
        info = detect_format(device)
    if not info["driver"]:
        return HdmiInStatus(ok=True,
                            device=device,
                            hdmi_link=hdmi_link,
                            reason="device found but could not be opened "
                                   "(check /dev/videoN permissions)")
    return HdmiInStatus(ok=True,
                        device=device,
                        driver=info["driver"],
                        card=info["card"],
                        hdmi_link=hdmi_link,
                        width=info["width"],
                        height=info["height"],
                        pixfmt=info["pixfmt"],
                        capabilities=info["capabilities"])
