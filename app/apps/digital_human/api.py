"""数字人 REST API。

端点
====

* ``GET    /api/digital-human``             — 当前状态（NPU 是否已加载 / 引擎是否就位 /
                                              当前作业）。
* ``POST   /api/digital-human/speak``       — 起一次「说话」：multipart 的 ``file``
                                              （上传的 wav）**或** form 的 ``path``
                                              （设备端已有的 wav）。返回 ``{job, …}``。
* ``GET    /api/digital-human/jobs/{id}``   — 作业进度（state / step / frame_count）。
* ``GET    /api/digital-human/jobs/{id}/frame/{n}`` — 第 n 帧 JPEG。
* ``GET    /api/digital-human/jobs/{id}/audio``     — 该次作业的音频（浏览器播放用）。
* ``GET    /api/digital-human/jobs/{id}/params``    — 嘴型参数（32 维/帧），调试用。
* ``POST   /api/digital-human/stop``        — 停掉当前作业。

错误码
======

* 400 —— 没有音频 / 文件不是 wav / 作业不存在。
* 502 —— 算力实例未就绪（到「算力」页启用「[AV]数字人 LiteAvatar 渲染」）。
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from app.apps.digital_human.avatars import AvatarError
from app.apps.digital_human.service import get_service
from app.core.uploads import get_card_uploads_root

_LOG = logging.getLogger("digital_human_api")
router = APIRouter(tags=["digital-human"])

#: 单个音频的大小上限（50 MB ≈ 25 分钟 16 kHz 单声道 wav）
MAX_AUDIO_BYTES = 50 * 1024 * 1024
ALLOWED_SUFFIXES = (".wav",)


def _save_upload(upload: UploadFile) -> Path:
    """把上传的音频落到本卡的 uploads 目录。"""
    name = (upload.filename or "speech.wav").strip()
    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(400, "只接受 wav 文件（收到 {}）".format(suffix or "无扩展名"))
    target_dir = get_card_uploads_root("digital_human") / "audio"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "{}{}".format(Path(name).stem[:40], suffix)
    with target.open("wb") as fh:
        shutil.copyfileobj(upload.file, fh, length=1024 * 256)
    if target.stat().st_size > MAX_AUDIO_BYTES:
        target.unlink(missing_ok=True)
        raise HTTPException(400, "音频太大（上限 {} MB）".format(MAX_AUDIO_BYTES // (1024 * 1024)))
    return target


# --------------------------------------------------------------------------- #
# REST
# --------------------------------------------------------------------------- #
@router.get("/")
async def get_status() -> dict:
    """当前状态。"""
    return get_service().status()


# --------------------------------------------------------------------------- #
# 形象库
# --------------------------------------------------------------------------- #
@router.get("/avatars")
async def list_avatars() -> dict:
    """本地已就绪的形象（可切换的那些）。"""
    svc = get_service()
    avatars = svc.avatars.list_avatars()
    return {
        "avatars": avatars,
        "default": svc.avatars.default_id(),
        "downloads": svc.avatars.downloads(),
    }


@router.get("/avatars/gallery")
async def list_gallery(refresh: bool = False) -> dict:
    """官方形象库清单（远端 ModelScope，带本地缓存）。"""
    svc = get_service()
    items = svc.avatars.gallery_list(refresh=refresh)
    local = {a["id"] for a in svc.avatars.list_avatars()}
    for item in items:
        item["local"] = item["id"] in local
    return {"items": items, "count": len(items)}


@router.get("/avatars/{avatar_id}/preview")
async def get_avatar_preview(avatar_id: str) -> FileResponse:
    """形象预览图（立绘 PNG）。

    本地没有就临时从官方形象库拉一张并缓存 —— 这样「形象库」面板里的缩略图
    不用等素材包下载完就能显示。
    """
    svc = get_service()
    try:
        path = svc.avatars.preview_path(avatar_id)
    except AvatarError as exc:
        raise HTTPException(400, str(exc)) from exc
    if path is None or not path.is_file():
        raise HTTPException(404, "没有这个形象的预览图: {}".format(avatar_id))
    return FileResponse(path, media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})


@router.get("/avatars/{avatar_id}/frame-preview")
async def get_avatar_frame_preview(avatar_id: str) -> FileResponse:
    """形象素材的**等比首帧**（舞台占位图）—— 从 ``bg_video.mp4`` 抽第一帧。

    与 ``/preview``（官方图库的 3:4 立绘）不同：这张图跟形象的真实比例
    一致，所以塞进按比例做的舞台里既不裁切也不留黑边。
    """
    svc = get_service()
    try:
        path = svc.avatars.frame_preview_path(avatar_id)
    except AvatarError as exc:
        raise HTTPException(400, str(exc)) from exc
    if path is None or not path.is_file():
        raise HTTPException(404, "抽不出这个形象的首帧: {}".format(avatar_id))
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})


@router.api_route("/avatars/pull", methods=["POST", "GET"])
async def pull_avatar(request: Request,
                      id: str = Form(default=""),
                      collection: str = Form(default="")) -> dict:
    """按需下载一个官方形象（后台线程，素材包 12~27 MB）。

    ``GET`` 也收（同 /speak 的兜底理由）：有些浏览器会把 POST 退化成 GET，
    这时从 query string 取参数。
    """
    svc = get_service()
    if request.method == "GET":
        id = request.query_params.get("id") or id
        collection = request.query_params.get("collection") or collection
    if not id:
        raise HTTPException(400, "缺少形象 id")
    try:
        result = svc.avatars.pull(id, collection)
    except AvatarError as exc:
        raise HTTPException(400, str(exc)) from exc
    return result


@router.api_route("/speak", methods=["POST", "GET"])
async def post_speak(
    request: Request,
    file: UploadFile | None = File(default=None),
    path: str | None = Form(default=None),
    avatar: str | None = Form(default=None),
    stride: str | None = Form(default=None),
) -> dict:
    """起一次「说话」作业。

    ``avatar`` 指定用哪个形象（form 字段；不给就用默认形象）。
    ``stride`` 渲染抽帧步长（``1`` = 30 fps 清晰，``2`` = 15 fps 更快）。

    ⚠️ **GET 也收** —— 不是设计，是兜底：实测有浏览器把 ``fetch(url, body)``
    的第二个参数吃掉，请求退化成 GET（服务端回 405 Method Not Allowed，用户只
    看到一句英文）。GET 只能带 ``?path=``（设备端已有的 wav），带不了上传文件。
    """
    svc = get_service()
    if request.method == "GET":
        q_path = request.query_params.get("path")
        if not q_path:
            raise HTTPException(400, "GET 方式只支持 ?path=<设备端 wav 路径>")
        _LOG.warning("digital_human: /speak 收到 GET 请求（浏览器把 POST 退化了）")
        path = q_path
        avatar = request.query_params.get("avatar") or avatar
        stride = request.query_params.get("stride") or stride

    if file is not None and file.filename:
        audio = _save_upload(file)
    elif path:
        p = Path(path).expanduser()
        if p.suffix.lower() not in ALLOWED_SUFFIXES:
            raise HTTPException(400, "只接受 wav 文件（收到 {}）".format(p.suffix or "无扩展名"))
        if not p.is_file():
            raise HTTPException(400, "文件不存在: {}".format(p))
        audio = p
    else:
        raise HTTPException(400, "没有音频（需要上传 wav 或给出设备端路径）")

    try:
        step = int(stride) if stride else None
        if step is not None and step not in (1, 2):
            raise HTTPException(400, "stride 只能是 1 或 2")
        job = svc.start(audio, avatar_id=avatar or None, stride=step)
    except AvatarError as exc:
        raise HTTPException(400, str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            502,
            "算力实例未就绪: {}".format(exc)
            + "（请到「算力」页启用「[AV]数字人 LiteAvatar 渲染」）") from exc
    return {"job": job.snapshot()}


@router.api_route("/speak-text", methods=["POST", "GET"])
async def post_speak_text(
    request: Request,
    text: str = Form(default=""),
    avatar: str | None = Form(default=None),
    stride: str | None = Form(default=None),
    language: str = Form(default="auto"),
    volume: str = Form(default="0.1"),
    speaker: str = Form(default=""),
    ref_speaker: str = Form(default=""),
) -> dict:
    """起一次**流式**「说话」：文本 → 板端 TTS → 边合成边渲染。

    与 ``/speak``（拿现成的 wav 渲）的区别：**不等合成完**。TTS 每吐一块
    音频就攒够一段送渲染，浏览器同时在放 ⇒ 台词再长也只等几秒就开口。

    ``/speak`` 那条路是「先合成完（35 秒）再渲染（40 秒）」⇒ 40 多秒才
    开始播 —— 这条路把两件事**并行**了（还跑在两块 NPU 上：TTS 在 EP 协卡、
    渲染在板载 NPU）。
    """
    svc = get_service()
    if request.method == "GET":
        q = request.query_params
        text = q.get("text") or text
        avatar = q.get("avatar") or avatar
        stride = q.get("stride") or stride
        language = q.get("language") or language
        volume = q.get("volume") or volume
        speaker = q.get("speaker") or speaker
        ref_speaker = q.get("ref_speaker") or ref_speaker

    text = (text or "").strip()
    if not text:
        raise HTTPException(400, "没有台词（text 不能为空）")

    try:
        step = int(stride) if stride else None
        if step is not None and step not in (1, 2, 3):
            raise HTTPException(400, "stride 只能是 1 / 2 / 3")
        try:
            vol = float(volume)
        except (TypeError, ValueError):
            vol = 0.1
        job = svc.start_text(
            text,
            avatar_id=avatar or None,
            stride=step,
            language=language or "auto",
            volume=max(0.0, min(1.0, vol)),
            speaker=speaker or "",
            ref_speaker=ref_speaker or "",
        )
    except AvatarError as exc:
        raise HTTPException(400, str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            502,
            "算力实例未就绪: {}".format(exc)
            + "（请到「算力」页启用「[AV]数字人 LiteAvatar 渲染」）") from exc
    return {"job": job.snapshot()}


@router.get("/jobs/{job_id}")
async def get_job(job_id: str) -> dict:
    """作业进度。"""
    job = get_service().get_job(job_id)
    if job is None:
        raise HTTPException(400, "作业不存在: {}".format(job_id))
    return {"job": job.snapshot()}


@router.get("/jobs/{job_id}/frame/{index}")
async def get_job_frame(job_id: str, index: int) -> FileResponse:
    """第 index 帧（JPEG）。"""
    path = get_service().frame_path(job_id, index)
    if path is None:
        raise HTTPException(400, "帧不存在: {}".format(index))
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=3600"})


@router.get("/jobs/{job_id}/audio")
async def get_job_audio(job_id: str):
    """该次作业的音频。

    * **流式作业** → 边合成边吐的 chunked WAV（长度未知，浏览器边下边放）；
    * 普通作业 → 落盘的 wav 文件。
    """
    svc = get_service()
    job = svc.get_job(job_id)
    if job is None:
        raise HTTPException(400, "作业不存在: {}".format(job_id))
    if job.streaming:
        return StreamingResponse(
            svc.wav_stream(job),
            media_type="audio/wav",
            headers={
                "Cache-Control": "no-store",
                # 别让反代缓冲整个响应 —— 那样又变成「等合成完才播」。
                "X-Accel-Buffering": "no",
            },
        )
    if not job.audio_path.is_file():
        raise HTTPException(400, "音频不存在")
    return FileResponse(job.audio_path, media_type="audio/wav")


@router.get("/jobs/{job_id}/params")
async def get_job_params(job_id: str) -> dict:
    """嘴型参数（调试用）。"""
    job = get_service().get_job(job_id)
    if job is None:
        raise HTTPException(400, "作业不存在: {}".format(job_id))
    path = job.work_dir / "params.npy"
    if not path.is_file():
        raise HTTPException(400, "参数还没算出来")
    import numpy as np
    arr = np.load(path)
    return {
        "shape": list(arr.shape),
        "fps": 30,
        "params": [[round(float(v), 4) for v in row] for row in arr[:120]],
    }


@router.api_route("/stop", methods=["POST", "GET"])
async def post_stop(request: Request) -> dict:
    """停掉当前作业（GET 兜底同 /speak）。"""
    if request.method == "GET":
        _LOG.warning("digital_human: /stop 收到 GET 请求（浏览器把 POST 退化了）")
    svc = get_service()
    svc.stop()
    job = svc.current_job()
    return {"job": job.snapshot() if job else None}
