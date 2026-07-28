"""Video surveillance API: 16-channel 4x4 monitoring grid.

REST endpoints (config + status):

  GET    /api/surveillance                  overall snapshot
  GET    /api/surveillance/config           full 16-channel config
  PUT    /api/surveillance/config           full 16-channel config (replace)
  PATCH  /api/surveillance/config/{id}      partial update of one channel
  POST   /api/surveillance/reset            reset all channels to defaults
  GET    /api/surveillance/{id}/events/...  per-channel VLM event history
  GET    /api/surveillance/{id}/thumb.jpg   latest server-side thumbnail
         (used as a poll fallback; the WS path is preferred)

WebSocket endpoints (per-channel):

  WS /api/surveillance/{id}/ingest          server-side ingest of H.264 NALs
                                            from the rkv.VideoWriter's
                                            rkv+ws:// client (forwarded
                                            by the channel's decode thread).
  WS /api/surveillance/{id}/stream          sends H.264 wire format to
                                            the browser. Browser uses
                                            WebCodecs (chrome 94+) or
                                            MSE+mp4-muxer to render.
  WS /api/surveillance/{id}/thumb           pushes JPEG thumbnails at
                                            ~5 fps for the grid cell preview.

Wire format end-to-end (matches ``rkv.encoder._WebSocketSink``):

    <type_byte:u8><pts_us:u64 LE><payload>

  type_byte:
    0x00  PKT_TYPE_DELTA   (length-prefixed H.264 access unit)
    0x01  PKT_TYPE_KEY     (key-frame access unit)
    0x02  PKT_TYPE_HEADER  (avcC configuration record)
    0xFF  PKT_TYPE_EOS     (encoder tore down)

The browser-side MSE helper expects the avcC record up front, then a
stream of length-prefixed NAL access units. WebCodecs-based decoding
takes the same payload directly.
"""
from __future__ import annotations

import asyncio
import struct
from typing import List, Optional

from fastapi import (
    APIRouter,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import BaseModel, Field, field_validator, model_validator

from app.services.surveillance import (
    CHANNEL_COUNT,
    get_manager,
)


router = APIRouter(tags=["surveillance"])


# --------------------------------------------------------------------------- #
# Pydantic schema for the PUT /api/surveillance/config body                  #
# --------------------------------------------------------------------------- #
class ChannelConfigModel(BaseModel):
    """One channel's persisted config. PUT validates the whole list."""

    model_config = {"extra": "ignore"}

    name: str = Field(default="", description="通道名称")
    source: str = Field(default="", description="视频源 (设备上的路径或网络流)")
    prompt: str = Field(default="", description="VLM 检测提示词")
    playback_enabled: bool = Field(
        default=False, description="是否启用视频播放（解码+编码管线）",
    )
    detection_enabled: bool = Field(
        default=False, description="是否启用 VLM 检测（消耗 NPU）",
    )
    # Detection size is a fixed dropdown choice: 384 (default,
    # qwen3-vl-4b's native res) or 768 (higher fidelity preset).
    # Detection is always square; the frontend sends the same
    # value in both fields.
    detect_w: int = Field(default=384, description="检测宽度（384 或 768）")
    detect_h: int = Field(default=384, description="检测高度（必须等于 detect_w）")

    @field_validator("detect_w", "detect_h", mode="before")
    @classmethod
    def _coerce_int(cls, v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return v

    @field_validator("detect_w")
    @classmethod
    def _detect_w_in_set(cls, v):
        if v not in (384, 768):
            raise ValueError(f"detect_w 必须是 384 或 768，得到 {v!r}")
        return v

    @field_validator("detect_h")
    @classmethod
    def _detect_h_in_set(cls, v):
        if v not in (384, 768):
            raise ValueError(f"detect_h 必须是 384 或 768，得到 {v!r}")
        return v

    @model_validator(mode="after")
    def _detect_size_square(self):
        # Detection is always square — 384×384 or 768×768, no
        # mixed values. Reject anything else at the API layer so
        # the frontend can't accidentally land on a half-state.
        if self.detect_w != self.detect_h:
            raise ValueError(
                f"检测尺寸必须是正方形；得到 {self.detect_w}×{self.detect_h}"
            )
        return self


class SurveillanceConfigModel(BaseModel):
    model_config = {"extra": "ignore"}
    channels: List[ChannelConfigModel] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# REST endpoints                                                              #
# --------------------------------------------------------------------------- #
@router.get("/surveillance")
async def get_overview() -> dict:
    """Per-channel runtime snapshot + persisted config."""
    mgr = get_manager()
    return {
        "channels": mgr.list_snapshots(),
        "configs": mgr.list_configs(),
    }


@router.get("/surveillance/config")
async def get_configs() -> dict:
    return {"channels": get_manager().list_configs()}


@router.put("/surveillance/config")
async def put_configs(payload: SurveillanceConfigModel) -> dict:
    """Replace the whole 16-channel config (other config sections
    untouched). Channels whose index is out of range are ignored."""
    mgr = get_manager()
    payload_list = [c.model_dump() for c in payload.channels]
    config_dicts = mgr.bulk_update(payload_list)
    return {"channels": config_dicts}


class ChannelPatchModel(BaseModel):
    model_config = {"extra": "ignore"}
    name: Optional[str] = None
    source: Optional[str] = None
    prompt: Optional[str] = None
    playback_enabled: Optional[bool] = None
    detection_enabled: Optional[bool] = None
    # Legacy alias: ``enabled`` from older clients still maps to
    # ``playback_enabled`` if the new field isn't supplied.
    enabled: Optional[bool] = None
    detect_w: Optional[int] = Field(default=None, description="检测宽度（384 或 768）")
    detect_h: Optional[int] = Field(default=None, description="检测高度（必须等于 detect_w）")

    @field_validator("detect_w", "detect_h", mode="before")
    @classmethod
    def _coerce_int_opt(cls, v):
        if v is None:
            return v
        try:
            return int(v)
        except (TypeError, ValueError):
            return v

    @field_validator("detect_w")
    @classmethod
    def _detect_w_in_set(cls, v):
        if v is None or v in (384, 768):
            return v
        raise ValueError(f"detect_w 必须是 384 或 768，得到 {v!r}")

    @field_validator("detect_h")
    @classmethod
    def _detect_h_in_set(cls, v):
        if v is None or v in (384, 768):
            return v
        raise ValueError(f"detect_h 必须是 384 或 768，得到 {v!r}")

    @model_validator(mode="after")
    def _detect_size_square_if_set(self):
        # Only validate the square invariant when both fields are
        # supplied; single-field partial patches (e.g. updating
        # only ``detect_w``) leave detect_h untouched and we let
        # the Channel layer snap to the smaller of (new, stored)
        # so a half-patched config can still save.
        if (self.detect_w is not None
                and self.detect_h is not None
                and self.detect_w != self.detect_h):
            raise ValueError(
                f"检测尺寸必须是正方形；得到 {self.detect_w}×{self.detect_h}"
            )
        return self


@router.patch("/surveillance/config/{channel_id}")
async def patch_config(channel_id: int, payload: ChannelPatchModel) -> dict:
    if not (1 <= channel_id <= CHANNEL_COUNT):
        raise HTTPException(
            status_code=404, detail=f"通道 {channel_id} 不存在"
        )
    fields = payload.model_dump(exclude_none=True)
    if not fields:
        return {"channel": get_manager().get_channel(channel_id - 1).config_dict()}
    new_cfg = get_manager().update_config(channel_id - 1, **fields)
    return {"channel": new_cfg}


@router.post("/surveillance/reset")
async def reset_configs() -> dict:
    return {"channels": get_manager().reset_to_defaults()}


@router.get("/surveillance/{channel_id}/events")
async def list_events(channel_id: int, since_id: int = 0) -> dict:
    if not (1 <= channel_id <= CHANNEL_COUNT):
        raise HTTPException(
            status_code=404, detail=f"通道 {channel_id} 不存在"
        )
    ch = get_manager().get_channel(channel_id - 1)
    return {
        "events": ch.events_since(since_id),
        "latest": ch.latest_event(),
        "snapshot": ch.snapshot(),
    }


@router.post("/surveillance/{channel_id}/events/clear")
async def clear_events(channel_id: int) -> dict:
    if not (1 <= channel_id <= CHANNEL_COUNT):
        raise HTTPException(
            status_code=404, detail=f"通道 {channel_id} 不存在"
        )
    get_manager().get_channel(channel_id - 1).clear_events()
    return {"ok": True}


# --------------------------------------------------------------------------- #
# WebSocket: ingest (rkv+ws:// client → server)                              #
# --------------------------------------------------------------------------- #
@router.websocket("/surveillance/{channel_id}/ingest")
async def ingest_ws(ws: WebSocket, channel_id: int) -> None:
    """Server-side ingest endpoint for the rkv.VideoWriter's
    rkv+ws:// client. The encoder connects here, sends H.264 wire format
    packets, and we route them to the channel's fan-out queue.

    The encoder uses a stdlib socket + hand-rolled RFC 6455 handshake;
    uvicorn's WS server accepts this (it's a vanilla client WS upgrade).
    """
    if not (1 <= channel_id <= CHANNEL_COUNT):
        await ws.close(code=4404)
        return
    await ws.accept()
    ch = get_manager().get_channel(channel_id - 1)
    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            data = msg.get("bytes")
            if data is None:
                # The rkv encoder only sends binary frames; text frames
                # are a protocol violation. Drop silently.
                continue
            # Wire format: <type_byte:u8><pts_us:u64 LE><payload>
            if len(data) < 9:
                continue
            (type_byte, pts_us) = struct.unpack("<BQ", data[:9])
            payload = data[9:]
            ch.push_packet(type_byte, pts_us, payload)
            if type_byte == 0xFF:  # EOS marker
                # Encoder is telling us the stream is over. Break out
                # so the socket closes cleanly.
                break
    except WebSocketDisconnect:
        return
    except Exception:
        # Defensive: any unexpected exception should not break the
        # server. The decode thread will keep retrying on the next
        # frame; the encoder's _WebSocketSink.send_packet reconnects
        # on send failure too.
        try:
            await ws.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# WebSocket: stream (server → browser)                                       #
# --------------------------------------------------------------------------- #
@router.websocket("/surveillance/{channel_id}/stream")
async def stream_ws(ws: WebSocket, channel_id: int) -> None:
    """Forward H.264 wire-format packets to the browser.

    The browser consumes this either via WebCodecs (recommended) or by
    running the same payload through a small JS fMP4 muxer before
    appending to a MSE SourceBuffer. The packet format mirrors the
    rkv encoder's on-wire layout so the browser can treat both paths
    identically.
    """
    if not (1 <= channel_id <= CHANNEL_COUNT):
        await ws.close(code=4404)
        return
    await ws.accept()
    ch = get_manager().get_channel(channel_id - 1)
    q, evt = ch.register_stream_subscriber()
    loop = asyncio.get_event_loop()
    try:
        while True:
            # Wait for the next packet. Bound the wait so a quiet
            # channel still flushes its close in a timely manner when
            # the client disconnects.
            try:
                await loop.run_in_executor(None, evt.wait, 1.0)
            except Exception:
                pass
            if not evt.is_set():
                continue
            evt.clear()
            # Drain everything currently in the queue.
            while True:
                try:
                    pkt = q.get_nowait()
                except Exception:
                    break
                type_byte, pts_us, payload = pkt
                try:
                    await ws.send_bytes(
                        struct.pack("<BQ", type_byte, pts_us) + payload
                    )
                except Exception:
                    return
    except WebSocketDisconnect:
        return
    except Exception:
        return
    finally:
        ch.unregister_stream_subscriber(q)


# --------------------------------------------------------------------------- #
# WebSocket: thumbnail (server → browser)                                    #
# --------------------------------------------------------------------------- #
@router.websocket("/surveillance/{channel_id}/thumb")
async def thumb_ws(ws: WebSocket, channel_id: int) -> None:
    """Push JPEG thumbnails at ~5 fps for the grid cell preview."""
    if not (1 <= channel_id <= CHANNEL_COUNT):
        await ws.close(code=4404)
        return
    await ws.accept()
    ch = get_manager().get_channel(channel_id - 1)
    q, evt = ch.register_thumb_subscriber()
    loop = asyncio.get_event_loop()
    try:
        while True:
            try:
                await loop.run_in_executor(None, evt.wait, 2.0)
            except Exception:
                pass
            if not evt.is_set():
                continue
            evt.clear()
            while True:
                try:
                    jpg = q.get_nowait()
                except Exception:
                    break
                try:
                    await ws.send_bytes(jpg)
                except Exception:
                    return
    except WebSocketDisconnect:
        return
    except Exception:
        return
    finally:
        ch.unregister_thumb_subscriber(q)
