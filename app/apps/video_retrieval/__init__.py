"""video-retrieval card.

Manifest-driven backend entry point. The plugin loader (``app.core.manifest``)
imports this module and calls :func:`register_api` to mount the card's
routers and :func:`register_shutdown` to register the cleanup hook.

Backend layout (per card):

    apps/video_retrieval/
    ├── manifest.json      # plugin declaration
    ├── api.py             # 检索 + 向导预览 FastAPI router
    ├── static/            # template.html + app.js + app.css + preview.mp4
    └── __init__.py        # this file — register_api / register_shutdown

The card may only import from ``app.core.*``. No cross-card imports —
检索业务逻辑在 ``app.core.video.search``。
"""
from __future__ import annotations

import logging

from fastapi import FastAPI

from app.apps.video_retrieval.api import preview_router, router

LOG = logging.getLogger("apps.video_retrieval")

_API_PREFIX = "/api/video-retrieval"


def register_api(app: FastAPI) -> None:
    """Mount the card's retrieval + preview routers.

    检索侧     → ``/api/video-retrieval/*``
    向导预览   → ``/api/video-retrieval/{upload,source/*}``
    """
    app.include_router(router, prefix=_API_PREFIX)
    app.include_router(preview_router, prefix=_API_PREFIX)
    LOG.info("video_retrieval: API mounted at %s", _API_PREFIX)


def register_shutdown() -> None:
    """Release the retrieval service singleton (join worker threads)."""
    import traceback

    try:
        from app.core.video import search as _search_mod
    except Exception:
        return
    inst = getattr(_search_mod, "_service", None)
    if inst is None:
        return
    close = getattr(inst, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception:
        LOG.warning("video_retrieval: search service close failed")
        LOG.debug("%s", traceback.format_exc())
