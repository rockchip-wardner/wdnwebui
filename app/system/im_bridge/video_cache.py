"""im_bridge's view of the shared video cache.

The actual lancedb cache lives in :mod:`app.core.video.cache` (shared
with the video-content-search card). This module is a thin re-export
for backward compatibility with existing call sites.
"""
from __future__ import annotations

from app.core.video.cache import (
    file_md5,
    get_video_cache,
    WechatVideoCache,
)

__all__ = ["file_md5", "get_video_cache", "WechatVideoCache"]
