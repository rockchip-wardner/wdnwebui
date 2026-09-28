"""Component version endpoint.

Reports the runtime ``__version__`` of every ``wdn*`` package the
process has loaded, plus the webapp's own version. Surfaced in the
sidebar (top-left) so operators can tell at a glance which build
of each upstream SDK they're running.
"""
from __future__ import annotations

from app import __version__ as _WEB_VERSION
from fastapi import APIRouter

router = APIRouter(tags=["versions"])


def _safe_version(pkg_name: str) -> str | None:
    """Best-effort ``__version__`` lookup for an installed SDK.

    Tries ``importlib.import_module(pkg_name).__version__`` first
    (what a Python user sees when they ``import`` the SDK) and falls
    back to the distribution metadata version if the package doesn't
    expose ``__version__`` directly.

    Returns ``None`` when neither lookup succeeds — the sidebar
    then shows ``missing`` instead of an empty string, and the
    endpoint still returns 200 with the packages that *did* load.
    """
    try:
        import importlib

        mod = importlib.import_module(pkg_name)
        v = getattr(mod, "__version__", None)
        if v:
            return v
    except Exception:
        pass
    try:
        import importlib.metadata as _md

        return _md.version(pkg_name)
    except Exception:
        return None


@router.get("/versions")
async def versions() -> dict:
    """Return webapp + every ``wdn*`` component version.

    Shape::

        {
          "wdnwebui": "0.7.0",
          "wdnai":    "0.2.1",
          "wdnav":    "0.3.0",
          "wdndds":   "0.2.0"
        }

    Components that failed to import (``wdnai`` / ``wdnav`` native
    libs missing on a non-RK dev box, etc.) come back as ``null`` so
    the UI can render a dash without the endpoint 500-ing.
    """
    return {
        "wdnwebui": _WEB_VERSION,
        "wdnai":    _safe_version("wdnai"),
        "wdnav":    _safe_version("wdnav"),
        "wdndds":   _safe_version("wdndds"),
    }