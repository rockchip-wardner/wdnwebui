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


# Module-level handle so register_shutdown() can remove the same sink
# we registered here. The video session singleton outlives the card's
# shutdown callback, so leaving the sink registered would keep firing
# on every decoded frame after the detector is closed.
_VTD_FRAME_SINK = None


def _make_vtd_sink():
    """Return a frame sink that forwards each decoded BGR frame into
    the VTD detector's ``submit_frame`` queue. See video_detection
    card for the full rationale — same pattern, second detector
    instance with its own prompt / history.
    """
    from app.core.llm.detector_base import get_target_detector

    def _sink(frame, frame_pos, frame_b64, judge_score=None):
        try:
            get_target_detector().submit_frame(frame, frame_pos, frame_b64,
                                               judge_score=judge_score)
        except Exception as e:  # noqa: BLE001
            LOG.debug(
                "video_target_detection: frame sink raised: %s: %s",
                type(e).__name__, e,
            )
    return _sink


def register_api(app: FastAPI) -> None:
    """Mount the card's FastAPI router at ``/api/video-target-detection``.

    Also wires the VTD detector as a frame sink on the shared
    ``VideoSession`` singleton (see video_detection card for the
    rationale — same pattern, second detector instance).
    """
    global _VTD_FRAME_SINK
    app.include_router(router, prefix="/api/video-target-detection")
    LOG.info("video_target_detection: API mounted at /api/video-target-detection")
    try:
        from app.core.video.session import get_session
        _VTD_FRAME_SINK = _make_vtd_sink()
        get_session().add_frame_sink(_VTD_FRAME_SINK)
        LOG.info(
            "video_target_detection: detector registered as video_session "
            "frame sink"
        )
    except Exception as e:
        LOG.warning(
            "video_target_detection: frame sink registration failed: %s", e
        )


def register_shutdown() -> None:
    """Release the card's service singletons (join worker threads).

    Best-effort: each cleanup call is wrapped in try/except so a single
    card's failure does not break the rest of the shutdown sequence.
    """
    global _VTD_FRAME_SINK
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
    # Drop the frame sink we registered in register_api(); see
    # video_detection card for rationale.
    if _VTD_FRAME_SINK is not None:
        try:
            from app.core.video.session import get_session
            get_session().remove_frame_sink(_VTD_FRAME_SINK)
        except Exception:
            LOG.debug("video_target_detection: remove_frame_sink failed", exc_info=True)
        _VTD_FRAME_SINK = None
