"""USB HID bridge card.

Drives the kernel-side USB Gadget HID function (configfs) through the
thin wrappers in :mod:`wdnhid`. The card is **stateless
from the request/response angle** — every endpoint just writes one or
more HID reports to ``/dev/hidg0`` (boot keyboard) or ``/dev/hidg1``
(digitizer) and returns. There is no background session, so the
``DemoGuard`` block in ``manifest.json`` carries no ``active_state_keys``
and no ``stop_endpoints`` — see the comment in
``app.static.core.framework`` on why that matters for the in-app
navigation guard.

Why a dedicated card instead of bolting this onto ``tools``:

* The other "tools" cards (compute config, system monitor) are
  operators-facing config. HID is *outbound* — we're producing events
  that the host sees. Mixing them invites a runaway keystroke injection
  from a config typo.
* Wizard shape is fundamentally different (canvas + keystroke sandbox,
  not model selection).
* Capability model is different: no ``swcap_*`` consumed; the only
  dependency is the kernel having ``CONFIG_USB_CONFIGFS_F_HID=y`` and
  the user having run ``scripts/setup_usb_hid_gadget.sh`` once.
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
    """Drop cached ``/dev/hidgN`` file descriptors and stop the
    HDMI-IN background capture thread.

    Best-effort: the only state we hold is the FD cache inside
    :mod:`wdnhid.manager` and a single ``HdmiInCapture`` daemon
    thread (kept running while the card is loaded so snapshots
    stay fresh between page hits). If the user calls ``/teardown``
    then the gadget disappears and the next request will reopen
    cleanly (or report an error if the kernel unbind left the
    chardevs dangling). The actual configfs teardown is owned by
    the ``scripts/teardown_usb_hid_gadget.sh`` script, not the
    service.
    """
    from wdnhid import manager
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
