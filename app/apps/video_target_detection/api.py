"""视频目标检测 endpoints: REST 控制 + WebSocket 帧流 + TargetDoc 检测。

页面结构与 ``app/api/video_detection.py`` 一致；区别仅：

* ``/video-target-detection`` 前缀；
* 后端 detector 用 ``get_target_detector()``，prompt / 事件历史与
  音视频描述页相互独立；
* 检测走 ``session.respond(generating=TargetDoc)`` 结构化输出直
  接拿 ``x1, y1, x2, y2`` + ``desc``，**不**再挂任何 ``@Callable``
  工具 —— 该页面没有"工具集合"概念，``/video-target-detection/tools``
  端点也不存在。
"""
from __future__ import annotations

import asyncio
import queue
import shutil
from pathlib import Path
from typing import Optional

from fastapi import (
    APIRouter,
    File,
    HTTPException,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)

from pydantic import BaseModel, Field

from app.core.llm.detector_base import get_target_detector
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
from app.core.video.multi_session import (
    MAX_CHANNELS,
    get_multi_session,
)


router = APIRouter(tags=["video-target-detection"])

# WS 连接时回放的历史事件条数上限，与前端 MAX_RENDERED 对齐：
# 多发只会白耗带宽（每条带 crop_b64 缩略图，实测单条 2~33KB）。
WS_EVENT_HISTORY_MAX = 50

# 向导模式拖放上传的视频文件落地目录 — 三个视频 app 共用同一目录
# ``<resource_dir>/uploads``(设备端 ``/userdata/assets/uploads``,off-device
# ``<HOME>/assets/uploads``)。不再按 app id 分目录,避免每个 app 重复上传
# 同一份视频占盘;同名文件直接覆盖(``open("wb")`` 截断写)。
# 路径解析走 :func:`app.core.config.default_resource_root`,
# 与 ``base.resource_dir`` 写盘真值一致 —— 改前者时这里自动跟着变。
# mkdir 容错:read-only fs / 权限不够时跳过,首次写文件时再报错。
UPLOAD_DIR = default_resource_root() / "uploads"
try:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    pass


@router.get("/")
async def get_state() -> dict:
    """本页状态：多路会话快照 + detector 快照。"""
    return {
        "session": get_multi_session().snapshot(),
        "detector": get_target_detector().snapshot(),
    }


@router.post("/upload")
async def upload_video(file: UploadFile = File(...)) -> dict:
    """向导模式拖放上传 → 写到 ``UPLOAD_DIR``(三个 app 共用)。

    同名文件直接覆盖(``open("wb")`` 截断写)—— 不再生成 UUID 后缀,
    避免每个 app 重复上传同一份视频占盘。``file.filename`` 先经
    ``Path(...).name`` 规整,剥掉路径穿越成分,只保留 basename;缺扩展名
    时补 ``.bin``。
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="empty filename")
    safe_name = Path(file.filename).name
    if not safe_name:
        raise HTTPException(status_code=400, detail="invalid filename")
    if not Path(safe_name).suffix:
        safe_name = safe_name + ".bin"
    dest = UPLOAD_DIR / safe_name
    try:
        # 同名覆盖: 先 unlink 再 open("wb"),避免目标文件由其他用户/不可写
        # 拥有时 ``open("wb")`` 因需 inode 上的写权限而失败
        # (PermissionError: [Errno 13]);unlink 走的是目录级写权限,与
        # 新建文件一致。missing_ok=True 容忍首次上传时目标不存在。
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
    return {
        "path": str(dest),
        "name": file.filename,
        "size": dest.stat().st_size,
    }


@router.post("/open")
async def open_source(payload: dict) -> dict:
    """打开 1~32 路视频。Body: ``{sources: [str, ...], loop: bool}``，
    每项一路（本地路径 / 目录 / rtsp://）。
    ``source``（单个字符串）是老前端的兼容写法，等价于 ``sources: [source]``。
    """
    payload = payload or {}
    raw = payload.get("sources")
    if raw is None:
        # 兼容：老前端 / 老调用方只传 source
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
        return get_multi_session().open(sources, loop=loop)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/control")
async def control(payload: dict) -> dict:
    action = (payload or {}).get("action")
    if action == "play":
        return get_multi_session().play()
    if action == "pause":
        return get_multi_session().pause()
    if action == "stop":
        return get_multi_session().close()
    raise HTTPException(status_code=400, detail=f"unknown action: {action!r}")


@router.post("/close")
async def close_source() -> dict:
    """停止并释放全部路。同时摘掉 VLM 轮转，否则调度线程会继续对着
    已释放的 VideoCapture 提交推理、白烧 NPU。"""
    get_multi_session().stop_round_robin()
    return get_multi_session().close()


# --------------------------------------------------------------------- #
# TargetDoc 检测                                                         #
# --------------------------------------------------------------------- #
@router.get("/prompt")
async def get_prompt() -> dict:
    return get_target_detector().snapshot()


@router.post("/prompt")
async def set_prompt(payload: dict) -> dict:
    """Start (or update) target-detection.

    Body: ``{prompt: str, every_n: int, image_size: int | None,
             model_id: str | None}``
    — ``prompt`` 必填；``image_size`` 可选，指定视觉模型目标尺寸
    （必须落在 worker 已加载变体尺寸集合 ``available_image_sizes``
    内；缺省 / ``None`` = 按帧自动选最近的变体，保留 legacy 行为）；
    ``model_id`` 可选，前端下拉框选中的 ``swcap_video_describe``
    实例 id（不传 / 空 = auto-pick）。

    旧版 ``enabled_tools`` 字段不再生效 —— 该页只走 ``TargetDoc``
    结构化输出，不再挂任何 ``@Callable`` 工具。仍接受该字段以便
    老前端过渡期不掉请求体；忽略即可。
    """
    payload = payload or {}
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt 不能为空")
    # every_n 是遗留字段：多路节流由轮转调度器承担，这里强制 every_n=1
    #（见下面 det.start），故不参与推理也不再做 >=1 校验 —— 否则前端合法的
    # 0（轮转间隔 0 秒）会被误判非法直接 400。只在解析失败时报错。
    try:
        every_n = int(payload.get("every_n", 30) or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="every_n 必须为整数")
    # image_size 解析：可选字段，缺省 / ``None`` → auto-pick；非法
    # 类型 → 400（detector ``start()`` 内部也校验实际合法性，这里提前
    # 到 HTTP 层只是为了返回更具体的错误消息）。
    raw_image_size = payload.get("image_size", None)
    image_size: int | None
    if raw_image_size is None:
        image_size = None
    elif isinstance(raw_image_size, bool) or not isinstance(raw_image_size, int):
        raise HTTPException(
            status_code=400,
            detail=f"image_size 必须是整数或 null: {raw_image_size!r}",
        )
    else:
        image_size = raw_image_size
    # model_id：前端下拉框选中值；None 或 "" 走 auto-pick。
    raw_model_id = payload.get("model_id", None)
    if raw_model_id is None or (isinstance(raw_model_id, str)
                                and not raw_model_id.strip()):
        model_id: str | None = None
    elif not isinstance(raw_model_id, str):
        raise HTTPException(
            status_code=400,
            detail=f"model_id 必须是字符串或 null: {raw_model_id!r}",
        )
    else:
        model_id = raw_model_id
    det = get_target_detector()
    try:
        # enabled_tools 显式置 [] — 该页不再挂工具。
        #
        # every_n 在多路下**必须锁死为 1**：`submit_frame()` 里
        # `if self._frame_count % self._every_n != 0: return` 是给单路
        # 「每 N 帧推一次」用的节流。多路的节流职责已经由轮转调度器承担
        # —— 调度器本来就是一路一帧地取「当前帧」提交。若这里沿用
        # every_n=30，30 次轮转里只有 1 次真进队列，其余 29 次直接
        # return（连 _pending 都不设），wait_idle 立刻返回 True →
        # 轮转变成 15 次/秒的空转，**看起来在轮转其实一帧没推理**。
        snap = det.start(
            prompt, every_n=1, enabled_tools=[],
            image_size=image_size, model_id=model_id,
        )
        snap["every_n"] = 1
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

    # 多路模式：把会话接给 detector 并起轮转线程。此后**不再走
    # VideoSession 的 frame sink** —— 帧由 MultiChannelSession 自己的
    # 调度线程按「第 1 路 → 第 N 路 → 回第 1 路」逐路取当前帧提交
    #（单路也走同一条路径）。
    #
    # every_n 在多路下无意义（一次提交就是一路一帧），保留入参只是
    # 让旧前端不报错；真正的节奏旋钮是 **rr_interval**（秒/路）。
    # 前端那个「轮转间隔」控件历史上一直是死控件（只传 every_n），
    # 这里正式接到调度器上。
    rr_interval = payload.get("rr_interval", None)
    if rr_interval is None:
        # 老前端没这个字段：把 every_n 当「秒/路」解读，兼容页面既有语义
        rr_interval = float(every_n)
    try:
        rr_interval = float(rr_interval)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="rr_interval 必须是数字")

    session = get_multi_session()
    session.attach_detector(det)
    session.set_round_robin_interval(rr_interval)
    session.start_round_robin()
    snap["session"] = session.snapshot()
    return snap


@router.post("/prompt/stop")
async def stop_prompt() -> dict:
    get_multi_session().stop_round_robin()
    return get_target_detector().stop()


@router.get("/events")
async def list_events() -> dict:
    det = get_target_detector()
    return {
        "snapshot": det.snapshot(),
        "events": det.events(),
        "latest": det.latest(),
        "session": get_multi_session().snapshot(),
    }


@router.post("/events/clear")
async def clear_events() -> dict:
    get_target_detector().clear()
    return {"ok": True}


# --------------------------------------------------------------------- #
# 诊断端点 (v0.13): 暴露最近 N 次推理的诊断 trace                          #
# --------------------------------------------------------------------- #
# 用户报告"概率性目标检测失败"时,需要快速看到:
#   - worker 端 schema parse 是否成功 (gen_name + actual_result_type)
#   - proxy 端 result_data 是否缺失 (TargetDoc 重建路径)
#   - detector 端 isinstance(result, TargetDoc) 是否命中
#   - 每次推理耗时 / 事件数 / exit_reason
# 把这些信息塞到一个 GET 端点,curl 即可 dump 现场。生产环境也保留。
@router.get("/diagnostics")
async def get_diagnostics() -> dict:
    """Dump the detector's recent inference diagnostics ring buffer.

    字段约定见 ``app/services/detector.py`` ``WdnaiDetector.__init__``
    里 ``_diag_history`` 的注释。每条记录包含:
      ``ts`` / ``frame_pos`` / ``it_infer_ms`` / ``schema`` /
      ``result_type`` / ``text_len`` / ``text`` / ``exit_reason`` /
      ``events`` / ``bbox`` / ``crop_w`` / ``crop_h`` /
      ``schema_parse_failed`` / ``had_exception`` / ``exception_type`` /
      ``exception_msg`` / ``raw_text_first``

    返回 ``{"count": int, "items": [dict, ...]}``,新 → 旧。
    """
    det = get_target_detector()
    items = det.diag_history()
    # 倒序 (新 → 旧) 方便调试时先看最近的失败
    items_reversed = list(reversed(items))
    return {"count": len(items_reversed), "items": items_reversed}


@router.post("/diagnostics/clear")
async def clear_diagnostics() -> dict:
    """Clear the diagnostics ring buffer (不影响 _events 历史)。"""
    get_target_detector().clear_diagnostics()
    return {"ok": True}


# --------------------------------------------------------------------- #
# WebSocket: server -> browser frame + event stream                     #
# --------------------------------------------------------------------- #
@router.websocket("/ws")
async def stream_ws(ws: WebSocket) -> None:
    """多路帧流 + TargetDoc 事件流。

    Protocol (server -> client):

      - JSON text  ``{"type":"status", ...}``  ~1Hz：会话快照（含每路
        state / fps / frame_pos / error）+ detector 快照 + VLM 轮转游标
        （``vlm.current`` = 当前正在推理第几路，``vlm.round`` = 已完成轮数）
      - JSON text  ``{"type":"frame_meta","idx":N}`` 紧跟其后是
        ``N`` 路的一张 JPEG **二进制**帧 —— 多路下浏览器必须先知道这帧
        属于哪一路，所以每帧前插一条极小的 meta（比在二进制里塞头部更省）
      - JSON text  ``{"type":"event", ..., "channel":N}``  TargetDoc 命中
        （``channel`` 标明来自第几路）
      - JSON text  ``{"type":"event_history","events":[...]}``  **仅在
        连接建立时发一次**：把最近的历史事件**打包成一条**发过去。
    """
    await ws.accept()
    session = get_multi_session()
    detector = get_target_detector()
    loop = asyncio.get_event_loop()
    sub_q = session.subscribe()
    last_status = 0.0
    last_event_id = 0
    # ---- 历史事件：一次打包，而不是逐条回放 ---------------------------- #
    # 原写法是 `last_event_id = 0` + 循环里逐条 send，于是**每次刷新 /
    # 每次重连都把全部历史重放一遍**。实测 200 条 = 519KB（79% 是
    # crop_b64 缩略图），前端每条都要 appendVtdEventLi → 建 DOM +
    # insertBefore + trim，200 条突发足以把浏览器渲染进程打满一个核
    # （实测 106%，页面直接卡死）。
    # 现在改成：一条 `event_history` 消息带最近 N 条，客户端一次性重建列表。
    # 上限取 50 —— 前端 `VTDDetector.MAX_RENDERED` 本来就只渲染 50 条，
    # 多发纯属浪费。
    history = detector.events_since(0)
    if history:
        last_event_id = history[-1]["id"]          # 已发过的后面不再单发
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
                await ws.send_json({"type": "frame_meta", "idx": idx})
                await ws.send_bytes(jpg)

            now = loop.time()
            if now - last_status >= 1.0:
                await ws.send_json({
                    "type": "status",
                    "session": session.snapshot(),
                    "detector": detector.snapshot(),
                })
                last_status = now
            if item is None:
                await asyncio.sleep(0.02)
    except WebSocketDisconnect:
        pass
    finally:
        session.unsubscribe(sub_q)


# --------------------------------------------------------------------------- #
# 帧判据（每路一个独立判据器）
# --------------------------------------------------------------------------- #
# 端点挂本卡而不是共用 /api/video/judge：那个操作的是单例 VideoSession
# （vd / vtl 共用），本页走 MultiChannelSession —— 自管理解码循环的东西
# 配置就得落到自己这边，与 VAS 挂 /api/video-analysis-search/judge 同理。


class JudgePayload(BaseModel):
    mode: str = Field(default=EXPOSED_METRICS[0],
                      description="manual（手动跳帧）或任一暴露的度量名")
    every_n: int = Field(default=DEFAULT_EVERY_N, ge=1, le=3600,
                         description="兜底帧间隔")
    threshold: Optional[float] = Field(default=None, gt=0, le=1000,
                                       description="变化阈值（不传=按模式规则）")


@router.get("/judge/metrics")
async def get_judge_metrics() -> dict:
    """下拉可选项 + 每个算法的实测单帧成本与默认阈值。

    字段与单路 /api/video/judge/metrics 逐字一致 —— 共享前端组件
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
    return get_multi_session().judge_snapshot()


@router.post("/judge")
async def set_judge(payload: JudgePayload) -> dict:
    """配置判据。契约：**字段缺失 = 沿用当前值**。

    靠 ``model_fields_set`` 判断 Pydantic 是不是替我们填了 default：这些
    字段的 default 全都非空，直接透传会把用户调好的值冲掉。
    （VAS 那边目前只给 mode 做了这个保护要对齐改 VAS，别反过来学它的宽松。）
    """
    f = payload.model_fields_set
    raw = str(payload.mode).strip().lower() if "mode" in f else ""
    return get_multi_session().configure_judge(
        mode=raw or None,                       # 显式 mode="" 也保留当前
        every_n=payload.every_n if "every_n" in f else None,
        threshold=payload.threshold if "threshold" in f else None)


@router.post("/judge/reset")
async def reset_judge() -> dict:
    """清空各路判据的参照帧（不动配置）。"""
    return get_multi_session().reset_judge()
