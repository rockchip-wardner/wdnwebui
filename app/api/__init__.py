"""API routers package.

Each module exposes an `APIRouter` named `router` that gets mounted under
``/api/`` in :func:`app.main.create_app`. Importing the submodules here is
what makes ``app.include_router(...)`` in ``main.py`` see them — without
it, only the bare package object is imported and nothing registers.
"""
from . import (
    audio_analysis,
    compute,
    config,
    coprocessor,
    power,
    system_logs,
    system_monitor,
    text_chat,
    tts,
    versions,
    video_content_search,
    video_detection,
    video_file_analysis,
    video_target_detection,
    wechat,
)

__all__ = [
    "audio_analysis",
    "compute",
    "config",
    "coprocessor",
    "power",
    "system_logs",
    "system_monitor",
    "text_chat",
    "tts",
    "versions",
    "video_content_search",
    "video_detection",
    "video_file_analysis",
    "video_target_detection",
    "wechat",
]