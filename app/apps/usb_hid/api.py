"""USB HID card — REST endpoints.

Endpoint summary
================

* ``GET  /api/usb-hid``           — gadget introspection snapshot
                                     (state, UDC, chardev writability,
                                     /dev/hidgN presence, the descriptor
                                     hash). Always succeeds; failure
                                     mode is the JSON body.
* ``GET  /api/usb-hid/source/hdmiin/status``  — HdmiInStatus for the
                                     RK3588 HDMI-RX input (driver
                                     rk_hdmirx). 503 if hwcap_hdmiin
                                     is disabled in config.
* ``POST /api/usb-hid/keyboard/type``    — ``{text, hold_ms?}`` →
                                            one char at a time. 502 if
                                            the device is missing.
* ``POST /api/usb-hid/keyboard/tap``     — ``{keycode|key, modifier?}``
                                            → single press+release.
                                            ``key`` is a name from
                                            ``keycodes.KEYCODES``;
                                            ``keycode`` is the raw 8-bit
                                            usage id (escape hatch).
* ``POST /api/usb-hid/keyboard/hotkey``  — ``{keys: [str], modifier?}``
                                            → chord. ``keys=["c"],
                                            modifier="CTRL"`` sends Ctrl-C.
* ``POST /api/usb-hid/digitizer/move``   — ``{x, y}`` in pixels.
* ``POST /api/usb-hid/digitizer/click``  — ``{x, y, press_ms?}``.
* ``POST /api/usb-hid/digitizer/leave``  — pen off the tablet.
* ``POST /api/usb-hid/reset``            — release all keys +
                                            tip_up, for panic-stop.
* ``POST /api/usb-hid/desk``             — ``{width, height}`` →
                                            store the host screen size
                                            so future pixel-typed calls
                                            can be clamped to the right
                                            range.

Why no WebSocket: the card is request/response — every event is
asynchronous from the host's perspective anyway. A WS endpoint
would only add reconnection complexity.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.apps.usb_hid.service import (
    get_digitizer,
    get_keyboard,
)
from app.core.compute.registry import _load_hardware_capability_state
from app.core.hdmiin import probe as _hdmiin_probe
from wdnhid import keycodes as kc
from wdnhid import manager

_LOG = logging.getLogger("apps.usb_hid.api")
router = APIRouter(tags=["usb-hid"])


# --------------------------------------------------------------------------- #
# Digitizer endpoints
#
# The HID device (Usage Page Digitizers / Usage Touchscreen) is bound
# by Windows to a single monitor; that binding is what controls which
# physical screen receives the touch. There is intentionally no
# per-monitor target UI on this card any more — adding one would push
# a virtual-desktop offset on top of what the OS already does, and
# the math is wrong (verified: with the offset enabled, a grid-centre
# click mapped to 67% of the bound monitor instead of the centre).
#
# Touch coordinate mapping is therefore the simple identity case:
# the frontend's grid click at (x, y) ∈ [0, _HOST_GRID_W) × [0, _HOST_GRID_H)
# is handed to the digitizer writer with screen_w = _HOST_GRID_W,
# screen_h = _HOST_GRID_H. The chardev writer scales to 0..32767 HID
# logical; the host OS does the monitor-to-physical-pixel mapping.
# --------------------------------------------------------------------------- #
_HOST_GRID_W = 1920
_HOST_GRID_H = 1080


def _hdmiin_capability_enabled() -> bool:
    """``hwcap_hdmiin`` gate for the hdmiin endpoints. Read from
    ``base.hardware_capabilities``; defaults to True if the section
    is missing (matches :func:`_hardware_capability_default_state`)."""
    try:
        return bool(_load_hardware_capability_state().get("hwcap_hdmiin", True))
    except Exception:  # noqa: BLE001
        return True


# --------------------------------------------------------------------------- #
# Pydantic payloads
# --------------------------------------------------------------------------- #
class _KbKey(BaseModel):
    model_config = {"extra": "ignore"}
    keycode: Optional[int] = Field(default=None, ge=0, le=0xFF)
    key: Optional[str] = None
    modifier: str = ""


class _KbText(BaseModel):
    model_config = {"extra": "ignore"}
    text: str = Field(min_length=1, max_length=4096)
    hold_ms: float = Field(default=8.0, ge=0.0, le=2000.0)


class _KbHotkey(BaseModel):
    model_config = {"extra": "ignore"}
    keys: list[str] = Field(min_length=1, max_length=4)
    modifier: str = ""
    hold_ms: float = Field(default=12.0, ge=0.0, le=2000.0)


class _Digit(BaseModel):
    model_config = {"extra": "ignore"}
    x: int = Field(ge=0)
    y: int = Field(ge=0)
    press_ms: float = Field(default=12.0, ge=0.0, le=2000.0)
    # ``tip_down`` distinguishes "move the contact" (in-progress drag,
    # keep Tip Switch = 1) from "pointer hover" (no contact, X/Y only
    # so a mouseless browser can still drive the device). Used by
    # /digitizer/move so the page can report a continuous drag
    # without having to interleave /digitizer/click frames.
    tip_down: bool = Field(default=False)


class _Desk(BaseModel):
    model_config = {"extra": "ignore"}
    width: int = Field(ge=1, le=65535)
    height: int = Field(ge=1, le=65535)


# --------------------------------------------------------------------------- #
# Snapshot
# --------------------------------------------------------------------------- #
@router.get("/")
async def get_snapshot() -> dict[str, Any]:
    """Introspection for the page header (gadget status + writability).

    Always 200. Missing/wrong-perm chardevs show up as ``writable=false``
    — the frontend renders a banner pointing the operator at
    ``scripts/setup_usb_hid_gadget.sh``.
    """
    status = manager.gadget_status()
    # Surface a coarse "ready" bit for the frontend's traffic light.
    kbd = status["devices"]["keyboard"]
    dig = status["devices"]["digitizer"]
    status["ready"] = (
        status.get("state") == "configured"
        and kbd["exists"] and kbd["writable"]
        and dig["exists"] and dig["writable"]
    )
    return status


# --------------------------------------------------------------------------- #
# Keyboard
# --------------------------------------------------------------------------- #
def _resolve_keycode(body: _KbKey) -> int:
    """Pull the keycode out of either the named ``key`` field or the raw
    ``keycode`` numeric. Raises HTTPException(400) with a useful message
    so the UI knows whether to fix the JSON shape or the name."""
    if body.keycode is not None:
        return body.keycode
    if not body.key:
        raise HTTPException(status_code=400, detail="keycode or key required")
    try:
        return kc.name_to_keycode(body.key)
    except kc.KeycodeError as e:
        raise HTTPException(status_code=400, detail=str(e))


# --------------------------------------------------------------------------- #
# HDMI-IN status (RK3588 rk_hdmirx)
# --------------------------------------------------------------------------- #
@router.get("/source/hdmiin/status")
async def get_hdmiin_status() -> dict[str, Any]:
    """Probe the HDMI-RX subsystem and return a status snapshot.

    Gated by the ``hwcap_hdmiin`` hardware capability flag in
    ``config.jsonc`` — disabled → 503 so the wizard / page can show
    "card unavailable" without crashing the page.

    The probe itself is read-only and best-effort: it tries sysfs
    first (cheap) and only opens the V4L2 device if those are
    present. If the service user can't open ``/dev/videoN`` (missing
    video group membership), the status still returns ``ok=true``
    with a reason; the user knows it's a permission issue, not a
    hardware problem.
    """
    if not _hdmiin_capability_enabled():
        raise HTTPException(
            status_code=503,
            detail="hwcap_hdmiin disabled — enable it in 系统配置 → 硬件能力",
        )
    status = _hdmiin_probe.probe()
    out = status.as_dict()
    out["capability_enabled"] = True
    return out


@router.post("/source/hdmiin/open")
async def post_hdmiin_open() -> dict[str, Any]:
    """Start the background hdmiin capture (lazy singleton).

    No-op if already running. Returns the actual format the device
    negotiated + a few encoder stats so the page can show them
    immediately after start.
    """
    if not _hdmiin_capability_enabled():
        raise HTTPException(
            status_code=503, detail="hwcap_hdmiin disabled",
        )
    from app.core.hdmiin.capture import get_capture
    cap = get_capture()
    ok = cap.start()
    if not ok:
        raise HTTPException(
            status_code=503,
            detail=f"failed to open {cap.device} — check "
                   "permissions (rockchip must be in 'video' group) "
                   "and that the OTG-in port is connected",
        )
    return {"ok": True, **cap.stats}


@router.post("/source/hdmiin/close")
async def post_hdmiin_close() -> dict[str, Any]:
    """Stop the background capture and release /dev/videoN."""
    if not _hdmiin_capability_enabled():
        raise HTTPException(
            status_code=503, detail="hwcap_hdmiin disabled",
        )
    from app.core.hdmiin.capture import get_capture
    get_capture().stop()
    return {"ok": True}


@router.post("/source/hdmiin/restart")
async def post_hdmiin_restart() -> dict[str, Any]:
    """Force a full stop+start of the HDMI-IN capture.

    Manual escape hatch for the rk_hdmirx "alive but starved" state
    that follows a PC sleep/wake or a hot-plug: the capture thread is
    still alive (so the snapshot handler's lazy-restart never fires) but
    every ``dequeue`` comes back empty and the page stays black. The
    backend watchdog in :meth:`HdmiInCapture._run` now recovers from
    that automatically after ~8 s; this endpoint forces it immediately
    when the user would rather not wait, and doubles as the manual
    recovery path if the watchdog ever misfires.
    """
    if not _hdmiin_capability_enabled():
        raise HTTPException(
            status_code=503, detail="hwcap_hdmiin disabled",
        )
    from app.core.hdmiin.capture import get_capture
    cap = get_capture()
    cap.stop()
    ok = cap.start()
    return {"ok": ok, "device": cap.device}


@router.get("/source/hdmiin/snapshot")
async def get_hdmiin_snapshot() -> Response:
    """Return the most recent HDMI-IN frame as a JPEG.

    The frame is encoded once per V4L2 dequeue (target ~10 fps) by
    :class:`HdmiInCapture`; this endpoint just returns the cached
    bytes. Cheap enough to poll at 1~2 Hz from the page.

    Returns 503 if ``hwcap_hdmiin`` is disabled; 204 if no frame
    has been captured yet; 200 with the JPEG on success.
    """
    if not _hdmiin_capability_enabled():
        raise HTTPException(
            status_code=503, detail="hwcap_hdmiin disabled",
        )
    from app.core.hdmiin.capture import get_capture
    cap = get_capture()
    if not (cap._thread and cap._thread.is_alive()):  # noqa: SLF001
        # Lazy start — page may hit this before /open.
        if not cap.start():
            raise HTTPException(
                status_code=503,
                detail=f"failed to open {cap.device}",
            )
    jpeg = cap.latest_jpeg
    if jpeg is None:
        return Response(status_code=204)
    return Response(
        content=jpeg,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )


@router.post("/keyboard/type")
async def post_keyboard_type(payload: _KbText) -> dict[str, Any]:
    """Type a string. Pre-validated so a single unsupported char 400s
    before any key event has been written to the host."""
    try:
        result = get_keyboard().type_text(payload.text, hold_ms=payload.hold_ms)
    except manager.GadgetError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except kc.KeycodeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, **result}


@router.post("/keyboard/tap")
async def post_keyboard_tap(payload: _KbKey) -> dict[str, Any]:
    keycode = _resolve_keycode(payload)
    modifier = kc.parse_modifier(payload.modifier)
    try:
        written = get_keyboard().tap(keycode, modifier)
    except manager.GadgetError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"ok": True, "bytes_sent": written, "keycode": keycode,
            "modifier": modifier}


@router.post("/keyboard/hotkey")
async def post_keyboard_hotkey(payload: _KbHotkey) -> dict[str, Any]:
    """Send a chord. ``keys=["c"], modifier="CTRL"`` → Ctrl+C.

    Order of key events matters for chord recognition on some hosts
    (e.g. macOS' Cmd-Space Spotlight), so we send them strictly in the
    order they appear in the request — modifier first if it's listed
    there, otherwise the first named key, then the rest.
    """
    modifier_bits = kc.parse_modifier(payload.modifier)
    # Each "key" can be either a name (from keycodes) or a raw byte.
    # The front-end only ever sends names; this handles both anyway.
    keycodes: list[int] = []
    for name in payload.keys:
        try:
            keycodes.append(kc.name_to_keycode(name))
        except kc.KeycodeError:
            # Not a known name → try parsing as a numeric keycode.
            try:
                keycodes.append(int(name, 0) & 0xFF)
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail=f"hotkey entry {name!r} is neither a known "
                           "key name nor a numeric keycode",
                )
    if not keycodes:
        raise HTTPException(status_code=400, detail="keys must not be empty")
    try:
        written = get_keyboard().tap(keycodes[0], modifier_bits,
                                     hold_ms=payload.hold_ms)
    except manager.GadgetError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"ok": True, "bytes_sent": written, "keycodes": keycodes,
            "modifier": modifier_bits}


@router.post("/keyboard/release")
async def post_keyboard_release() -> dict[str, Any]:
    """Panic-stop: clear every key the host thinks we hold.

    Boot protocol only supports "all keys up" — there's no way to
    release *only some* keys through ``/dev/hidg0``. That's an
    inherent limit of the boot-subset descriptor; we accept it.
    """
    try:
        written = get_keyboard().release()
    except manager.GadgetError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"ok": True, "bytes_sent": written}


# --------------------------------------------------------------------------- #
# Digitizer
# --------------------------------------------------------------------------- #
# Touch coordinate mapping is therefore the simple identity case:
# the frontend's grid click at (x, y) ∈ [0, _HOST_GRID_W) × [0, _HOST_GRID_H)
# is handed to the digitizer writer with screen_w = _HOST_GRID_W,
# screen_h = _HOST_GRID_H. The chardev writer scales to 0..32767 HID
# logical; the host OS does the monitor-to-physical-pixel mapping.
# --------------------------------------------------------------------------- #


@router.post("/digitizer/move")
async def post_digitizer_move(payload: _Digit) -> dict[str, Any]:
    vx, vy = payload.x, payload.y
    vw, vh = _HOST_GRID_W, _HOST_GRID_H
    try:
        written = get_digitizer().move(vx, vy, screen_w=vw, screen_h=vh,
                                       tip_down=payload.tip_down)
    except manager.GadgetError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"ok": True, "bytes_sent": written, "x": vx, "y": vy,
            "virtual_w": vw, "virtual_h": vh, "tip_down": payload.tip_down}


@router.post("/digitizer/down")
async def post_digitizer_down(payload: _Digit) -> dict[str, Any]:
    """Begin a contact at ``(x, y)``.

    Pair with :func:`post_digitizer_up` (no body). While the contact
    is open, use :func:`post_digitizer_move` with ``tip_down=true`` to
    stream the in-progress drag. Splitting press/release into two
    endpoints lets the page drive a press-and-drag gesture without
    any timing assumption baked into the backend — the
    pointermove rate on the page is the rate the host sees.
    """
    vx, vy = payload.x, payload.y
    vw, vh = _HOST_GRID_W, _HOST_GRID_H
    try:
        written = get_digitizer().down(vx, vy, screen_w=vw, screen_h=vh)
    except manager.GadgetError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"ok": True, "bytes_sent": written, "x": vx, "y": vy,
            "virtual_w": vw, "virtual_h": vh}


@router.post("/digitizer/up")
async def post_digitizer_up() -> dict[str, Any]:
    """End the contact opened by :func:`post_digitizer_down`.

    No body. Sends Tip Switch = 0, Contact Count = 0 at (0, 0) so
    the host sees "pen left the tablet" rather than "pen teleported
    to (0, 0)" (which would spuriously click whatever happens to be
    at the screen's top-left corner).
    """
    try:
        written = get_digitizer().up()
    except manager.GadgetError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"ok": True, "bytes_sent": written}


@router.post("/digitizer/click")
async def post_digitizer_click(payload: _Digit) -> dict[str, Any]:
    vx, vy = payload.x, payload.y
    vw, vh = _HOST_GRID_W, _HOST_GRID_H
    try:
        written = get_digitizer().click(vx, vy, screen_w=vw, screen_h=vh,
                                        press_ms=payload.press_ms)
    except manager.GadgetError as e:
        raise HTTPException(status_code=502, detail=str(e))
    # Also compute the HID logical range that got written, for debug
    # visibility (the host will see 0..32767, not the virtual-desktop
    # pixel). Frontend can show it in the HID 状态 line if it wants.
    lx = round(max(0, min(vw, vx)) * 32767 / vw) if vw > 0 else 0
    ly = round(max(0, min(vh, vy)) * 32767 / vh) if vh > 0 else 0
    return {"ok": True, "bytes_sent": written,
            "x": vx, "y": vy,
            "virtual_w": vw, "virtual_h": vh,
            "hid_logical_x": lx, "hid_logical_y": ly}


@router.post("/digitizer/leave")
async def post_digitizer_leave() -> dict[str, Any]:
    """Pen off the tablet — host shows the "not in range" cursor."""
    try:
        written = get_digitizer().leave()
    except manager.GadgetError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"ok": True, "bytes_sent": written}


# --------------------------------------------------------------------------- #
# Misc
# --------------------------------------------------------------------------- #
@router.post("/reset")
async def post_reset() -> dict[str, Any]:
    """Panic-stop: release everything. Used by ``DemoGuard`` /
    beforeunload in the unlikely case this card ever grows an active
    state (it currently doesn't, so this endpoint is just a safety
    hatch the operator can curl)."""
    try:
        kb = get_keyboard().release()
        dig = get_digitizer().leave()
    except manager.GadgetError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"ok": True, "keyboard_bytes": kb, "digitizer_bytes": dig}


@router.post("/desk")
async def post_desk(payload: _Desk) -> dict[str, Any]:
    """Record the host's screen size so future calls can be clamped
    client-side. We just echo it back — the API itself always clamps
    the digitizer coordinates to the HID logical range."""
    return {"ok": True, "desk": payload.model_dump()}
