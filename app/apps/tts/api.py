"""TTS endpoints: REST control + WebSocket status stream.

Mirrors ``app/api/video_detection.py``:

* ``GET  /api/tts``         — current snapshot
* ``POST /api/tts/start``   — kick off a synthesis run
* ``POST /api/tts/stop``    — request graceful stop
* ``GET  /api/tts/history`` — recent output WAVs
* ``GET  /api/tts/output``  — stream the WAV produced by the latest run
* ``WS   /api/tts/ws``      — pushes snapshots on state change
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from app.apps.tts.service import (
    PLAY_DEVICE,
    PLAY_TARGETS,
    TTSRequest,
    get_session,
)


router = APIRouter(tags=["tts"])


# Plain-print so diagnostics land in uvicorn's stdout regardless of
# how the root logger is configured.
def _tts_log(msg: str) -> None:
    print(f"[tts_api] {msg}", flush=True)


@router.get("/")
async def get_tts() -> dict:
    return get_session().snapshot()


@router.post("/start")
async def tts_start(payload: Optional[dict] = None) -> dict:
    """Body fields match ``TTSRequest``: ``text`` (required), ``language``,
    ``speaker``, ``ref_speaker`` (default ``"girl_base"``), ``instruct``,
    ``volume`` (0.0–1.0, default 0.1), ``play_to`` (``"device"`` /
    ``"browser"`` / ``"both"``, default ``"device"``), ``no_play``,
    ``non_streaming_mode``, ``output`` (auto-filled when empty).

    When ``play_to`` contains ``"browser"`` the WAV written to ``output``
    is served back via ``GET /api/tts/output?path=...`` so the page can
    play it through a hidden ``<audio>`` element after generation."""
    payload = payload or {}
    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text 不能为空")

    try:
        volume = float(payload.get("volume", 0.1))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="volume 必须为 0.0 ~ 1.0 之间的数")
    volume = max(0.0, min(1.0, volume))

    play_to = str(payload.get("play_to", "") or PLAY_DEVICE).strip().lower()
    if play_to not in PLAY_TARGETS:
        raise HTTPException(
            status_code=400,
            detail=f"play_to 必须是 {', '.join(PLAY_TARGETS)} 之一",
        )

    req = TTSRequest(
        text=text,
        language=str(payload.get("language", "auto") or "auto"),
        speaker=str(payload.get("speaker", "") or ""),
        ref_speaker=str(payload.get("ref_speaker", "girl_base") or "girl_base"),
        instruct=str(payload.get("instruct", "") or ""),
        volume=volume,
        no_play=bool(payload.get("no_play", False)),
        play_to=play_to,
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


@router.post("/stop")
async def tts_stop() -> dict:
    return get_session().stop()


@router.get("/history")
async def tts_history() -> dict:
    return {"items": get_session().history()}


@router.get("/output")
async def tts_output(path: str) -> FileResponse:
    """Stream the WAV the most recent (or any historical) run wrote.

    Constrained to the service's own ``uploads/`` directory so this
    endpoint can't be turned into an arbitrary-file proxy. The path is
    canonicalised and must resolve under ``app/services/uploads/``."""
    if not path:
        raise HTTPException(status_code=400, detail="path 必填")

    uploads_root = (Path(__file__).resolve().parent.parent /
                    "services" / "uploads").resolve()
    target = Path(path).resolve()
    try:
        target.relative_to(uploads_root)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"path 必须在 {uploads_root} 之下",
        )

    if not target.is_file():
        raise HTTPException(status_code=404, detail=f"文件不存在: {path}")
    if target.stat().st_size == 0:
        raise HTTPException(status_code=404, detail="输出为空")

    return FileResponse(
        path=str(target),
        media_type="audio/wav",
        filename=target.name,
        headers={"Cache-Control": "no-store"},
    )


# --------------------------------------------------------------------- #
# WebSocket status stream
# --------------------------------------------------------------------- #
@router.websocket("/ws")
async def tts_ws(ws: WebSocket) -> None:
    """Push TTS session snapshots on state change.

    Audio stream is opt-in: client sends ``{"type":"subscribe_audio"}``
    after connecting, then receives one ``audio_start`` JSON frame,
    followed by binary frames of 12-byte header
    ``(sr:u32, ch:u32, n:u32 little-endian)`` + ``n*ch*4`` bytes of
    float32 LE PCM, terminated by an ``audio_end`` JSON frame. The
    server's pump runs as a per-subscription background task; on
    pump exit it clears ``sub_id`` so the next ``subscribe_audio``
    always builds a fresh pump."""
    await ws.accept()
    sess = get_session()
    last_state: Optional[str] = None
    last_frames = -1
    last_msg: Optional[str] = None

    sub_id: Optional[str] = None
    audio_q: Optional[asyncio.Queue] = None
    audio_sr = 24000
    audio_ch = 1
    stop_event = asyncio.Event()

    async def _pump_audio() -> None:
        nonlocal sub_id, audio_q
        assert audio_q is not None
        try:
            await ws.send_json({
                "type": "audio_start",
                "sample_rate": audio_sr,
                "channels": audio_ch,
            })
            chunks_sent = 0
            while not stop_event.is_set():
                data = await audio_q.get()
                if data == b"EOF":
                    break
                try:
                    await ws.send_bytes(data)
                    chunks_sent += 1
                except Exception:
                    return
            try:
                await ws.send_json({"type": "audio_end"})
            except Exception:
                pass
            _tts_log(f"ws[{id(ws)}] audio_end ({chunks_sent} chunks)")
        finally:
            # Clear the sub so the NEXT subscribe_audio creates a
            # fresh pump; without this, the ``and sub_id is None``
            # guard in ``_client_msg_loop`` would short-circuit.
            if sub_id is not None:
                sess.unsubscribe_audio(sub_id)
            sub_id = None
            audio_q = None

    async def _client_msg_loop() -> None:
        nonlocal sub_id, audio_q
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                mtype = (msg or {}).get("type")
                if mtype == "ping":
                    try:
                        await ws.send_json({"type": "pong"})
                    except Exception:
                        return
                elif mtype == "subscribe_audio" and sub_id is None:
                    sub_id = f"ws-{id(ws)}"
                    audio_q = sess.subscribe_audio(
                        sub_id, asyncio.get_running_loop())
                    stop_event.clear()
                    _tts_log(f"ws[{id(ws)}] subscribe_audio sub_id={sub_id}")
                    asyncio.create_task(_pump_audio())
                elif mtype == "unsubscribe_audio" and sub_id is not None:
                    stop_event.set()
                    sess.unsubscribe_audio(sub_id)
                    _tts_log(f"ws[{id(ws)}] unsubscribe")
                    sub_id = None
                    audio_q = None
        except WebSocketDisconnect:
            return

    def _push_snap() -> dict:
        snap = sess.snapshot()
        snap["type"] = "status"
        return snap

    client_task = asyncio.create_task(_client_msg_loop())

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
    finally:
        stop_event.set()
        if sub_id is not None:
            sess.unsubscribe_audio(sub_id)
        client_task.cancel()
        try:
            await client_task
        except (asyncio.CancelledError, Exception):
            pass