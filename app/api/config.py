"""Config import/export + sectioned CRUD endpoints.

Whole-file:
- ``GET  /api/config``        — current snapshot + on-disk path (JSON body)
- ``GET  /api/config/export`` — download the raw ``config.json`` file
- ``POST /api/config/import`` — upload a new ``config.json`` (multipart
  file), validate it parses to a JSON object, persist atomically.

Per-section (``base`` for now):
- ``GET /api/config/base`` — read the ``base`` section (with defaults
  filled in for missing fields)
- ``PUT /api/config/base`` — replace the ``base`` section (other
  top-level sections like future ``power`` are untouched)

The runtime path is whatever ``PYRKAIWEB_CONFIG`` resolved to (see
``run.py --config``); export always serves that exact file.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

from app.services.config import get_config


router = APIRouter(tags=["config"])


# --- base section schema --------------------------------------------------
#
# Add new fields here when more基础配置 entries show up. The PUT endpoint
# validates with this model, so unknown fields from the client are
# silently dropped (Pydantic's default `extra="ignore"`).
#
# `model_path` is the on-device directory where downstream SDKs (e.g.
# rkai's RKNN model cache) look for weights. Services that consume it
# should re-read it from `get_config().get_section("base")["model_path"]`
# after each successful PUT so a save takes effect without a restart.

_BASE_DEFAULTS: dict[str, Any] = {
    "model_path": "/userdata/models/",
}


class BaseConfig(BaseModel):
    model_config = {"extra": "ignore"}

    model_path: str = Field(
        default=_BASE_DEFAULTS["model_path"],
        description="模型存放路径（设备上的绝对路径）",
    )

    @field_validator("model_path")
    @classmethod
    def _abs_path(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("model_path 不能为空")
        if not v.startswith("/"):
            raise ValueError("model_path 必须是绝对路径 (以 / 开头)")
        return v


@router.get("/config")
async def get_config_endpoint() -> dict:
    store = get_config()
    return {
        "path": str(store.path),
        "data": store.snapshot(),
    }


@router.get("/config/export")
async def export_config() -> FileResponse:
    """Serve the on-disk config.json as a download."""
    store = get_config()
    if not store.path.exists():
        # First-time boot with no file: write an empty object so the
        # download always succeeds rather than returning 404.
        store.replace({})
    return FileResponse(
        store.path,
        media_type="application/json",
        filename="config.json",
    )


@router.post("/config/import")
async def import_config(file: UploadFile = File(...)) -> dict:
    """Replace the on-disk config.json with the uploaded one.

    Validates: non-empty, valid UTF-8, parses as JSON, top-level value
    is a JSON object (lists / scalars are rejected so config stays a
    strict key→value bag).
    """
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="文件为空")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise HTTPException(status_code=400, detail=f"不是 UTF-8: {e}") from e
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"JSON 解析失败: {e}") from e
    if not isinstance(decoded, dict):
        raise HTTPException(
            status_code=400,
            detail="必须是顶层 JSON object（不允许 list / scalar）",
        )
    try:
        snap = get_config().replace(decoded)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except OSError as e:
        raise HTTPException(
            status_code=500, detail=f"写入失败: {e}"
        ) from e
    return {"ok": True, "data": snap, "path": str(get_config().path)}


@router.get("/config/base")
async def get_base_config() -> dict:
    """Return the ``base`` section, filling in defaults for any missing
    fields so the page always has a complete object to render."""
    store = get_config()
    stored = store.get_section("base", {})
    # Filter down to known fields (drop unknowns from prior versions).
    filtered = {k: v for k, v in stored.items() if k in _BASE_DEFAULTS}
    try:
        bc = BaseConfig(**filtered)
    except Exception:
        # Stored data is broken beyond defaults — fall back cleanly.
        bc = BaseConfig()
    section = bc.model_dump()
    # Soft existence check so the UI can warn without making it fatal.
    model_path = section.get("model_path") or ""
    exists = bool(model_path) and Path(model_path).exists()
    return {"section": section, "model_path_exists": exists}


@router.put("/config/base")
async def put_base_config(payload: BaseConfig) -> dict:
    """Replace the ``base`` section. Other top-level sections
    (e.g. future ``power`` settings) are preserved."""
    section = payload.model_dump()
    snap = get_config().set_section("base", section)
    model_path = section.get("model_path") or ""
    exists = bool(model_path) and Path(model_path).exists()
    return {
        "ok": True,
        "section": section,
        "model_path_exists": exists,
        "data": snap,
    }