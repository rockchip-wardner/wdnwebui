"""Tool registry: @Callable tool registration + dispatch.

``app.core.tools.registry.get_tool_registry()`` returns the singleton
registry. Each card registers the @Callable classes it owns.

``TtsAlert`` (``tts_alert``) is itself a **core** builtin rather than a
card-owned tool: ``app.core.tools._builtins`` — which lives in core and is
also imported by the detached ``compute_worker`` subprocess — has to
resolve it without importing any card. When it lived inside the
video-detection card, this was a core→plugin dependency that would have
silently degraded (the import is deliberately soft-failing) to "the tool
does not exist" as soon as that card shipped as a separate wheel.
"""
from __future__ import annotations
