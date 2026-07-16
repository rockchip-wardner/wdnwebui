"""FastAPI application factory."""
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app import __version__
from app.api import power, system_logs, system_monitor, video_detection
from app.services.detector import get_detector
from app.services.video_session import get_session

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

    app.include_router(system_monitor.router, prefix="/api")
    app.include_router(video_detection.router, prefix="/api")
    app.include_router(system_logs.router, prefix="/api")
    app.include_router(power.router, prefix="/api")

    # Wire the detector as a frame sink on the video session so each
    # decoded BGR frame is offered to it (it picks the Nth one).
    get_session().add_frame_sink(
        lambda frame, frame_pos: get_detector().submit_frame(
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
        # Free the RKNN session handle if one was ever built.
        try:
            get_detector().close()
        except Exception:
            pass

    return app


app = create_app()