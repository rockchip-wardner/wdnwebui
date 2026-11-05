"""模型下载 REST API —— 算力配置页「模型下载」卡片的后端。

Endpoints
---------
* ``GET  /api/compute/model-repos``            远端仓库清单 + 本地已装状态
                                               （含候选下载目录清单）。
* ``POST /api/compute/model-download``          ``{repos:[...], dest_dir}`` → 排队。
* ``GET  /api/compute/model-download/status``   任务进度（前端 1Hz 轮询）。
* ``POST /api/compute/model-download/cancel``   取消某个任务。

错误码：400 参数不合法 / 409 目标已存在或已在队列 / 404 任务不存在 /
502 远端镜像不可达。

⚠️ **凡是碰文件系统的路由都写成同步 def** —— ``async def`` 会在事件循环里
做阻塞 I/O，把整个 Web 服务卡住（实测一次 22s，期间无响应）。FastAPI 会把
同步路由丢到线程池里跑。
"""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core.compute import model_download as md


router = APIRouter(tags=["compute"])


class DownloadRequest(BaseModel):
    """一键下载的入参。"""

    repos: List[str] = Field(default_factory=list,
                             description="要下载的仓库 id，如 wardner/Qwen3.5-4B")
    dest_dir: str = Field(default="",
                          description="下载目录（绝对路径）；默认 = 当前配置的模型根")


class CancelRequest(BaseModel):
    """取消任务的入参。"""

    job_id: str = Field(default="", description="要取消的任务 id")


@router.get("/compute/model-repos")
def get_model_repos() -> dict:
    """远端仓库清单 + 本地已装状态 + 下载目录候选。

    每项含：``name`` / ``repo_id`` / ``bytes`` / ``files`` / ``installed``
    / ``local_path``（已装时是本板绝对路径，鼠标悬停用）。
    """
    try:
        return md.repo_inventory()
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/compute/model-download")
def start_download(payload: DownloadRequest) -> dict:
    """把选中的仓库排进下载队列（串行）。

    * 下载目录必须是候选模型根之一（见 :func:`md.resolve_dest`）。
    * 已在队列里的仓库会被跳过（不重复下）。
    * 目标已存在的仓库也会被跳过 —— **不覆盖**板上已有模型。
    """
    if not payload.repos:
        raise HTTPException(status_code=400, detail="没有选择要下载的模型")
    try:
        dest = md.resolve_dest(payload.dest_dir or md.default_dest())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    manager = md.get_manager()
    try:
        result = manager.enqueue(payload.repos, str(dest))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not result["created"]:
        raise HTTPException(
            status_code=409,
            detail="；".join(s["reason"] for s in result["skipped"]) or "没有可下载的模型",
        )
    return {"ok": True, **result, "status": manager.status()}


@router.get("/compute/model-download/status")
def get_download_status() -> dict:
    """当前下载任务进度（前端 1Hz 轮询）。"""
    return md.get_manager().status()


@router.post("/compute/model-download/cancel")
def cancel_download(payload: CancelRequest) -> dict:
    """取消某个下载任务。

    取消只停**当前进程**里的下载；``.dl-*`` 暂存目录保留，下次可以续传。
    """
    if not payload.job_id:
        raise HTTPException(status_code=400, detail="缺少 job_id")
    manager = md.get_manager()
    result = manager.cancel(payload.job_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404,
                            detail=result.get("error") or "找不到该任务")
    return {"ok": True, "status": manager.status()}
