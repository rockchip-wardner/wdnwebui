"""音频理解 REST + WS 端点。

错误码：400 (prompt/source 非法) / 404 (source 不存在) /
502 (capability 未启用) / 500 (其他)。

WS 协议：连接即推 ``hello``；状态变化推 ``state``；
推理完成推 ``event``；段落推进推 ``progress``。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from app.services.audio_analysis import get_service


LOG = logging.getLogger("wdnwebui.api.audio_analysis")
router = APIRouter(tags=["audio-analysis"])


class StartPayload(BaseModel):
    source: str = Field(..., min_length=1, description="视频文件绝对路径")
    prompt: str = Field(..., min_length=1, description="音频理解提示词")
    model_id: str | None = Field(
        default=None,
        description=("可选,从下拉框选的 swcap_audio_understand 实例 id;"
                     " 不传/空 = auto-pick (本机优先,旧行为)"),
    )


@router.get("/audio-analysis")
async def get_audio_analysis() -> dict:
    return get_service().snapshot()


@router.get("/audio-analysis/events")
async def list_audio_analysis_events() -> dict:
    svc = get_service()
    return {
        "snapshot": svc.snapshot(),
        "events":   svc.list_events(),
    }


@router.post("/audio-analysis/start")
async def start_audio_analysis(payload: StartPayload) -> dict:
    try:
        return get_service().start(
            payload.source, payload.prompt,
            model_id=payload.model_id,
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    except Exception as e:                                   # noqa: BLE001
        LOG.exception("start_audio_analysis failed")
        raise HTTPException(
            status_code=500,
            detail=f"{type(e).__name__}: {e}",
        ) from e


@router.post("/audio-analysis/stop")
async def stop_audio_analysis() -> dict:
    return get_service().stop()


@router.post("/audio-analysis/events/clear")
async def clear_audio_analysis_events() -> dict:
    get_service().clear_events()
    return {"ok": True}


@router.websocket("/audio-analysis/ws")
async def audio_analysis_ws(websocket: WebSocket) -> None:
    await websocket.accept()
    svc = get_service()
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=128)

    def _push(msg: dict) -> None:
        try:
            loop.call_soon_threadsafe(queue.put_nowait, msg)
        except Exception:                                     # noqa: BLE001
            pass

    svc.register_broadcaster(_push)
    try:
        # 连接即推快照，让前端不必再额外 GET 一次
        await websocket.send_text(json.dumps(
            {"type": "hello", **svc.snapshot()},
            ensure_ascii=False,
        ))
        while True:
            msg: Any = await queue.get()
            await websocket.send_text(json.dumps(msg, ensure_ascii=False))
    except WebSocketDisconnect:
        pass
    except Exception:                                         # noqa: BLE001
        LOG.exception("audio_analysis_ws loop error")
    finally:
        svc.unregister_broadcaster(_push)