"""算力注册中心 REST API (v0.7)。

Endpoints
---------
* ``GET    /api/compute``                  完整 snapshot（cards / services /
                                          instances / capabilities / tools）。
                                          UI 算力配置页 / discovery 页都从这里读。
* ``GET    /api/compute/config``           读 ``config.json["compute"]`` 数据。
* ``PUT    /api/compute/config``           全量替换 ``compute`` section。
* ``POST   /api/compute/instances``        ``{npu_index, model}`` → 用户在算力
                                          配置页点"启用"。两步：
                                          (1) ensure worker（probe → adopt /
                                          spawn）；(2) RPC ``load`` 绑定 npu。
                                          adopted peer → 403（本机无权 bind）。
* ``DELETE /api/compute/instances``        ``{npu_index, model}`` → "禁用"。
                                          仅卸载模型（worker 进程保留，
                                          status=not_loaded），可秒级重
                                          启用。spawned / adopted_local /
                                          adopted 三类都允许。
* ``POST   /api/compute/restart``          ``{npu_index?, model}`` → Q3 4 步：
                                          unload → shutdown → wait/kill →
                                          走 boot 流程（不自动 bind）。
                                          仅 spawned 允许。
* ``POST   /api/compute/stop-all``         全部停止（仅 spawned，adopted 不动）。
* ``GET    /api/compute/capabilities``     discovery 页用：``sw_capabilities``
                                          + ``hw_capabilities``。
* ``PUT    /api/compute/hardware-capabilities``   HW 能力 enable/disable。

错误码：

* 400 —— pydantic 校验失败（缺字段、model 未知、npu_index 越界）。
* 403 —— 操作的是 adopted / unavailable service（本机无权启停）。
* 404 —— list_cards 返回空（未检测到 NPU）或 stop 时实例不存在。
"""
from __future__ import annotations

from typing import Optional

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
class InstanceRequest(BaseModel):
    """``POST /compute/instances`` 入参：用户选好 (model, npu) 后启用。

    npu_index 必填，因为 enable 步骤包含 RPC load 绑定 NPU。
    """
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


class StopInstanceRequest(BaseModel):
    """``DELETE /compute/instances`` 入参：只需要 model_id。

    删除不需要 npu_index（registry 内部按 model 唯一索引）。
    """
    model: str

    @field_validator("model")
    @classmethod
    def _known_model(cls, v: str) -> str:
        if v not in MODEL_PRESETS:
            raise ValueError(
                f"未知 model: {v!r}（可选: {sorted(MODEL_PRESETS.keys())}）"
            )
        return v


class RestartRequest(BaseModel):
    """``POST /compute/restart`` 入参：只需要 model_id。

    重启后 worker 回到 ``not_loaded`` 状态；用户在算力配置页再点"启用"
    + 选 NPU 才完成 reload（per Q3 "走 run.py 启动流程" 语义）。
    """
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
    """``PUT /api/compute/config`` 入参。

    其它字段（hardware_capabilities / app_prompts /
    tool_call_max_iters 等）按现有语义落盘；``instances`` 若存在被
    ``extra="ignore"`` 丢弃（实例选择不在 config 持久化）。
    """

    model_config = {"extra": "ignore"}


# --------------------------------------------------------------------------- #
# 权限 / 状态工具
# --------------------------------------------------------------------------- #
def _require_owned(inst, action: str) -> None:
    """确保 ``inst`` 是本机 spawn 或 adopted_local（reload 遗留 worker），
    即本机拿得到 process handle（spawned）或能通过 RPC 触达（adopted_local）；
    仅 ``adopted`` (远端 peer) → 403。

    v0.8: 之前用 ``inst.proc is None`` 会把 reload 留下的 adopted_local
    也拒掉，导致用户在算力配置页点启用后端返 403；现改为按 ``_source``
    判断：adopted_local 跟 spawned 一样可写。
    """
    if inst is None:
        raise HTTPException(
            status_code=404,
            detail=f"{action}: model 不在 registry",
        )
    if inst._source == "adopted":
        peer_label = inst._peer_name or (inst._peer_guid or "")[:8] or "peer"
        raise HTTPException(
            status_code=403,
            detail=(
                f"不能 {action}：该 service 由远端 peer ({peer_label}) 提供，"
                "请到对端机器操作，或先在本机启动一个新实例"
            ),
        )


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
    services = section.get("services") if isinstance(section, dict) else None
    if not isinstance(services, list):
        services = []
    return {"services": services}


@router.put("/compute/config")
async def put_compute_config(payload: ComputeConfigPayload) -> dict:
    """全量替换 ``config.json["compute"]``，**不**触发 live 启动。

    Pydantic ``extra="ignore"``：未知字段（``instances`` 等）静默丢弃。
    """
    from app.services.config import get_config
    try:
        snap = get_config().set_section("compute", payload.model_dump())
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"persist 失败: {e}") from e
    return {"ok": True, "data": snap}


@router.post("/compute/instances")
async def start_instance(req: InstanceRequest) -> dict:
    """用户在算力配置页点"启用"（model + NPU 已选定）。

    流程：
      1. ``ensure_worker_for_model(model)``：
         - 本机已有 → 复用
         - daemon 上有 peer → adopt
         - 否则 → spawn 本机新进程
      2. ``bind_instance(model, npu_index)``：RPC ``load{npu_index}`` →
         worker 调 ``LLMSession(model, npu=N)``。spawned 与 adopted_local
         都允许；adopted (远端 peer) → 403。

    Raises:
        400: npu_index 越界 / 未知 model。
        403: model 是 adopted from peer，本机无权 bind。
    """
    reg = get_registry()
    try:
        inst = reg.ensure_worker_for_model(req.model, allow_adopt=False)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if inst._source == "adopted":
        peer_label = inst._peer_name or (inst._peer_guid or "")[:8] or "peer"
        raise HTTPException(
            status_code=403,
            detail=(
                f"{req.model} 是远端 peer ({peer_label}) 提供的 service，"
                "本机无权 bind。请到对端机器上点启用，或先在本机启动"
                "一个新实例（unavailable → spawned）。"
            ),
        )
    try:
        inst = reg.bind_instance(inst, req.npu_index)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True, "instance": inst.to_dict(), "snapshot": reg.snapshot()}


@router.delete("/compute/instances")
async def stop_instance(req: StopInstanceRequest) -> dict:
    """用户在算力配置页点"禁用"——只卸载模型，**不**关 worker 进程。

    worker 进程保留在 ``not_loaded`` 状态：NPU 显存释放（LLMSession 句柄
    drop），但 cshim Hub / RpcClient / 进程句柄全部保留；用户在算力配置
    页再次点"启用"可秒级走 RPC ``load`` 重载，无需重启 worker。

    对 spawned / adopted_local / adopted 三类都允许：
      * spawned / adopted_local —— 本机 RPC 给本机 worker，LLMSession
        释放后 worker 回到 ``not_loaded``；
      * adopted —— RPC 让对端 peer 释放 LLMSession，本机 registry 状态
        同步翻成 ``not_loaded``；peer 进程不被本机关。

    完全 kill worker 进程的能力已**移除**（"重启服务"按钮走 Q3 4 步自带
    unload+shutdown+respawn）；孤儿 worker 由下次 boot 的
    ``_find_workers_by_name`` 兜底。

    Raises:
        404: model 不在 registry。
    """
    reg = get_registry()
    inst = reg._find_by_model(req.model)
    if inst is None:
        raise HTTPException(
            status_code=404, detail=f"{req.model} 不在 registry")
    try:
        reg.unload_instance(req.model)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True, "snapshot": reg.snapshot()}


@router.post("/compute/restart")
async def restart_instance(req: RestartRequest) -> dict:
    """用户在算力配置页点"重启"——Q3 4 步：
      1. RPC ``unload``
      2. RPC ``shutdown``
      3. wait 3s → kill -9 fallback
      4. 走 boot 流程（probe → adopt / spawn）。不自动 bind。

    重启后 worker 回到 ``not_loaded``，用户需在算力配置页再点"启用"选 NPU。

    仅 spawned 允许；adopted peer → 403。
    """
    reg = get_registry()
    inst = reg._find_by_model(req.model)
    if inst is None:
        raise HTTPException(
            status_code=404, detail=f"{req.model} 不在 registry")
    _require_owned(inst, "重启")
    try:
        reg.restart_instance_local(req.model)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True, "snapshot": reg.snapshot()}


@router.get("/compute/capabilities")
async def get_capabilities() -> dict:
    """Discovery 页面渲染源 —— 拆分为 SW + HW 两部分。

    * ``sw_capabilities`` 来自 running/loading/not_loaded 实例
    * ``hw_capabilities`` 来自 ``HARDWARE_CAPABILITIES`` 静态列表
      + ``config.json["compute"]["hardware_capabilities"]`` 的 enabled 状态。
    """
    reg = get_registry()
    return {
        "sw_capabilities": reg.list_active_capabilities(),
        "hw_capabilities": reg.list_hardware_capabilities(),
    }


class HardwareCapabilityUpdate(BaseModel):
    """PUT /api/compute/hardware-capabilities 的请求体。

    key 必须是已注册的 HW capability_type（如 ``hwcap_video_input``），
    value 必须是严格的 bool（不接受字符串 / 数字 0/1 等的隐式转换）。
    未知的 key 静默忽略（响应里 rejected 列出）。
    """

    model_config = {"extra": "ignore"}

    updates: dict[str, bool] = Field(
        default_factory=dict,
        description="HW capability_type → enabled 映射。",
    )

    @field_validator("updates", mode="before")
    @classmethod
    def _strict_bools(cls, v):
        if not isinstance(v, dict):
            raise ValueError("updates 必须是 object")
        for k, val in v.items():
            if not isinstance(val, bool):
                raise ValueError(
                    f"updates[{k!r}] 必须是 bool (true/false), got {type(val).__name__}"
                )
        return v


@router.put("/compute/hardware-capabilities")
async def put_hardware_capabilities(payload: HardwareCapabilityUpdate) -> dict:
    """更新 HW 能力启用状态，落盘到 ``config.json["compute"]["hardware_capabilities"]``。"""
    reg = get_registry()
    result = reg.set_hardware_capability_state(payload.updates)
    return {
        "ok":       True,
        "applied":   result["applied"],
        "rejected":  result["rejected"],
        "state":     result["state"],
        "hw_capabilities": reg.list_hardware_capabilities(),
    }


@router.post("/compute/stop-all")
async def stop_all_instances() -> dict:
    """全部停止 —— 只停 spawned workers；adopted peers 不动。"""


class ClearErrorsRequest(BaseModel):
    """``POST /compute/clear-errors`` 入参：清空错误状态 + UI banner。

    ``model_id`` 可选 —— 缺省清全部，传入时只清那一条。
    """

    model_id: Optional[str] = None


@router.post("/compute/clear-errors")
async def clear_errors(payload: ClearErrorsRequest) -> dict:
    """清空 error banner —— 仅清错误状态（status 翻 not_loaded +
    ``error`` 字符串置空），不重启 / 卸载 worker。下次用户点 toggle
    启用会重新走 RPC load。

    用于修复 v0.8 banner "全部清空" 误调 stopAll 的 bug：banner 跟
    stopAll 是两件事。
    """
    reg = get_registry()
    cleared = reg.clear_error(payload.model_id)
    return {"ok": True, "cleared": cleared, "snapshot": reg.snapshot()}
    reg = get_registry()
    reg.stop_all_local()
    return {"ok": True, "snapshot": reg.snapshot()}