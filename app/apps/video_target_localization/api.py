"""视频目标定位 endpoints: REST 控制 + WebSocket 帧流 + LocatorDoc 检测。

页面结构与 ``app/api/video_target_detection.py`` 一致；区别仅：

* ``/video-target-localization`` 前缀；
* 后端 detector 用 ``get_localizer_detector()``（第 3 个 ``WdnaiDetector``
  单例，挂 ``LocatorDoc`` 多 bbox schema），prompt / 事件历史与音视频描述
  页 + 视频目标检测页相互独立；
* 检测走 ``session.respond(generating=LocatorDoc)`` 结构化输出直接拿
  ``boxes: List[BoundingBox]`` + ``text`` + ``level``，**不**挂任何
  ``@Callable`` 工具 —— 该页面没有"工具集合"概念，
  ``/video-target-localization/tools`` 端点也不存在；
* 本页固定 448×448 视觉变体（LocateAnythingBackend 单变体）且
  capability ``swcap_grounding`` 当前只有一个 model preset，prompt body
  仅 ``{prompt, every_n}``，**不**暴露 ``image_size`` / ``model_id`` 字段。
"""
from __future__ import annotations

import asyncio
import queue
import shutil
from pathlib import Path

from fastapi import (
    APIRouter,
    File,
    HTTPException,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)

from app.core.llm.detector_base import get_localizer_detector
from app.core.config import default_resource_root
from app.core.video.session import get_session


router = APIRouter(tags=["video-target-localization"])

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


# --------------------------------------------------------------------- #
# Locator prompt construction                                            #
# --------------------------------------------------------------------- #
DEFAULT_LOCATOR_PROMPT_PREFIX = "找出所有符合以下描述的对象："
LOCATOR_PROMPT_SEPARATOR = "</c>"


def _build_vtl_prompt(items: list) -> str:
    """Build the canonical locator prompt from a list of raw items.

    Pure function — no I/O, no detector dependency. Returns
    ``DEFAULT_LOCATOR_PROMPT_PREFIX + LOCATOR_PROMPT_SEPARATOR.join(<non-empty stripped items>)``;
    raises ``ValueError("至少需要一条非空的定位描述")`` if the filtered
    list is empty. Non-string items are coerced via ``str(x)`` before
    stripping.
    """
    cleaned: list[str] = []
    for x in items:
        if not isinstance(x, str):
            x = str(x)
        s = x.strip()
        if s:
            cleaned.append(s)
    if not cleaned:
        raise ValueError("至少需要一条非空的定位描述")
    return DEFAULT_LOCATOR_PROMPT_PREFIX + LOCATOR_PROMPT_SEPARATOR.join(cleaned)


@router.get("/")
async def get_state() -> dict:
    return get_session().snapshot()


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
    source = (payload or {}).get("source")
    if not source or not isinstance(source, str):
        raise HTTPException(status_code=400, detail="source required")
    loop = bool((payload or {}).get("loop", False))
    try:
        return get_session().open(source, loop=loop)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/control")
async def control(payload: dict) -> dict:
    action = (payload or {}).get("action")
    if action == "play":
        return get_session().play()
    if action == "pause":
        return get_session().pause()
    if action == "stop":
        return get_session().close()
    raise HTTPException(status_code=400, detail=f"unknown action: {action!r}")


@router.post("/close")
async def close_source() -> dict:
    return get_session().close()


# --------------------------------------------------------------------- #
# LocatorDoc 检测                                                        #
# --------------------------------------------------------------------- #
@router.get("/prompt")
async def get_prompt() -> dict:
    return get_localizer_detector().snapshot()


@router.post("/prompt")
async def set_prompt(payload: dict) -> dict:
    """Start (or update) target-localization.

    Body — two **mutually exclusive** shapes:

    1. Legacy ``prompt: str`` (single user-supplied full prompt string).
       ``{"prompt": str, "every_n": int}`` — ``prompt`` 必填 (non-empty
       after strip); ``every_n`` 选填，默认 30.

    2. New ``prompts: list[str]`` (raw items; backend owns the join).
       ``{"prompts": [str, ...], "every_n": int}`` — backend prepends
       ``DEFAULT_LOCATOR_PROMPT_PREFIX`` ("找出所有符合以下描述的对象：")
       and joins non-empty stripped items with
       ``LOCATOR_PROMPT_SEPARATOR`` ("</c>"). All-empty after strip →
       400 "至少需要一条非空的定位描述" (Decision 13).

    Validation order:

    * ``prompts`` present but not a list → 400 "prompts 必须是字符串列表".
    * Both ``prompt`` (non-empty after strip) and ``prompts`` (non-empty
      list) → 400 "prompt 与 prompts 互斥，只能传一个".
    * ``prompts`` present (any list value, including ``[]``) → built via
      ``_build_vtl_prompt``; its ``ValueError`` → 400 with that message.
    * Otherwise fall back to legacy ``prompt``; empty after strip →
      400 "prompt 不能为空" (preserve legacy message).

    本页**不**接受 ``image_size`` —— LocateAnythingBackend 视觉固定
    448×448 单变体，size picker 无意义。
    本页**不**接受 ``model_id`` —— capability ``swcap_grounding`` 当前
    只有 1 个 preset，model picker 无意义。

    旧版 ``enabled_tools`` 字段不再生效 —— 该页只走 ``LocatorDoc``
    结构化输出，不再挂任何 ``@Callable`` 工具。
    """
    payload = payload or {}
    legacy_prompt = payload.get("prompt")
    prompts_list = payload.get("prompts")

    if prompts_list is not None and not isinstance(prompts_list, list):
        raise HTTPException(
            status_code=400, detail="prompts 必须是字符串列表",
        )

    legacy_non_empty = (
        isinstance(legacy_prompt, str) and legacy_prompt.strip() != ""
    )
    prompts_non_empty = isinstance(prompts_list, list) and len(prompts_list) > 0

    if legacy_non_empty and prompts_non_empty:
        raise HTTPException(
            status_code=400, detail="prompt 与 prompts 互斥，只能传一个",
        )

    if prompts_list is not None:
        try:
            prompt = _build_vtl_prompt(prompts_list)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    else:
        prompt = legacy_prompt.strip() if isinstance(legacy_prompt, str) else ""
        if not prompt:
            raise HTTPException(status_code=400, detail="prompt 不能为空")

    try:
        every_n = int(payload.get("every_n", 30))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="every_n 必须为整数")
    if every_n < 1:
        raise HTTPException(status_code=400, detail="every_n 必须 >= 1")
    try:
        # enabled_tools 显式置 [] — 该页不再挂工具。
        return get_localizer_detector().start(
            prompt, every_n=every_n, enabled_tools=[],
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/prompt/stop")
async def stop_prompt() -> dict:
    return get_localizer_detector().stop()


@router.get("/events")
async def list_events() -> dict:
    det = get_localizer_detector()
    return {
        "snapshot": det.snapshot(),
        "events": det.events(),
        "latest": det.latest(),
    }


@router.post("/events/clear")
async def clear_events() -> dict:
    get_localizer_detector().clear()
    return {"ok": True}


# --------------------------------------------------------------------- #
# 诊断端点 (镜像 VTD): 暴露最近 N 次推理的诊断 trace                       #
# --------------------------------------------------------------------- #
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
    det = get_localizer_detector()
    items = det.diag_history()
    # 倒序 (新 → 旧) 方便调试时先看最近的失败
    items_reversed = list(reversed(items))
    return {"count": len(items_reversed), "items": items_reversed}


@router.post("/diagnostics/clear")
async def clear_diagnostics() -> dict:
    """Clear the diagnostics ring buffer (不影响 _events 历史)。"""
    get_localizer_detector().clear_diagnostics()
    return {"ok": True}


# --------------------------------------------------------------------- #
# WebSocket: server -> browser frame + event stream                     #
# --------------------------------------------------------------------- #
@router.websocket("/ws")
async def stream_ws(ws: WebSocket) -> None:
    """Push decoded JPEG frames + LocatorDoc events to the browser.

    Protocol (server -> client):
      - JSON text: {"type":"status", ...session snapshot, detector snapshot...}
      - Binary:    raw JPEG bytes for one frame
      - JSON text: {"type":"eof"} when a file source reaches end
      - JSON text: {"type":"event", ...detector event...} when ``LocatorDoc``
                   fires (event.boxes 是 ``[{label, bbox}, ...]`` 多框列表
                   像素坐标 + event.frame_b64 是整帧 base64 JPEG +
                   event.frame_w / event.frame_h 是整帧像素尺寸 +
                   event.text 是模型对整张图的描述文本)。
    """
    await ws.accept()
    session = get_session()
    detector = get_localizer_detector()
    loop = asyncio.get_event_loop()
    # Per-WS subscriber queue — shared VideoSession can have multiple
    # WS handlers connected; broadcast lets each receive every frame.
    sub_q = session.subscribe()
    last_status = 0.0
    last_event_id = 0
    eof_sent = False
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
            if item is None:
                now = loop.time()
                if now - last_status >= 1.0:
                    snap = session.snapshot()
                    snap["type"] = "status"
                    snap["detector"] = detector.snapshot()
                    await ws.send_json(snap)
                    last_status = now
                await asyncio.sleep(0.02)
                continue
            if item is session._EOF:
                if not eof_sent:
                    snap = session.snapshot()
                    snap["type"] = "eof"
                    await ws.send_json(snap)
                    eof_sent = True
                await asyncio.sleep(0.2)
                continue
            eof_sent = False
            await send_frame_bytes(ws, item)
            now = loop.time()
            if now - last_status >= 1.0:
                snap = session.snapshot()
                snap["type"] = "status"
                snap["detector"] = detector.snapshot()
                await ws.send_json(snap)
                last_status = now
            await asyncio.sleep(0.02)
    except WebSocketDisconnect:
        return
    except Exception as e:  # pragma: no cover - defensive
        try:
            await ws.send_json({"type": "error", "detail": str(e)})
        except Exception:
            pass
    finally:
        session.unsubscribe(sub_q)


async def send_frame_bytes(ws, data: bytes) -> None:
    """Send JPEG bytes — small wrapper so tests / overrides can stub."""
    await ws.send_bytes(data)