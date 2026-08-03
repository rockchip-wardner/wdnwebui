"""视频目标检测 endpoints: REST 控制 + WebSocket 帧流 + annotate_roi 检测。

页面结构与 ``app/api/video_detection.py`` 一致；区别仅：
* ``/video-target-detection`` 前缀；
* 后端 detector 用 ``get_target_detector()``，prompt / enabled_tools / 事件
  历史与音视频描述页相互独立；
* 工具默认只有 ``annotate_roi``（接口强制传入 / 忽略）。
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from app.services.detector import get_target_detector
from app.services.video_session import get_session


router = APIRouter(tags=["video-target-detection"])

# 视频目标检测页只允许「目标标注」工具。其他工具即便注册表里有也不
# 在这里暴露 —— 该页面是面向「目标检测+红框标注」场景的固定工具集。
ALLOWED_TOOLS = ("annotate_roi",)


@router.get("/video-target-detection")
async def get_state() -> dict:
    return get_session().snapshot()


@router.post("/video-target-detection/open")
async def open_source(payload: dict) -> dict:
    source = (payload or {}).get("source")
    if not source or not isinstance(source, str):
        raise HTTPException(status_code=400, detail="source required")
    loop = bool((payload or {}).get("loop", False))
    try:
        return get_session().open(source, loop=loop)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/video-target-detection/control")
async def control(payload: dict) -> dict:
    action = (payload or {}).get("action")
    if action == "play":
        return get_session().play()
    if action == "pause":
        return get_session().pause()
    if action == "stop":
        return get_session().close()
    raise HTTPException(status_code=400, detail=f"unknown action: {action!r}")


@router.post("/video-target-detection/close")
async def close_source() -> dict:
    return get_session().close()


# --------------------------------------------------------------------- #
# annotate_roi 检测                                                       #
# --------------------------------------------------------------------- #
@router.get("/video-target-detection/prompt")
async def get_prompt() -> dict:
    return get_target_detector().snapshot()


@router.post("/video-target-detection/prompt")
async def set_prompt(payload: dict) -> dict:
    """Start (or update) annotate_roi target detection.

    Body: ``{prompt: str, every_n: int, enabled_tools: list[str]}``
    ``enabled_tools`` 可选 —— 后端会按 ``ALLOWED_TOOLS`` 兜底过滤，
    当前只允许 ``annotate_roi``。
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
    enabled_tools = payload.get("enabled_tools") or []
    if not isinstance(enabled_tools, list):
        raise HTTPException(status_code=400, detail="enabled_tools 必须是列表")
    enabled_tools = [k for k in enabled_tools if k in ALLOWED_TOOLS]
    if not enabled_tools:
        # 没有任何可用工具时回退到该页唯一允许的工具，避免误传空导致
        # detector 启动后没工具可调。
        enabled_tools = list(ALLOWED_TOOLS)
    try:
        return get_target_detector().start(
            prompt, every_n=every_n, enabled_tools=enabled_tools
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.get("/video-target-detection/tools")
async def list_tools() -> dict:
    """返回当前 detector 可启用的工具列表 + 能力可用性。

    每条记录包含 ``key`` / ``name`` / ``description`` /
    ``requires_capability`` / ``available`` / ``reason`` / ``fields``。
    该页只暴露 ``ALLOWED_TOOLS`` 集合里的工具（目标标注）。
    """
    from app.services.compute_registry import get_registry
    from app.services.tools import get_tool_registry

    reg = get_tool_registry()
    out = []
    for spec in reg.list_specs():
        if spec.key not in ALLOWED_TOOLS:
            continue
        available = True
        reason = ""
        if spec.requires_capability:
            try:
                get_registry().get_session(spec.requires_capability)
            except RuntimeError as e:
                available = False
                reason = str(e)
        out.append({
            "key":                 spec.key,
            "name":                spec.name,
            "description":         spec.description,
            "requires_capability": spec.requires_capability,
            "available":           available,
            "reason":              reason,
            "fields":              [
                {"name": fname, "description": fdesc}
                for fname, fdesc in spec.fields
            ],
        })
    return {"tools": out}


@router.post("/video-target-detection/prompt/stop")
async def stop_prompt() -> dict:
    return get_target_detector().stop()


@router.get("/video-target-detection/events")
async def list_events() -> dict:
    det = get_target_detector()
    return {
        "snapshot": det.snapshot(),
        "events": det.events(),
        "latest": det.latest(),
    }


@router.post("/video-target-detection/events/clear")
async def clear_events() -> dict:
    get_target_detector().clear()
    return {"ok": True}


# --------------------------------------------------------------------- #
# WebSocket: server -> browser frame + event stream                     #
# --------------------------------------------------------------------- #
@router.websocket("/video-target-detection/ws")
async def stream_ws(ws: WebSocket) -> None:
    """Push decoded JPEG frames + annotate_roi events to the browser.

    Protocol (server -> client):
      - JSON text: {"type":"status", ...session snapshot, detector snapshot...}
      - Binary:    raw JPEG bytes for one frame
      - JSON text: {"type":"eof"} when a file source reaches end
      - JSON text: {"type":"event", ...detector event...} when annotate_roi
                   fires (event.tool_call == "annotate_roi" 且 event.bbox 不为空)
    """
    await ws.accept()
    session = get_session()
    detector = get_target_detector()
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
