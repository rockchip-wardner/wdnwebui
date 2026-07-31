"""算力注册中心 REST API。

Endpoints
---------
* ``GET    /api/compute``           完整 snapshot（cards / supported_models /
                                   instances / 内存汇总 / 队列长度 / 当前 capabilities）。
                                   UI 算力配置页 / discovery 页都从这里读。
* ``GET    /api/compute/config``     读 ``config.json["compute"]``（仅数据，不启动）。
* ``PUT    /api/compute/config``     全量替换（仅持久化，不触发启动）。Pydantic 校验
                                   （同 npu 重复 model 拒绝）。
* ``POST   /api/compute/instances``  ``{npu_index, model}`` → live 启动（后台 worker
                                   真正加载）+ persist。
* ``DELETE /api/compute/instances``  ``{npu_index, model}`` → live 停止 + persist。
* ``GET    /api/compute/capabilities``  当前 running 实例的 capability 列表（discovery
                                       页面渲染源）。
* ``POST   /api/compute/stop-all``   全部停止 + 清空 config.json["compute"].instances。

错误处理：

* 400 —— pydantic 校验失败（缺字段、model 未知、npu_index 越界、重复实例）。
* 404 —— list_cards 返回空（未检测到 NPU），或 stop 时实例不存在。
* 500 —— 后台启动抛了非预期异常（理论上不应该 —— ``_run_one`` 内已经 try/except
  捕获并写到 ``status='error'``）。
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field, field_validator

from app.services.compute_registry import (
    MODEL_PRESETS,
    get_registry,
)


router = APIRouter(tags=["compute"])


# --------------------------------------------------------------------------- #
# Pydantic 模型
# --------------------------------------------------------------------------- #
class InstanceSpec(BaseModel):
    npu_index: int = Field(ge=0, le=15)
    model: str

    @field_validator("model")
    @classmethod
    def _known_model(cls, v: str) -> str:
        if v not in MODEL_PRESETS:
            raise ValueError(
                f"未知 model: {v!r}（可选: {sorted(MODEL_PRESETS.keys())}）"
            )
        return v


class InstanceRequest(BaseModel):
    npu_index: int = Field(ge=0, le=15)
    model: str

    @field_validator("model")
    @classmethod
    def _known_model(cls, v: str) -> str:
        if v not in MODEL_PRESETS:
            raise ValueError(
                f"未知 model: {v!r}（可选: {sorted(MODEL_PRESETS.keys())}）"
            )
        return v


class ComputeConfigPayload(BaseModel):
    instances: List[InstanceSpec] = Field(default_factory=list)

    @field_validator("instances")
    @classmethod
    def _no_duplicate_per_npu(cls, v: List[InstanceSpec]) -> List[InstanceSpec]:
        seen: set[tuple[int, str]] = set()
        for inst in v:
            key = (inst.npu_index, inst.model)
            if key in seen:
                raise ValueError(
                    f"重复实例: npu={inst.npu_index} model={inst.model}"
                )
            seen.add(key)
        return v


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@router.get("/compute")
async def get_compute(response: Response) -> dict:
    """完整 snapshot：UI 算力配置页 + discovery 页面都从这里读。

    每秒轮询：UI 通过 ``Cache-Control: no-cache`` 避免浏览器缓存陈旧响应。
    """
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return get_registry().snapshot()


@router.get("/compute/config")
async def get_compute_config() -> dict:
    """读 ``config.json["compute"]``（不触发任何启动）。"""
    from app.services.config import get_config
    section = get_config().get_section("compute", {})
    instances = section.get("instances") if isinstance(section, dict) else None
    if not isinstance(instances, list):
        instances = []
    return {"instances": instances}


@router.put("/compute/config")
async def put_compute_config(payload: ComputeConfigPayload) -> dict:
    """全量替换 ``config.json["compute"]``，**不**触发 live 启动。

    Pydantic 校验：model 必须是 preset 白名单成员；同 npu 重复 model 拒绝。
    """
    from app.services.config import get_config
    try:
        snap = get_config().set_section("compute", payload.model_dump())
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"persist 失败: {e}") from e
    return {"ok": True, "data": snap}


@router.post("/compute/instances")
async def start_instance(req: InstanceRequest) -> dict:
    """Live 启动一条实例。后台 worker 异步加载；返回时 status="queued"。

    Raises:
        400: 重复实例 / npu_index 越界 / 未知 model。
        404: list_cards 返回空（未检测到 NPU 卡）。
    """
    reg = get_registry()
    if not reg.list_cards():
        raise HTTPException(
            status_code=404,
            detail="未检测到任何 NPU 卡（RKNN3Lite.get_devices_id() 返回空）",
        )
    try:
        inst = reg.start_one(req.npu_index, req.model)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True, "instance": inst.to_dict(), "snapshot": reg.snapshot()}


@router.delete("/compute/instances")
async def stop_instance(req: InstanceRequest) -> dict:
    """Live 停止一条实例 + 从 config.json 移除。"""
    reg = get_registry()
    reg.stop_one(req.npu_index, req.model)
    reg._persist()  # 重新写一次（已 stop 的不写）
    return {"ok": True, "snapshot": reg.snapshot()}


@router.get("/compute/capabilities")
async def get_capabilities() -> dict:
    """当前 running 实例的 capability 列表（discovery 页面渲染源）。"""
    return {"capabilities": get_registry().list_active_capabilities()}


@router.post("/compute/stop-all")
async def stop_all_instances() -> dict:
    """全部停止 + 清空 config.json["compute"].instances。"""
    reg = get_registry()
    reg.stop_all()
    try:
        from app.services.config import get_config
        get_config().set_section("compute", {"instances": []})
    except Exception:
        pass
    return {"ok": True, "snapshot": reg.snapshot()}