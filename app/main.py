"""FastAPI application factory."""
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app import __version__
from app.api import (
    compute,
    config,
    coprocessor,
    lan_discovery,
    power,
    system_logs,
    system_monitor,
    tts,
    video_detection,
    video_target_detection,
    wechat,
)
from app.services.compute_registry import get_registry
from app.services.config import get_config
from app.services.detector import get_detector, get_target_detector
from app.services.tts_session import get_session as get_tts_session
from app.services.video_session import get_session
from app.services.wechat import close_wechat, get_wechat

STATIC_DIR = Path(__file__).parent / "static"


class NoCacheStaticMiddleware(BaseHTTPMiddleware):
    """Dev: keep /static/ fresh so JS changes don't get hidden behind
    the browser cache. The reload watcher only sees Python edits, so
    without this the user keeps running stale app.js / style.css after
    a static edit unless they hard-refresh."""

    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response


def create_app() -> FastAPI:
    app = FastAPI(
        title="pyrkaiweb",
        version=__version__,
        description="pyrkaiweb service",
    )

    app.add_middleware(NoCacheStaticMiddleware)

    # Resolve the on-disk config.json path early so it shows up in the
    # /health endpoint and the UI's config popover can render its
    # absolute path. Missing files are tolerated — they read as {}.
    cfg = get_config()

    # Propagate RKAI_MODELS_DIR from config.json so downstream SDKs
    # (rkai's `_models_root()`) pick up the configured model root
    # without each consumer having to read config separately. Setting
    # it before any LLMSession is built means the SDK reads the right
    # path on its first lookup. Done once at app boot — a runtime
    # config change requires a restart to take effect (the detector
    # already loaded an LLMSession by then).
    base = cfg.get_section("base", {})
    model_path = (base or {}).get("model_path")
    if isinstance(model_path, str) and model_path.strip():
        os.environ["RKAI_MODELS_DIR"] = model_path

    app.include_router(system_monitor.router, prefix="/api")
    app.include_router(video_detection.router, prefix="/api")
    app.include_router(system_logs.router, prefix="/api")
    app.include_router(power.router, prefix="/api")
    app.include_router(config.router, prefix="/api")
    app.include_router(coprocessor.router, prefix="/api")
    app.include_router(tts.router, prefix="/api")
    app.include_router(compute.router, prefix="/api")
    app.include_router(lan_discovery.router, prefix="/api")
    app.include_router(video_target_detection.router, prefix="/api")
    app.include_router(wechat.router, prefix="/api")

    # 暂时关闭：boot 时**不**自动加载 config.json["compute"]["instances"]
    # 里的实例 —— 用户必须从算力配置页手动启用。Worker 线程在第一次
    # POST /api/compute/instances 时由 ``start_one`` 走 ``_wake_worker``
    # → ``_ensure_worker`` 按需起 daemon thread。
    # 重新打开 boot 自动加载：
    #   get_registry().apply_boot_config_async()

    # Wire the detector as a frame sink on the video session so each
    # decoded BGR frame is offered to it (it picks the Nth one).
    get_session().add_frame_sink(
        lambda frame, frame_pos: get_detector().submit_frame(
            frame, frame_pos
        )
    )
    # Same for the video-target-detection page — second detector
    # instance with its own prompt / history.
    get_session().add_frame_sink(
        lambda frame, frame_pos: get_target_detector().submit_frame(
            frame, frame_pos
        )
    )

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.on_event("shutdown")
    async def _on_shutdown():
        # Stop all compute instances (close their LLMSession handles
        # and join the worker thread). Done first so other shutdown
        # steps that touch the SDK see a clean state.
        try:
            get_registry().close_all()
        except Exception:
            pass
        # Free the RKNN session handle if one was ever built.
        try:
            get_detector().close()
        except Exception:
            pass
        try:
            get_target_detector().close()
        except Exception:
            pass
        # Free the Qwen3-TTS RKNN sessions if any were built.
        try:
            get_tts_session().close()
        except Exception:
            pass
        # Stop the WeChat bot manager (cancels its poller, closes the
        # underlying wdiot.Wechat which owns the long-poll task).
        try:
            close_wechat()
        except Exception:
            pass

    return app


app = create_app()