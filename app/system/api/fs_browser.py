"""设备端文件系统浏览器 endpoint —— v0.4 后从 video_detection 抽到 core。

``GET /api/fs?path=...`` 返回一个目录的子项(限定在配置的资源根
内),供任何需要"浏览设备文件"的卡片使用:音视频描述 / 视频目标监控 /
视频目标定位 / 视频内容检索 / 视频文件分析。各卡片前端统一走
``window.FSModal``(见 app/static/core/fs-modal.js),modal 自挂 DOM
并调用本 endpoint。

设计:
* 任何路径必须落在 ``FS_ROOT``(``FS_FS_ROOT`` env 优先,其次
  ``base.resource_dir`` from config.jsonc)下,避免 web UI 漫游到
  设计外的目录。
* 伪文件系统(/proc, /sys, /dev, ...)黑名单,UI 也走不到。
* 目录深度与子项数量有上限,避免意外枚举整盘。
"""
from __future__ import annotations

import stat
from pathlib import Path

from fastapi import APIRouter, HTTPException

from app.core.fs_paths import (
    get_fs_root as _get_fs_root,
    is_skipped as _is_skipped,
    is_within as _is_within,
    resolve_within_root,
)


router = APIRouter(tags=["fs-browser"])


_MAX_DEPTH = 8
_MAX_ENTRIES = 2000


def _depth_from(target: Path, root: Path) -> int:
    try:
        rel = target.relative_to(root)
    except ValueError:
        return 0
    return len(rel.parts)


@router.get("/fs")
async def browse_fs(path: str = "") -> dict:
    """List one directory on the server's filesystem.

    The path is resolved and must stay under the configured FS root
    (``base.resource_dir``, with ``FS_FS_ROOT`` overriding) so the web
    UI cannot wander outside what's intended. Pseudo-filesystems and
    well-known system trees are filtered.
    """
    fs_root = _get_fs_root()
    target = resolve_within_root(path)
    root = Path(fs_root).resolve(strict=False)
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"不存在: {target}")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail=f"不是目录: {target}")
    if _depth_from(target, root) > _MAX_DEPTH:
        raise HTTPException(status_code=400, detail="层级过深")

    entries: list[dict] = []
    try:
        children = list(target.iterdir())
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=f"权限不足: {e}")

    def _sort_key(c: Path):
        try:
            st = c.stat()
        except OSError:
            return (1, c.name.lower())
        return (0 if stat.S_ISDIR(st.st_mode) else 1, c.name.lower())

    children.sort(key=_sort_key)

    for child in children:
        if child.name.startswith("."):
            continue
        sp = str(child)
        if _is_skipped(sp):
            continue
        try:
            st = child.stat()
        except OSError:
            continue
        if stat.S_ISDIR(st.st_mode):
            entries.append({
                "name": child.name,
                "path": sp,
                "type": "dir",
                "size": None,
            })
        elif stat.S_ISREG(st.st_mode):
            entries.append({
                "name": child.name,
                "path": sp,
                "type": "file",
                "size": st.st_size,
            })
        if len(entries) >= _MAX_ENTRIES:
            break

    parent = None
    if _is_within(target, root) and target != root:
        parent = str(target.parent)

    return {
        "path": str(target),
        "root": str(root),
        "parent": parent,
        "is_root": target == root,
        "entries": entries,
        "truncated": len(entries) >= _MAX_ENTRIES,
    }