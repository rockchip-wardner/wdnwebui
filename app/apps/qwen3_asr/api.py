"""Qwen3-ASR REST + WebSocket API。

端点
=====

* ``GET    /api/qwen3-asr/``            — 当前 snapshot。
* ``GET    /api/qwen3-asr/rounds``       — 本次识别的逐轮明细（刷新页面后恢复）。
* ``POST   /api/qwen3-asr/upload``       — 上传音频/视频到设备端（向导的「上传…」）。
* ``POST   /api/qwen3-asr/transcribe``   — 发起一次流式识别。
  来源二选一：multipart 的 ``file``（上传内容）**或** form 的 ``path``
  （设备端路径，向导「浏览…」选出来的）。音频与**含音轨的视频**都支持。
  400 / 409 / 502。
* ``GET    /api/qwen3-asr/media?path=`` — 把**设备端**的音视频文件交给浏览器
  播放（「含音轨的视频」来源要把画面播出来）。支持 Range → ``<video>``
  可拖进度条。⚠️ 本服务**没有鉴权** → 路径被三道闸限制（根目录白名单 /
  扩展名白名单 / resolve 后仍在根内），详见 :func:`_resolve_media_path`。
* ``POST   /api/qwen3-asr/stop``         — 立即停止正在跑的识别。
* ``POST   /api/qwen3-asr/reset``        — 清空本轮结果。
* ``WS     /api/qwen3-asr/ws``           — server-push 流式增量 + 状态变更。
  连接建立后立即推 ``snapshot``。

实时（麦克风）端点
==================

* ``POST   /api/qwen3-asr/live/start``   — 开一个实时识别会话。
* ``POST   /api/qwen3-asr/live/chunk``   — 提交一块 PCM（裸 s16le）。
* ``POST   /api/qwen3-asr/live/finish``  — 收尾，返回最终文本。
* ``DELETE /api/qwen3-asr/live/stop``    — 立即中止。

板载麦克风（设备端 ALSA 采集）端点：

* ``GET    /api/qwen3-asr/live/board/devices`` — 可用采集设备列表。
* ``POST   /api/qwen3-asr/live/board/start``   — 起采集进程。
* ``POST   /api/qwen3-asr/live/board/stop``    — 停采集进程。
* ``GET    /api/qwen3-asr/live/board/status``  — 采集进程状态。

这四个端点由 **feature flag** ``asr.live.enabled`` 门控 —— 关掉时一律
**404**，前端隐藏实时区块，**文件 / 视频路径完全不受影响**。

WS 帧类型
=========

* ``snapshot`` —— 连接建立后立即推一次。
* ``state``   —— 状态字段 diff（含完整 snapshot）。
* ``round``   —— 一轮增量：``{round, committed, unfixed, language, final}``。
  ``committed`` 是**累计**的已确认文本，``unfixed`` 是本轮还没确认的尾巴
  （下一轮可能被改写）。
* ``done``    —— 识别结束：``{language, text, elapsed_sec}``。
* ``error``   —— 推理异常或 capability 未启用。
* ``gap``     —— **新增**：音频有丢失，识别结果可能有缺口。
* ``endpoint``—— **新增**：一句话结束（端点检测命中）。
* ``live_done`` —— **新增**：实时会话结束。
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path
from typing import Any, List, Optional

from fastapi import (APIRouter, Body, File, Form, Header, HTTPException,
                     Query, Request, UploadFile, WebSocket, WebSocketDisconnect)
from fastapi.responses import FileResponse, JSONResponse

from app.apps.qwen3_asr.board_mic import (DEFAULT_DEVICE, BoardMicError,
                                           get_board_mic)
from app.apps.qwen3_asr.service import (_LIVE_ENDPOINT_CONFIG,
                                        _LIVE_SESSION_TTL_SEC,
                                        get_asr_service, get_live_asr_service)
from app.core.audio.capture import (CAPTURE_N_CHANNELS, CAPTURE_SAMPLE_RATE,
                                    CAPTURE_SAMPLE_WIDTH, DEFAULT_CHUNK_MS,
                                    validate_capture_format)
from app.core.audio.decode import MEDIA_EXTS, iter_media_pcm
from app.core.config import asr_live_enabled

_LOG = logging.getLogger("qwen3_asr_api")
router = APIRouter(tags=["qwen3-asr"])

#: 单次上传上限。60 秒 48 kHz s24 单声道 ≈ 8.6 MB，留足余量；
#: 含音轨的视频会大得多 —— 走「设备端路径」时不受这个限制。
_MAX_UPLOAD_BYTES = 32 * 1024 * 1024

#: 设备端媒体文件**允许的根目录**（相对 ``<assets>``）。
#:
#: ⚠️ 这个 webui **没有鉴权** —— 媒体端点等于「任意文件读取」入口，
#: 所以根目录必须是**白名单**，不能是「整个 /userdata」。
#:
#: ``uploads`` —— 向导「上传…」落盘处（用户选的视频都在这）；
#: ``tts``    —— TTS 生成的音频。
_MEDIA_ROOT_NAMES = ("uploads", "tts")

#: 常见容器在 ``mimetypes`` 里查不到的 MIME（``mkv`` / ``avi`` 之类）。
#: 浏览器靠 Content-Type 决定用哪个解码器 —— 给错会直接播不出来。
_MEDIA_TYPES = {
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
    ".mov": "video/quicktime",
    ".m4v": "video/x-m4v",
    ".3gp": "video/3gpp",
    ".ts": "video/mp2t",
    ".flv": "video/x-flv",
    ".wmv": "video/x-ms-wmv",
    ".mpg": "video/mpeg",
    ".mpeg": "video/mpeg",
    ".aiff": "audio/aiff",
    ".aif": "audio/aiff",
    ".opus": "audio/opus",
    ".amr": "audio/amr",
    ".m4a": "audio/mp4",
    ".wma": "audio/x-ms-wma",
}

#: 向导「上传…」落盘目录。与视频卡共用 ``<resource_dir>/uploads``，
#: 这样从任一应用上传的文件，另一个应用直接用同一绝对路径不会产生副本。
_UPLOAD_DIR: Optional[Path] = None


def _upload_dir() -> Path:
    global _UPLOAD_DIR
    if _UPLOAD_DIR is None:
        from app.core.config import default_resource_root
        d = default_resource_root() / "uploads"
        d.mkdir(parents=True, exist_ok=True)
        _UPLOAD_DIR = d
    return _UPLOAD_DIR


# --------------------------------------------------------------------------- #
# 设备端媒体文件（给 <video> 播）
# --------------------------------------------------------------------------- #
def _media_roots() -> List[Path]:
    """媒体文件允许的根目录（已 resolve）。"""
    from app.core.config import default_resource_root

    base = default_resource_root()
    out: List[Path] = []
    for name in _MEDIA_ROOT_NAMES:
        try:
            out.append((base / name).resolve())
        except OSError:  # pragma: no cover — 目录不存在时跳过
            continue
    return out


def _resolve_media_path(raw: str) -> Path:
    """把用户给的路径解析成**允许根目录内**的媒体文件。

    ⚠️ 这个 webui **没有鉴权** —— 这个端点等于「任意文件读取」入口
    （``?path=/etc/shadow`` 能把 shadow 读出来）。所以必须三道闸：

    1. **根目录白名单** —— 只允许 ``<assets>/uploads`` 与 ``<assets>/tts``；
    2. **扩展名白名单** —— 只允许 :data:`MEDIA_EXTS`（音视频）；
    3. **resolve 之后仍在根内** —— ``resolve()`` 会把 ``..`` 和软链接都
       规范化，指向根外的软链接因此被拒。

    Raises
    ------
    HTTPException
        400 参数不对 / 404 不存在或不在允许范围。
    """
    raw = (raw or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="缺少 path 参数")
    p = Path(raw)
    if not p.is_absolute():
        raise HTTPException(
            status_code=400,
            detail="path 必须是设备端的绝对路径: {}".format(raw))

    try:
        resolved = p.resolve()
    except OSError as exc:
        raise HTTPException(
            status_code=404, detail="文件不存在: {}".format(exc)) from exc

    if resolved.suffix.lower() not in MEDIA_EXTS:
        raise HTTPException(
            status_code=400,
            detail="不是可播放的音视频文件（支持 {}）".format(
                ", ".join(sorted(MEDIA_EXTS))))

    roots = _media_roots()
    for root in roots:
        try:
            inside = resolved.is_relative_to(root)
        except ValueError:  # pragma: no cover
            inside = False
        if inside:
            break
    else:
        raise HTTPException(
            status_code=403,
            detail="只能播放资源目录下的文件（{}）".format(
                ", ".join(str(r) for r in roots)))

    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return resolved


def _media_type(path: Path) -> str:
    """猜 MIME；``mimetypes`` 认不出的容器用内置表。"""
    import mimetypes

    ext = path.suffix.lower()
    if ext in _MEDIA_TYPES:
        return _MEDIA_TYPES[ext]
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


@router.get("/media")
async def get_media(path: str = Query(..., description="设备端媒体文件绝对路径")):
    """把设备端的音视频文件交给浏览器播放。

    用 ``FileResponse`` 而不是手写流式：Starlette 的 ``FileResponse``
    **支持 Range 请求** —— ``<video>`` 拖进度条、``<audio>`` 定位都靠它。
    没有 Range 的话浏览器只能从头播、拖不动。

    ⚠️ 这个端点**没有鉴权**，所以路径必须过
    :func:`_resolve_media_path` 的三道闸（根目录 / 扩展名 / resolve）。
    """
    p = _resolve_media_path(path)
    return FileResponse(p, media_type=_media_type(p),
                        filename=p.name, content_disposition_type="inline")


# --------------------------------------------------------------------------- #
# REST
# --------------------------------------------------------------------------- #
@router.get("/")
async def get_asr_snapshot() -> dict:
    """完整 snapshot；UI 在进入页面时拉一次，后续靠 WS 推送增量。"""
    return get_asr_service().snapshot()


@router.get("/rounds")
async def get_asr_rounds() -> dict:
    """本次识别的逐轮明细（页面刷新后恢复）。"""
    return {"rounds": get_asr_service().rounds()}


@router.post("/upload")
async def post_asr_upload(file: UploadFile = File(...)) -> dict:
    """向导「上传…」→ 落到设备端 ``<resource_dir>/uploads``。

    返回 ``{path, name, size}`` —— 与向导框架的 upload 契约一致
    （``wizard.json`` 的 ``upload_endpoint``）。
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="empty filename")
    safe_name = Path(file.filename).name
    if not safe_name:
        raise HTTPException(status_code=400, detail="invalid filename")
    if not Path(safe_name).suffix:
        safe_name += ".bin"
    dest = _upload_dir() / safe_name
    try:
        # 同名覆盖：先 unlink 再 open("wb") —— 目标文件若由别的属主创建，
        # open("wb") 会因 inode 写权限失败，unlink 走的是目录级写权限。
        try:
            dest.unlink()
        except FileNotFoundError:
            pass
        with dest.open("wb") as f:
            shutil.copyfileobj(file.file, f)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail="write failed: {}".format(e))
    finally:
        try:
            file.file.close()
        except Exception:  # noqa: BLE001
            pass
    return {
        "path": str(dest),
        "name": file.filename,
        "size": dest.stat().st_size,
    }


@router.post("/transcribe")
async def post_asr_transcribe(
    file: Optional[UploadFile] = File(None),
    path: Optional[str] = Form(None),
    chunk_seconds: Optional[float] = Form(None),
    max_new_tokens: Optional[int] = Form(None),
    rollback_tokens: Optional[int] = Form(None),
    rollback_rounds: Optional[int] = Form(None),
    start_sec: Optional[float] = Form(None),
    append: Optional[bool] = Form(None),
) -> dict:
    """发起一次流式识别。

    来源二选一：

    * ``file`` —— multipart 上传的音频 / 含音轨的视频；
    * ``path`` —— 设备端文件路径（向导「浏览…」选出来的）。

    ⚠️ **不再截断到 60 s** —— 那是 C demo 离线在线编码器的限制
    （``ONLINE_MAX_AUDIO_SECONDS``）。这条路径改走**增量会话**
    （每 1 秒一块喂给 SDK），长录音的代价是线性的 → 不该再砍尾巴。

    ⚠️ 解码（WAV 解析 / PyAV）是**阻塞 I/O** —— 解码器在 worker 线程
    里**边解边喂**，整个过程都不占事件循环（否则整个 Web 服务会卡住，
    与 model-download 清单端点同一个坑）。

    ``start_sec`` —— 从**容器的第几秒**开始识别（秒，0 = 从头）。
    「拖动进度条 → 字幕跟过去」用它：不用每次都从头解一遍 98 分钟的
    音轨（实测 PyAV seek 到 5000 s 只要 0.04 s，定位误差 ±20 ms）。
    轮次时间戳仍是**绝对**音频时间 → 前端的时间轴不用改。

    ``append`` —— **保留**已有的轮次（而不是清空）。拖动后重开识别时
    用它：前面识别过的部分要留在字幕时间轴上。
    """
    if file is not None and file.filename:
        raw = await file.read()
        if not raw:
            raise HTTPException(status_code=400, detail="音频内容为空")
        if len(raw) > _MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=400,
                detail="音频过大（上限 {} MB）；大文件请用设备端路径".format(
                    _MAX_UPLOAD_BYTES // (1024 * 1024)),
            )
        source: Any = raw
        name = file.filename
    elif path and path.strip():
        source = path.strip()
        name = os.path.basename(source)
    else:
        raise HTTPException(status_code=400, detail="没有音频来源（file 或 path）")

    options = {}
    if chunk_seconds is not None:
        options["online_chunk_seconds"] = float(chunk_seconds)
    if max_new_tokens is not None:
        options["online_max_new_tokens"] = int(max_new_tokens)
    if rollback_tokens is not None:
        options["online_rollback_tokens"] = int(rollback_tokens)
    if rollback_rounds is not None:
        options["online_rollback_rounds"] = int(rollback_rounds)

    svc = get_asr_service()
    start_at = max(0.0, float(start_sec or 0.0))

    def _factory():
        """惰性解码器 —— 真正的解码在 worker 线程里做（阻塞 I/O）。"""
        return iter_media_pcm(source, filename=name, chunk_seconds=1.0,
                              start_sec=start_at)

    try:
        rid = svc.transcribe_stream(_factory, options=options,
                                     append=bool(append), start_sec=start_at)
    except ValueError as e:
        msg = str(e)
        if "上一条" in msg:
            return JSONResponse(
                status_code=409,
                content={"detail": msg, "snapshot": svc.snapshot()},
            )
        raise HTTPException(status_code=400, detail=msg) from e
    except RuntimeError as e:
        # 「功能未启用: swcap_audio_asr」 —— 模型没在算力页启动。
        raise HTTPException(status_code=502, detail=str(e)) from e
    return {
        "ok": True,
        "request_id": rid,
        "source_name": name,
        "snapshot": svc.snapshot(),
    }


@router.post("/stop")
async def post_asr_stop() -> dict:
    """立即停止正在跑的识别。"""
    svc = get_asr_service()
    svc.stop()
    return {"ok": True, "snapshot": svc.snapshot()}


@router.post("/reset")
async def post_asr_reset() -> dict:
    """清空本轮结果。"""
    svc = get_asr_service()
    svc.reset()
    return {"ok": True, "snapshot": svc.snapshot()}


# --------------------------------------------------------------------------- #
# 实时（麦克风）会话
# --------------------------------------------------------------------------- #
def _require_live_enabled() -> None:
    """feature flag 门控。

    ``asr.live.enabled=false`` → 404（不是 403）：这个特性对外**不存在**，
    前端据此隐藏实时区块，文件 / 视频路径完全不受影响。
    """
    if not asr_live_enabled():
        raise HTTPException(status_code=404, detail="实时识别未启用")


@router.post("/live/start")
async def post_live_start(body: Optional[dict] = Body(None)) -> dict:
    """开一个实时识别会话。

    请求体（JSON）：

    ==============  =======  =======================================
    字段             默认      说明
    ==============  =======  =======================================
    ``source``       —         ``"browser"`` / ``"board"``
    ``sample_rate``  16000     **必须**是 16000
    ``n_channels``   1         只支持 1
    ``sample_width`` 2         只支持 2（s16le）
    ``chunk_ms``     500       采集端攒块粒度
    ==============  =======  =======================================
    """
    _require_live_enabled()
    data = body or {}
    source = str(data.get("source") or "browser").strip().lower()
    if source not in ("browser", "board"):
        raise HTTPException(status_code=400,
                            detail="source 必须是 browser 或 board")
    try:
        sample_rate = int(data.get("sample_rate") or CAPTURE_SAMPLE_RATE)
        n_channels = int(data.get("n_channels") or CAPTURE_N_CHANNELS)
        sample_width = int(data.get("sample_width") or CAPTURE_SAMPLE_WIDTH)
        chunk_ms = int(data.get("chunk_ms") or DEFAULT_CHUNK_MS)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="参数类型不对") from None
    try:
        validate_capture_format(sample_rate, n_channels, sample_width)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if not (50 <= chunk_ms <= 5000):
        raise HTTPException(status_code=400,
                            detail="chunk_ms 必须在 50..5000 之间")

    mgr = get_live_asr_service()
    session = mgr.create(
        source=source, sample_rate=sample_rate, n_channels=n_channels,
        sample_width=sample_width, chunk_ms=chunk_ms)
    try:
        session.select_session()
    except RuntimeError as e:
        # 「功能未启用: swcap_audio_asr」 —— 模型没在算力页启动。
        mgr.drop(session.stream_id)
        raise HTTPException(status_code=502, detail=str(e)) from e
    await session.start()
    return {
        "ok": True,
        "stream_id": session.stream_id,
        "session_ttl_sec": _LIVE_SESSION_TTL_SEC,
        "chunk_seconds": 1.0,
        "endpoint": _LIVE_ENDPOINT_CONFIG.as_dict(),
    }


@router.post("/live/chunk")
async def post_live_chunk(
    request: Request,
    stream_id: str = Header(..., alias="X-Asr-Stream-Id"),
    seq: int = Header(..., alias="X-Asr-Seq"),
) -> dict:
    """提交一块 PCM（**裸 s16le**，无 WAV 头）。

    响应::

        {"ok": true, "seq": 12, "round": 7, "committed": "...",
         "unfixed": "...", "language": "English", "lag_ms": 143,
         "buffered_chunks": 0, "dropped_chunks": 0}

    ``round`` 为 0 表示这一块**没凑满 1 秒**（只缓冲，尚未推理）。
    """
    _require_live_enabled()
    mgr = get_live_asr_service()
    session = mgr.get(stream_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在或已过期")
    pcm = await request.body()
    if not pcm:
        raise HTTPException(status_code=400, detail="音频块为空")
    try:
        result = await session.submit(pcm, seq)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    if result.get("error"):
        # 熔断 / 会话出错 —— 409（冲突），不是 500。
        raise HTTPException(status_code=409, detail=result["error"])
    return result


@router.post("/live/finish")
async def post_live_finish(
    stream_id: str = Header(..., alias="X-Asr-Stream-Id"),
) -> dict:
    """收尾：把尾巴喂给 SDK → 返回最终文本。"""
    _require_live_enabled()
    mgr = get_live_asr_service()
    session = mgr.get(stream_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在或已过期")
    try:
        result = await session.finish()
    finally:
        mgr.drop(stream_id)
    return result


@router.delete("/live/stop")
async def delete_live_stop(
    stream_id: Optional[str] = Header(None, alias="X-Asr-Stream-Id"),
) -> dict:
    """立即中止。

    **不带** ``X-Asr-Stream-Id`` → 停掉**全部**实时会话（DemoGuard 的
    离开页面兜底走的就是这条路径 —— 它不知道会话 id）。
    """
    _require_live_enabled()
    mgr = get_live_asr_service()
    dropped = 0
    if stream_id:
        session = mgr.get(stream_id)
        if session is not None:
            result = await session.stop()
            dropped = int(result.get("dropped_chunks") or 0)
            mgr.drop(stream_id)
        return {"ok": True, "dropped_chunks": dropped}
    for s in mgr.sessions():
        try:
            result = await s.stop()
            dropped += int(result.get("dropped_chunks") or 0)
        except Exception as e:  # noqa: BLE001
            _LOG.debug("live stop failed: %s", e)
        mgr.drop(s.stream_id)
    return {"ok": True, "dropped_chunks": dropped}


# --------------------------------------------------------------------------- #
# 板载麦克风（设备端 ALSA 采集）
# --------------------------------------------------------------------------- #
@router.get("/live/board/devices")
async def get_board_devices() -> dict:
    """本机可用的 ALSA 采集设备（UI 下拉用）。"""
    ctl = get_board_mic()
    return {"ok": True, "devices": ctl.devices(),
            "default_device": DEFAULT_DEVICE}


@router.post("/live/board/start")
async def post_board_start(device: Optional[str] = Body(None,
                                                        embed=True)) -> dict:
    """起板载麦克风采集（独立进程）。

    请求体 ``{"device": "hw:1,0"}``（省略则用默认设备）。

    ⚠️ 采集进程自己开实时会话、自己推流 —— 服务端不碰声卡。
    """
    _require_live_enabled()
    ctl = get_board_mic()
    try:
        st = ctl.start(device or "")
    except BoardMicError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return {"ok": True, **st}


@router.post("/live/board/stop")
async def post_board_stop() -> dict:
    """停板载麦克风采集（SIGTERM → agent 优雅收尾）。"""
    ctl = get_board_mic()
    # ⚠️⚠️ **不能**直接调 ``ctl.stop()``。
    #
    # ``ctl.stop()`` 是**同步阻塞**的（等 agent 退出，最多 8 s）——
    # 写在 ``async def`` 端点里等于**把事件循环堵死**。而 agent 收到
    # SIGTERM 后正要 POST ``/live/finish`` 收尾，那个请求正好排在被
    # 堵死的事件循环后面 → **互相等**：
    #
    #   webui: ctl.stop() 等 agent 退出 ─┐
    #   agent: /live/finish 等 webui 处理 ─┘  → agent 被 SIGKILL，
    #   ``/live/finish`` 永远丢，live 会话残留到 600 s TTL。
    #
    # 实测症状：stop 端点耗时 5055 ms（= grace 秒数）、agent 日志停在
    # 「收到信号 15，准备收尾」、webui 日志在这 5 秒里**一条请求都没有**。
    #
    # 所以 ``stop`` 丢到线程里跑 —— 事件循环保持响应，``/live/finish``
    # 才处理得掉。
    result = await asyncio.to_thread(ctl.stop)
    # 安全网：agent 万一被 SIGKILL（没走完 finish）→ 把板载会话收掉，
    # 别让它占着 SDK 会话直到 600 s TTL。
    closed = await _close_board_live_sessions()
    return {**result, "closed_sessions": closed}


async def _close_board_live_sessions() -> int:
    """收掉所有**板载**来源的实时会话；返回收掉几个。

    只收 ``source == "board"`` 的 —— 浏览器采集的会话归浏览器自己管
    （``/live/stop``），不该被板载麦克风的停止顺手带走。
    """
    mgr = get_live_asr_service()
    closed = 0
    for s in mgr.sessions():
        if s.source != "board":
            continue
        try:
            await s.stop()
        except Exception as e:  # noqa: BLE001
            _LOG.debug("board live session stop failed: %s", e)
        mgr.drop(s.stream_id)
        closed += 1
    if closed:
        _LOG.info("board mic stop: 收掉 %s 个板载实时会话", closed)
    return closed


@router.get("/live/board/status")
async def get_board_status() -> dict:
    """板载麦克风采集进程的状态。"""
    ctl = get_board_mic()
    return {"ok": True, **ctl.status()}


# --------------------------------------------------------------------------- #
# WebSocket —— push 流式增量 + 状态变更
# --------------------------------------------------------------------------- #
@router.websocket("/ws")
async def asr_ws(ws: WebSocket) -> None:
    """WS handler：snapshot hydrate + 流式增量推送。"""
    await ws.accept()
    svc = get_asr_service()
    queue: asyncio.Queue = asyncio.Queue(maxsize=500)

    try:
        await ws.send_json({"type": "snapshot", "snapshot": svc.snapshot()})
    except Exception:
        return

    svc.register_subscriber(queue)

    async def _relay() -> None:
        try:
            while True:
                payload = await queue.get()
                try:
                    await ws.send_json(payload)
                except Exception:
                    return
        except asyncio.CancelledError:
            return

    relay_task = asyncio.create_task(_relay(), name="qwen3-asr-ws-relay")

    try:
        # 客户端不发任何帧；这里循环 receive 仅用于检测断开
        while True:
            try:
                await ws.receive_text()
            except WebSocketDisconnect:
                break
    finally:
        svc.unregister_subscriber(queue)
        relay_task.cancel()
        try:
            await relay_task
        except (asyncio.CancelledError, Exception):
            pass
