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

import errno as _errno
import logging
import os
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

from . import load_v4l2_capture

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


def _capture_state() -> tuple[str, dict]:
    """State of the process-wide capture instance.

    Returns ``(state, info)``:

    * ``("running", info)`` — the capture thread is up and has published
      the format it negotiated.  ``info`` is non-empty.
    * ``("starting", info)`` — the device is **already open** (the
      capture thread is alive, or an open is in flight) but the
      negotiated format hasn't been published yet.
    * ``("idle", {})`` — nothing is running.

    Why the "starting" case needs its own branch:
    :meth:`HdmiInCapture.start` opens ``/dev/videoN`` *before* it fills
    in ``_info``.  A probe that lands in that window used to fall
    through to :func:`detect_format`, which tries to open the very same
    node a second time — the driver answers ``EBUSY`` and the user was
    told *"check /dev/videoN permissions"*, which points at the wrong
    thing entirely: the permissions are fine, **we** are the ones
    holding the device.
    """
    try:
        from .capture import get_capture  # local import: avoid cycle
    except Exception:  # noqa: BLE001
        return "idle", {}
    inst = get_capture()
    info = getattr(inst, "_info", None) or {}
    cap = getattr(inst, "_cap", None)
    thread = getattr(inst, "_thread", None)
    running = bool(thread and thread.is_alive())
    if running and info:
        return "running", dict(info)
    if running or cap is not None:
        # Device is open (or opening) but the format isn't published yet.
        return "starting", dict(info)
    return "idle", {}


def _open_failure_reason(info: dict) -> str:
    """Why the node exists but won't open — as a *correct* reason.

    The old single message ("check /dev/videoN permissions") was wrong
    for the two cases that actually happen in the field: the device is
    busy because our own capture is starting (permissions are fine), or
    because some other process holds it.  Only ``EACCES`` is really
    about permissions.
    """
    err = info.get("errno")
    if err == _errno.EBUSY:
        return ("device found but busy — 另一个进程或采集线程正持有它"
                "（采集刚启动/已开着一个实例）")
    if err == _errno.EACCES:
        return ("device found but permission denied — 给运行服务的用户加 "
                "video 组权限，或装 udev 规则")
    return "device found but could not be opened (check /dev/videoN permissions)"


def detect_format(device: str) -> dict:
    """Open ``device`` with :class:`wdnav.v4l2.V4L2Capture` and read
    QUERYCAP + G_FMT. Returns a dict suitable for ``HdmiInStatus``.

    On failure the dict still carries ``driver=""`` plus ``errno`` /
    ``error`` so the caller can tell "busy" from "no permission" —
    see :func:`_open_failure_reason`.

    Heavy: this calls into wdnav (native lib, root-only on some
    boards). Caller is expected to gate behind the ``hwcap_hdmiin``
    capability flag.
    """
    out = {"driver": "", "card": "", "capabilities": 0,
           "width": 0, "height": 0, "pixfmt": ""}
    # Lazy import — wdnav on a dev box may not have its native .so.
    cap_cls = load_v4l2_capture()
    if cap_cls is None:
        out["error"] = "wdnav.v4l2 不可用"
        return out
    try:
        # width/height/pixfmt are placeholders; the driver picks.
        cap = cap_cls(device, width=1920, height=1080,
                      pixel_format="nv12", num_buffers=2)
    except (OSError, RuntimeError) as e:
        LOG.info("hdmiin detect_format(%s) failed: %s", device, e)
        out["errno"] = getattr(e, "errno", None)
        out["error"] = f"{type(e).__name__}: {e}"
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
    # Best-effort: enrich with QUERYCAP.  Three cases, and they must
    # not be conflated:
    #
    #   running  — the capture is up; it already negotiated a format, so
    #              read it from there.  Opening the node a second time
    #              would race with the running stream (v4l2 chardevs are
    #              exclusive) and report a bogus error.
    #   starting — the device is *already open* but the format isn't
    #              published yet.  Do NOT probe: a second open returns
    #              EBUSY and the user gets told to check permissions,
    #              which is the wrong advice (see _capture_state).
    #   idle     — nothing is running; open it once to read the format
    #              (and to surface genuine permission errors).
    state, info = _capture_state()
    if state == "running":
        pass
    elif state == "starting":
        return HdmiInStatus(ok=False,
                            device=device,
                            hdmi_link=hdmi_link,
                            reason="正在打开采集设备…")
    else:
        info = detect_format(device)
        if not info["driver"]:
            return HdmiInStatus(ok=True,
                                device=device,
                                hdmi_link=hdmi_link,
                                reason=_open_failure_reason(info))
    return HdmiInStatus(ok=True,
                        device=device,
                        driver=info["driver"],
                        card=info["card"],
                        hdmi_link=hdmi_link,
                        width=info["width"],
                        height=info["height"],
                        pixfmt=info["pixfmt"],
                        capabilities=info["capabilities"])
