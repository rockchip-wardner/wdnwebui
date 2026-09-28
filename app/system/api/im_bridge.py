"""WeChat bot (iLink) REST + WebSocket API for the im-bridge page.

* ``GET    /api/wechat``             — full snapshot (state + recent msgs).
* ``POST   /api/wechat/login``       — kick off QR login; returns snapshot.
* ``POST   /api/wechat/cancel-login``— hide the in-flight QR.
* ``POST   /api/wechat/logout``      — clear stored credentials + stop bot.
* ``POST   /api/wechat/send``        — send a text message to a user.
* ``GET    /api/wechat/users``       — list known user IDs.
* ``GET    /api/wechat/runtime``     — diagnostic dict for the UI footer.
* ``WS     /api/wechat/ws``          — push state-diff + new-message events.

The QR URL delivered by the SDK is a string (e.g. ``weixin://...``); the
client renders it to an actual image using the vendored
``/static/js/qrcode.js`` encoder.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from app.system.im_bridge.service_wechat import get_wechat
from app.system.im_bridge.bot import Wechat


_LOG = logging.getLogger("wechat_api")
router = APIRouter(tags=["wechat"])


# --------------------------------------------------------------------- #
# Pydantic
# --------------------------------------------------------------------- #
class LoginPayload(BaseModel):
    model_config = {"extra": "ignore"}
    force: bool = False


class SendPayload(BaseModel):
    model_config = {"extra": "ignore"}
    user_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=4000)


class LlmBridgePayload(BaseModel):
    model_config = {"extra": "ignore"}
    enabled: bool = False


# --------------------------------------------------------------------- #
# REST
# --------------------------------------------------------------------- #
@router.get("/")
async def get_wechat_snapshot() -> dict:
    """Full snapshot consumed by the im-bridge page on load + every poll."""
    return get_wechat().snapshot()


@router.get("/users")
async def get_wechat_users() -> dict:
    return {"users": get_wechat().known_users()}


@router.get("/runtime")
async def get_wechat_runtime() -> dict:
    info = Wechat.check_runtime()
    info["wechat_loaded"] = info.get("available", False)
    return info


@router.post("/login")
async def post_wechat_login(payload: Optional[LoginPayload] = None) -> dict:
    force = bool(payload.force) if payload else False
    snap = get_wechat().start_login(force=force)
    return {"ok": True, "snapshot": get_wechat().snapshot(), "login": snap}


@router.post("/cancel-login")
async def post_wechat_cancel_login() -> dict:
    snap = get_wechat().cancel_login()
    return {"ok": True, "snapshot": get_wechat().snapshot(), "login": snap}


@router.post("/logout")
async def post_wechat_logout() -> dict:
    return get_wechat().logout()


@router.post("/send")
async def post_wechat_send(payload: SendPayload) -> dict:
    user_id = payload.user_id.strip()
    text = payload.text
    if not user_id or not text:
        raise HTTPException(status_code=400, detail="user_id / text 不能为空")
    # 开关开启时禁止手动"插话"，避免手动消息未进模型上下文造成上下文错乱。
    if get_wechat()._llm_bridge.enabled():
        raise HTTPException(
            status_code=409,
            detail="对接文字对话模型已开启，手动发送已禁用",
        )
    try:
        get_wechat().send_text(user_id, text)
    except Exception as exc:  # noqa: BLE001
        # ``wechatbot.NoContextError`` is the only meaningful one here;
        # surface a 400 with a clear reason so the UI can prompt the user
        # to wait for an inbound message first.
        name = type(exc).__name__
        if name == "NoContextError" or "NoContextError" in repr(exc):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"该用户 {user_id} 尚未向你发送过消息，"
                    "context_token 未建立；请先在微信里向机器人发一条消息。"
                ),
            ) from exc
        raise HTTPException(status_code=502, detail=f"send failed: {exc}") from exc
    return {"ok": True}


@router.post("/llm-bridge")
async def post_wechat_llm_bridge(payload: LlmBridgePayload) -> dict:
    """开/关微信文字对话模型桥接。

    开启时 ``swcap_text_chat`` capability 必须可用（任一注册了
    该 capability 的实例已 running —— ``qwen3-4b`` /
    ``qwen3.5-vl-4b``）；否则 409。开启后手动
    发送会被 ``/wechat/send`` 拒绝。开关不持久化（内存态，重启即关）。
    """
    mgr = get_wechat()
    try:
        bridge_snap = mgr.set_llm_bridge(payload.enabled)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return {"ok": True, "snapshot": mgr.snapshot(), "llm_bridge": bridge_snap}


# --------------------------------------------------------------------- #
# WebSocket — push state-diff + new-message events
# --------------------------------------------------------------------- #
@router.websocket("/ws")
async def wechat_ws(ws: WebSocket) -> None:
    """Push model: server emits JSON frames only when state changes.

    Frames:
      * ``{"type":"snapshot", "snapshot": {...}}`` — sent immediately on
        connect so the client can hydrate without an extra GET.
      * ``{"type":"state", "state": {...}}``       — only the keys that
        changed since the last push.
      * ``{"type":"message", "message": {...}}``   — newest incoming or
        outgoing message in the ring buffer.
    """
    await ws.accept()
    mgr = get_wechat()
    queue: asyncio.Queue = asyncio.Queue(maxsize=200)

    # Send an initial snapshot so the client doesn't have to also GET.
    try:
        await ws.send_json({"type": "snapshot", "snapshot": mgr.snapshot()})
    except Exception:
        return

    mgr.register_subscriber(asyncio.get_running_loop(), queue)

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

    relay_task = asyncio.create_task(_relay(), name="wechat-ws-relay")

    try:
        # Drain any client frames (we don't expect any, but keep the
        # connection healthy by reading).
        while True:
            try:
                await ws.receive_text()
            except WebSocketDisconnect:
                break
    finally:
        mgr.unregister_subscriber(queue)
        relay_task.cancel()
        try:
            await relay_task
        except (asyncio.CancelledError, Exception):
            pass