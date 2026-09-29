"""video-analysis-search card.

Manifest-driven backend entry point. The plugin loader (``app.core.manifest``)
imports this module and calls :func:`register_api` to mount the card's
routers and :func:`register_shutdown` to register the cleanup hook.

Backend layout (per card):

    apps/video_analysis_search/
    ├── manifest.json      # plugin declaration
    ├── api.py             # 分析侧 FastAPI router (mounted at .../analysis)
    ├── search_api.py      # 检索侧 FastAPI router (mounted at .../search)
    ├── service.py         # 分析业务逻辑（LanceDB 持久化）
    ├── static/            # template.html + app.js + app.css + preview.mp4
    └── __init__.py        # this file — register_api / register_shutdown

The card may only import from ``app.core.*``. No cross-card imports —
检索业务逻辑在 ``app.core.video.search``。
"""
from __future__ import annotations

from fastapi import FastAPI

from app.apps.video_analysis_search.api import router as analysis_router
from app.apps.video_analysis_search.search_api import router as search_router

LOG = __import__("logging").getLogger("apps.video_analysis_search")

_API_PREFIX = "/api/video-analysis-search"


def register_api(app: FastAPI) -> None:
    """Mount the card's analysis + search routers.

    分析侧 → ``/api/video-analysis-search/analysis/*``；
    检索侧 → ``/api/video-analysis-search/search/*``。
    """
    app.include_router(analysis_router, prefix=_API_PREFIX + "/analysis")
    app.include_router(search_router, prefix=_API_PREFIX + "/search")
    LOG.info("video_analysis_search: API mounted at %s/{analysis,search}",
             _API_PREFIX)


def register_shutdown() -> None:
    """Release the card's service singletons (join worker threads).

    Best-effort: 直接读模块级 ``_service``（避免 ``get_service()`` 在
    从未使用时实例化），逐个 close，异常不互相影响。
    """
    import traceback

    targets = []
    try:
        from app.apps.video_analysis_search import service as _analysis_mod
        targets.append(_analysis_mod)
    except Exception:
        pass
    try:
        from app.core.video import search as _search_mod
        targets.append(_search_mod)
    except Exception:
        pass

    for mod in targets:
        inst = getattr(mod, "_service", None)
        if inst is None:
            continue
        close = getattr(inst, "close", None)
        if not callable(close):
            continue
        try:
            close()
        except Exception:
            LOG.warning("video_analysis_search: %s close failed", mod.__name__)
            LOG.debug("%s", traceback.format_exc())
