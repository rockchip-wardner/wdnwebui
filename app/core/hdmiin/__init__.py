"""HDMI-IN capture helpers for RK3588 (``rk_hdmirx``).

This module wraps :mod:`wdnav.v4l2` for the specific case of
streaming the PC's display output that is connected to the RK3588's
HDMI-RX input. The kernel's ``rk_hdmirx`` driver exposes the capture
node at a fixed path (``/dev/video0`` on the stock board, but we
auto-discover it from ``/sys/class/video4linux`` to be safe) and the
``/sys/class/hdmirx/hdmirx/status`` file reports whether the upstream
HDMI link is connected.

Design notes:

* The hdmirx driver is **signal-driven** on 6.12 — it refuses S_FMT
  for any pixfmt other than the one it picked based on the incoming
  HDMI signal. :func:`wdnav.v4l2.V4L2Capture` already handles that
  via the G_FMT-then-S_FMT fallback added in v0.5; we just feed it
  the discovered node and read back whatever the driver negotiates.
* All sysfs reads are best-effort; on a board without HDMI-RX
  hardware the probe degrades to ``ok=False`` with a reason, never
  throws. The capability gate (``hwcap_hdmiin``) decides whether the
  /api/usb-hid/source/hdmiin/* endpoints are exposed in the first
  place; this module just answers "what's there".
"""
