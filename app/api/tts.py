"""TTS endpoints: REST control + WebSocket status stream.

Mirrors ``app/api/video_detection.py``:

* ``GET  /api/tts``         — current snapshot
* ``POST /api/tts/start``   — kick off a synthesis run
* ``POST /api/tts/stop``    — request graceful stop
* ``GET  /api/tts/history`` — recent output WAVs
* ``WS   /api/tts/ws``      — pushes snapshots on state change
"""
from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from app.services.tts_session import TTSRequest, get_session


router = APIRouter(tags=["tts"])


@router.get("/tts")
async def get_tts() -> dict:
    return get_session().snapshot()


@router.post("/tts/start")
async def tts_start(payload: Optional[dict] = None) -> dict:
    """Body fields match ``TTSRequest``: ``text`` (required), ``language``,
    ``speaker``, ``ref_speaker`` (default ``"girl_base"``), ``instruct``,
    ``volume`` (0.0–1.0, default 0.1), ``no_play``, ``non_streaming_mode``,
    ``output`` (auto-filled when empty)."""
    payload = payload or {}
    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text 不能为空")

    try:
        volume = float(payload.get("volume", 0.1))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="volume 必须为 0.0 ~ 1.0 之间的数")
    volume = max(0.0, min(1.0, volume))

    req = TTSRequest(
        text=text,
        language=str(payload.get("language", "auto") or "auto"),
        speaker=str(payload.get("speaker", "") or ""),
        ref_speaker=str(payload.get("ref_speaker", "girl_base") or "girl_base"),
        instruct=str(payload.get("instruct", "") or ""),
        volume=volume,
        no_play=bool(payload.get("no_play", False)),
        output=str(payload.get("output", "") or ""),
        non_streaming_mode=bool(payload.get("non_streaming_mode", False)),
    )

    try:
        return get_session().start(req)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        # Service unavailable (upstream missing / busy)
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/tts/stop")
async def tts_stop() -> dict:
    return get_session().stop()


@router.get("/tts/history")
async def tts_history() -> dict:
    return {"items": get_session().history()}


# --------------------------------------------------------------------- #
# WebSocket status stream
# --------------------------------------------------------------------- #
@router.websocket("/tts/ws")
async def tts_ws(ws: WebSocket) -> None:
    """Push TTS session snapshots on state change. Server sends JSON
    ``{"type": "status", ...snapshot...}`` frames only — no binary PCM
    frames (audio is consumed by aplay / sounddevice on the server)."""
    await ws.accept()
    sess = get_session()
    last_state: Optional[str] = None
    last_frames = -1
    last_msg: Optional[str] = None

    def _push_snap() -> dict:
        snap = sess.snapshot()
        snap["type"] = "status"
        return snap

    # Send an initial snapshot so the page can render immediately.
    try:
        await ws.send_json(_push_snap())
        last_state = sess.snapshot()["state"]
        last_frames = sess.snapshot()["frames"]
        last_msg = sess.snapshot()["error"]

        while True:
            snap = sess.snapshot()
            changed = (
                snap["state"] != last_state
                or snap["frames"] != last_frames
                or (snap["error"] or "") != (last_msg or "")
            )
            if changed:
                await ws.send_json(_push_snap())
                last_state = snap["state"]
                last_frames = snap["frames"]
                last_msg = snap["error"]
            await asyncio.sleep(0.2)
    except WebSocketDisconnect:
        return
    except Exception as e:                       # pragma: no cover
        try:
            await ws.send_json({"type": "error", "detail": str(e)})
        except Exception:
            pass