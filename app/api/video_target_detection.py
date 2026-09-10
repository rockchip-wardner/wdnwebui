"""视频目标检测 endpoints: REST 控制 + WebSocket 帧流 + TargetDoc 检测。

页面结构与 ``app/api/video_detection.py`` 一致；区别仅：

* ``/video-target-detection`` 前缀；
* 后端 detector 用 ``get_target_detector()``，prompt / 事件历史与
  音视频描述页相互独立；
* 检测走 ``session.respond(generating=TargetDoc)`` 结构化输出直
  接拿 ``x1, y1, x2, y2`` + ``desc``，**不**再挂任何 ``@Callable``
  工具 —— 该页面没有"工具集合"概念，``/video-target-detection/tools``
  端点也不存在。
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from app.services.detector import get_target_detector
from app.services.video_session import get_session


router = APIRouter(tags=["video-target-detection"])


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
# TargetDoc 检测                                                         #
# --------------------------------------------------------------------- #
@router.get("/video-target-detection/prompt")
async def get_prompt() -> dict:
    return get_target_detector().snapshot()


@router.post("/video-target-detection/prompt")
async def set_prompt(payload: dict) -> dict:
    """Start (or update) target-detection.

    Body: ``{prompt: str, every_n: int, image_size: int | None,
             model_id: str | None}``
    — ``prompt`` 必填；``image_size`` 可选，指定视觉模型目标尺寸
    （必须落在 worker 已加载变体尺寸集合 ``available_image_sizes``
    内；缺省 / ``None`` = 按帧自动选最近的变体，保留 legacy 行为）；
    ``model_id`` 可选，前端下拉框选中的 ``swcap_video_describe``
    实例 id（不传 / 空 = auto-pick）。

    旧版 ``enabled_tools`` 字段不再生效 —— 该页只走 ``TargetDoc``
    结构化输出，不再挂任何 ``@Callable`` 工具。仍接受该字段以便
    老前端过渡期不掉请求体；忽略即可。
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
    # image_size 解析：可选字段，缺省 / ``None`` → auto-pick；非法
    # 类型 → 400（detector ``start()`` 内部也校验实际合法性，这里提前
    # 到 HTTP 层只是为了返回更具体的错误消息）。
    raw_image_size = payload.get("image_size", None)
    image_size: int | None
    if raw_image_size is None:
        image_size = None
    elif isinstance(raw_image_size, bool) or not isinstance(raw_image_size, int):
        raise HTTPException(
            status_code=400,
            detail=f"image_size 必须是整数或 null: {raw_image_size!r}",
        )
    else:
        image_size = raw_image_size
    # model_id：前端下拉框选中值；None 或 "" 走 auto-pick。
    raw_model_id = payload.get("model_id", None)
    if raw_model_id is None or (isinstance(raw_model_id, str)
                                and not raw_model_id.strip()):
        model_id: str | None = None
    elif not isinstance(raw_model_id, str):
        raise HTTPException(
            status_code=400,
            detail=f"model_id 必须是字符串或 null: {raw_model_id!r}",
        )
    else:
        model_id = raw_model_id
    try:
        # enabled_tools 显式置 [] — 该页不再挂工具。
        return get_target_detector().start(
            prompt, every_n=every_n, enabled_tools=[],
            image_size=image_size, model_id=model_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


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
# 诊断端点 (v0.13): 暴露最近 N 次推理的诊断 trace                          #
# --------------------------------------------------------------------- #
# 用户报告"概率性目标检测失败"时,需要快速看到:
#   - worker 端 schema parse 是否成功 (gen_name + actual_result_type)
#   - proxy 端 result_data 是否缺失 (TargetDoc 重建路径)
#   - detector 端 isinstance(result, TargetDoc) 是否命中
#   - 每次推理耗时 / 事件数 / exit_reason
# 把这些信息塞到一个 GET 端点,curl 即可 dump 现场。生产环境也保留。
@router.get("/video-target-detection/diagnostics")
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
    det = get_target_detector()
    items = det.diag_history()
    # 倒序 (新 → 旧) 方便调试时先看最近的失败
    items_reversed = list(reversed(items))
    return {"count": len(items_reversed), "items": items_reversed}


@router.post("/video-target-detection/diagnostics/clear")
async def clear_diagnostics() -> dict:
    """Clear the diagnostics ring buffer (不影响 _events 历史)。"""
    get_target_detector().clear_diagnostics()
    return {"ok": True}


# --------------------------------------------------------------------- #
# WebSocket: server -> browser frame + event stream                     #
# --------------------------------------------------------------------- #
@router.websocket("/video-target-detection/ws")
async def stream_ws(ws: WebSocket) -> None:
    """Push decoded JPEG frames + TargetDoc events to the browser.

    Protocol (server -> client):
      - JSON text: {"type":"status", ...session snapshot, detector snapshot...}
      - Binary:    raw JPEG bytes for one frame
      - JSON text: {"type":"eof"} when a file source reaches end
      - JSON text: {"type":"event", ...detector event...} when  ``TargetDoc``
                   fires (event.bbox 是 4 元 [x1,y1,x2,y2] + event.crop_b64
                   是裁图的 base64 JPEG + event.desc 是模型对 ROI 的描述
                   文本)。
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
