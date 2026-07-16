"""FastAPI application factory."""
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api import power, system_logs, system_monitor, video_detection

STATIC_DIR = Path(__file__).parent / "static"


def create_app() -> FastAPI:
    app = FastAPI(
        title="pyrkaiweb",
        version=__version__,
        description="pyrkaiweb service",
    )

    app.include_router(system_monitor.router, prefix="/api")
    app.include_router(video_detection.router, prefix="/api")
    app.include_router(system_logs.router, prefix="/api")
    app.include_router(power.router, prefix="/api")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    return app


app = create_app()