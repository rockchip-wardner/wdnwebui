"""视频目标定位 endpoints: REST 控制 + WebSocket 帧流 + LocatorDoc 检测。

页面结构与 ``app/api/video_target_detection.py`` 一致；区别仅：

* ``/video-target-localization`` 前缀；
* 后端 detector 用 ``get_localizer_detector()``（第 3 个 ``WdnaiDetector``
  单例，挂 ``LocatorDoc`` 多 bbox schema），prompt / 事件历史与音视频描述
  页 + 视频目标检测页相互独立；
* 检测走 ``session.respond(generating=LocatorDoc)`` 结构化输出直接拿
  ``boxes: List[BoundingBox]`` + ``text`` + ``level``，**不**挂任何
  ``@Callable`` 工具 —— 该页面没有"工具集合"概念，
  ``/video-target-localization/tools`` 端点也不存在；
* 本页固定 448×448 视觉变体（LocateAnythingBackend 单变体）且
  capability ``swcap_grounding`` 当前只有一个 model preset，prompt body
  仅 ``{prompt, every_n}``，**不**暴露 ``image_size`` / ``model_id`` 字段。
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from app.services.detector import get_localizer_detector
from app.services.video_session import get_session


router = APIRouter(tags=["video-target-localization"])


@router.get("/video-target-localization")
async def get_state() -> dict:
    return get_session().snapshot()


@router.post("/video-target-localization/open")
async def open_source(payload: dict) -> dict:
    source = (payload or {}).get("source")
    if not source or not isinstance(source, str):
        raise HTTPException(status_code=400, detail="source required")
    loop = bool((payload or {}).get("loop", False))
    try:
        return get_session().open(source, loop=loop)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/video-target-localization/control")
async def control(payload: dict) -> dict:
    action = (payload or {}).get("action")
    if action == "play":
        return get_session().play()
    if action == "pause":
        return get_session().pause()
    if action == "stop":
        return get_session().close()
    raise HTTPException(status_code=400, detail=f"unknown action: {action!r}")


@router.post("/video-target-localization/close")
async def close_source() -> dict:
    return get_session().close()


# --------------------------------------------------------------------- #
# LocatorDoc 检测                                                        #
# --------------------------------------------------------------------- #
@router.get("/video-target-localization/prompt")
async def get_prompt() -> dict:
    return get_localizer_detector().snapshot()


@router.post("/video-target-localization/prompt")
async def set_prompt(payload: dict) -> dict:
    """Start (or update) target-localization.

    Body: ``{prompt: str, every_n: int}``
    — ``prompt`` 必填；``every_n`` 选填，默认 30。

    本页**不**接受 ``image_size`` —— LocateAnythingBackend 视觉固定
    448×448 单变体，size picker 无意义。
    本页**不**接受 ``model_id`` —— capability ``swcap_grounding`` 当前
    只有 1 个 preset，model picker 无意义。

    旧版 ``enabled_tools`` 字段不再生效 —— 该页只走 ``LocatorDoc``
    结构化输出，不再挂任何 ``@Callable`` 工具。
    """
    payload = payload or {}
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt 不能为空")
    try:
        every_n = int(payload.get("every_n", 30))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="every_n 必须为整数")
    if every_n < 1:
        raise HTTPException(status_code=400, detail="every_n 必须 >= 1")
    try:
        # enabled_tools 显式置 [] — 该页不再挂工具。
        return get_localizer_detector().start(
            prompt, every_n=every_n, enabled_tools=[],
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/video-target-localization/prompt/stop")
async def stop_prompt() -> dict:
    return get_localizer_detector().stop()


@router.get("/video-target-localization/events")
async def list_events() -> dict:
    det = get_localizer_detector()
    return {
        "snapshot": det.snapshot(),
        "events": det.events(),
        "latest": det.latest(),
    }


@router.post("/video-target-localization/events/clear")
async def clear_events() -> dict:
    get_localizer_detector().clear()
    return {"ok": True}


# --------------------------------------------------------------------- #
# 诊断端点 (镜像 VTD): 暴露最近 N 次推理的诊断 trace                       #
# --------------------------------------------------------------------- #
@router.get("/video-target-localization/diagnostics")
async def get_diagnostics() -> dict:
    """Dump the detector's recent inference diagnostics ring buffer.

    字段约定见 ``app/services/detector.py`` ``WdnaiDetector.__init__``
    里 ``_diag_history`` 的注释。每条记录包含:
      ``ts`` / ``frame_pos`` / ``it_infer_ms`` / ``schema`` /
      ``result_type`` / ``text_len`` / ``text`` / ``exit_reason`` /
      ``events`` / ``bbox`` / ``crop_w`` / ``crop_h`` /
      ``schema_parse_failed`` / ``had_exception`` / ``exception_type`` /
      ``exception_msg`` / ``raw_text_first``

    返回 ``{"count": int, "items": [dict, ...]}``,新 → 旧。
    """
    det = get_localizer_detector()
    items = det.diag_history()
    # 倒序 (新 → 旧) 方便调试时先看最近的失败
    items_reversed = list(reversed(items))
    return {"count": len(items_reversed), "items": items_reversed}


@router.post("/video-target-localization/diagnostics/clear")
async def clear_diagnostics() -> dict:
    """Clear the diagnostics ring buffer (不影响 _events 历史)。"""
    get_localizer_detector().clear_diagnostics()
    return {"ok": True}


# --------------------------------------------------------------------- #
# WebSocket: server -> browser frame + event stream                     #
# --------------------------------------------------------------------- #
@router.websocket("/video-target-localization/ws")
async def stream_ws(ws: WebSocket) -> None:
    """Push decoded JPEG frames + LocatorDoc events to the browser.

    Protocol (server -> client):
      - JSON text: {"type":"status", ...session snapshot, detector snapshot...}
      - Binary:    raw JPEG bytes for one frame
      - JSON text: {"type":"eof"} when a file source reaches end
      - JSON text: {"type":"event", ...detector event...} when ``LocatorDoc``
                   fires (event.boxes 是 ``[{label, bbox, crop_b64,
                   crop_w, crop_h}, ...]`` 多框列表 + event.frame_b64 是
                   整帧 base64 JPEG + event.frame_w / event.frame_h 是整帧
                   像素尺寸 + event.text 是模型对整张图的描述文本)。
    """
    await ws.accept()
    session = get_session()
    detector = get_localizer_detector()
    loop = asyncio.get_event_loop()
    last_status = 0.0
    last_event_id = 0
    eof_sent = False
    try:
        while True:
            for ev in detector.events_since(last_event_id):
                last_event_id = ev["id"]
                await ws.send_json({"type": "event", **ev})

            kind, data = await loop.run_in_executor(
                None, lambda: session.fetch_frame(timeout=0.05)
            )
            if kind == "frame":
                eof_sent = False
                await send_frame_bytes(ws, data)
                continue
            if kind == "eof":
                if not eof_sent:
                    snap = session.snapshot()
                    snap["type"] = "eof"
                    await ws.send_json(snap)
                    eof_sent = True
                await asyncio.sleep(0.2)
                continue
            now = loop.time()
            if now - last_status >= 1.0:
                snap = session.snapshot()
                snap["type"] = "status"
                snap["detector"] = detector.snapshot()
                await ws.send_json(snap)
                last_status = now
            await asyncio.sleep(0.02)
    except WebSocketDisconnect:
        return
    except Exception as e:  # pragma: no cover - defensive
        try:
            await ws.send_json({"type": "error", "detail": str(e)})
        except Exception:
            pass


async def send_frame_bytes(ws, data: bytes) -> None:
    """Send JPEG bytes — small wrapper so tests / overrides can stub."""
    await ws.send_bytes(data)