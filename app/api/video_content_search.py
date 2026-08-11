"""视频内容检索：REST + WebSocket。

后端功能本期为 mock（见 :mod:`app.services.video_content_search`），
前端先用这套契约跑通完整交互；后续按 vl_rag_demo 实现真实后端时
只替换 service 内部实现，字段名（``chunk_seconds`` / ``sim_threshold``
/ ``top_k`` / ``start_time`` 等）保持不变。
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from app.services.video_content_search import get_service


router = APIRouter(tags=["video-content-search"])


@router.get("/video-content-search")
async def get_snapshot() -> dict:
    """当前快照（状态 / 进度 / 检索结果）。"""
    return get_service().snapshot()


@router.post("/video-content-search/open")
async def open_source(payload: dict) -> dict:
    """选择视频源。Body: ``{source: str}``（本期只允许单个视频文件）。"""
    payload = payload or {}
    source = (payload.get("source") or "").strip()
    if not source:
        raise HTTPException(status_code=400, detail="source 不能为空")
    return get_service().open(source)


@router.post("/video-content-search/extract")
async def start_extract(payload: dict) -> dict:
    """开始切片 embedding 提取。

    Body: ``{chunk_seconds: int, chunk_overlap_seconds: int,
    chunk_prompt?: str}``。参数值域以**前端档位**为准：
    chunk_seconds ∈ {3,4,5,6}，chunk_overlap_seconds ∈ {1,2}。
    """
    payload = payload or {}
    try:
        chunk_seconds = int(payload.get("chunk_seconds", 4))
        chunk_overlap_seconds = int(payload.get("chunk_overlap_seconds", 1))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="切片参数必须为整数")
    try:
        return get_service().start_extract(chunk_seconds, chunk_overlap_seconds)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/video-content-search/extract/stop")
async def stop_extract() -> dict:
    """停止切片提取。"""
    return get_service().stop_extract()


@router.post("/video-content-search/query")
async def query_video(payload: dict) -> dict:
    """文本检索。Body: ``{text, sim_threshold, top_k}``。"""
    payload = payload or {}
    text = (payload.get("text") or "").strip()
    try:
        sim_threshold = float(payload.get("sim_threshold", 0.5))
        top_k = int(payload.get("top_k", 10))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="阈值/上限必须为数值")
    try:
        return get_service().query(text, sim_threshold, top_k)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.websocket("/video-content-search/ws")
async def vcs_ws(ws: WebSocket) -> None:
    """单向上行（server → client）。

    协议：
      - ``{"type":"hello", ...snapshot...}``  连上瞬间的初始快照
      - ``{"type":"progress", embedded, total}``  切片嵌入进度
      - ``{"type":"state", ...snapshot...}``  状态变更（完成 / 停止 / 出错）
    """
    await ws.accept()
    svc = get_service()
    loop = asyncio.get_event_loop()
    main_q: "asyncio.Queue[dict]" = asyncio.Queue(maxsize=128)

    def _push(payload: dict) -> None:
        try:
            loop.call_soon_threadsafe(main_q.put_nowait, payload)
        except Exception:
            pass

    svc.register_broadcaster(_push)
    try:
        await ws.send_json({"type": "hello", **svc.snapshot()})
        while True:
            try:
                payload = await asyncio.wait_for(main_q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            await ws.send_json(payload)
    except WebSocketDisconnect:
        return
    except Exception as e:  # pragma: no cover - defensive
        try:
            await ws.send_json({"type": "error", "detail": str(e)})
        except Exception:
            pass
    finally:
        svc.unregister_broadcaster(_push)
