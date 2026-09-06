"""compute_worker_lazy — deferred wdnai imports + session factory.

Imported by ``compute_worker.router`` on the first ``action="infer"``
or ``action="load"`` call. Keeping the imports out of module top-level
preserves the "compute_worker can be even if wdnai isn't installed"
behavior — same constraint as ``WdnaiDetector._load_wdnai``.

Worker-side construction differs from main-process:

  - The worker holds exactly ONE LLMSession (no queue, no locking).
  - Each ``infer`` action is a fresh request — we ``reset()`` between
    calls so KV cache state never leaks between requests from
    different wdnwebui threads/processes.
  - The payload schema mirrors ``app.services.detector._infer_sync``:
    ``{"prompt": str, "attachment": [<b64 image bytes>], ...}``.
    Conversion BGR-ndarray -> base64 happens on the main side
    (``RpcSessionProxy``); the worker only sees base64 strings.
"""
from __future__ import annotations

import base64
import json
import logging
from typing import Any, Optional

LOG = logging.getLogger("compute_worker_lazy")


_WDNAI_CACHE: dict = {}


def _load_wdnai() -> dict:
    """Lazily import wdnai. Cached after first call."""
    if _WDNAI_CACHE:
        return _WDNAI_CACHE
    try:
        from wdnai import (
            Attachment,
            Field,
            Generable,
            LLMSession,
            LMOutput,
            SystemLanguageModel,
        )
    except ImportError as e:
        LOG.exception("wdnai import failed")
        raise RuntimeError(
            f"wdnai import failed: {e}. "
            "compute_worker requires wdnai installed in the same venv."
        )
    _WDNAI_CACHE.update({
        "Attachment": Attachment,
        "Field": Field,
        "Generable": Generable,
        "LLMSession": LLMSession,
        "LMOutput": LMOutput,
        "SystemLanguageModel": SystemLanguageModel,
    })
    return _WDNAI_CACHE


# --------------------------------------------------------------------------- #
# Per-model preset (mirrors app/services/compute_registry.py MODEL_PRESETS)  #
# --------------------------------------------------------------------------- #
def _preset_for(model_id: str) -> Any:
    """Build the wdnai ModelSpec for ``model_id``. Same logic as
    ComputeRegistry.MODEL_PRESETS — kept in sync. New models only
    need adding here and to MODEL_PRESETS."""
    mod = _load_wdnai()
    SLM = mod["SystemLanguageModel"]
    if model_id == "qwen3-vl-4b":
        return SLM.qwen3_vl_4b()
    if model_id == "qwen3-tts":
        return SLM.qwen3_tts()
    if model_id == "qwen3-4b":
        from dataclasses import replace as _dc_replace
        spec = SLM.qwen3_4b()
        return _dc_replace(spec, max_context_len=16384).with_options(
            max_new_tokens=2048,
            keep_history=1,
            enable_thinking=True,
        )
    if model_id == "qwen3-vl-embedding-2b":
        # Embedding 走 RKNN3Lite 普通模型路径输出 2048 维归一化向量；
        # LLM 推理不走 token 生成。
        return SLM.qwen3_vl_embedding()
    if model_id == "qwen2.5-omni-3b":
        # Qwen2.5-Omni-3B（kind="omni"）。单次 run_audio 上限 6s，调用方切片。
        return SLM.qwen_2_5_omni_3b()
    if model_id == "LocateAnything-3b":
        # LocateAnything（kind="grounding"）。lazy 版本与 kind 1
        # (compute_worker._build_session) 的 preset 结构不同：返回
        # (kind, MODEL_PRESETS["LocateAnything-3b"]) 元组供上层分发。
        from app.services.compute_registry import MODEL_PRESETS
        return ("grounding", MODEL_PRESETS["LocateAnything-3b"])
    raise ValueError(f"unknown model_id: {model_id!r}")


# --------------------------------------------------------------------------- #
# Session factory                                                            #
# --------------------------------------------------------------------------- #
def build_session(model_id: str, npu_index: int):
    """Construct the LLMSession for one instance. Lazy-imports wdnai."""
    mod = _load_wdnai()
    spec = _preset_for(model_id)
    LOG.info("building LLMSession(model_id=%s, npu=%d, kind=%s)",
             model_id, npu_index, getattr(spec, "kind", "?"))
    return mod["LLMSession"](model=spec, npu=npu_index)


# --------------------------------------------------------------------------- #
# Inference dispatcher                                                       #
# --------------------------------------------------------------------------- #
def _attachment_from_b64(b64_str: str) -> Any:
    """Decode a base64 image string -> numpy ndarray. Lazy-imports cv2."""
    import base64 as _b64
    import numpy as _np
    raw = _b64.b64decode(b64_str)
    arr = _np.frombuffer(raw, dtype=_np.uint8)
    import cv2  # lazy
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("cv2.imdecode returned None")
    return img


def run_infer(session, payload: dict) -> dict:
    """Run one inference.

    Supported payload shapes (MVP):

      * Text-only / structured: ``{"prompt": str, "attachment": [str]}``
        — attachment[0] is a base64-encoded image (for VLM); text strings
        are appended as text attachments.

      * The reply schema mirrors what the original detector returned:
        ``{"text": str, "level": str, "tool_call": str|None,
            "bbox": [...], "crop_b64": str, ...}``.

    TODO Phase 3: port the full detector/TargetDoc/SceneFrame pipeline
    here. For now, return a minimal ``{"text": <raw model text>}``
    so the wiring is exercised end-to-end.
    """
    mod = _load_wdnai()
    Attachment = mod["Attachment"]

    prompt = payload.get("prompt", "")
    atts_meta = payload.get("attachment", [])  # list of strings

    attachments = []
    for item in atts_meta:
        if isinstance(item, str):
            # Heuristic: if it parses as base64 + decodes to image-bytes,
            # build an image attachment; otherwise treat as text.
            try:
                img = _attachment_from_b64(item)
                attachments.append(Attachment(img))
                continue
            except Exception:
                pass
            attachments.append(Attachment(item, kind="text"))
        elif isinstance(item, dict) and "data_b64" in item:
            img = _attachment_from_b64(item["data_b64"])
            attachments.append(Attachment(img))

    generating_name = payload.get("generating")
    generating_cls = None
    if generating_name:
        # Look up @Generable subclass by simple name (best-effort).
        try:
            generating_cls = mod["Generable"].__class__  # placeholder
            # The actual @Generable registry is in the user's process
            # space — not accessible here. For MVP, ignore generating.
        except Exception:
            pass

    import asyncio

    async def _call():
        # Reset KV between calls — fresh inference per request.
        try:
            session.reset()
        except Exception:
            pass
        kwargs = {}
        if generating_cls is not None:
            kwargs["generating"] = generating_cls
        return await session.respond(
            prompt=prompt or None,
            attachment=attachments,
            **kwargs,
        )

    try:
        result = asyncio.run(_call())
    except Exception as e:
        LOG.exception("session.respond failed")
        raise RuntimeError(f"session.respond failed: {type(e).__name__}: {e}") from e

    # Normalise result -> JSON-friendly dict.
    text = getattr(result, "text", None) or (result if isinstance(result, str) else None)
    return {
        "text": text,
        "level": getattr(result, "level", None),
        "tool_call": getattr(result, "tool_call", None),
        "bbox": getattr(result, "bbox", None),
        "crop_b64": getattr(result, "crop_b64", None),
    }