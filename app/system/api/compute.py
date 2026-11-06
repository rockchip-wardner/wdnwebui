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
* ``POST   /api/compute/refresh-models``   重新扫描模型根目录，刷新「本板
                                            装了哪些模型」（后台每 ~2s 自动
                                            扫一次，这个端点是显式版本）。
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
* ``DELETE /api/compute/active-app``      清掉 ``compute.active_app``
                                          **并把 ``compute.autostart``
                                          置空**（退出应用 = "不想让这些
                                          算力下次开机自己起来"）。配合上面
                                          PUT 的"增量追加"，autostart 就是
                                          本次会话用到的算力集合，进卡加、
                                          退出清，不再只增不减。

错误码：

* 400 —— 业务校验失败（**npu_index 越界 / 不是算力卡位**）。
* 403 —— 操作的是 adopted / unavailable service（本机无权启停）。
* 404 —— list_cards 返回空（未检测到 NPU）或 stop 时实例不存在。
* 409 —— 该模型在这张卡上还处于过渡态（loading / stopping），排队等完
         仍没结束。
* 422 —— pydantic 校验失败（缺字段、model 未知、npu_index 不是整数）。
"""
from __future__ import annotations

import asyncio
import time
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.compute.registry import (
    MODEL_PRESETS,
    card_count_override,
    get_registry,
    refresh_model_presets,
    transition_of,
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
    #:
    #: 这里只校验**形状**（整数 / 非负 / 去重）。「卡号是否真的存在」是
    #: **运行时状态**（本机探测到几张卡、哪几张能跑 rknn 模型），只能在
    #: handler 里查 —— 见 ``start_instance`` 里的 ``reg.npu_card_error()``。
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
    npu_index: int | list[int]

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
    def _npu_cards(cls, v):
        items = [v] if isinstance(v, int) else list(v)
        if not items:
            raise ValueError("npu_index 不能为空列表")
        out: list[int] = []
        for n in items:
            if isinstance(n, bool) or not isinstance(n, int):
                raise ValueError(f"npu_index 必须是整数，got {n!r}")
            if n < 0:
                raise ValueError(
                    f"npu_index 必须 >= 0（实际卡号，0 起），got {n}")
            if n not in out:
                out.append(n)
        return out[0] if len(out) == 1 else out

    @model_validator(mode="after")
    def _requires_cards(self):
        requires = int(MODEL_PRESETS[self.model].get("requires_cards", 1) or 1)
        cards = (self.npu_index if isinstance(self.npu_index, list)
                 else [self.npu_index])
        if len(cards) != requires:
            raise ValueError(
                f"model={self.model!r} 需要 {requires} 张卡，"
                f"npu_index={self.npu_index!r}")
        return self


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
    #: 算力卡位数上限（手工指定）。**必须在这里声明** —— 本端点是全量替换
    #: ``compute`` section，未声明的字段会被 ``extra="ignore"`` 丢掉，运维
    #: 手写的 ``card_count`` 会在任何一次保存后静默消失。
    #: 语义是「对探测结果的上限」：双卡板写 1 → 模拟单卡板。
    card_count: int | None = Field(default=None, ge=1)

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
        seen: set[tuple[str, tuple]] = set()
        for entry in v:
            cards = (tuple(entry.npu_index)
                     if isinstance(entry.npu_index, list)
                     else (entry.npu_index,))
            key = (entry.model, cards)
            if key in seen:
                raise ValueError(
                    f"compute.autostart 含重复条目: model={entry.model!r} "
                    f"npu_index={entry.npu_index}；同一张卡上不能挂两份同一个"
                    "模型（同卡叠模型会加载失败），换卡请写不同的 npu_index。"
                )
            seen.add(key)
        return v


# --------------------------------------------------------------------------- #
# 过渡态排队
#
# 模型处在「启动中」(loading) 或「关闭中」(stopping) 时,新的启停请求
# 不能直接执行 —— 都会 rc=-6:
#
#   * loading 中再 load  → worker 侧只在 running 时幂等 → 再起一个 init
#     线程 → 两个 init 线程抢同一份 NPU 内存。
#   * stopping 中再 load → session 正在被 close,新 session 又要占内存。
#   * loading 中再 stop  → init 线程被 SIGKILL 砍掉,rknn3_destroy 没跑
#     → NPU 显存不归还。**硬**停才有这个风险;等它加载完再停是安全的。
#   * stopping 中再 stop → 实例已出 registry → 误报 404。
#
# 策略:启 → 排队等它结束;停 → 也排队(loading 等它加载完,stopping 等它
# 关完)。停之所以不能拒绝:调用方要先腾出目标卡再启新模型,拒绝的话卡上
# 那份继续占显存 → 新模型显存不足 → worker rc=-6 起不来。
# 详见 AGENTS.md「模型启停的过渡态」。
# --------------------------------------------------------------------------- #

# loading 要等模型加载完:真机 30~90s(omni / LocateAnything 约 60s)。
_QUEUE_WAIT_LOADING_S = 120.0
# stopping 要等 5 步拆除(unload RPC + SIGTERM + 3s 宽限 + SIGKILL):~10s。
_QUEUE_WAIT_STOPPING_S = 30.0


async def _await_quiet(reg, model_id: str, npus: Optional[set],
                       timeout_s: float) -> bool:
    """等 ``model_id`` 在 ``npus``(None = 全部卡)上的过渡态结束。

    True = 已静默可执行启停;False = 超时。

    **只看本机实例**(``transition_cards(..., local_only=True)``):远端
    peer 的过渡态是对端板子的事,它的卡号不是本机的卡号 —— 把它算进来
    会让本机启同名模型被对端的状态挡住(真机实测:peer 的 qwen3-vl-4b 卡
    在 loading → 本机排队 120s → 409 → 向导「一键开启」一直转圈)。

    **必须 async 轮询**:单进程 uvicorn 只有一个事件循环,同步 sleep 会
    把整个服务卡住(DELETE 本身就在同步阻塞事件循环)。
    """
    deadline = time.monotonic() + timeout_s
    while True:
        busy = [(n, k) for (n, k) in reg.transition_cards(model_id, local_only=True)
                if npus is None or n in npus]
        if not busy:
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.25)


def _transitions_of(reg, model_id: str, npu: Optional[int]) -> set:
    """该模型在指定卡(不给 = 全部卡)上的过渡态种类集合(只算本机实例)。"""
    return {k for (n, k) in reg.transition_cards(model_id, local_only=True)
            if npu is None or n == npu}


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
    """读 ``config.jsonc["compute"]``（不触发任何启动）。

    这个端点原本配套系统配置页的「算力实例列表」编辑表格；那张表已随
    ``compute.services`` 一起删除，这里保留为**只读**，方便脚本 / 运维
    确认当前开机自启配置。写入仍走 Pydantic 的 ``AutostartEntry`` 校验。

    ``card_count`` 是算力卡位数上限（未配置 = 按探测结果）。
    """
    from app.core.config import get_config
    section = get_config().get_section("compute", {})
    raw = section.get("autostart") if isinstance(section, dict) else None
    entries: list[dict] = []
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, dict) and isinstance(entry.get("model"), str):
                entries.append(entry)
    # 走 registry 的同一个函数取值 —— 非法值（0 / 非整数 / bool）在两处
    # 必须判成同一个结果，不能这边回读出 0、那边当没配。
    return {"autostart": entries, "card_count": card_count_override()}


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
        400: npu_index 越界 / 不是算力卡位。
        403: model 是 adopted from peer，本机无权 bind。
        422: pydantic 校验失败（缺字段 / model 未知 / npu_index 不是整数）。
    """
    reg = get_registry()
    npu_indices = (req.npu_index if isinstance(req.npu_index, list)
                   else [req.npu_index])
    preset = MODEL_PRESETS.get(req.model, {})
    requires_cards = int(preset.get("requires_cards", 1) or 1)
    if requires_cards > 1:
        if len(npu_indices) != requires_cards:
            if len(npu_indices) == 1:
                npu_indices = list(reg.model_cards())[:requires_cards]
            if len(npu_indices) != requires_cards:
                raise HTTPException(
                    status_code=400,
                    detail=(f"{req.model} 需要 {requires_cards} 张协处理器卡同时"
                            f"运行（例如 npu_index=[0, 1]）"),
                )
        if len(reg.model_cards()) < requires_cards:
            raise HTTPException(
                status_code=400,
                detail=(f"{req.model} 需要 {requires_cards} 张算力卡，本机只有 "
                        f"{len(reg.model_cards())} 张"),
            )
    # 卡位校验**必须在过渡态排队之前**（v0.29 补）：越界的卡号等再久也
    # 起不来（worker 会 IndexError 秒退），先排队只会让用户白等 120s
    # 再收到一个语焉不详的 409。docstring 早就承诺了「npu_index 越界 → 400」，
    # 以前只有 `>= 0` 的校验，单卡板上传 npu=1 会造出僵尸实例。
    for n in npu_indices:
        bad = reg.npu_card_error(n)
        if bad:
            raise HTTPException(status_code=400, detail=bad)
    # 过渡态前置判定：目标卡还在启动/关闭 → 排队等它结束，而不是直接
    # 再 load 一次（见文件头的「过渡态排队」注释：那是 rc=-6 的来源）。
    want = set(npu_indices)
    if not await _await_quiet(reg, req.model, want, _QUEUE_WAIT_LOADING_S):
        raise HTTPException(
            status_code=409,
            detail=(f"{req.model} 仍在启动/关闭中，请稍后重试"),
        )
    created: list[dict] = []
    errors: list[dict] = []
    if requires_cards > 1:
        with reg.model_op_lock(req.model):
            try:
                inst = reg.ensure_worker_for_model(
                    req.model, allow_adopt=False,
                    npu_index=list(npu_indices), eager_load=False)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
            if inst._source == "adopted":
                raise HTTPException(
                    status_code=403,
                    detail=(f"{req.model} 是远端 peer 提供的 service，"
                            "本机无权 bind"),
                )
            if (tuple(inst.npu_indices or ()) == tuple(npu_indices)
                    and inst.status in ("loading", "running")):
                created.append(inst.to_dict())
            else:
                try:
                    inst = reg.bind_instance(inst, list(npu_indices))
                    created.append(inst.to_dict())
                except RuntimeError as e:
                    errors.append({"npu_index": npu_indices, "detail": str(e)})
        return {"ok": not errors, "instances": created,
                "errors": errors, "snapshot": reg.snapshot()}
    for n in npu_indices:
        # 同一模型的启停串行化 —— 排队的多个请求可能同时醒来。
        with reg.model_op_lock(req.model):
            try:
                # eager_load=False：算力页的卡是用户在 UI 上选的，走
                # 「spawn（不带 --npu）→ RPC action=load」。若同时传 --npu
                # 让 worker 自己 eager-load，两条 load 会争同一个 session
                # slot，worker 直接 rc=-6（真机实测）。
                inst = reg.ensure_worker_for_model(
                    req.model, allow_adopt=False,
                    npu_index=n, eager_load=False)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
            if inst._source == "adopted":
                peer_label = (inst._peer_name
                              or (inst._peer_guid or "")[:8] or "peer")
                raise HTTPException(
                    status_code=403,
                    detail=(
                        f"{req.model} 是远端 peer ({peer_label}) 提供的 "
                        "service，本机无权 bind。请到对端机器上点启用，或先在"
                        "本机启动一个新实例（unavailable → spawned）。"
                    ),
                )
            # 幂等：已经在这张卡上 loading / running → 不用再 load。
            # worker 侧只在 running 时幂等，loading 时会再起一个 init 线程
            # 抢同一份 NPU 内存（rc=-6），所以这里必须挡住。
            if inst.npu_index == n and inst.status in ("loading", "running"):
                created.append(inst.to_dict())
                continue
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
        409: 排队等过渡态结束超时（模型还在启动/关闭中）。
    """
    reg = get_registry()
    npu = req.npu_index
    want = {npu} if npu is not None else None
    # 过渡态先判 —— 停止期间 unload 的乐观写会把 npu_index 重置成 -1，
    # 按卡号查会「找不到」→ 误报 404（用户看到的「关闭一个正在关闭中
    # 的模型」报错就是这么来的）。过渡态表用 _stop_npu 兜住卡号。
    kinds = _transitions_of(reg, req.model, npu)
    waited_loading = False
    if "loading" in kinds:
        # **排队等它加载完 → 再停**,不是拒绝。
        #
        # 为什么不能拒绝:调用方「一键开启所需模型」要先腾出目标卡再启新
        # 模型。拒绝的话那份还在占显存 → 新模型加载时显存不足 → worker
        # rc=-6 起不来(真机实测:vd 的 omni 正在 npu1 启动,vtd 要把
        # qwen3-vl-4b 启到 npu1 → 起不来)。
        #
        # 为什么等它**加载完**是安全的:init 线程正常跑完、显存已归还,
        # 随后的 unload 走正常路径。不安全的反而是**硬** unload —— 那才会
        # 让 init 线程带不走 rknn3_destroy。
        if not await _await_quiet(reg, req.model, want, _QUEUE_WAIT_LOADING_S):
            raise HTTPException(
                status_code=409,
                detail=(f"{req.model} 仍在启动中，请稍后重试"),
            )
        waited_loading = True
        kinds = _transitions_of(reg, req.model, npu)
    if "stopping" in kinds:
        # 已经在关了 → 排队等它关完，返回幂等成功（不再发第二次 stop）。
        if not await _await_quiet(reg, req.model, want, _QUEUE_WAIT_STOPPING_S):
            raise HTTPException(
                status_code=409,
                detail=(f"{req.model} 正在关闭中，请稍后重试"),
            )
        return {"ok": True, "snapshot": reg.snapshot()}
    inst = (reg._instances_of(req.model, npu) or [None])[0]
    if inst is None:
        # 刚被本机停过 → 幂等成功(DELETE 本来就是幂等语义)。单进程下
        # 并发 DELETE 只能在事件循环里排队,轮到它时实例已不在(卡号也被
        # 重置成 -1)→ 靠 registry._recently_stopped 兜住。
        if reg.was_recently_stopped(req.model, npu):
            return {"ok": True, "snapshot": reg.snapshot()}
        # 等的那次启动**失败**了 → 实例已移出 registry，卡本来就是空的
        # → 幂等成功（别报 404：卡确实腾空了，调用方的目的已达成）。
        if waited_loading:
            return {"ok": True, "snapshot": reg.snapshot()}
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
        with reg.model_op_lock(req.model):
            # 锁内复查：等锁期间可能已经有人启/停过同一个模型。
            kinds2 = _transitions_of(reg, req.model, npu)
            if "stopping" in kinds2:
                # 已经在关了 → 幂等成功。
                return {"ok": True, "snapshot": reg.snapshot()}
            if "loading" in kinds2:
                # 又被启成 loading 了 → 本次**没停掉** → 别返回 ok 假装
                # 成功：调用方会以为卡腾空了就去启新模型 → 显存不够起不来。
                raise HTTPException(
                    status_code=409,
                    detail=(f"{req.model} 正在启动中，请稍后重试"),
                )
            reg.stop_instance_local(req.model, npu)
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
    # restart = unload + spawn,过渡态中做必然撞车(rc=-6 / 显存不归还)。
    kinds = _transitions_of(reg, req.model, None)
    if kinds:
        what = "启动中" if "loading" in kinds else "关闭中"
        raise HTTPException(
            status_code=409,
            detail=f"{req.model} 正在{what}，请稍后重试",
        )
    inst = reg._find_by_model(req.model)
    if inst is None:
        raise HTTPException(
            status_code=404, detail=f"{req.model} 不在 registry")
    _require_owned(inst, "重启")
    try:
        with reg.model_op_lock(req.model):
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
async def list_instances_by_capability(
    capability: Optional[List[str]] = Query(
        None,
        description=(
            "可重复：?capability=a&capability=b 表示 AND（同一实例须同时具备）"
        ),
    ),
    capabilities: Optional[str] = Query(
        None, description="逗号分隔的等价写法：?capabilities=a,b",
    ),
) -> dict:
    """返回当前 running 实例中能提供指定 capability 的清单。

    供音视频描述 / 视频目标检测 / 音频理解等消费者页面的"模型选择"
    下拉框使用。返回每条实例的最小展示信息 (``model_id`` /
    ``label`` / ``source`` / ``npu_index`` / ``pid``),前端用
    ``model_id`` 在 start 请求体里回传给后端。 本机实例排序在前,
    跟 ``get_session`` 自动选择语义一致,默认选项就是无下拉框时
    用户会拿到的那个。

    **多个能力 = AND 语义**: ``?capability=a&capability=b``(重复参数)
    或 ``?capabilities=a,b`` 返回**同时**具备两项能力的实例。这服务
    卡片的能力组 —— 一张卡要求视觉 + 工具调用来自**同一个**模型时,
    逐项分别查再在前端求交集分不清「一个实例都满足」和「两个实例各
    满足一半」,后者会一路绿灯到推理时才炸。

    只给一个(或零个)能力时行为与从前逐字节一致。
    """
    wanted: list[str] = []
    for c in (capability or []):
        if isinstance(c, str) and c.strip():
            wanted.append(c.strip())
    if isinstance(capabilities, str) and capabilities.strip():
        wanted.extend(p.strip() for p in capabilities.split(",") if p.strip())
    if not wanted:
        raise HTTPException(
            status_code=400,
            detail=(
                "capability query 参数必填 "
                "(例如 capability=swcap_vision_chat)"
            )
        )
    reg = get_registry()
    if len(wanted) == 1:
        instances = reg.list_running_by_capability(wanted[0])
    else:
        instances = reg.list_running_by_capabilities(wanted)
    return {
        "capability":  wanted[0],
        "capabilities": wanted,
        "instances":   instances,
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
    """全部停止 — 释放所有**本机** model 的 LLMSession (worker 进程保留)。

    只动本机实例(spawned / adopted_local)。**adopted peer 不动** —— 那是
    别的板子的算力,本机无权启停(每块板子只管自己的本地算力,与
    ``DELETE /api/compute/instances`` 的 403 护栏同一条原则)。

    **有意与 ``DELETE /api/compute/instances`` 语义不同**：那个端点
    （算力页"禁用"单个）现在会关闭 worker 进程,这个不会。它只
    ``unload`` —— 进程留着,重新点"启用"走 RPC load 重载。
    别把两者当成同一件事。"""
    reg = get_registry()
    stopped: list[str] = []
    errors: list[str] = []
    skipped: list[str] = []
    for inst in list(reg._instances.values()):
        # 跳过过渡态：正在启动中的实例硬 unload 会让 worker 的 init
        # 线程带不走 rknn3_destroy → NPU 显存不归还；正在关闭中的本来
        # 就在关,重复 unload 没意义。
        kind = transition_of(inst)
        if kind:
            skipped.append(f"{inst.model_id}:{kind}")
            continue
        # peer 是别的板子的算力 → 本机无权卸。
        if inst._source == "adopted":
            skipped.append(f"{inst.model_id}:peer")
            continue
        try:
            # 按**实例**卸(按模型名会连带卸掉同模型在其它卡上的实例,含 peer)。
            reg.unload_one_instance(inst)
            if inst.model_id not in stopped:
                stopped.append(inst.model_id)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{inst.model_id}: {e}")
    return {
        "ok": not errors,
        "stopped": stopped,
        "skipped": skipped,
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


@router.post("/compute/refresh-models")
async def refresh_models() -> dict:
    """重新扫描模型根目录，刷新「本板安装了哪些模型」。

    worker 的 manifest 随模型文件夹一起分发，所以**装了哪些模型是板子
    的状态**：用户从 HuggingFace clone 一个模型下来，本板就多一个模型。

    后台已有 ~2s 一次的自动重扫（``_model_rescan_loop``），这个端点是
    显式版本 —— clone 完等不及时点一下立刻生效。不会启停任何 worker。
    """
    reg = get_registry()
    changed = refresh_model_presets()
    return {
        "ok": True,
        "changed": changed,
        "installed": sorted(MODEL_PRESETS.keys()),
        "snapshot": reg.snapshot(),
    }


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
#                                         调用，**同时把 compute.autostart
#                                         置空**。旧行为是"不动 autostart"
#                                         （理由：autostart 仍负责"该启的算
#                                         力"，由用户在算力页显式关闭），但
#                                         配上上面 PUT 的增量追加，autostart
#                                         就只增不减：进过几张视频/音频卡之后
#                                         config.jsonc 攒到 5 条、npu0 挤 3 个
#                                         模型（估算 12500 MB），
#                                         qwen3-vl-embedding-2b 直接 rc=-6
#                                         起不来，而"去算力页手动关"没人记得
#                                         做。现在退出即清空，autostart 回到
#                                         "本次会话用到的算力"这个语义。

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
    capability_groups: Optional[List[List[str]]] = Field(
        None,
        description=(
            "可选的能力组：每组必须由**同一个**本机实例同时满足。卡片要求"
            "多个能力来自同一模型时必须传（如 usb_hid 需要视觉 + 工具调用）。"
            "缺省 = 每个能力各自成组（历史行为）。"
        ),
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
       那台板子 reboot 之后 autostart 等于无效。传了
       ``capability_groups`` 时按组校验：**单个**实例须同时满足组内全部
       能力，否则「两个实例各满足一半」会被误判成就绪。
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
    # 组优先用调用方传来的，否则回落到 manifest 派生值 —— 后端才是权威，
    # 浏览器漏传 / 传漏了也不至于把「两个实例各满足一半」判成就绪。
    groups = payload.capability_groups
    if groups is None:
        groups = [list(g) for g in (getattr(manifest, "capability_groups", []) or [])]
    reg = get_registry()
    try:
        result = reg.set_active_app(app_id, caps, groups)
    except ValueError as e:
        # 校验失败（缺 cap / 未知 cap / 组未被单个实例满足）→ 409 展示原因。
        raise HTTPException(status_code=409, detail=str(e)) from e
    return result


@router.delete("/compute/active-app")
async def delete_active_app() -> dict:
    """清掉 ``compute.active_app`` **并把 ``compute.autostart`` 置空**。

    响应永远是 ``{"ok": True, "active_app": None}``。autostart 的清理由
    :meth:`ComputeRegistry.clear_active_app` 一并落盘 —— 退出应用就是
    "我不想让这些算力下次开机自己起来"，留着只会让 autostart 只增不减、
    把 NPU 撑爆（见模块 docstring 里的实测数据）。
    """
    reg = get_registry()
    reg.clear_active_app()
    return {"ok": True, "active_app": None}