"""System API routers — manual registration in :mod:`app.system`.

Unlike ``app.apps`` (manifest-driven), the system routers are listed by
hand here so :func:`app.system.register_all` can mount them in one place.
Each module exposes a top-level ``router`` (FastAPI APIRouter).
"""
from __future__ import annotations
