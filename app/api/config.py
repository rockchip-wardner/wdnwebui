"""Config import/export + sectioned CRUD endpoints.

Whole-file:
- ``GET  /api/config``        — current snapshot + on-disk path (JSON body)
- ``GET  /api/config/export`` — download the raw ``config.jsonc`` file
- ``POST /api/config/import`` — upload a new ``config.jsonc`` (multipart
  file), validate it parses to a JSON object, persist atomically.

Per-section (``base`` for now):
- ``GET /api/config/base`` — read the ``base`` section (with defaults
  filled in for missing fields)
- ``PUT /api/config/base`` — replace the ``base`` section (other
  top-level sections like future ``power`` are untouched)

The runtime path is whatever ``WDNWEBUI_CONFIG`` resolved to (see
``run.py --config``); export always serves that exact file.
"""
from __future__ import annotations

import json
import secrets
import string
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

from app.services.config import get_config
from app.services.jsonc import parse_jsonc


router = APIRouter(tags=["config"])


_DEVICE_NAME_ALPHABET = string.ascii_letters + string.digits
_DEVICE_NAME_LENGTH = 8


def _random_device_name() -> str:
    """8-char alphanumeric identifier (mixed case + digits)."""
    return "".join(
        secrets.choice(_DEVICE_NAME_ALPHABET)
        for _ in range(_DEVICE_NAME_LENGTH)
    )


# --- base section schema --------------------------------------------------
#
# Add new fields here when more基础配置 entries show up. The PUT endpoint
# validates with this model, so unknown fields from the client are
# silently dropped (Pydantic's default `extra="ignore"`).
#
# `device_name` is a human-readable identifier for this device. The
# default is generated once (random 8-char alphanumeric) the first time
# the `base` section is read without one, then persisted to
# ``config.jsonc`` so the value stays stable across reads and reboots.
# Users can overwrite it via PUT /api/config/base.
#
# `model_path` is the on-device directory where downstream SDKs (e.g.
# wdnai's RKNN model cache) look for weights. Services that consume it
# should re-read it from `get_config().get_section("base")["model_path"]`
# after each successful PUT so a save takes effect without a restart.
#
# `resource_dir` is the on-device directory that downstream UIs (e.g. the
# video-detection file picker) use as the root for server-side browsing.
# Same lazy re-read contract as `model_path`.

_BASE_DEFAULTS: dict[str, Any] = {
    "device_name": "",  # filled lazily by _generate_missing_defaults()
    "model_path": "/userdata/models/",
    "resource_dir": "/userdata",
}


class BaseConfig(BaseModel):
    model_config = {"extra": "ignore", "validate_default": True}

    device_name: str = Field(
        default="",
        description="设备名（默认首次读取时随机生成）",
    )

    model_path: str = Field(
        default=_BASE_DEFAULTS["model_path"],
        description="模型存放路径（设备上的绝对路径）",
    )

    resource_dir: str = Field(
        default=_BASE_DEFAULTS["resource_dir"],
        description="资源存放目录（设备上的绝对路径，视频检测浏览等场景使用）",
    )

    @field_validator("device_name")
    @classmethod
    def _device_name_nonempty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("device_name 不能为空")
        return v

    @field_validator("model_path")
    @classmethod
    def _abs_path(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("model_path 不能为空")
        if not v.startswith("/"):
            raise ValueError("model_path 必须是绝对路径 (以 / 开头)")
        return v

    @field_validator("resource_dir")
    @classmethod
    def _abs_resource_dir(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("resource_dir 不能为空")
        if not v.startswith("/"):
            raise ValueError("resource_dir 必须是绝对路径 (以 / 开头)")
        return v


# --- app-prompts section schema -------------------------------------------
#
# Per-app Pydantic schema field descriptions (the natural-language hints
# baked into ``Field(description=...)``) are user-editable from the
# 系统配置 page. Defaults live in ``app/services/detector.py``
# (``DEFAULT_*_DESC``). Three-layer nesting on disk:
#
#     config.jsonc["compute"]["app_prompts"][<app_key>][<schema_cls>][<field>]
#
# Adding a new app's prompts:
#   1. Define a new ``<Schema>Prompts`` class with ``field_validator``
#      enforcing ≤ 4096 chars per non-empty string field (empty strings
#      are allowed and mean "use the detector-side default").
#   2. Register the sub-shape check in
#      ``AppPromptsConfig._check_*`` so PUT enforces it (422 on bad input).
#   3. In ``app/services/detector.py`` add a ``DEFAULT_<...>_DESC``
#      constant + thread the user value through ``_build_*_class``.
#
# ``TargetDoc`` coordinate descriptions (x1 / y1 / x2 / y2) are
# deliberately NOT exposed — they are protocol-level format specs
# (0–1000 normalized ROI bounds), not natural language, so they
# remain hardcoded inside ``detector._build_target_doc_class()``.

# Upper bound for a single field description (chosen to comfortably
# exceed any reasonable one-liner but bound memory / UI render cost).
_PROMPT_DESC_MAX_LEN = 4096


def _check_prompt_desc(v: str, field_label: str) -> str:
    """Shared validator body for prompt-description string fields.
    Strips whitespace; allows empty strings (interpreted as "use the
    detector-side default" by :func:`get_app_prompts` /
    :func:`put_app_prompts`); rejects oversized non-empty values
    (raises ``ValueError`` so Pydantic surfaces a 422)."""
    v = v.strip()
    if v and len(v) > _PROMPT_DESC_MAX_LEN:
        raise ValueError(
            f"{field_label} 描述不能超过 {_PROMPT_DESC_MAX_LEN} 字符"
        )
    return v


class VLMDocPrompts(BaseModel):
    """Field descriptions for the ``VLMDoc`` schema used by the
    音视频描述 (video-detection) page.

    Both fields correspond 1:1 to the ``text`` / ``level`` attributes
    on ``VLMDoc``. The detector rebuilds the schema class on
    ``start()`` when these values change, so saving here is picked
    up on the next detection launch (no hot-reload inside a running
    worker)."""

    model_config = {"extra": "ignore", "validate_default": True}

    text: str = Field(
        default="",
        description="VLMDoc.text 字段的自然语言描述（指导模型如何描述画面）",
    )
    level: str = Field(
        default="",
        description="VLMDoc.level 字段的自然语言描述（事件严重等级）",
    )

    @field_validator("text", "level")
    @classmethod
    def _bounded(cls, v: str) -> str:
        # field_label is the same for both — keep the error generic
        # so the UI doesn't have to special-case each field.
        return _check_prompt_desc(v, "VLMDoc 字段")


class TargetDocPrompts(BaseModel):
    """Field descriptions for the ``TargetDoc`` schema used by the
    视频目标检测 (video-target-detection) page.

    Only the ``desc`` description is user-editable. The 4 coordinate
    descriptions (``x1`` / ``y1`` / ``x2`` / ``y2``) are protocol-
    level format specs (0–1000 normalized ROI bounds) and are
    intentionally hardcoded inside
    ``detector._build_target_doc_class()`` — adding them here would
    expose implementation details that should never change per
    device."""

    model_config = {"extra": "ignore", "validate_default": True}

    desc: str = Field(
        default="",
        description=(
            "TargetDoc.desc 字段的自然语言描述"
            "（对目标 ROI 框出区域的内容描述）"
        ),
    )

    @field_validator("desc")
    @classmethod
    def _bounded(cls, v: str) -> str:
        return _check_prompt_desc(v, "TargetDoc 字段")


class AppPromptsConfig(BaseModel):
    """Top-level container for per-app Pydantic schema field
    descriptions. Each app section is a loose ``Dict[str, Any]`` so
    new schema-class entries (e.g. an audio-understanding schema) can
    be added without bumping this model. Known sub-shapes
    (``VLMDoc`` under ``video_detection`` and ``TargetDoc`` under
    ``video_target_detection``) are still validated against
    :class:`VLMDocPrompts` / :class:`TargetDocPrompts` so a malformed
    field surfaces as 422 at the API boundary instead of leaking
    bad data into the detector cache.

    Empty-string leaf fields are accepted (interpreted as "use the
    detector-side default") but get pruned from the persisted
    payload in :func:`put_app_prompts` via
    :func:`_strip_empty_strings` so they never overwrite an existing
    user value nor appear as ``""`` in ``config.jsonc``."""

    model_config = {"extra": "ignore", "validate_default": True}

    video_detection: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "音视频描述页 schema 字段描述，"
            "key 为 schema 类名（如 VLMDoc），value 为字段名→描述"
        ),
    )
    video_target_detection: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "视频目标检测页 schema 字段描述，"
            "key 为 schema 类名（如 TargetDoc），value 为字段名→描述"
        ),
    )

    @field_validator("video_detection")
    @classmethod
    def _check_video_detection(
        cls, v: Optional[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        if v is None:
            return v
        if not isinstance(v, dict):
            raise ValueError("video_detection 必须是 dict")
        # Validate the well-known VLMDoc sub-shape against
        # VLMDocPrompts (422 on bad leaf fields) and replace it with
        # the cleaned dump so extras (e.g. junk the user pasted in)
        # don't leak into the persisted config — Pydantic's
        # ``extra="ignore"`` only applies inside the sub-model, not
        # to the outer ``Dict[str, Any]``. Unknown top-level keys
        # (e.g. a future schema class) are preserved so this
        # endpoint stays schema-flexible.
        cleaned: dict[str, Any] = dict(v)
        if "VLMDoc" in cleaned and isinstance(cleaned["VLMDoc"], dict):
            cleaned["VLMDoc"] = VLMDocPrompts(**cleaned["VLMDoc"]).model_dump()
        return cleaned

    @field_validator("video_target_detection")
    @classmethod
    def _check_video_target_detection(
        cls, v: Optional[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        if v is None:
            return v
        if not isinstance(v, dict):
            raise ValueError("video_target_detection 必须是 dict")
        # Same pattern as ``_check_video_detection`` — validate the
        # TargetDoc sub-shape and replace it with the cleaned dump so
        # coordinate fields (x1 / y1 / x2 / y2) the user might paste
        # in by mistake never get persisted (TargetDocPrompts has
        # ``extra="ignore"`` but the outer Dict does not).
        cleaned: dict[str, Any] = dict(v)
        if "TargetDoc" in cleaned and isinstance(cleaned["TargetDoc"], dict):
            cleaned["TargetDoc"] = TargetDocPrompts(
                **cleaned["TargetDoc"]
            ).model_dump()
        return cleaned


def _generate_missing_defaults(stored: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``stored`` with any missing defaultable fields
    filled in. Persists the result so subsequent reads see the same
    value (e.g. the randomly-generated device_name stays stable)."""
    merged = dict(stored)
    changed = False
    if not str(merged.get("device_name") or "").strip():
        merged["device_name"] = _random_device_name()
        changed = True
    # Persist the other defaults too so config.jsonc is a complete record
    # of what's actually in effect (otherwise a fresh install would only
    # see `device_name` written, with the rest living only as Pydantic
    # defaults invisible on disk).
    for key, default in _BASE_DEFAULTS.items():
        if key == "device_name":
            continue  # handled above with the random fill
        if key not in merged or not str(merged.get(key) or "").strip():
            merged[key] = default
            changed = True
    if changed:
        # Keep any other (unknown) keys the user has stored so we don't
        # silently drop them on first-touch upgrade.
        get_config().set_section("base", merged)
    return merged


@router.get("/config")
async def get_config_endpoint() -> dict:
    store = get_config()
    return {
        "path": str(store.path),
        "data": store.snapshot(),
    }


@router.get("/config/export")
async def export_config() -> FileResponse:
    """Serve the on-disk config.jsonc as a download."""
    store = get_config()
    if not store.path.exists():
        # First-time boot with no file: write an empty object so the
        # download always succeeds rather than returning 404.
        store.replace({})
    return FileResponse(
        store.path,
        media_type="application/json",
        filename="config.jsonc",
    )


@router.post("/config/import")
async def import_config(file: UploadFile = File(...)) -> dict:
    """Replace the on-disk config.jsonc with the uploaded one.

    Validates: non-empty, valid UTF-8, parses as JSONC, top-level value
    is a JSON object (lists / scalars are rejected so config stays a
    strict key→value bag). JSONC means ``//`` and ``/* */`` comments
    are accepted on the way in; user-written comments are *not*
    preserved through the import (the next save will discard them).
    """
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="文件为空")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise HTTPException(status_code=400, detail=f"不是 UTF-8: {e}") from e
    try:
        decoded = parse_jsonc(text)
    except ValueError as e:
        # json5 raises ValueError on parse failure; this catches both
        # standard-JSON syntax errors and JSONC-only constructs gone wrong.
        raise HTTPException(status_code=400, detail=f"JSONC 解析失败: {e}") from e
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
    # Lazily materialize defaults (random device_name) and persist so the
    # value stays stable across reads / reboots.
    filtered = _generate_missing_defaults(filtered)
    try:
        bc = BaseConfig(**filtered)
    except Exception:
        # Stored data is broken beyond defaults — fall back cleanly.
        bc = BaseConfig()
        # Still try to backfill the random device_name into the broken
        # section so the page has something to render.
        if not (bc.device_name or "").strip():
            bc = BaseConfig(
                device_name=_random_device_name(),
                model_path=bc.model_path,
            )
    section = bc.model_dump()
    # Soft existence check so the UI can warn without making it fatal.
    model_path = section.get("model_path") or ""
    resource_dir = section.get("resource_dir") or ""
    return {
        "section": section,
        "model_path_exists": bool(model_path) and Path(model_path).exists(),
        "resource_dir_exists": bool(resource_dir) and Path(resource_dir).exists(),
    }


@router.put("/config/base")
async def put_base_config(payload: BaseConfig) -> dict:
    """Replace the ``base`` section. Other top-level sections
    (e.g. future ``power`` settings) are preserved."""
    section = payload.model_dump()
    snap = get_config().set_section("base", section)
    model_path = section.get("model_path") or ""
    resource_dir = section.get("resource_dir") or ""
    return {
        "ok": True,
        "section": section,
        "model_path_exists": bool(model_path) and Path(model_path).exists(),
        "resource_dir_exists": bool(resource_dir) and Path(resource_dir).exists(),
        "data": snap,
    }


def _app_prompts_leaf(
    app_prompts: dict[str, Any], app_key: str, schema_cls: str, field: str
) -> str:
    """Read a single leaf string from the prompts block.

    Walks ``app_prompts → app_key → schema_cls → field`` defensively
    (every layer can be absent / wrong-typed if the config was hand-
    edited or carried over from a prior schema). Returns ``""`` for
    any missing or non-string node so the GET response always has a
    fully-populated leaf string."""
    cur: Any = app_prompts
    for key in (app_key, schema_cls, field):
        if not isinstance(cur, dict):
            return ""
        cur = cur.get(key)
    if not isinstance(cur, str):
        return ""
    return cur.strip()


def _read_app_prompts() -> dict[str, Any]:
    """Return the current prompts dict (always a dict, may be empty).

    Read path prefers ``app.prompts`` (new layout, v0.11+); falls back
    to ``compute.app_prompts`` for files carried over from older
    releases so a fresh boot doesn't lose user overrides. The fallback
    is intentionally read-only — ``put_app_prompts`` always writes to
    ``app.prompts``, which on next save replaces the stale
    ``compute.app_prompts`` cleanly (it's a top-level set_section on
    the whole dict).
    """
    cfg_store = get_config()
    # New path
    app_cfg = cfg_store.get_section("app", {}) or {}
    prompts_raw = app_cfg.get("prompts")
    if isinstance(prompts_raw, dict) and prompts_raw:
        return prompts_raw
    # Backward-compat fallback
    compute_cfg = cfg_store.get_section("compute", {}) or {}
    prompts_raw = compute_cfg.get("app_prompts")
    return prompts_raw if isinstance(prompts_raw, dict) else {}


def _write_app_prompts(prompts: dict[str, Any]) -> None:
    """Persist the prompts dict to ``app.prompts``.

    Writes the whole ``app`` section back via ``set_section("app", ...)``
    so sibling app-level keys (added later) are preserved.
    """
    cfg_store = get_config()
    app_section = cfg_store.get_section("app", {}) or {}
    if not isinstance(app_section, dict):
        app_section = {}
    app_section = dict(app_section)
    app_section["prompts"] = json.loads(json.dumps(prompts))
    cfg_store.set_section("app", app_section)


def _deep_merge_dict(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Return a new dict with ``overlay`` deep-merged into ``base``.

    Dict-on-dict recurses so sibling keys at every layer are preserved
    (a PUT that only updates ``video_detection.VLMDoc.text`` must not
    wipe ``video_detection.VLMDoc.level`` or
    ``video_target_detection``). Non-dict values are replaced wholesale
    (no list-element merge). ``base`` is deep-copied so the caller's
    object is untouched.
    """
    out: dict[str, Any] = json.loads(json.dumps(base))
    for k, v in overlay.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge_dict(out[k], v)
        else:
            out[k] = v
    return out


def _strip_empty_strings(d: dict[str, Any]) -> dict[str, Any]:
    """Return a new dict with empty-string leaf values removed.

    Walks ``d`` recursively and prunes any leaf whose value is
    ``""`` (after the upstream Pydantic validator strips it). Dict
    subtrees are recursed into and replaced with their pruned form;
    non-dict non-string values are kept as-is. The empty-string
    sentinel in the PUT payload means "use the detector-side
    default" (see :func:`put_app_prompts`), so dropping it from the
    payload prevents both (a) overwriting an existing user value
    with ``""`` during deep-merge and (b) leaving a literal ``""
    key behind in ``config.jsonc``.

    Example:
        ``{"text": "X", "level": ""}`` → ``{"text": "X"}``
    """
    out: dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out[k] = _strip_empty_strings(v)
        elif v == "":
            continue
        else:
            out[k] = v
    return out


def _effective_app_prompts_section(
    app_prompts: dict[str, Any],
    default_text: str,
    default_level: str,
    default_desc: str,
) -> dict[str, Any]:
    """Return the GET/PUT response shape with defaults filled in for
    every empty / missing leaf.

    Reads via :func:`_app_prompts_leaf` (which returns ``""`` for any
    missing node) and substitutes each leaf's detector-side default
    when the read value strips to empty, so the API always surfaces
    the "effective" value the model will use. Shared by
    :func:`get_app_prompts` and :func:`put_app_prompts` so the two
    endpoints can't drift apart."""
    text_val = _app_prompts_leaf(
        app_prompts, "video_detection", "VLMDoc", "text"
    )
    level_val = _app_prompts_leaf(
        app_prompts, "video_detection", "VLMDoc", "level"
    )
    desc_val = _app_prompts_leaf(
        app_prompts, "video_target_detection", "TargetDoc", "desc"
    )
    return {
        "video_detection": {
            "VLMDoc": {
                "text": text_val if text_val.strip() else default_text,
                "level": level_val if level_val.strip() else default_level,
            },
        },
        "video_target_detection": {
            "TargetDoc": {
                "desc": desc_val if desc_val.strip() else default_desc,
            },
        },
    }


@router.get("/config/app-prompts")
async def get_app_prompts() -> dict:
    """Return the prompts section as a fully-populated object — every
    known leaf is always present, and missing or empty leaves are
    filled with the detector-side defaults from
    :mod:`app.services.detector` so the page always sees the
    "effective" values the model will actually use.

    Read path is ``app.prompts`` (v0.11+); falls back to
    ``compute.app_prompts`` for configs from older releases.

    Shape:

        {
          "section": {
            "video_detection":         {"VLMDoc":   {"text": str, "level": str}},
            "video_target_detection":  {"TargetDoc": {"desc": str}},
          }
        }

    Defaults:

      * ``video_detection.VLMDoc.text``  ← ``DEFAULT_VLM_DOC_TEXT_DESC``
      * ``video_detection.VLMDoc.level`` ← ``DEFAULT_VLM_DOC_LEVEL_DESC``
      * ``video_target_detection.TargetDoc.desc`` ← ``DEFAULT_TARGET_DOC_DESC_DESC``

    Implementation note: the default constants are imported lazily
    inside this function body to break the
    ``app.api.config ↔ app.services.detector`` import cycle
    (``detector`` already imports :func:`get_config` from
    ``app.services.config``). Lazy imports keep both sides of the
    cycle loadable."""

    # Lazy import to break the app.api.config ↔ app.services.detector
    # cycle — see implementation note above.
    from app.services.detector import (
        DEFAULT_TARGET_DOC_DESC_DESC,
        DEFAULT_VLM_DOC_LEVEL_DESC,
        DEFAULT_VLM_DOC_TEXT_DESC,
    )

    return {
        "section": _effective_app_prompts_section(
            _read_app_prompts(),
            DEFAULT_VLM_DOC_TEXT_DESC,
            DEFAULT_VLM_DOC_LEVEL_DESC,
            DEFAULT_TARGET_DOC_DESC_DESC,
        ),
    }


@router.put("/config/app-prompts")
async def put_app_prompts(payload: AppPromptsConfig) -> dict:
    """Deep-merge ``payload`` into ``config.jsonc["app"]["prompts"]``
    and persist the updated ``app`` section. Other app subsections
    (added in the future) are preserved; sibling apps in ``prompts``
    are preserved too — a PUT that only mentions ``video_detection``
    never wipes ``video_target_detection``.

    Validation:

      * ``AppPromptsConfig`` field_validators enforce ≤ 4096 chars per
        non-empty leaf string and reject malformed ``VLMDoc`` /
        ``TargetDoc`` sub-shapes — bad input surfaces as HTTP 422.
        Empty strings are accepted and mean "use the detector-side
        default" (see :func:`get_app_prompts`).
      * Empty / missing app_keys are dropped via
        ``model_dump(exclude_none=True)`` so they neither overwrite
        existing data nor get persisted as ``None``.
      * Empty-string leaf fields are recursively pruned via
        :func:`_strip_empty_strings` so a PUT like
        ``{"text": "X", "level": ""}`` only persists ``"text": "X"``
        (no ``"level"`` key) — the existing ``level`` value (if any)
        is preserved by the deep-merge.

    Returns the same shape as :func:`get_app_prompts` (with the same
    effective-value substitution so PUT and GET can't drift) plus
    ``data=<full snapshot>`` (matches ``put_base_config``)."""
    # Lazy import to break the app.api.config ↔ app.services.detector
    # cycle — same as :func:`get_app_prompts`.
    from app.services.detector import (
        DEFAULT_TARGET_DOC_DESC_DESC,
        DEFAULT_VLM_DOC_LEVEL_DESC,
        DEFAULT_VLM_DOC_TEXT_DESC,
    )

    existing_prompts = _read_app_prompts()
    incoming = _strip_empty_strings(payload.model_dump(exclude_none=True))
    merged_prompts = _deep_merge_dict(existing_prompts, incoming)

    _write_app_prompts(merged_prompts)

    snap = get_config().snapshot()
    return {
        "ok": True,
        "section": _effective_app_prompts_section(
            merged_prompts,
            DEFAULT_VLM_DOC_TEXT_DESC,
            DEFAULT_VLM_DOC_LEVEL_DESC,
            DEFAULT_TARGET_DOC_DESC_DESC,
        ),
        "data": snap,
    }


@router.delete("/config/app-prompts")
async def delete_app_prompts() -> dict:
    """Clear all per-app prompt overrides from config.

    Removes ``app.prompts`` from ``config.jsonc`` so the GET
    endpoint falls back to the detector-side defaults. Other app
    subsections (added in the future) are preserved.

    The response shape mirrors :func:`put_app_prompts` (same
    effective-section helper) so the UI can re-render the form without
    a second round-trip; on the already-clean path the helper is
    invoked against an empty dict, which yields exactly the
    detector-side defaults.
    """
    # Lazy import to break the app.api.config ↔ app.services.detector
    # cycle — same as :func:`get_app_prompts` / :func:`put_app_prompts`.
    from app.services.detector import (
        DEFAULT_TARGET_DOC_DESC_DESC,
        DEFAULT_VLM_DOC_LEVEL_DESC,
        DEFAULT_VLM_DOC_TEXT_DESC,
    )

    store = get_config()
    app_section = store.get_section("app", {})
    has_app_prompts = (
        isinstance(app_section, dict)
        and "prompts" in app_section
    )
    # Also check the legacy path so a stale compute.app_prompts gets
    # wiped on DELETE — matches the GET read fallback semantics.
    compute_section = store.get_section("compute", {})
    has_legacy_compute = (
        isinstance(compute_section, dict)
        and "app_prompts" in compute_section
    )

    if not has_app_prompts and not has_legacy_compute:
        # Already clean — skip the disk write and just return the
        # effective (default) section.
        return {
            "ok": True,
            "section": _effective_app_prompts_section(
                {},
                DEFAULT_VLM_DOC_TEXT_DESC,
                DEFAULT_VLM_DOC_LEVEL_DESC,
                DEFAULT_TARGET_DOC_DESC_DESC,
            ),
        }

    # Wipe both: app.prompts (new) and compute.app_prompts (legacy)
    # so the next GET is forced onto the default path.
    if has_app_prompts and isinstance(app_section, dict):
        new_app = dict(app_section)
        new_app.pop("prompts", None)
        store.set_section("app", new_app)
    if has_legacy_compute and isinstance(compute_section, dict):
        new_compute = dict(compute_section)
        new_compute.pop("app_prompts", None)
        store.set_section("compute", new_compute)

    snap = store.snapshot()
    return {
        "ok": True,
        "section": _effective_app_prompts_section(
            {},
            DEFAULT_VLM_DOC_TEXT_DESC,
            DEFAULT_VLM_DOC_LEVEL_DESC,
            DEFAULT_TARGET_DOC_DESC_DESC,
        ),
        "data": snap,
    }