"""FastAPI application factory."""
from fastapi import FastAPI

from app import __version__


def create_app() -> FastAPI:
    app = FastAPI(
        title="pyrkaiweb",
        version=__version__,
        description="pyrkaiweb service",
    )

    @app.get("/")
    async def root() -> dict:
        return {"message": "Hello, pyrkaiweb!", "version": __version__}

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    return app


app = create_app()