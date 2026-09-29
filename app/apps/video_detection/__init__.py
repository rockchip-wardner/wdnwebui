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


# Module-level handle so register_shutdown() can remove the same sink
# we registered here. The video session singleton outlives the card's
# shutdown callback, so leaving the sink registered would keep firing
# on every decoded frame after the detector is closed.
_VIDEO_DETECTION_FRAME_SINK = None


def _make_video_detection_sink():
    """Return a frame sink ``(frame, frame_pos, frame_b64) -> None``
    that forwards each decoded BGR frame into the VD detector's
    ``submit_frame`` queue. The detector itself decides whether to drop
    the frame (when ``_enabled`` is False) and which Nth one to keep
    (via ``every_n``), so the sink is always-on and cheap.
    """
    from app.core.llm.detector_base import get_detector

    def _sink(frame, frame_pos, frame_b64):
        try:
            get_detector().submit_frame(frame, frame_pos, frame_b64)
        except Exception as e:  # noqa: BLE001
            LOG.debug(
                "video_detection: frame sink raised: %s: %s",
                type(e).__name__, e,
            )
    return _sink


def register_api(app: FastAPI) -> None:
    """Mount the card's FastAPI router at ``/api/video-detection``.

    Also:
      * registers the ``tts_alert`` @Callable tool with the central
        tool registry. Each card owns its own tools (zero-coupling rule).
      * wires the VD detector as a frame sink on the shared
        ``VideoSession`` singleton so every decoded BGR frame is offered
        to it. This is the link between playback and inference — without
        it the detector worker thread idles forever because no frames
        ever arrive in its queue (root cause of the v0.4 regression
        reported as "启动检测 → 模型未推理"). The sink is a closure
        over ``get_detector()`` so the detector singleton is resolved
        lazily on first frame (avoids re-entrancy if ``register_api``
        is called before ``app.core.llm.detector_base`` is fully
        imported).
    """
    global _VIDEO_DETECTION_FRAME_SINK
    app.include_router(router, prefix="/api/video-detection")
    LOG.info("video_detection: API mounted at /api/video-detection")
    try:
        from app.core.tools.registry import get_tool_registry
        # 走 _builtins 共用注册函数,与 compute_worker 子进程共享
        # registry 真源:worker 端 _resolve_tool_classes(['TtsAlert'])
        # 否则查不到类 → 提示词缺工具 schema。详见 app/core/tools/_builtins.py。
        from app.core.tools._builtins import register_builtin_tools
        register_builtin_tools(get_tool_registry())
        LOG.info("video_detection: registered tool 'tts_alert'")
    except Exception as e:
        LOG.warning("video_detection: tts_alert registration failed: %s", e)
    # Frame sink wiring — see docstring.
    try:
        from app.core.video.session import get_session
        _VIDEO_DETECTION_FRAME_SINK = _make_video_detection_sink()
        get_session().add_frame_sink(_VIDEO_DETECTION_FRAME_SINK)
        LOG.info("video_detection: detector registered as video_session frame sink")
    except Exception as e:
        LOG.warning("video_detection: frame sink registration failed: %s", e)


def register_shutdown() -> None:
    """Release the card's service singletons (join worker threads).

    Best-effort: each cleanup call is wrapped in try/except so a single
    card's failure does not break the rest of the shutdown sequence.
    """
    global _VIDEO_DETECTION_FRAME_SINK
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
    # Drop the frame sink we registered in register_api(). The
    # VideoSession singleton outlives this card's shutdown, so without
    # this remove, the sink would keep firing on every decoded frame
    # after the detector is closed (LOG.debug spam, no functional harm
    # since submit_frame is a no-op when _enabled is False, but cleaner
    # to detach explicitly).
    if _VIDEO_DETECTION_FRAME_SINK is not None:
        try:
            from app.core.video.session import get_session
            get_session().remove_frame_sink(_VIDEO_DETECTION_FRAME_SINK)
        except Exception:
            LOG.debug("video_detection: remove_frame_sink failed", exc_info=True)
        _VIDEO_DETECTION_FRAME_SINK = None
