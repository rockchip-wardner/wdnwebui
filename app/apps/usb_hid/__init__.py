"""USB HID bridge card.

Drives the kernel-side USB Gadget HID function (configfs) through the
thin wrappers in :mod:`wdnhid`.

Most of the card is stateless from the request/response angle — every
endpoint just writes one or more HID reports to ``/dev/hidg0`` (boot
keyboard) or ``/dev/hidg1`` (digitizer) and returns. **Exception: the
AI-control loop** (``app.apps.usb_hid.agent``), which is the one
long-lived background thing here. That's why ``manifest.json``'s
``DemoGuard`` block is populated with ``/task/stop``: see the comment
in ``app.static.core.framework`` on why an active background state
must be stopped when the operator navigates away.

Why a dedicated card instead of bolting this onto ``tools``:

* The other "tools" cards (compute config, system monitor) are
  operators-facing config. HID is *outbound* — we're producing events
  that the host sees. Mixing them invites a runaway keystroke injection
  from a config typo.
* Wizard shape is fundamentally different (canvas + keystroke sandbox,
  not model selection).
* Capability model: the manual-control half needs no model, but the
  AI-control half consumes ``swcap_computer_control``
  (qwen3.5-vl-4b). Without it, ``get_session`` raises and
  ``POST /task/start`` answers 502 — the manual half is unaffected.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI

from app.apps.usb_hid.api import router

LOG = logging.getLogger("apps.usb_hid")


def register_api(app: FastAPI) -> None:
    """Mount the card's FastAPI router at ``/api/usb-hid``."""
    app.include_router(router, prefix="/api/usb-hid")
    LOG.info("usb_hid: API mounted at /api/usb-hid")


def register_shutdown() -> None:
    """Stop the AI-control loop, drop cached ``/dev/hidgN`` file
    descriptors and stop the HDMI-IN background capture thread.

    Best-effort: the only state we hold is the FD cache inside
    :mod:`wdnhid.manager`, a single ``HdmiInCapture`` daemon
    thread (kept running while the card is loaded so snapshots
    stay fresh between page hits), and the agent's worker thread.
    If the user calls ``/teardown``
    then the gadget disappears and the next request will reopen
    cleanly (or report an error if the kernel unbind left the
    chardevs dangling). The actual configfs teardown is owned by
    the ``scripts/teardown_usb_hid_gadget.sh`` script, not the
    service.

    The agent stop comes **first** on purpose: it's the only
    component that can be mid-action, and ``stop()`` releases the
    keyboard/digitizer on the way out. Killing the chardev FDs under a
    running loop would leave the host with whatever report happened to
    be in flight.
    """
    from wdnhid import manager
    # Stop the AI-control loop first — it is the only thing that can be
    # holding a key or a touch down.
    try:
        from app.apps.usb_hid.agent import get_agent
        get_agent().stop()
    except Exception as e:  # noqa: BLE001
        LOG.warning("usb_hid: agent stop failed: %s", e)
    try:
        manager.close_chardevs()
    except Exception as e:  # noqa: BLE001
        LOG.warning("usb_hid: close_chardevs failed: %s", e)
    # Stop the hdmiin background capture so /dev/videoN is released.
    # Lazy import — the capture module is only loaded on first use.
    try:
        from app.core.hdmiin.capture import get_capture
        get_capture().stop()
    except Exception as e:  # noqa: BLE001
        LOG.debug("usb_hid: hdmiin capture stop skipped: %s", e)
