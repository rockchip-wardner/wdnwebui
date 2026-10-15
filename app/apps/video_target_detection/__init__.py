"""video-target-detection card.

Manifest-driven backend entry point. The plugin loader (``app.core.manifest``)
imports this module and calls :func:`register_api` to mount the router
and :func:`register_shutdown` to register the cleanup hook.

Backend layout (per card):

    apps/video_target_detection/
    ├── manifest.json      # plugin declaration
    ├── api.py             # FastAPI router (mounted at /api/video-target-detection)
    ├── service.py / *.py  # business logic
    ├── detector.py        # (vd / vtd / vtl only) WdnaiDetector subclass
    ├── prompts.py         # (vd / vtd / vtl only) schema field defaults
    ├── static/            # template.html + app.js + app.css + preview.mp4
    └── __init__.py        # this file — register_api / register_shutdown

The card may only import from ``app.core.*``. No cross-card imports.
"""
from __future__ import annotations

from fastapi import FastAPI

from app.apps.video_target_detection.api import router

LOG = __import__("logging").getLogger("apps.video_target_detection")


def register_api(app: FastAPI) -> None:
    """Mount the card's FastAPI router at ``/api/video-target-detection``.

    **不注册 frame sink**。多路化之后本卡的帧由 ``MultiChannelSession``
    自带：工厂在 ``POST /prompt`` 里 ``attach_detector`` + 轮转调度器供给，
    并带着正确的 ``channel``。

    历史上有过一份挂在单例 ``VideoSession`` 上的 sink，它不只是白费——
    ``VideoSession`` 现在仍被 ``video_analysis_search`` 的 ``/source/open``
    单路预览使用，于是「vtd 开着检测 → 切到 VAS 点打开」会把 **VAS 的帧以
    channel=0 灌进 vtd 的检测器**（``submit_frame`` 只在 detector 未启用时
    才 no-op），事件被错记到第 1 路。删掉它同时省掉一次无谓的每帧调用。
    """
    app.include_router(router, prefix="/api/video-target-detection")
    LOG.info("video_target_detection: API mounted at /api/video-target-detection")


def register_shutdown() -> None:
    """Release the card's service singletons (join worker threads).

    Best-effort: each cleanup call is wrapped in try/except so a single
    card's failure does not break the rest of the shutdown sequence.
    """
    import importlib
    import traceback
    cleanup_targets = []
    try:
        from app.apps.video_target_detection import service as _service_mod
        cleanup_targets.append(_service_mod)
    except Exception:
        pass
    # For multi-module cards (e.g. im_bridge), also try bot/llm/video_cache
    for subname in ("bot", "llm", "video_cache", "video_manager", "service_wechat"):
        try:
            mod = importlib.import_module("app.apps.video_target_detection." + subname)
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
                    LOG.warning("video_target_detection: %s.%s failed", mod.__name__, attr)
                    LOG.debug("%s", traceback.format_exc())
