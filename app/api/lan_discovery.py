"""局域网设备发现 REST API。

* ``GET    /api/lan-discovery``               完整 snapshot：配置 + 设备列表 +
                                              本机 IP / 扫描子网 / 状态。
                                              Discovery 页每秒轮询这个端点。
* ``PUT    /api/lan-discovery``               更新 enabled / port /
                                              scan_interval_seconds；启用后会
                                              清空旧设备并立即触发首轮扫描。
* ``POST   /api/lan-discovery/scan``          手动触发扫描（即便 enabled=false
                                              也允许跑一次；用于"立即扫描"按钮）。
* ``DELETE /api/lan-discovery/devices``       清空内存里的设备列表。
* ``GET    /api/lan-discovery/announce``      对端探测本机用的公共接口，返回
                                              ``device_name`` + ``sw_capabilities``
                                              + ``hw_capabilities``。

错误处理：

* 400 —— pydantic 校验失败（port 越界 / scan_interval 越界）。
* 500 —— 配置持久化失败（理论上不应出现 —— ``_save_config`` 内部已 try/except
  写日志，落到用户层面只是不会持久化，不影响 in-memory state）。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from app.services.lan_discovery import get_lan_discovery


router = APIRouter(tags=["lan-discovery"])


# --------------------------------------------------------------------------- #
# Pydantic 模型
# --------------------------------------------------------------------------- #
class LanDiscoveryUpdate(BaseModel):
    """PUT /api/lan-discovery 请求体 —— 字段全部可选，至少给一个。"""

    model_config = {"extra": "ignore"}

    enabled: Optional[bool] = None
    port: Optional[int] = Field(default=None, ge=1, le=65535)
    scan_interval_seconds: Optional[int] = Field(default=None, ge=5, le=600)

    @field_validator("scan_interval_seconds")
    @classmethod
    def _scan_interval_sane(cls, v: Optional[int]) -> Optional[int]:
        if v is None:
            return v
        # 强制整数（拒绝浮点）
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError("scan_interval_seconds 必须是整数")
        return v

    def _require_at_least_one(self) -> bool:
        return (
            self.enabled is not None
            or self.port is not None
            or self.scan_interval_seconds is not None
        )


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@router.get("/lan-discovery")
async def get_lan_discovery_snapshot() -> dict:
    """Discovery 页"局域网设备"栏目渲染源 —— 每秒轮询。

    不依赖响应头缓存：前端 fetch 本身就带防缓存 query string，重复的
    no-cache 头只是冗余。
    """
    return get_lan_discovery().snapshot()


@router.put("/lan-discovery")
async def put_lan_discovery(payload: LanDiscoveryUpdate) -> dict:
    """更新 enabled / port / scan_interval_seconds。"""
    if not payload._require_at_least_one():
        raise HTTPException(
            status_code=400,
            detail="至少需要传一个字段 (enabled / port / scan_interval_seconds)",
        )
    mgr = get_lan_discovery()
    try:
        cfg = mgr.update_config(
            port=payload.port,
            scan_interval_seconds=payload.scan_interval_seconds,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    if payload.enabled is not None:
        mgr.set_enabled(bool(payload.enabled))

    return {
        "ok": True,
        "snapshot": get_lan_discovery().snapshot(),
    }


@router.post("/lan-discovery/scan")
async def trigger_lan_discovery_scan() -> dict:
    """手动触发一轮扫描（不等 interval 到点）。"""
    return get_lan_discovery().trigger_scan()


@router.delete("/lan-discovery/devices")
async def clear_lan_discovery_devices() -> dict:
    """清空内存里的设备列表（不影响 enabled 开关与 config.json）。"""
    mgr = get_lan_discovery()
    mgr.clear_devices()
    return {"ok": True, "snapshot": mgr.snapshot()}


@router.get("/lan-discovery/announce")
async def lan_discovery_announce(request: Request) -> dict:
    """对端探测本机的公共接口 —— 永远可用，跟 enabled 开关无关。

    只要访问方能 TCP 连到本机 8000，就能拿到本机的 device_name +
    能力快照。这样关闭"主动扫描"的设备也能被其他实例发现。
    """
    # 优先拿 HTTP 头里的对端 IP（经过反向代理时直接拿 client.host）
    client_host = None
    if request.client is not None:
        client_host = request.client.host
    return get_lan_discovery().announce(remote_ip=client_host or "")