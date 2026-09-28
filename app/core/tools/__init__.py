"""Tool registry: @Callable tool registration + dispatch.

``app.core.tools.registry.get_tool_registry()`` returns the singleton
registry. Each card registers the @Callable classes it owns (e.g.
``tts_alert`` from the video-detection card).
"""
from __future__ import annotations
