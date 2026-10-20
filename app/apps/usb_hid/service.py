"""USB HID service — request/response writers to /dev/hidg{0,1}.

Two writers, one per HID function:

* :class:`KeyboardWriter` — boot keyboard reports. Holds an "all keys
  released" baseline; ``tap`` / ``type_text`` / ``hotkey`` write the
  press, the release. State (currently-held keys) lives only for the
  duration of one HTTP call.
* :class:`DigitizerWriter` — single-finger absolute pointer. Maintains
  the current position so the host sees a smooth drag; ``click`` does
  tip-down → move → tip-up atomically.

The writers never share state with each other. Each opens its FD
through :func:`wdnhid.manager.ensure_chardev_open` and
reuses the cached FD across calls (reopening resets host protocol
state).
"""

from __future__ import annotations

import logging
import os
import struct
import time
from typing import Iterable

from wdnhid import manager
from wdnhid import keycodes as kc

LOG = logging.getLogger("apps.usb_hid.service")


# --------------------------------------------------------------------------- #
# Boot keyboard
# --------------------------------------------------------------------------- #
class KeyboardWriter:
    """Write 8-byte boot keyboard reports to ``/dev/hidg0``.

    All public methods are synchronous; the underlying ``write`` returns
    immediately because the kernel HID gadget driver buffers a single
    report in flight. We do, however, sleep ``inter_key_delay`` between
    keystrokes so a host OS running at low USB poll rate (e.g. BIOS
    125 Hz) doesn't see two reports coalesce into one frame.
    """

    REPORT_LEN = manager.KEYBOARD_REPORT_LEN  # 8

    def __init__(self, inter_key_delay: float = 0.005):
        self._inter_key_delay = inter_key_delay

    def _open(self) -> int:
        return manager.get_chardev_fd("keyboard")

    @staticmethod
    def _encode(modifier: int, keycodes: Iterable[int]) -> bytes:
        """Build an 8-byte boot report. Extra keycodes beyond 6 are
        silently dropped (boot spec limits 6KRO)."""
        keys = list(keycodes)[:6]
        # Pad with zeros to 6 entries
        keys = keys + [0] * (6 - len(keys))
        return struct.pack("<BB6B", modifier & 0xFF, 0, *keys)

    def tap(self, keycode: int, modifier: int = 0,
            hold_ms: float = 12.0) -> int:
        """Press + release a single key. Returns the number of bytes
        successfully written (should always be 8 — short writes on
        ``/dev/hidg0`` would mean the host stalled us)."""
        fd = self._open()
        press = self._encode(modifier, [keycode])
        release = self._encode(0, [])
        written = manager.safe_write(fd, press)
        time.sleep(max(0.0, hold_ms / 1000.0))
        manager.safe_write(fd, release)
        return written

    def press(self, keycode: int, modifier: int = 0) -> int:
        return manager.safe_write(self._open(), self._encode(modifier, [keycode]))

    def release(self) -> int:
        return manager.safe_write(self._open(), self._encode(0, []))

    def hotkey(self, modifiers: int, keycode: int) -> int:
        """Press a single key with one or more modifier bits held. The
        hotkey is realized as press+release; the host sees a real
        chord, not sequential presses."""
        return self.tap(keycode, modifiers, hold_ms=12.0)

    def type_text(self, text: str, *, hold_ms: float = 8.0) -> dict:
        """Type a string of ASCII characters, one key at a time.

        Non-ASCII characters raise :class:`ValueError` before any
        characters are sent — so a half-typed string never lands on the
        host. ``\n`` and ``\t`` are translated to the matching keycode;
        other control characters raise.
        """
        # Pre-validate so we don't type half the string before failing.
        plan: list[tuple[int, int]] = []
        for ch in text:
            if ch == "\n":
                plan.append((0, kc.KEYCODES["ENTER"]))
            elif ch == "\t":
                plan.append((0, kc.KEYCODES["TAB"]))
            elif ch == " ":
                plan.append((0, kc.KEYCODES["SPACE"]))
            else:
                modifier, kc_val = kc.char_to_keycode(ch)
                plan.append((modifier, kc_val))

        fd = self._open()
        bytes_sent = 0
        for modifier, kc_val in plan:
            bytes_sent += manager.safe_write(fd, self._encode(modifier, [kc_val]))
            time.sleep(max(0.0, hold_ms / 1000.0))
            manager.safe_write(fd, self._encode(0, []))   # release between keys
            if self._inter_key_delay > 0:
                time.sleep(self._inter_key_delay)
        return {"chars_typed": len(plan), "bytes_sent": bytes_sent}


# --------------------------------------------------------------------------- #
# Digitizer / Tablet
# --------------------------------------------------------------------------- #
class DigitizerWriter:
    """Write 5-byte digitizer reports to ``/dev/hidg1``.

    Logical coordinate space is the HID spec default 0..32767 on both
    axes (see ``DIGITIZER_TABLET_REPORT_DESC`` in wdnhid). The host
    OS will scale those to its screen pixels based on the
    digitizer's physical size — for a "no specific size" generic
    digitizer (this is what the descriptor declares) Windows and
    macOS use the *bound monitor's* pixel dimensions directly, so
    logical 32767 maps to the rightmost / bottommost pixel of the
    monitor the digitizer is currently routed to.

    The API surface takes **pixels in the PC's virtual desktop**
    (the ``screen_w``/``screen_h`` argument to :meth:`click` /
    :meth:`move`). Internally we normalize to [0, 1] and then scale
    to 0..32767, so the call site stays in pixels and doesn't have
    to remember the HID logical range.
    """

    REPORT_LEN = manager.DIGITIZER_REPORT_LEN  # 6 (post Precision-Touch refit)
    LOG_MAX = 0x7FFF  # 32767

    def __init__(self):
        self._last_x = 0
        self._last_y = 0

    def _open(self) -> int:
        return manager.get_chardev_fd("digitizer")

    def _encode(self, tip: bool, contact_id: int, x: int, y: int) -> bytes:
        """Build a 6-byte digitizer report:
            byte 0: Contact Count (1 = finger down, 0 = no contact)
            byte 1: bit 0 = Tip Switch, bits 1-4 = Contact Identifier (0..15),
                    bits 5-7 = reserved (0)
            bytes 2-3: X logical (LE, 0..32767)
            bytes 4-5: Y logical (LE, 0..32767)
        The Win 8+ Precision Touchpad driver only forwards the report
        as a Win32 touch event when Contact Count and Tip Switch are
        consistent; sending Tip=1 with Count=0 (or vice versa) is
        silently dropped. Always send Count=1 together with Tip=1
        while the contact is active, and Count=0 to release.
        """
        x = max(0, min(self.LOG_MAX, int(x)))
        y = max(0, min(self.LOG_MAX, int(y)))
        cid = max(0, min(0x0F, int(contact_id)))
        flags = (1 if tip else 0) | (cid << 1)
        # Contact Count is a single byte; we never report >1 finger down.
        count = 1 if tip else 0
        return struct.pack("<BBHH", count, flags, x, y)

    @staticmethod
    def _to_logical(px: int, screen_size: int) -> int:
        """Map a pixel position on a virtual desktop of
        ``screen_size`` pixels on this axis into the HID logical
        0..32767 range. Defends against a zero / negative screen
        size (treats it as 1 px so the division doesn't crash; the
        host will ignore an effectively meaningless report)."""
        if screen_size <= 0:
            screen_size = 1
        clamped = max(0, min(screen_size, int(px)))
        return round(clamped * DigitizerWriter.LOG_MAX / screen_size)

    def move(self, x: int, y: int, *, screen_w: int, screen_h: int,
             tip_down: bool = False) -> int:
        """Move the pointer. Two flavours:

        ``tip_down=False`` (default) — Contact Count = 0, Tip Switch = 0,
        X/Y present. The host touch driver treats this as a
        move-without-touch event (Win PT drivers silently discard
        this, so it's effectively the legacy "hover" stub retained
        for the keyboard tap → release path).

        ``tip_down=True`` — Contact Count = 1, Tip Switch = 1. The host
        sees an *in-progress* contact moving across the surface, so
        drag operations (window dragging, file drops, text selection
        sliders, etc.) route through the same Win PT event stream as
        a finger being held down and slid. This is what
        :meth:`PointerEvent.mousemove` on the page surface needs.
        Called many times per second from :func:`bindGridInteraction`'s
        pointermove handler.

        ``x``/``y`` are absolute pixel coordinates in the **PC
        virtual desktop** the operator described (e.g. primary 2560
        + secondary 1920 on the X axis when the secondary is to the
        right of the primary). The function scales into the HID
        0..32767 logical range here, so the API stays in pixels —
        the most useful unit for a UI that points at a canvas.
        """
        lx = self._to_logical(x, screen_w)
        ly = self._to_logical(y, screen_h)
        self._last_x, self._last_y = lx, ly
        tip = bool(tip_down)
        # count is 1 iff tip is down — Win PT driver rejects count=1
        # with tip=0 (or count=0 with tip=1) as inconsistent, so they
        # are bound together here rather than relying on the caller
        # to keep them in sync.
        return manager.safe_write(self._open(),
                                  self._encode(tip, 0, lx, ly))

    def down(self, x: int, y: int, *, screen_w: int, screen_h: int) -> int:
        """Begin a contact: Tip Switch = 1, Contact Count = 1, at
        ``(x, y)`` in PC virtual desktop pixels.

        Used by the frontend's pointerdown handler to mark the
        *start* of a press/drag gesture. The caller is expected to
        follow with zero or more :meth:`move` (``tip_down=True``)
        frames as the pointer slides, and then :meth:`up` on release.
        Splitting tip_down and tip_up into separate calls lets the
        page control press duration precisely (a "click" becomes
        down → up with no move frames; a "drag" becomes down → N×move
        → up).
        """
        lx = self._to_logical(x, screen_w)
        ly = self._to_logical(y, screen_h)
        self._last_x, self._last_y = lx, ly
        return manager.safe_write(self._open(),
                                  self._encode(True, 0, lx, ly))

    def up(self) -> int:
        """End the contact: Tip Switch = 0, Contact Count = 0.

        Pair with :meth:`down`. Sends 0×0 so the host sees "pen left
        the tablet" instead of "pen teleported to origin" — a teleport
        spuriously fires a click on whichever screen element happens
        to be at (0, 0) on the host.
        """
        return manager.safe_write(self._open(),
                                  self._encode(False, 0, 0, 0))

    def click(self, x: int, y: int, *, screen_w: int, screen_h: int,
              press_ms: float = 12.0) -> int:
        """tip_down → (small hold) → tip_up at the given position.

        ``x``/``y`` are pixels on a virtual desktop of size
        ``screen_w`` × ``screen_h``; the function maps them into
        the HID 0..32767 logical range. Returns the number of
        bytes written for the tip_down (6); tip_up is sent on the
        same FD and succeeds silently.

        Convenience wrapper around :meth:`down` + sleep +
        :meth:`up`. Kept for the "one-shot tap" endpoint; the new
        /digitizer/down + /digitizer/up pair handles press-and-drag.
        """
        lx = self._to_logical(x, screen_w)
        ly = self._to_logical(y, screen_h)
        fd = self._open()
        written = manager.safe_write(fd, self._encode(True, 0, lx, ly))
        time.sleep(max(0.0, press_ms / 1000.0))
        manager.safe_write(fd, self._encode(False, 0, lx, ly))
        return written

    def leave(self) -> int:
        """Out-of-range — host shows the "pen left the tablet" cursor."""
        return manager.safe_write(self._open(),
                                  self._encode(False, 0, 0, 0))


# --------------------------------------------------------------------------- #
# Module-level singletons (one per HID function)
# --------------------------------------------------------------------------- #
_keyboard: KeyboardWriter | None = None
_digitizer: DigitizerWriter | None = None


def get_keyboard() -> KeyboardWriter:
    """Return the singleton keyboard writer. Constructed on first call;
    subsequent calls reuse the same object (and its cached FD)."""
    global _keyboard
    if _keyboard is None:
        _keyboard = KeyboardWriter()
    return _keyboard


def get_digitizer() -> DigitizerWriter:
    """Return the singleton digitizer writer. The first call constructs
    it; the FD is opened lazily inside :class:`DigitizerWriter` so a
    missing ``/dev/hidg1`` only fails when an endpoint actually runs
    (not at module import)."""
    global _digitizer
    if _digitizer is None:
        _digitizer = DigitizerWriter()
    return _digitizer
