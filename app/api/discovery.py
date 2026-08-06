"""DDS 注册发现 REST API。

* ``GET    /api/discovery/dds``           完整 snapshot（state + dds_runtime
                                            + discovered[] + last_error）。
                                            Discovery 页每秒轮询这个端点。
* ``POST   /api/discovery/dds/start``     "在网注册" 开关 ON 时调：起 wdndds
                                            Hub + register + start_scan。
* ``POST   /api/discovery/dds/stop``      "在网注册" 开关 OFF 时调：stop_scan
                                            + close + 重置 wdndds 内部状态。
* ``GET    /api/discovery/dds/runtime``   只读 fastdds 可用性检查（不触发
                                            connect）。UI banner 用。

错误处理：

* 503 —— ``POST /start`` 时 FastDDS native bindings 没装（带 ``detail`` 是
  dict，UI 直接渲染）。
* 502 —— ``POST /start`` / ``/stop`` 时启动 / 关闭流程报错。
* 409 —— 切换中（STARTING / STOPPING）又来一次 POST。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.services.dds_discovery import get_dds_discovery


router = APIRouter(tags=["discovery"])


@router.get("/discovery/dds")
async def get_dds_discovery_snapshot() -> dict:
    """Discovery 页"在网注册"渲染源 —— 每秒轮询。"""
    return get_dds_discovery().snapshot()


@router.get("/discovery/dds/runtime")
async def get_dds_discovery_runtime() -> dict:
    """只读 FastDDS 可用性快照（不触发 connect / register）。"""
    return get_dds_discovery().runtime_info()


@router.post("/discovery/dds/start")
async def post_dds_discovery_start() -> dict:
    """开 toggle：起 wdndds Hub 并注册本机能力。"""
    return get_dds_discovery().start()


@router.post("/discovery/dds/stop")
async def post_dds_discovery_stop() -> dict:
    """关 toggle：拆 Hub，对端 SPDP 失活后从它们的列表里移除本机。"""
    return get_dds_discovery().stop()