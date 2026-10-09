"""文本对话 REST + WebSocket API。

端点
====

* ``GET    /api/text-chat``        — 当前 snapshot（state / busy / error /
                                     history_count / history_tokens /
                                     context_limit / current_request_id）。
* ``POST   /api/text-chat/send``   — ``{text}`` → 发起一轮推理，返回
                                     ``{request_id, snapshot}``。400 / 409 / 502。
* ``POST   /api/text-chat/stop``   — 立即停止正在跑的推理。
* ``POST   /api/text-chat/reset``  — 清空 history，回到 idle。
* ``WS     /api/text-chat/ws``     — server-push 流式 token + 状态变更。
                                     连接建立后立即推 ``snapshot``。

错误码
======

* 400 —— text 为空 / 对话过长（prompt 超过 16K 上下文）。
* 409 —— 上一条请求还在生成中。
* 502 —— ``RuntimeError("功能未启用: swcap_text_chat")``（未启动任何对话模型）。

WS 帧类型
=========

* ``snapshot`` —— 连接建立后立即推一次。
* ``state`` —— 状态字段 diff（含完整 snapshot）。
* ``user_echo`` —— 用户消息 echo（前端用它立即渲染 user 气泡）。
* ``thinking`` —— ``<think>...</think>`` 段增量。
* ``answer`` —— ``</think>`` 之后的回答增量。
* ``stopped`` —— 用户中途停止。
* ``done`` —— 一轮推理结束。
* ``error`` —— 推理异常或 capability 未启用。
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.apps.text_chat.service import get_chat_service


_LOG = logging.getLogger("text_chat_api")
router = APIRouter(tags=["text-chat"])


# --------------------------------------------------------------------------- #
# Pydantic
# --------------------------------------------------------------------------- #
class SendPayload(BaseModel):
    model_config = {"extra": "ignore"}
    text: str = Field(min_length=1, max_length=8192)


# --------------------------------------------------------------------------- #
# REST
# --------------------------------------------------------------------------- #
@router.get("/")
async def get_text_chat_snapshot() -> dict:
    """完整 snapshot；UI 在进入页面时拉一次，后续靠 WS 推送增量。"""
    return get_chat_service().snapshot()


@router.post("/send")
async def post_text_chat_send(payload: SendPayload) -> dict:
    """发起一轮推理；返回 ``request_id`` + 当前 snapshot。"""
    svc = get_chat_service()
    try:
        rid = svc.send(payload.text)
    except ValueError as e:
        msg = str(e)
        if "上一条" in msg:
            # 409 附带服务端最新 snapshot：前端（或另一标签页）本地状态
            # 滞后时据此立即纠正 busy/按钮态，而不是显示误导性报错。
            return JSONResponse(
                status_code=409,
                content={"detail": msg, "snapshot": svc.snapshot()},
            )
        if "text" in msg:
            raise HTTPException(status_code=400, detail=msg) from e
        raise HTTPException(status_code=400, detail=msg) from e
    return {"ok": True, "request_id": rid, "snapshot": svc.snapshot()}


@router.post("/stop")
async def post_text_chat_stop() -> dict:
    """立即停止正在跑的推理；当前轮丢弃，不写历史。"""
    svc = get_chat_service()
    svc.stop()
    return {"ok": True, "snapshot": svc.snapshot()}


@router.post("/reset")
async def post_text_chat_reset() -> dict:
    """清空 history + history_tokens。"""
    svc = get_chat_service()
    svc.reset()
    return {"ok": True, "snapshot": svc.snapshot()}


# --------------------------------------------------------------------------- #
# WebSocket —— push 流式 token + 状态变更
# --------------------------------------------------------------------------- #
@router.websocket("/ws")
async def text_chat_ws(ws: WebSocket) -> None:
    """WS handler：snapshot hydrate + 流式 token 推送。

    推模型（与 wechat 一致）：
    * 连接后立即推 ``{"type":"snapshot", "snapshot":...}``。
    * 之后增量推 ``state`` / ``user_echo`` / ``thinking`` / ``answer`` /
      ``done`` / ``stopped`` / ``error``。
    """
    await ws.accept()
    svc = get_chat_service()
    queue: asyncio.Queue = asyncio.Queue(maxsize=500)

    # 1) 立即推 snapshot
    try:
        await ws.send_json({"type": "snapshot", "snapshot": svc.snapshot()})
    except Exception:
        return

    svc.register_subscriber(queue)

    async def _relay() -> None:
        try:
            while True:
                payload = await queue.get()
                try:
                    await ws.send_json(payload)
                except Exception:
                    return
        except asyncio.CancelledError:
            return

    relay_task = asyncio.create_task(_relay(), name="text-chat-ws-relay")

    try:
        # 客户端不发任何帧；这里循环 receive 仅用于检测断开
        while True:
            try:
                await ws.receive_text()
            except WebSocketDisconnect:
                break
    finally:
        svc.unregister_subscriber(queue)
        relay_task.cancel()
        try:
            await relay_task
        except (asyncio.CancelledError, Exception):
            pass