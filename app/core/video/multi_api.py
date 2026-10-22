"""vd / vtd / vtl 三张多路视频卡共用的 router 工厂。

三张卡后端同构：同一套 :class:`MultiChannelSession` 解码 + 同一套
``WdnaiDetector`` 事件 + 同一套每路独立 FrameJudge，端点也几乎逐字相同。
抽成工厂是为了不复制三份（改一处漏两处）。差异收在
:class:`MultiCardSpec` 的几个回调里；卡片独有的端点（vd 的 ``/tools``、
``/available-vision-sizes``）不进来，卡片自己往同一个 router 上追加。
"""
from __future__ import annotations

import asyncio
import queue
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from fastapi import (
    APIRouter,
    File,
    HTTPException,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import BaseModel, Field

from app.core.config import default_resource_root
from app.core.video.frame_judge import (
    DEFAULT_EVERY_N,
    DEFAULT_LONG_SIDE,
    EXPOSED_METRICS,
    MODE_MANUAL,
    cost_ms,
    default_threshold,
    metrics,
)
from app.core.video.multi_session import MAX_CHANNELS, MultiChannelSession

# WS 连接时回放的历史事件条数上限：多发只白耗带宽（每条带 crop_b64 缩略图，
# 实测单条 2~33KB），且前端本来就只渲染这么多。
WS_EVENT_HISTORY_MAX = 50

# 三张卡共用 ``<resource_dir>/uploads``，不按 app id 分目录，避免同一份视频
# 重复上传占盘。走 :func:`app.core.config.default_resource_root` 而不是
# ``base.resource_dir``，以保证与写盘真值一致。
UPLOAD_DIR = default_resource_root() / "uploads"
try:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    pass


@dataclass(frozen=True)
class PromptPlan:
    """``spec.parse_prompt`` 的返回值。

    ``start_kwargs`` 透传给 ``detector.start(**kwargs)``；``rr_interval``
    是「轮转间隔」秒/路，多路下真正的节奏旋钮。
    """
    start_kwargs: Dict[str, Any] = field(default_factory=dict)
    rr_interval: float = 0.0


def parse_models_field(payload: dict) -> Optional[list]:
    """解析多卡并行的 ``models`` 字段 → ``[{"model_id","npu_index"}, ...]``。

    勾选粒度是**实例**而不是模型：同一个 model_id 出现在不同 npu_index 上就是
    「同一个模型跑多张卡并行」，每条各占一个推理 slot。只传 model_id 的话后端
    只能拿到一个卡号，多卡整个退化成单卡。

    字段缺省 / 为 null → 返回 None，调用方走单实例旧路径。

    Raises:
        HTTPException: 400 —— 类型不对、空数组、项不是 dict、重复实例。
    """
    raw = payload.get("models", None)
    if raw is None:
        return None
    if not isinstance(raw, list) or not raw:
        raise HTTPException(
            status_code=400, detail=f"models 必须是非空数组: {raw!r}")
    out: list = []
    seen: set = set()
    for item in raw:
        if not isinstance(item, dict):
            raise HTTPException(
                status_code=400,
                detail=f"models 每项必须是 {{model_id, npu_index}}: {item!r}")
        mid = item.get("model_id")
        if not isinstance(mid, str) or not mid.strip():
            raise HTTPException(
                status_code=400,
                detail=f"models[].model_id 必须是非空字符串: {item!r}")
        npu = item.get("npu_index")
        if npu is not None and (isinstance(npu, bool) or not isinstance(npu, int)):
            raise HTTPException(
                status_code=400,
                detail=f"models[].npu_index 必须是整数或 null: {item!r}")
        key = (mid.strip(), npu)
        # 同一实例勾两次 = 两条线程挤同一个 worker 进程，句柄不可重入
        if key in seen:
            raise HTTPException(
                status_code=400,
                detail=f"models 含重复实例 model_id={mid!r} npu_index={npu!r}")
        seen.add(key)
        out.append({"model_id": mid.strip(), "npu_index": npu})
    return out


class ZoomPayload(BaseModel):
    """``POST /zoom`` 入参：第 ``channel`` 路（0 基）切到 / 退出放大显示档。

    必须模块级，理由同 :class:`JudgePayload`。
    """

    channel: int
    zoomed: bool = True


@dataclass(frozen=True)
class MultiCardSpec:
    """一张多路视频卡接入本工厂要提供的东西。"""
    session_getter: Callable[[], MultiChannelSession]
    detector_getter: Callable[[], Any]
    #: payload → PromptPlan。抛 ``HTTPException`` 表示 400。
    parse_prompt: Callable[[dict], PromptPlan]
    upload_dir: Path = UPLOAD_DIR


class JudgeChannelPayload(BaseModel):
    """单路判据配置（多路下每路独立）。

    字段缺失 = 沿用当前值（与 :class:`JudgePayload` 同契约）。
    """
    mode: Optional[str] = None
    every_n: Optional[int] = Field(default=None, ge=1, le=3600)
    threshold: Optional[float] = Field(default=None, gt=0, le=1000)


class JudgePayload(BaseModel):
    """``POST /judge`` 的请求体。

    **必须定义在模块级**：``from __future__ import annotations`` 让端点
    签名变成字符串 ``"JudgePayload"``，FastAPI 解析时只查模块 globals ——
    定义在 :func:`build_multi_router` 局部作用域里会抛
    ``PydanticUserError: ForwardRef('JudgePayload') is not fully defined``。

    **多路语义**（v0.23 起）：
    - 顶层 ``mode`` / ``every_n`` / ``threshold`` 仍接受 —— 视为对**所有路**
      一次性下发（兼容单路老用法 / 向导批量设）；
    - 顶层 ``channels`` 是 ``{channel_idx: JudgeChannelPayload}`` 字典，按路
      精细覆盖（v0.23 UI 上点 tile → 改阈值的入口）。
    - 同一字段：``channels`` 里的该路配置**覆盖**顶层（merge 顺序：先顶层、
      再按 idx 覆盖）。``None`` 字段跳过 = 沿用当前值。
    """
    mode: Optional[str] = Field(default=None,
                                description="manual 或任一暴露的度量名；不传=沿用")
    every_n: Optional[int] = Field(default=None, ge=1, le=3600,
                                   description="兜底帧间隔；不传=沿用")
    threshold: Optional[float] = Field(default=None, gt=0, le=1000,
                                       description="变化阈值；不传=沿用")
    channels: Optional[Dict[int, JudgeChannelPayload]] = Field(
        default=None,
        description="按路精细覆盖。例：{2: {threshold: 8.0}} 只改第 3 路阈值。",
    )


def build_multi_router(spec: MultiCardSpec, tag: str) -> APIRouter:
    """构造一张多路视频卡的 router。``tag`` 是 OpenAPI 分组名。"""
    router = APIRouter(tags=[tag])

    # ----------------------------------------------------------------- #
    # 会话                                                              #
    # ----------------------------------------------------------------- #
    @router.get("/")
    async def get_state() -> dict:
        return {
            "session": spec.session_getter().snapshot(),
            "detector": spec.detector_getter().snapshot(),
        }

    @router.post("/upload")
    async def upload_video(file: UploadFile = File(...)) -> dict:
        """同名文件直接覆盖：先 unlink 再 ``open("wb")``，避免目标由其他
        用户拥有时因 inode 写权限而失败（unlink 走的是目录级写权限）。
        """
        if not file.filename:
            raise HTTPException(status_code=400, detail="empty filename")
        safe_name = Path(file.filename).name
        if not safe_name:
            raise HTTPException(status_code=400, detail="invalid filename")
        if not Path(safe_name).suffix:
            safe_name += ".bin"
        dest = spec.upload_dir / safe_name
        try:
            try:
                dest.unlink()
            except FileNotFoundError:
                pass
            with dest.open("wb") as f:
                shutil.copyfileobj(file.file, f)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"write failed: {e}")
        finally:
            try:
                file.file.close()
            except Exception:
                pass
        return {"path": str(dest), "name": file.filename,
                "size": dest.stat().st_size}

    @router.post("/open")
    async def open_source(payload: dict) -> dict:
        """打开 1~32 路视频。Body: ``{sources: [str, ...], loop: bool}``。

        ``source``（单个字符串）是老前端的兼容写法，等价于
        ``sources: [source]``。
        """
        payload = payload or {}
        raw = payload.get("sources")
        if raw is None:
            single = payload.get("source")
            raw = [single] if isinstance(single, str) else []
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            raise HTTPException(status_code=400, detail="sources 必须是列表")
        sources = [s.strip() for s in raw if isinstance(s, str) and s.strip()]
        if not sources:
            raise HTTPException(status_code=400, detail="sources 不能为空")
        if len(sources) > MAX_CHANNELS:
            raise HTTPException(
                status_code=400,
                detail=f"最多 {MAX_CHANNELS} 路，收到 {len(sources)} 路",
            )
        loop = bool(payload.get("loop", True))
        try:
            return spec.session_getter().open(sources, loop=loop)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))

    @router.post("/control")
    async def control(payload: dict) -> dict:
        action = (payload or {}).get("action")
        sess = spec.session_getter()
        if action == "play":
            return sess.play()
        if action == "pause":
            return sess.pause()
        if action == "stop":
            return sess.close()
        raise HTTPException(status_code=400, detail=f"unknown action: {action!r}")

    @router.post("/close")
    async def close_source() -> dict:
        """连 VLM 轮转一起摘掉，否则调度线程会继续对着已释放的
        ``VideoCapture`` 提交推理、白烧 NPU。
        """
        sess = spec.session_getter()
        sess.stop_round_robin()
        return sess.close()

    @router.post("/zoom")
    async def set_zoom(payload: ZoomPayload) -> dict:
        """切换某一路的放大显示档。纯显示：只改那一路的显示分辨率，不碰
        解码 fps / 送检尺寸 / 检测。前端布局纯 CSS 先切，这里异步补帧。

        Raises:
            400: channel 为负，或不在已打开的通道范围内。
        """
        if payload.channel < 0:
            raise HTTPException(
                status_code=400,
                detail=f"channel 必须非负: {payload.channel}")
        try:
            return spec.session_getter().set_zoom(payload.channel,
                                                   payload.zoomed)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    # ----------------------------------------------------------------- #
    # 检测                                                              #
    # ----------------------------------------------------------------- #
    @router.get("/prompt")
    async def get_prompt() -> dict:
        return spec.detector_getter().snapshot()

    @router.post("/prompt")
    async def set_prompt(payload: dict) -> dict:
        """启动 / 更新检测。校验由 ``spec.parse_prompt`` 负责。

        **every_n 必须锁死 1** —— ``submit_frame()`` 里
        ``if self._frame_count % self._every_n != 0: return`` 是给单路
        「每 N 帧推一次」用的节流。多路的节流职责已由轮转调度器承担（它本来
        就是一路一帧地取当前帧）。若沿用 every_n=30，30 次轮转里只有 1 次
        真进队列，其余 29 次直接 return（连 ``_pending`` 都不设），
        ``wait_idle`` 秒回 True → 轮转变成 15 次/秒的空转，**看起来在轮转
        其实一帧没推理**。要控制节奏用 ``rr_interval``（秒/路）：老前端
        只有 ``every_n`` 这个字段且一直是死控件，这里把
        ``spec.parse_prompt`` 解读出来的值当「秒/路」用，兼容既有页面语义。
        """
        payload = payload or {}
        plan = spec.parse_prompt(payload)
        det = spec.detector_getter()
        kwargs = dict(plan.start_kwargs)
        kwargs["every_n"] = 1
        kwargs.setdefault("enabled_tools", [])
        try:
            snap = det.start(**kwargs)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except RuntimeError as e:
            raise HTTPException(status_code=503, detail=str(e))
        snap["every_n"] = 1

        session = spec.session_getter()
        session.attach_detector(det)
        session.set_round_robin_interval(plan.rr_interval)
        session.start_round_robin()
        snap["session"] = session.snapshot()
        return snap

    @router.post("/prompt/stop")
    async def stop_prompt() -> dict:
        spec.session_getter().stop_round_robin()
        return spec.detector_getter().stop()

    @router.get("/events")
    async def list_events(frames: bool = False) -> dict:
        """事件列表。

        默认**不带整帧**：定位事件的 ``frame_b64`` 是「算框用的那一帧」
        （显示尺寸 JPEG，单条 ~130KB），200 条就是 26MB —— 页面初始化
        会拉一次这个端点，扛不住。实时事件走 WS（那里带着整帧，前端定格
        要用）。要拿整帧就加 ``?frames=1``。
        """
        det = spec.detector_getter()
        events = det.events()
        latest = det.latest()
        if not frames:
            for ev in events:
                ev.pop("frame_b64", None)
            if latest:
                latest.pop("frame_b64", None)
        return {
            "snapshot": det.snapshot(),
            "events": events,
            "latest": latest,
            "session": spec.session_getter().snapshot(),
        }

    @router.post("/events/clear")
    async def clear_events() -> dict:
        spec.detector_getter().clear()
        return {"ok": True}

    # ----------------------------------------------------------------- #
    # 诊断端点                                                          #
    # ----------------------------------------------------------------- #
    # 用户报「概率性检测失败」时要快速看到：worker 端 schema parse 是否成功、
    # proxy 端 result_data 是否缺失、detector 端 isinstance 是否命中、每次
    # 推理耗时 / 事件数 / exit_reason。curl 一下就能 dump 现场，生产环境也
    # 保留。字段约定见 ``WdnaiDetector.__init__`` 里 ``_diag_history`` 的注释。
    @router.get("/diagnostics")
    async def get_diagnostics() -> dict:
        """最近 N 次推理的诊断 trace，**新 → 旧**。"""
        items = list(reversed(spec.detector_getter().diag_history()))
        return {"count": len(items), "items": items}

    @router.post("/diagnostics/clear")
    async def clear_diagnostics() -> dict:
        """只清诊断 ring buffer，不动 ``_events`` 历史。"""
        spec.detector_getter().clear_diagnostics()
        return {"ok": True}

    # ----------------------------------------------------------------- #
    # 帧判据（每路一个独立判据器）                                       #
    # ----------------------------------------------------------------- #
    # 端点挂本卡而不是共用 /api/video/judge：那个操作的是单例 VideoSession，
    # 多路页走 MultiChannelSession —— 自管理解码循环的东西配置就得落到
    # 自己这边。

    @router.get("/judge/metrics")
    async def get_judge_metrics() -> dict:
        """下拉可选项 + 每个算法的实测单帧成本与默认阈值。

        字段与单路 ``/api/video/judge/metrics`` 逐字一致 —— 共享前端组件
        ``frame-judge.js`` 读的就是这套结构，多路这边不能改名。
        """
        ms = metrics()
        return {
            "mode": MODE_MANUAL,
            "metrics": ms,
            "cost_ms": {m: round(cost_ms(m) or 0.0, 2) for m in ms},
            "default_threshold": {m: default_threshold(m) for m in ms},
            "long_side": DEFAULT_LONG_SIDE,
        }

    @router.get("/judge")
    async def get_judge() -> dict:
        return spec.session_getter().judge_snapshot()

    @router.post("/judge")
    async def set_judge(payload: JudgePayload) -> dict:
        """契约：**字段缺失 = 沿用当前值**。

        靠 ``model_fields_set`` 判断 Pydantic 是不是替我们填了 default：
        这些字段的 default 全都非空，直接透传会把用户调好的值冲掉。

        **v0.23 多路独立配置**：
        - 顶层 mode/every_n/threshold 同字段缺失语义
        - ``channels`` 字典 = 按路精细下发（每路 JudgeChannelPayload
          自身也是"字段缺失 = 沿用该路当前值"）
        - channels 内部以 idx 索引；不在字典里的路不受影响
        """
        f = payload.model_fields_set
        # 顶层 mode：模型 default 是 None，字段缺失时 model_fields_set
        # 不会包含它（用 default）。但显式 "" 也想"保留当前"，所以 raw 不空就传
        raw = str(payload.mode).strip().lower() if "mode" in f else None
        # payload.channels 字段缺失 → None（按入参 model_fields_set 判定；
        # Pydantic v2 不再靠 default 区分）。
        # channels 内的子元素是 JudgeChannelPayload（Pydantic 模型），
        # 走 model_dump 拿 dict；exclude_none 跳过"未指定字段"，让后端按
        # 每路当前值沿用。
        if "channels" in f and payload.channels:
            chs = {idx: c.model_dump(exclude_none=True)
                   for idx, c in payload.channels.items()}
        else:
            chs = None
        return spec.session_getter().configure_judge(
            mode=raw or None,
            every_n=payload.every_n if "every_n" in f else None,
            threshold=payload.threshold if "threshold" in f else None,
            channels=chs,
        )

    @router.post("/judge/reset")
    async def reset_judge() -> dict:
        """清空各路判据的参照帧，不动配置。"""
        return spec.session_getter().reset_judge()

    # ----------------------------------------------------------------- #
    # WebSocket                                                         #
    # ----------------------------------------------------------------- #
    @router.websocket("/ws")
    async def stream_ws(ws: WebSocket) -> None:
        """帧流 + 事件流。协议（server -> client）：

          - ``{"type":"status","session":…,"detector":…,"ws":…}`` ~1Hz：
            会话快照（含每路 state / fps / frame_pos / error）+ detector 快照 +
            VLM 轮转游标（``vlm.current`` = 当前在推理第几路，``vlm.round``
            = 已完成轮数）+ ``ws.sent`` = 本连接累计发出的帧数（对同一连接
            与订阅队列的丢弃计数做差，即可判断帧丢在服务端还是浏览器）
          - **第 1 字节是通道号、其后是 JPEG** 的二进制帧。多路下浏览器必须
            先知道这帧属于哪一路；塞进二进制头而不是单独发一条 meta，是为了
            把满负载时的 WS 消息数减半（每帧多一条消息就多一次 json.dumps
            + 一次 WS 写）。1 字节够用是因为多路上限 32，越界时退回 0 而不
            截断 —— 截断会把画面画到错的路上。
          - ``{"type":"event", …, "channel":N}``  命中（``channel`` 标明
            来自第几路）
          - ``{"type":"event_history","events":[…]}``  **仅连接时发一次**

        事件历史必须打包：逐条回放时实测 200 条 = 519KB（79% 是 crop_b64
        缩略图），前端每条都要建 DOM + insertBefore，200 条突发足以把
        浏览器渲染进程打满一个核（实测 106%，页面直接卡死）。
        """
        await ws.accept()
        session = spec.session_getter()
        detector = spec.detector_getter()
        loop = asyncio.get_event_loop()
        sub_q = session.subscribe()
        last_status = 0.0
        last_event_id = 0
        sent = 0
        history = detector.events_since(0)
        if history:
            last_event_id = history[-1]["id"]      # 已发过的后面不再单发
            # 历史包里**去掉整帧**：定位事件带的 frame_b64 是「算框用的
            # 那一帧」（显示尺寸 JPEG，单条 ~130KB），50 条就是 6.6MB ——
            # 建连时一次性灌进浏览器会把渲染进程压死，而历史列表本来只
            # 需要框和文字。实时事件（下面那条 send_json）仍带整帧 ——
            # 前端定格靠的就是它。
            for ev in history[-WS_EVENT_HISTORY_MAX:]:
                ev.pop("frame_b64", None)
            await ws.send_json({"type": "event_history",
                                "events": history[-WS_EVENT_HISTORY_MAX:]})
        try:
            while True:
                for ev in detector.events_since(last_event_id):
                    last_event_id = ev["id"]
                    await ws.send_json({"type": "event", **ev})

                try:
                    item = await loop.run_in_executor(
                        None, lambda: sub_q.get(timeout=0.05)
                    )
                except queue.Empty:
                    item = None

                if item is not None:
                    idx, jpg = item
                    await ws.send_bytes(bytes((idx,)) + jpg)
                    sent += 1

                now = loop.time()
                if now - last_status >= 1.0:
                    await ws.send_json({
                        "type": "status",
                        "session": session.snapshot(),
                        "detector": detector.snapshot(),
                        "ws": {"sent": sent},
                    })
                    last_status = now
                if item is None:
                    await asyncio.sleep(0.02)
        except WebSocketDisconnect:
            pass
        finally:
            session.unsubscribe(sub_q)

    return router