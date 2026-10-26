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
import json
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from app.apps.tts.service import (
    PLAY_DEVICE,
    PLAY_TARGETS,
    TTSRequest,
    get_session,
    voice_catalog,
)


router = APIRouter(tags=["tts"])


# Plain-print so diagnostics land in uvicorn's stdout regardless of
# how the root logger is configured.
def _tts_log(msg: str) -> None:
    print(f"[tts_api] {msg}", flush=True)


@router.get("/")
async def get_tts() -> dict:
    return get_session().snapshot()


def _voice_catalog() -> dict:
    """音色 + 语种清单(取自 SDK 的表,不在前端硬编码)。"""
    try:
        return voice_catalog(get_session().model_dir)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"音色清单不可用: {e}")


@router.get("/voices")
async def tts_voices() -> dict:
    """可用音色 + 语种清单。前端下拉按这个渲染,不在页面里硬编码。"""
    return _voice_catalog()


@router.post("/start")
async def tts_start(payload: Optional[dict] = None) -> dict:
    """Body fields match ``TTSRequest``: ``text`` (required), ``language``,
    ``speaker``, ``ref_speaker`` (default ``"girl_base"``),
    ``volume`` (0.0–1.0, default 0.1), ``play_to`` (``"device"`` /
    ``"browser"`` / ``"both"``, default ``"device"``), ``no_play``,
    ``non_streaming_mode``, ``output`` (auto-filled when empty).

    ``instruct`` (风格提示) 只作 SDK 透传,**页面不暴露**: 官方能力表里
    Base 权重没有 instruction control,填了不改变语气。

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

    speaker = str(payload.get("speaker", "") or "").strip()
    ref_speaker = str(payload.get("ref_speaker", "") or "").strip()
    # 两个都不给才回落 girl_base —— 无条件补会把 speaker 的路堵死
    # (SDK 里 ref_speaker 优先级高于 speaker)。
    if not speaker and not ref_speaker:
        ref_speaker = "girl_base"

    # 音色必须是本模型真有的:Base 权重里内置音色那几行是未训练的噪声,
    # 塞给 talker 等于没给音色条件 → 每次生成自由发挥。按清单拒掉。
    avail = {v["id"] for v in _voice_catalog().get("voices", [])}
    for name, kind in ((speaker, "内置音色"), (ref_speaker, "参考音色")):
        if name and name not in avail:
            raise HTTPException(
                status_code=400,
                detail=f"本模型没有{kind} {name!r};"
                       f"当前可用: {', '.join(sorted(avail)) or '无'}",
            )

    req = TTSRequest(
        text=text,
        language=str(payload.get("language", "auto") or "auto"),
        speaker=speaker,
        ref_speaker=ref_speaker,
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

    Constrained to the TTS output directory so this endpoint can't be
    turned into an arbitrary-file proxy. The path is canonicalised
    and must resolve under ``<resource_dir>/tts``(典型设备端
    ``/userdata/test/tts``,off-device ``<HOME>/test/tts``;由
    :func:`app.core.uploads.get_tts_output_root` 解析,**不**走 repo
    内的 ``app/uploads/tts/`` —— 那个在 emmc 上,TTS 生成的音频写多了
    会踩爆 emmc 寿命)。"""
    if not path:
        raise HTTPException(status_code=400, detail="path 必填")

    from app.core.uploads import get_tts_output_root
    uploads_root = get_tts_output_root().resolve()
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
    # capability_ready 单独记一份:模型被启用/停用时**会话状态不变**
    # (state 仍是 idle),原来的变化判据看不到 → 前端拿不到新快照 →
    # 启动按钮一直是 disabled,直到下一次 5s 轮询才恢复。
    # 把「能力就绪」也纳入推送判据,这一拍延迟就没了。
    last_cap: Optional[bool] = None

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
        snap0 = sess.snapshot()
        last_state = snap0["state"]
        last_frames = snap0["frames"]
        last_msg = snap0["error"]
        last_cap = snap0.get("capability_ready")

        while True:
            snap = sess.snapshot()
            changed = (
                snap["state"] != last_state
                or snap["frames"] != last_frames
                or (snap["error"] or "") != (last_msg or "")
                or snap.get("capability_ready") != last_cap
            )
            if changed:
                await ws.send_json(_push_snap())
                last_state = snap["state"]
                last_frames = snap["frames"]
                last_msg = snap["error"]
                last_cap = snap.get("capability_ready")
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