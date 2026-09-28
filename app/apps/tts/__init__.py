"""tts card.

Manifest-driven backend entry point. The plugin loader (``app.core.manifest``)
imports this module and calls :func:`register_api` to mount the router
and :func:`register_shutdown` to register the cleanup hook.

Backend layout (per card):

    apps/tts/
    ├── manifest.json      # plugin declaration
    ├── api.py             # FastAPI router (mounted at /api/tts)
    ├── service.py / *.py  # business logic
    ├── detector.py        # (vd / vtd / vtl only) WdnaiDetector subclass
    ├── prompts.py         # (vd / vtd / vtl only) schema field defaults
    ├── static/            # template.html + app.js + app.css + preview.mp4
    └── __init__.py        # this file — register_api / register_shutdown

The card may only import from ``app.core.*``. No cross-card imports.
"""
from __future__ import annotations

from fastapi import FastAPI

from app.apps.tts.api import router

LOG = __import__("logging").getLogger("apps.tts")


def register_api(app: FastAPI) -> None:
    """Mount the card's FastAPI router at ``/api/tts``."""
    app.include_router(router, prefix="/api/tts")
    LOG.info("tts: API mounted at /api/tts")


def register_shutdown() -> None:
    """Release the card's service singletons (join worker threads).

    Best-effort: each cleanup call is wrapped in try/except so a single
    card's failure does not break the rest of the shutdown sequence.
    """
    import importlib
    import traceback
    cleanup_targets = []
    try:
        from app.apps.tts import service as _service_mod
        cleanup_targets.append(_service_mod)
    except Exception:
        pass
    # For multi-module cards (e.g. im_bridge), also try bot/llm/video_cache
    for subname in ("bot", "llm", "video_cache", "video_manager", "service_wechat"):
        try:
            mod = importlib.import_module("app.apps.tts." + subname)
            cleanup_targets.append(mod)
        except Exception:
            continue
    for mod in cleanup_targets:
        for attr in ("close", "stop", "shutdown", "close_all"):
            fn = getattr(mod, attr, None)
            if callable(fn):
                try:
                    fn()
                except Exception:
                    LOG.warning("tts: %s.%s failed", mod.__name__, attr)
                    LOG.debug("%s", traceback.format_exc())
