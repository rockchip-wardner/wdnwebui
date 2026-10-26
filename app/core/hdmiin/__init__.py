"""HDMI-IN capture helpers for RK3588 (``rk_hdmirx``).

This module wraps :mod:`wdnav.v4l2` for the specific case of
streaming the PC's display output that is connected to the RK3588's
HDMI-RX input. The kernel's ``rk_hdmirx`` driver exposes the capture
node at a fixed path (``/dev/video0`` on the stock board, but we
auto-discover it from ``/sys/class/video4linux`` to be safe) and the
``/sys/class/hdmirx/hdmirx/status`` file reports whether the upstream
HDMI link is connected.

Design notes:

* The hdmirx driver is **signal-driven** — it refuses S_FMT for any
  pixfmt other than the one it picked based on the incoming HDMI signal
  (本机 rk_hdmirx 只接受 ``type=CAPTURE_MPLANE + 'NV24'``，bgr3 / nv12 /
  rgb3 / yuyv / uyvy 一律 EINVAL). :func:`wdnav.v4l2.V4L2Capture` handles
  that via its **G_FMT-then-S_FMT fallback**: S_FMT 失败就
  ``VIDIOC_G_FMT`` 问驱动当前格式再重试一次。我们只管把发现的节点喂给它，
  再读回它协商到的格式 —— 那是 ``cap.pixel_format``（**协商结果**，不是
  我们请求的值）。
* 驱动报的四cc**可能是撒谎的**：rk_hdmirx 报 'NV24'（V4L2 规范里是 12bit
  YUV420SP），实际写的是 8bit **YUV422SP**。缓冲区怎么解读一律问
  :func:`wdnav.v4l2.frame_layout`，别按 fourcc 猜。
* All sysfs reads are best-effort; on a board without HDMI-RX
  hardware the probe degrades to ``ok=False`` with a reason, never
  throws. The capability gate (``hwcap_hdmiin``) decides whether the
  /api/usb-hid/source/hdmiin/* endpoints are exposed in the first
  place; this module just answers "what's there".
"""

from __future__ import annotations

import logging
from typing import Optional

_LOG = logging.getLogger("core.hdmiin")


def load_v4l2_capture() -> Optional[type]:
    """Return ``wdnav.v4l2.V4L2Capture``, or ``None`` if it can't be used.

    ``wdnav/src/wdnav/v4l2.py`` does a **module level** ``import v4l2
    as _v4l2`` — the third-party PyPI package (pure ctypes Video4L2
    bindings), *not* the stdlib.  ``wdnav`` declares it in its
    ``pyproject.toml`` but is installed with ``pip install --no-deps
    -e``, so it never reaches the venv on its own.

    Two distinct failures, both raised **at import time**:

    1. package missing → ``ModuleNotFoundError`` (an ``ImportError``)::

           No module named 'v4l2'

       Fix: ``pip install -r requirements.txt`` (the ``v4l2>=0.2`` entry
       exists precisely because of this).

    2. package installed but broken → ``TypeError``::

           unsupported operand type(s) for +: 'range' and 'list'

       Upstream ``v4l2`` 0.2 (2014) still uses the Python-2 idiom
       ``range(...) + [...]``; on py3 that raises during import.  It is
       a ``TypeError``, so a plain ``except ImportError`` guard does
       **not** catch it and the request would blow up as a 500 instead
       of degrading to 503.  Fix: ``python scripts/fix_v4l2_py3.py``.

    Both are logged with the remedy and reported as ``None`` so callers
    degrade to "feature unavailable" (503) instead of crashing.
    """
    try:
        from wdnav.v4l2 import V4L2Capture
        return V4L2Capture
    except ImportError as e:
        if getattr(e, "name", None) == "v4l2":
            _LOG.error(
                "wdnav.v4l2 import failed: %s — the third-party `v4l2` "
                "(PyPI, pure ctypes) is required by "
                "wdnav/src/wdnav/v4l2.py but the SDK is installed with "
                "--no-deps, so it never lands in the venv. "
                "Fix: pip install -r requirements.txt",
                e,
            )
        else:
            _LOG.error("wdnav.v4l2 import failed: %s", e)
    except Exception as e:  # noqa: BLE001 — third-party import, any failure
        _LOG.error(
            "wdnav.v4l2 import failed: %s: %s — upstream `v4l2` 0.2 uses "
            "the py2 idiom `range(...) + [...]` and cannot be imported on "
            "python 3. Fix: python scripts/fix_v4l2_py3.py",
            type(e).__name__,
            e,
        )
    return None
