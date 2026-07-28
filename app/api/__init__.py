"""API routers package.

Each module exposes an `APIRouter` named `router` that gets mounted under
``/api/`` in :func:`app.main.create_app`. Importing the submodules here is
what makes ``app.include_router(...)`` in ``main.py`` see them — without
it, only the bare package object is imported and nothing registers.
"""
from . import (
    config,
    coprocessor,
    power,
    surveillance,
    system_logs,
    system_monitor,
    tts,
    video_detection,
)

__all__ = [
    "config",
    "coprocessor",
    "power",
    "surveillance",
    "system_logs",
    "system_monitor",
    "tts",
    "video_detection",
]