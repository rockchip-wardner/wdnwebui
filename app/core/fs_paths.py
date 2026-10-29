"""设备端文件路径解析 —— 「用户给的路径是否落在允许的根目录内」。

为什么在 core 而不是 ``app/system/api/fs_browser.py``
-----------------------------------------------------
``/api/fs`` 浏览端点与「从板子上的文件注册音色」等**卡片侧**入口必须共用
同一份根目录校验 —— 各写一份就等于开了个任意读文件的口子。而卡片只能
``from app.core.*``(AGENTS.md 零耦合铁律,插件包更是明文规定),所以这段
校验必须待在 core 里,不能挂在 system 层让卡片反向依赖。

本模块只做**解析与边界判定**,不含任何 HTTP 端点。
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import HTTPException

from app.core.config import default_resource_root, get_config


# Pseudo-filesystems (and other large / sensitive trees) we never want to
# descend into from the modal. Matched by prefix so /proc/1, /proc/foo,
# etc. are all skipped.
_SKIP_DIR_PREFIXES = (
    "/proc",
    "/sys",
    "/dev",
    "/run",
    "/snap",
    "/var/lib",
    "/var/cache",
    "/usr/src",
    "/usr/lib/modules",
    "/usr/lib/firmware",
    "/usr/share/locale",
    "/usr/share/doc",
    "/usr/share/man",
)


def get_fs_root() -> str:
    """Resolve the root directory the server-side file browser is
    allowed to descend into. Priority:

    1. ``FS_FS_ROOT`` env var (operator override; takes precedence over
       anything stored in config so a host-side lockdown still wins)。
       向后兼容旧 ``VD_FS_ROOT`` 命名。
    2. ``base.resource_dir`` from ``config.jsonc`` —— 基础配置页指定的
       资源目录(默认 ``<root>/test``,见
       :func:`app.core.config.default_resource_root`)。每次调用实时读,
       保存即生效。
    3. Hard fallback via :func:`app.core.config.default_resource_root`
       —— on-device: ``/userdata/test``; off-device: ``<HOME>/test``.
    """
    env_root = os.environ.get("FS_FS_ROOT") or os.environ.get("VD_FS_ROOT")
    if env_root:
        return env_root
    base = get_config().get_section("base", {}) or {}
    rd = base.get("resource_dir")
    if isinstance(rd, str) and rd.strip():
        return rd.strip()
    return str(default_resource_root())


def is_within(p: Path, root: Path) -> bool:
    p_s = str(p)
    r_s = str(root)
    if p_s == r_s:
        return True
    return p_s.startswith(r_s.rstrip("/") + "/")


def is_skipped(path_str: str) -> bool:
    for prefix in _SKIP_DIR_PREFIXES:
        base = prefix.rstrip("/")
        p = path_str.rstrip("/") or "/"
        if p == base or p.startswith(base + "/"):
            return True
    return False


def resolve_within_root(raw: str) -> Path:
    """把用户给的路径解析成 **FS 根目录内** 的真实路径。

    越界 / 黑名单一律拒（HTTPException）。``/api/fs`` 和「从板子上的
    文件注册音色」共用这一份校验 —— 别让第二条路径绕过根目录限制，
    那等于开了个任意读文件的口子。
    """
    fs_root = get_fs_root()
    try:
        target = Path((raw or "").strip() or fs_root).resolve(strict=False)
    except (OSError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=f"bad path: {e}")
    if is_skipped(str(target)):
        raise HTTPException(
            status_code=403,
            detail=f"路径 {target} 在黑名单中",
        )
    root = Path(fs_root).resolve(strict=False)
    if not is_within(target, root):
        raise HTTPException(
            status_code=400,
            detail=f"path {target} 超出允许的根目录 {root}",
        )
    return target
