"""video-detection card.

Manifest-driven backend entry point. The plugin loader (``app.core.manifest``)
imports this module and calls :func:`register_api` to mount the router
and :func:`register_shutdown` to register the cleanup hook.

Backend layout (per card):

    apps/video_detection/
    ├── manifest.json      # plugin declaration
    ├── api.py             # FastAPI router (mounted at /api/video-detection)
    ├── service.py / *.py  # business logic
    ├── detector.py        # (vd / vtd / vtl only) WdnaiDetector subclass
    ├── prompts.py         # (vd / vtd / vtl only) schema field defaults
    ├── static/            # template.html + app.js + app.css + preview.mp4
    └── __init__.py        # this file — register_api / register_shutdown

The card may only import from ``app.core.*``. No cross-card imports.
"""
from __future__ import annotations

from fastapi import FastAPI

from app.apps.video_detection.api import router

LOG = __import__("logging").getLogger("apps.video_detection")


def register_api(app: FastAPI) -> None:
    """Mount the card's FastAPI router at ``/api/video-detection``.

    Also registers the ``tts_alert`` @Callable tool with the central tool
    registry. Each card owns its own tools (zero-coupling rule).

    **这里不再把 detector 挂成 ``VideoSession`` 的 frame sink** —— 多路会话
    自带帧源（见 api.py 模块 docstring），挂在 ``VideoSession`` 上的 sink
    永远不会被触发，留着只会让人以为"帧是这么喂进去的"。
    """
    app.include_router(router, prefix="/api/video-detection")
    LOG.info("video_detection: API mounted at /api/video-detection")
    try:
        from app.core.tools.registry import get_tool_registry
        # 走 _builtins 共用注册函数,与 compute_worker 子进程共享
        # registry 真源:主进程侧 chat_agent._tool_classes(['TtsAlert'])
        # 否则查不到类 → 提示词缺工具 schema。详见 app/core/tools/_builtins.py。
        from app.core.tools._builtins import register_builtin_tools
        register_builtin_tools(get_tool_registry())
        LOG.info("video_detection: registered tool 'tts_alert'")
    except Exception as e:
        LOG.warning("video_detection: tts_alert registration failed: %s", e)


def register_shutdown() -> None:
    """Release the card's service singletons (join worker threads).

    Best-effort: each cleanup call is wrapped in try/except so a single
    card's failure does not break the rest of the shutdown sequence.
    """
    import importlib
    import traceback
    cleanup_targets = []
    try:
        from app.apps.video_detection import service as _service_mod
        cleanup_targets.append(_service_mod)
    except Exception:
        pass
    # For multi-module cards (e.g. im_bridge), also try bot/llm/video_cache
    for subname in ("bot", "llm", "video_cache", "video_manager", "service_wechat"):
        try:
            mod = importlib.import_module("app.apps.video_detection." + subname)
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
                    LOG.warning("video_detection: %s.%s failed", mod.__name__, attr)
                    LOG.debug("%s", traceback.format_exc())
