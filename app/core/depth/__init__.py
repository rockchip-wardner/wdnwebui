"""深度估计 —— DA3 实时推理侧。

这一层只做「把帧变成深度图」这一件事：

* :mod:`app.core.depth.palette` —— turbo 调色板 + 色标窗口管理（纯 numpy）
* :mod:`app.core.depth.depth_detector` —— :class:`DepthDetector`，与
  :class:`app.core.llm.detector_base.WdnaiDetector` 同构的 duck-type

卡片侧（``wdnapps_video_analyzer.cards.video_depth``）只负责把这些能力
接到多路会话与前端上。
"""
from __future__ import annotations

from .depth_detector import (
    DEBUG_ENV,
    DepthDetector,
    depth_debug_enabled,
    get_depth_detector,
    stop_depth_detector,
)
from .palette import DepthWindow, render_depth_jpeg, turbo_rgb

__all__ = [
    "DEBUG_ENV",
    "DepthDetector",
    "DepthWindow",
    "depth_debug_enabled",
    "get_depth_detector",
    "render_depth_jpeg",
    "stop_depth_detector",
    "turbo_rgb",
]
