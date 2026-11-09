"""qwen3-asr card —— Qwen3-ASR 流式语音识别。

Manifest-driven backend entry point. The plugin loader
(:mod:`app.core.manifest`) imports this module and calls :func:`register_api`
to mount the router and :func:`register_shutdown` to register the cleanup
hook.

Backend layout (per card)::

    apps/qwen3_asr/
    ├── manifest.json      # plugin declaration
    ├── api.py             # FastAPI router (mounted at /api/qwen3-asr)
    ├── service.py         # business logic (singleton + WS broadcast)
    ├── static/            # template.html + app.js + app.css + wizard.json
    └── __init__.py        # this file — register_api / register_shutdown

The card may only import from ``app.core.*`` (plus the SDK). No cross-card
imports.
"""
from __future__ import annotations

from fastapi import FastAPI

from app.apps.qwen3_asr.api import router

LOG = __import__("logging").getLogger("apps.qwen3_asr")


def register_api(app: FastAPI) -> None:
    """Mount the card's FastAPI router at ``/api/qwen3-asr``."""
    app.include_router(router, prefix="/api/qwen3-asr")
    LOG.info("qwen3_asr: API mounted at /api/qwen3-asr")


def register_shutdown() -> None:
    """Release the card's service singleton."""
    try:
        from app.apps.qwen3_asr import service as _service_mod
    except Exception:
        return
    for attr in ("close", "stop", "shutdown"):
        fn = getattr(_service_mod, attr, None)
        if callable(fn):
            try:
                fn()
            except Exception:
                LOG.warning("qwen3_asr: %s.%s failed", _service_mod.__name__, attr)
