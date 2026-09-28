"""App core: shared infrastructure used by every card.

Cards (``app/apps/<id>/...``) are the only consumers allowed to
``import app.core.*``. The other internal namespaces (``app.apps.*``
and ``app.system.*``) are off-limits to card code by the zero-coupling
rule.

Modules:
    config            — config.jsonc load + seed defaults
    manifest          — plugin manifest discovery + validation
    uploads           — per-card uploads dir manager + safe_path()
    compute           — compute_registry + worker + rpc proxy + jsonc
    llm               — WdnaiDetector base class
    video             — shared VideoSession (frame-sink model)
    chat              — shared chat agent / runner / stream
    tools             — tool registry for @Callable tools
"""
from __future__ import annotations

__version__ = "0.4.0"
