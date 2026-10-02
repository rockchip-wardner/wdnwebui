"""Video detection endpoints: REST control + WebSocket frame stream."""
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

from app.core.llm.detector_base import get_detector
from app.core.config import default_resource_root
from app.core.video.session import get_session


router = APIRouter(tags=["video-detection"])

# 音视频描述页只允许「语音提醒」工具。其他工具即便注册表里有也不
# 在这里暴露 —— 该页面是面向「描述+提醒」场景的固定工具集。
ALLOWED_TOOLS = ("tts_alert",)

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
async def get_video_detection() -> dict:
    return get_session().snapshot()


# 注:设备端文件浏览 endpoint 已 v0.4 抽出到 ``app/system/api/fs_browser.py``
# (全局 /api/fs),由 ``window.FSModal`` 模态共用。VD 之前单独的
# /api/video-detection/fs 已废弃,前端走 /api/fs。


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
# wdnai VLM detection                                                     #
# --------------------------------------------------------------------- #
@router.get("/prompt")
async def get_prompt() -> dict:
    return get_detector().snapshot()


@router.post("/prompt")
async def set_prompt(payload: dict) -> dict:
    """Start (or update) VLM detection.

    Body: ``{prompt: str, every_n: int, enabled_tools: list[str],
            image_size: int | None, model_id: str | None}``
    — ``prompt`` 必填；``enabled_tools`` 可选，默认 ``[]``，每一项
    必须是 ``/api/video-detection/tools`` 返回的合法 ``key``；
    ``image_size`` 可选，指定视觉模型目标尺寸（必须由
    ``/api/video-detection/available-vision-sizes`` 返回的合法值之
    一；缺省 / ``None`` = 按帧自动选最近的变体，保留 legacy 行为）；
    ``model_id`` 可选，从前端下拉框选的 ``swcap_video_describe``
    实例 id（不传 / 空 = 走 ``get_session`` 的本机优先 auto-pick，
    与旧行为一致）。
    """
    payload = payload or {}
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt 不能为空")
    try:
        every_n = int(payload.get("every_n", 30))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="every_n 必须为整数")
    if every_n < 1:
        raise HTTPException(status_code=400, detail="every_n 必须 >= 1")
    enabled_tools = payload.get("enabled_tools") or []
    if not isinstance(enabled_tools, list):
        raise HTTPException(status_code=400, detail="enabled_tools 必须是列表")
    # 后端兜底：仅允许白名单内的工具，丢弃其他项。前端 UI 已经按
    # ``ALLOWED_TOOLS`` 过滤；这里再卡一层避免直接 curl 越权。
    enabled_tools = [k for k in enabled_tools if k in ALLOWED_TOOLS]
    # image_size 解析：可选字段，缺省 / ``None`` → auto-pick；非法
    # 类型 / 不在合法列表 → 400（detector ``start()`` 内部也校验，
    # 这里提前到 HTTP 层是为了返回更具体的错误消息）。
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
    try:
        return get_detector().start(
            prompt, every_n=every_n, enabled_tools=enabled_tools,
            image_size=image_size, model_id=model_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.get("/available-vision-sizes")
async def available_vision_sizes() -> dict:
    """List the vision-model native sizes currently loaded into the
    running ``swcap_video_describe`` compute instance (qwen3-vl-4b /
    qwen3.5-vl-4b / future VLM presets — anything advertising that
    capability whose backend exposes ``available_image_sizes``).

    Returned shape: ``{"sizes": [384, 768], "default": 384}`` where
    ``sizes`` is the data-driven enumeration reported by the worker's
    info RPC (single-element ``[384]`` on legacy single-resolution
    installs; ``[384, 768]`` on full multi-resolution installs;
    empty when the instance isn't running yet). ``default`` is the
    smallest reported size — matches the prior hardcoded behaviour
    of the 384x384 default.

    The 音视频描述 page's ``#vd-detect-size`` dropdown is populated
    from this endpoint on page open. **No** 384/768 list is baked
    into the client.
    """
    from app.core.compute.registry import get_registry
    registry = get_registry()
    sizes: list[int] = []
    for inst in registry.list_with_source():
        # Filter by capability instead of model_id so any future VLM
        # preset exposing ``swcap_video_describe`` (e.g.
        # qwen3.5-vl-4b) is picked up automatically.
        caps = inst.get("capability_types") or []
        if "swcap_video_describe" not in caps:
            continue
        raw = inst.get("available_image_sizes") or []
        if not isinstance(raw, list):
            continue
        for s in raw:
            if isinstance(s, bool):
                continue
            try:
                sizes.append(int(s))
            except (TypeError, ValueError):
                continue
        sizes = sorted(set(sizes))
        break
    return {
        "sizes":   sizes,
        "default": sizes[0] if sizes else None,
    }


@router.get("/tools")
async def list_tools() -> dict:
    """返回当前 detector 可启用的工具列表 + 能力可用性。

    每条记录包含 ``key`` / ``name`` / ``description`` /
    ``requires_capability`` / ``available`` / ``reason`` / ``fields``。
    该页只暴露 ``ALLOWED_TOOLS`` 集合里的工具（语音提醒）；其他工具
    注册在 ``app.services.tools.registry`` 里但不在这里展示。
    """
    from app.core.compute.registry import get_registry
    from app.core.tools.registry import get_tool_registry

    reg = get_tool_registry()
    out = []
    for spec in reg.list_specs():
        if spec.key not in ALLOWED_TOOLS:
            continue
        available = True
        reason = ""
        if spec.requires_capability:
            try:
                get_registry().get_session(spec.requires_capability)
            except RuntimeError as e:
                available = False
                reason = str(e)
        out.append({
            "key":                 spec.key,
            "name":                spec.name,
            "icon":                spec.icon,
            "description":         spec.description,
            "requires_capability": spec.requires_capability,
            "available":           available,
            "reason":              reason,
            "fields":              [
                {"name": fname, "description": fdesc}
                for fname, fdesc in spec.fields
            ],
        })
    return {"tools": out}


@router.post("/prompt/stop")
async def stop_prompt() -> dict:
    return get_detector().stop()


@router.get("/events")
async def list_events() -> dict:
    det = get_detector()
    return {
        "snapshot": det.snapshot(),
        "events": det.events(),
        "latest": det.latest(),
    }


@router.post("/events/clear")
async def clear_events() -> dict:
    get_detector().clear()
    return {"ok": True}


# --------------------------------------------------------------------- #
# 诊断端点 (v0.13): 暴露最近 N 次推理的诊断 trace                          #
# --------------------------------------------------------------------- #
# 与 ``/api/video-target-detection/diagnostics`` 同源,指向音视频描述页
# 的 detector 单例 (VLMDoc + tts_alert 工具)。
@router.get("/diagnostics")
async def get_diagnostics() -> dict:
    """Dump the detector's recent inference diagnostics ring buffer.

    字段约定见 ``app/services/detector.py`` ``WdnaiDetector.__init__``
    里 ``_diag_history`` 的注释。
    """
    det = get_detector()
    items = det.diag_history()
    items_reversed = list(reversed(items))
    return {"count": len(items_reversed), "items": items_reversed}


@router.post("/diagnostics/clear")
async def clear_diagnostics() -> dict:
    get_detector().clear_diagnostics()
    return {"ok": True}


# --------------------------------------------------------------------- #
@router.websocket("/ws")
async def stream_ws(ws: WebSocket) -> None:
    """Push decoded JPEG frames + VLM events to the browser.

    Protocol (server -> client):
      - JSON text: {"type":"status", ...session snapshot...}
      - Binary:    raw JPEG bytes for one frame
      - JSON text: {"type":"eof"} when a file source reaches end
      - JSON text: {"type":"event", ...detector event...} when a VLM
                   inference completes

    The connection is one-way (server pushes). Pause / play / stop go
    through the REST ``/control`` endpoint so the decode thread reacts
    independently of WS state.
    """
    await ws.accept()
    session = get_session()
    detector = get_detector()
    loop = asyncio.get_event_loop()
    # Per-WS subscriber queue — shared VideoSession can have multiple
    # WS handlers connected; broadcast lets each receive every frame.
    sub_q = session.subscribe()
    last_status = 0.0
    last_event_id = 0
    eof_sent = False
    try:
        while True:
            # Push any new detector events first — they have lower
            # latency tolerance than frames and the user is staring at
            # the page waiting for them.
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
            await ws.send_bytes(item)
            now = loop.time()
            if now - last_status >= 1.0:
                snap = session.snapshot()
                snap["type"] = "status"
                # also include detector snapshot so the UI can show
                # prompt state without extra round-trips
                snap["detector"] = detector.snapshot()
                await ws.send_json(snap)
                last_status = now
    except Exception:
        pass
    finally:
        session.unsubscribe(sub_q)