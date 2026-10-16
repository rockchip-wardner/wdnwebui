"""算力注册中心 REST API (v0.7)。

Endpoints
---------
* ``GET    /api/compute``                  完整 snapshot（cards / services /
                                          instances / capabilities / tools）。
                                          UI 算力配置页 / discovery 页都从这里读。
* ``GET    /api/compute/config``           读 ``config.jsonc["compute"]["autostart"]``
                                          （只读；编辑请直接改 config.jsonc）。
* ``PUT    /api/compute/config``           全量替换 ``compute`` section。
                                          **不**触发 live 启停，要生效需重启。
* ``POST   /api/compute/instances``        ``{npu_index, model}`` → 用户在算力
                                          配置页点"启用"。两步：
                                          (1) ensure worker（probe → adopt /
                                          spawn）；(2) RPC ``load`` 绑定 npu。
                                          adopted peer → 403（本机无权 bind）。
* ``DELETE /api/compute/instances``        ``{model}`` → "禁用"。
                                          卸载模型**并关闭 worker 进程**
                                          （unload → shutdown → SIGTERM →
                                          3s → SIGKILL），实例随后从
                                          registry 移除。重启用走冷启。
                                          spawned / adopted_local 允许；
                                          adopted peer → 403。
* ``POST   /api/compute/restart``          ``{npu_index?, model}`` → Q3 4 步：
                                          unload → shutdown → wait/kill →
                                          走 boot 流程（不自动 bind）。
                                          仅 spawned 允许。
* ``POST   /api/compute/stop-all``         全部停止（仅 spawned，adopted 不动）。
* ``GET    /api/compute/capabilities``     discovery 页用：``sw_capabilities``
                                          + ``hw_capabilities``。
* ``PUT    /api/compute/hardware-capabilities``   HW 能力 enable/disable。
* ``GET    /api/compute/active-app``      当前"卡片模式"：用户进入某
                                          demo 卡片后写入；reboot 后
                                          前端 boot 时读，若非空则直接
                                          跳到该卡片页。
* ``PUT    /api/compute/active-app``      ``{app_id}`` → 校验该 app
                                          所需的 software_capabilities
                                          全部有 local running 实例承载，
                                          写 ``compute.active_app``，
                                          并把承载它的 (model, npu_index)
                                          增量写进 ``compute.autostart``
                                          （下次 reboot 也会启）。
* ``DELETE /api/compute/active-app``      清掉 ``compute.active_app`` 标记。
                                          **不**动 autostart。

错误码：

* 400 —— pydantic 校验失败（缺字段、model 未知、npu_index 越界）。
* 403 —— 操作的是 adopted / unavailable service（本机无权启停）。
* 404 —— list_cards 返回空（未检测到 NPU）或 stop 时实例不存在。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.compute.registry import (
    MODEL_PRESETS,
    get_registry,
)


router = APIRouter(tags=["compute"])


# --------------------------------------------------------------------------- #
# Pydantic 模型
# --------------------------------------------------------------------------- #
class InstanceRequest(BaseModel):
    """``POST /compute/instances`` 入参：用户选好 (model, npu) 后启用。

    ``npu_index`` 必填 —— enable 步骤包含 RPC load 绑定 NPU。

    **可以是单个卡号，也可以是一串卡号**：算力页的「运行在」是多选，
    同一个模型可以同时跑在两张卡上（两个独立 worker，各占一卡，各自
    一个按 PID 命名的 DDS service）。传 ``[0, 1]`` 会依次为每张卡
    spawn+bind，返回的 ``instances`` 是本次**新建**的那些。
    """
    #: Range checked in ``_npu_list`` instead of via ``Field(ge=..., le=...)``:
    #: those constraints do not survive the ``int | list[int]`` union.
    npu_index: int | list[int]
    model: str

    @field_validator("npu_index")
    @classmethod
    def _npu_list(cls, v):
        """Normalise ``0`` / ``[0]`` / ``[0, 1]`` to a de-duplicated list."""
        items = [v] if isinstance(v, int) else list(v)
        if not items:
            raise ValueError("npu_index 不能为空列表")
        out: list[int] = []
        for n in items:
            if not isinstance(n, int) or isinstance(n, bool):
                raise ValueError(f"npu_index 必须是整数，got {n!r}")
            if n < 0:
                raise ValueError(f"npu_index 必须 >= 0（实际卡号，0 起），got {n}")
            if n not in out:
                out.append(n)
        return out

    @field_validator("model")
    @classmethod
    def _known_model(cls, v: str) -> str:
        if v not in MODEL_PRESETS:
            raise ValueError(
                f"未知 model: {v!r}（可选: {sorted(MODEL_PRESETS.keys())}）"
            )
        return v


class StopInstanceRequest(BaseModel):
    """``DELETE /compute/instances`` 入参。

    ``npu_index`` 缺省 = 停掉该模型的全部实例；给了卡号就只停那一张（多卡
    并行推理时用户要能「停掉 NPU1 那份、留着 NPU0」）。
    """
    model: str
    npu_index: Optional[int] = None

    @field_validator("model")
    @classmethod
    def _known_model(cls, v: str) -> str:
        if v not in MODEL_PRESETS:
            raise ValueError(
                f"未知 model: {v!r}（可选: {sorted(MODEL_PRESETS.keys())}）"
            )
        return v

    @field_validator("npu_index")
    @classmethod
    def _card_id(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and v < 0:
            raise ValueError(f"npu_index 必须是非负整数: {v!r}")
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


class AutostartEntry(BaseModel):
    """``compute.autostart[]`` 的单条 entry schema.

    ``model``      ——  ``MODEL_PRESETS`` key，即 ``workers/<model_id>.json``
                      的文件名主干（如 ``"qwen3-vl-4b"``）。
    ``npu_index``  ——  开机要绑定的**真实卡号**（直接来自 ``list_cards()``
                      的 ``npu_index``，0 起）。spawn 时作为 ``--npu``
                      传给 worker，worker 自己开始加载模型。

    不在 ``autostart`` 里的模型开机不启动（不 spawn / 不 adopt / 不占
    NPU 显存），算力页仍显示为 unavailable，可手动启用。

    校验规则：
    * ``model`` 必须在 ``MODEL_PRESETS`` 中存在（否则 422）；
    * ``npu_index`` 必须是非负整数（否则 422）；
    * 同一模型允许写多条，只要 ``npu_index`` 不同（跨卡并行推理）；同一张
      卡上重复同模型 → 422；
    * 旧 ``services[]`` 的 ``label`` / ``device`` 字段已删除 —— label
      归 ``workers/<id>.json``，卡号直接写 ``npu_index``。带上旧字段会
      被当成未知字段报错，而不是被静默忽略。
    """

    model_config = {"extra": "forbid"}

    model: str
    npu_index: int

    @field_validator("model")
    @classmethod
    def _known_model(cls, v: str) -> str:
        if v not in MODEL_PRESETS:
            raise ValueError(
                f"未知 model: {v!r}（可选: {sorted(MODEL_PRESETS.keys())}）"
            )
        return v

    @field_validator("npu_index")
    @classmethod
    def _npu_nonnegative(cls, v: int) -> int:
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError(f"npu_index 必须是整数，got {v!r}")
        if v < 0:
            raise ValueError(
                f"npu_index 必须 >= 0（实际卡号，0 起），got {v}")
        return v


class ComputeConfigPayload(BaseModel):
    """``PUT /api/compute/config`` 入参。

    ``autostart`` 每条由 :class:`AutostartEntry` 校验；(model, npu_index) 组合唯一。

    其它字段（hardware_capabilities / app.prompts /
    tool_call_max_iters 等）按现有语义落盘；未知字段被 ``extra="ignore"``
    丢弃。注意这里只 ignore **顶层**字段——``autostart`` 的每一条内部
    是 ``extra="forbid"``，所以把旧 ``services`` 里的 label / device 顺手
    拷进来会明确报错，而不是悄悄丢掉。

    已删除的顶层 ``services`` 键**单独拒绝**（见 ``_reject_legacy_services``）：
    本端点是全量替换 ``compute`` section，旧客户端发来的
    ``{"services": [...]}`` 若只是 ignore 掉，``autostart`` 会取默认值
    ``[]`` 并把用户现有的开机自启配置整个清空——静默数据丢失比报错糟得多。
    """

    model_config = {"extra": "ignore"}

    autostart: list[AutostartEntry] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _reject_legacy_services(cls, data):
        if isinstance(data, dict) and "services" in data:
            raise ValueError(
                "compute.services 已删除，请改用 autostart："
                '[{"model": "qwen3-vl-4b", "npu_index": 0}]。'
                "旧字段（label / device / autostart=enabled）不再被接受。"
            )
        return data

    @field_validator("autostart")
    @classmethod
    def _unique_models(cls, v: list[AutostartEntry]) -> list[AutostartEntry]:
        """唯一性按 (model, npu_index)：跨卡重复是并行推理要的，同卡重复才是冲突。"""
        seen: set[tuple[str, int]] = set()
        for entry in v:
            key = (entry.model, entry.npu_index)
            if key in seen:
                raise ValueError(
                    f"compute.autostart 含重复条目: model={entry.model!r} "
                    f"npu_index={entry.npu_index}；同一张卡上不能挂两份同一个"
                    "模型（同卡叠模型会加载失败），换卡请写不同的 npu_index。"
                )
            seen.add(key)
        return v


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
    """读 ``config.jsonc["compute"]["autostart"]``（不触发任何启动）。

    这个端点原本配套系统配置页的「算力实例列表」编辑表格；那张表已随
    ``compute.services`` 一起删除，这里保留为**只读**，方便脚本 / 运维
    确认当前开机自启配置。写入仍走 Pydantic 的 ``AutostartEntry`` 校验。
    """
    from app.core.config import get_config
    section = get_config().get_section("compute", {})
    raw = section.get("autostart") if isinstance(section, dict) else None
    entries: list[dict] = []
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, dict) and isinstance(entry.get("model"), str):
                entries.append(entry)
    return {"autostart": entries}


@router.put("/compute/config")
async def put_compute_config(payload: ComputeConfigPayload) -> dict:
    """全量替换 ``config.jsonc["compute"]``，**不**触发 live 启动。

    改了 ``autostart`` 不会立刻启停模型 —— 要等下次 boot 生效。启停请用
    ``/api/compute/instances``。

    未知**顶层**字段被 ``extra="ignore"`` 丢弃；但 ``autostart`` 的每条
    内部是 ``extra="forbid"``，所以把旧 ``services`` 的 label / device
    拷进来会明确报错，而不是悄悄丢掉。
    """
    from app.core.config import get_config
    try:
        snap = get_config().set_section("compute", payload.model_dump())
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"persist 失败: {e}") from e
    return {"ok": True, "data": snap}


@router.post("/compute/instances")
async def start_instance(req: InstanceRequest) -> dict:
    """用户在算力配置页点"启用"（model + NPU 已选定）。

    流程（每张选中的卡各走一遍）：
      1. ``ensure_worker_for_model(model, npu_index=n)``：
         - 本机该卡上已有 → 复用
         - daemon 上有 peer → adopt
         - 否则 → spawn 本机新进程
      2. ``bind_instance(inst, n)``：RPC ``load{n}`` →
         worker 调 ``LLMSession(model, npu=n)``。spawned 与 adopted_local
         都允许；adopted (远端 peer) → 403。

    ``req.npu_index`` 可以是单个卡号或一串。传一串时按顺序逐张启用，
    同一个模型因此可以在多张卡上并存（各有各的 worker 和 per-PID DDS
    service）。单张失败不影响其余卡 —— 失败信息落在对应的
    ``errors[]`` 里，最终仍返回 200 + 完整 snapshot，让算力页照常
    渲染部分成功的状态。

    Raises:
        400: npu_index 越界 / 未知 model。
        403: model 是 adopted from peer，本机无权 bind。
    """
    reg = get_registry()
    npu_indices = (req.npu_index if isinstance(req.npu_index, list)
                   else [req.npu_index])
    created: list[dict] = []
    errors: list[dict] = []
    for n in npu_indices:
        try:
            # eager_load=False：算力页的卡是用户在 UI 上选的，走
            # 「spawn（不带 --npu）→ RPC action=load」这条路。若同时传
            # --npu 让 worker 自己 eager-load，两条 load 会争同一个
            # session slot，worker 直接 rc=-6（真机实测）。
            inst = reg.ensure_worker_for_model(req.model, allow_adopt=False,
                                               npu_index=n, eager_load=False)
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
            inst = reg.bind_instance(inst, n)
        except RuntimeError as e:
            errors.append({"npu_index": n, "detail": str(e)})
            continue
        created.append(inst.to_dict())
    return {"ok": not errors, "instances": created,
            "errors": errors, "snapshot": reg.snapshot()}


@router.delete("/compute/instances")
async def stop_instance(req: StopInstanceRequest) -> dict:
    """用户在算力配置页点"禁用"——卸载模型**并关闭 worker 进程**。

    走 :meth:`ComputeRegistry.stop_instance_local`，顺序是
    unload → RPC shutdown → SIGTERM → 3s → SIGKILL：

    * **先 unload**（``unload_instance`` 同步阻塞等 worker reply，
      worker 在 reply 前已跑完 ``rknn3_destroy``）——NPU 内存池是真的
      归还了才动进程。反过来先 shutdown 会让 worker 在 LLMSession
      还活着时退出，池子一直被占，光杀进程就没意义。
    * shutdown 让 worker 走优雅退出（主循环 finally 关闭 audio
      writer / served session / side hub，atexit 清 ``/dev/shm``）；
      SIGTERM 是第二保险——worker 侧装了等效 handler，且不依赖
      Requester 还能用（``adopted_local`` 的 ``proc`` 是 None，
      SIGTERM 那步会跳过，它只能靠 shutdown RPC 自退）。

    禁用后实例从 registry 移除，算力页状态变成 ``unavailable``（不再是
    ``not_loaded``）。重新点"启用"要走完整冷启：spawn + RPC load。

    ``spawned`` / ``adopted_local`` 都允许；``adopted``（远端 peer）
    仍然 403 —— 那是别的板子的算力，本机无权关。

    **注意**：推理进行中禁用会打断推理。worker 侧 ``close()`` 用的是
    ``executor.shutdown(wait=False)``，不会等在跑的推理跑完。

    与 ``POST /compute/stop-all`` 的**有意不一致**：那个端点（只有 TTS
    卡片在用）仍然只 unload 不杀进程。别把它们当成同一个语义。

    Raises:
        404: model（+ npu_index）不在 registry。
        403: 实例来自远端 peer。
        400: registry 侧停止失败。
    """
    reg = get_registry()
    inst = (reg._instances_of(req.model, req.npu_index) or [None])[0]
    if inst is None:
        where = f" (npu_index={req.npu_index})" if req.npu_index is not None else ""
        raise HTTPException(
            status_code=404,
            detail=f"{req.model}{where} 不在 registry")
    # 安全护栏:adopted 来自其他板子的 DDS peer worker,本机无权限
    # 启停(每块板子只能管理自己的本地算力)。若允许 DELETE,后端会
    # 通过 RPC 通知对端 worker 卸载 LLMSession,误关其他板子的 NPU
    # 显存 — 这是 wizard 一键开启的早期严重 bug 留下的纵深防御。
    # spawned / adopted_local(本机)继续原逻辑。
    if inst._source == "adopted":
        peer_label = (inst._peer_name
                      or (inst._peer_guid or "")[:8]
                      or "远端 peer")
        raise HTTPException(
            status_code=403,
            detail=(
                f"不能通过网络删除 {req.model}(来自 {peer_label});"
                f"请到目标板子上点禁用,或在本机启动一个新实例后删除本地副本。"
            ),
        )
    try:
        reg.stop_instance_local(req.model, req.npu_index)
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
      + ``config.jsonc["base"]["hardware_capabilities"]`` 的 enabled 状态。
    """
    reg = get_registry()
    return {
        "sw_capabilities": reg.list_active_capabilities(),
        "hw_capabilities": reg.list_hardware_capabilities(),
    }


@router.get("/compute/instances-by-capability")
async def list_instances_by_capability(capability: str) -> dict:
    """返回当前 running 实例中能提供指定 capability 的清单。

    供音视频描述 / 视频目标检测 / 音频理解等消费者页面的"模型选择"
    下拉框使用。返回每条实例的最小展示信息 (``model_id`` /
    ``label`` / ``source`` / ``npu_index`` / ``pid``),前端用
    ``model_id`` 在 start 请求体里回传给后端。 本机实例排序在前,
    跟 ``get_session`` 自动选择语义一致,默认选项就是无下拉框时
    用户会拿到的那个。
    """
    if not capability or not isinstance(capability, str):
        raise HTTPException(
            status_code=400,
            detail="capability query 参数必填 (例如 swcap_video_describe)")
    reg = get_registry()
    return {
        "capability":  capability,
        "instances":   reg.list_running_by_capability(capability),
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
    """更新 HW 能力启用状态，落盘到 ``config.jsonc["base"]["hardware_capabilities"]``。"""
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
    """全部停止 — 释放所有 model 的 LLMSession (worker 进程保留)。
    spawned / adopted_local 走本机 RPC;adopted peers 也走 RPC 让对端
    释放,但本机不终止 worker。

    **有意与 ``DELETE /api/compute/instances`` 语义不同**：那个端点
    （算力页"禁用"单个）现在会关闭 worker 进程,这个不会。它只
    ``unload_instance`` —— 进程留着,重新点"启用"走 RPC load 重载。
    别把两者当成同一件事。"""
    reg = get_registry()
    stopped: list[str] = []
    errors: list[str] = []
    for inst in list(reg._instances.values()):
        try:
            reg.unload_instance(inst.model_id)
            stopped.append(inst.model_id)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{inst.model_id}: {e}")
    return {
        "ok": not errors,
        "stopped": stopped,
        "errors": errors,
        "snapshot": reg.snapshot(),
    }


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


# --------------------------------------------------------------------------- #
# active_app：用户进入卡片 → 记录 → reboot 后自动回到同一卡片                  #
# --------------------------------------------------------------------------- #
#
# 三段语义：
#   GET    /api/compute/active-app     —— 前端 boot 时读，若非空 + 该卡片
#                                         ok 状态 → 直接 ``#<app_id>`` 跳过去。
#   PUT    /api/compute/active-app     —— 用户进入 app、且该 app 所有
#                                         software_capabilities 都有 local
#                                         running 实例时调用。后端会：
#                                         (1) 校验 (a) app_id 真实存在且 status=ok
#                                         (b) 所需 caps 全部有 local running；
#                                         (2) 把承载这些 caps 的 (model, npu_index)
#                                         增量写进 compute.autostart —— 之前没
#                                         启的 worker 下次 reboot 也会起来。
#                                         失败时 409 + 原因。
#   DELETE /api/compute/active-app     —— 用户离开 / 切换 / 关闭 tab 时
#                                         调用，**不**动 autostart（autostart
#                                         仍负责"该启的算力"，由用户在算力页
#                                         显式关闭）。

def _resolve_card_manifest(app_id: str):
    """按 app_id 查 manifest。

    找不到 / 状态非 ok → 返回 ``None``。API 层据此决定 404 vs 200。
    """
    # 懒加载：避免在 system register 时就 import manifest 模块循环依赖。
    from app.core.manifest import discover as discover_cards
    try:
        manifests = discover_cards()
    except Exception:  # noqa: BLE001
        return None
    for m in manifests:
        if m.id == app_id and getattr(m, "status", "ok") == "ok":
            return m
    return None


@router.get("/compute/active-app")
async def get_active_app() -> dict:
    """读 ``compute.active_app``。

    返回 ``{"active_app": str | null}``。前端 boot 时拿到非空 + 该卡片
    ok → 直接跳过去，不再让用户走一次 wizard。``null`` 表示当前
    没有处于"app 模式"——首页照常显示。
    """
    reg = get_registry()
    return {"active_app": reg.get_active_app()}


class ActiveAppSet(BaseModel):
    """``PUT /compute/active-app`` 入参。"""

    app_id: str = Field(
        ...,
        min_length=1,
        description="目标卡片 id，对应 app/apps/<id>/manifest.json 的 id 字段。",
    )

    @field_validator("app_id", mode="before")
    @classmethod
    def _strip(cls, v):
        if isinstance(v, str):
            v = v.strip()
        return v


@router.put("/compute/active-app")
async def put_active_app(payload: ActiveAppSet) -> dict:
    """记录"用户当前所在 app"。

    校验链（任一不过都 409 + 明确文案）：

    1. ``app_id`` 对应的 manifest 必须存在且 ``status="ok"``：
   找不到 → 409（前端不可能触发，是防御性的）。
    2. manifest 的 ``software_capabilities`` 必须全部有 **local** running
       实例承载（spawned ∪ adopted_local）。adopted peer 故意排除——
       那台板子 reboot 之后 autostart 等于无效。
    3. 落盘 ``compute.active_app = app_id``，并把当前承载这些 caps
       的 (model, npu_index) 增量写进 ``compute.autostart``。已存在
       的项不重复加。
    """
    app_id = payload.app_id
    manifest = _resolve_card_manifest(app_id)
    if manifest is None:
        raise HTTPException(
            status_code=404,
            detail=f"未找到卡片 {app_id!r}，或该卡片状态非 ok",
        )

    caps = list(getattr(manifest, "software_capabilities", []) or [])
    reg = get_registry()
    try:
        result = reg.set_active_app(app_id, caps)
    except ValueError as e:
        # 校验失败（缺 cap / 未知 cap）→ 409 让前端展示原因。
        raise HTTPException(status_code=409, detail=str(e)) from e
    return result


@router.delete("/compute/active-app")
async def delete_active_app() -> dict:
    """清掉 ``compute.active_app`` 标记。

    不动 autostart。响应永远是 ``{"ok": True, "active_app": null}``。
    """
    reg = get_registry()
    reg.clear_active_app()
    return {"ok": True, "active_app": None}