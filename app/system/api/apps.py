"""``/api/apps`` —— 应用包的安装。

应用网格里那个「添加应用」磁位把 wheel 拖上来,这里负责:

1. **装之前**先验它是不是应用包 —— 声明了 ``wdnwebui.cards`` entry point
   组、且带着卡片 ``manifest.json``。两个条件缺一不可,因为
   ``manifest._discover_from_entry_points`` 就是靠这两样把卡片捞出来的:
   只声明 entry point 而没有 manifest.json 的包会在发现时被 LOG.error
   跳过,用户看到的是「装成功了但应用没出现」—— 比直接拒绝更难排查。
2. ``pip install --no-deps`` 装进 venv。

为什么是 ``--no-deps``:卡片包的依赖是 wdnhid / wdnav 这类私有包,PyPI 上
没有,让 pip 去解析必然失败,而它们本来就随 SDK 一起装好了。这与
``update_rkwhl.sh`` 的 ``update_pkg`` 口径一致(那里也是 ``--no-deps``)。

⚠️ **安全**:装包等价于在本机执行任意代码(build backend + 包体的 ``__init__``)。
本端点不做鉴权也不限制来源,默认信任局域网 —— 与整套 Web UI 无鉴权、
监听 ``0.0.0.0`` 的现状一致。若要把这扇门关上,在这个文件里加一个
配置开关是最小改动点。
"""
from __future__ import annotations

import configparser
import importlib
import io
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, File, HTTPException, UploadFile

from app.core.manifest import discover

LOG = logging.getLogger("system.apps")

router = APIRouter()

#: 卡片插件必须声明的 entry point 组（见 manifest._ENTRY_POINT_GROUP）
CARD_EP_GROUP = "wdnwebui.cards"
#: entry_points.txt 里该组的段名
_EP_SECTION = CARD_EP_GROUP
#: 单个包上限，防止把 venv 撑爆
MAX_WHEEL_BYTES = 256 * 1024 * 1024
#: pip install 超时。带 build backend 的源码包可能慢，但 10 分钟还不完
#: 基本就是卡住了。
PIP_TIMEOUT_S = 600


# --------------------------------------------------------------------------- #
# 装之前的检查                                                             #
# --------------------------------------------------------------------------- #
def _wheel_info(path: Path) -> dict[str, Any]:
    """Read the metadata out of a wheel without installing it.

    A wheel is a zip; the two files that decide "is this an app package" are
    ``<dist-info>/entry_points.txt`` and the card's ``manifest.json``. Both
    are readable straight out of the archive, so the reject/accept decision
    happens **before** anything is written into the venv — a non-app wheel
    never gets to run code here.
    """
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise HTTPException(
            status_code=400,
            detail=f"不是合法的 wheel（zip 解析失败）：{e}",
        ) from e

    with zf:
        names = zf.namelist()

        # ---- 包名 / 版本 -------------------------------------------------
        dist_infos = [n for n in names if n.endswith(".dist-info/METADATA")]
        if not dist_infos:
            raise HTTPException(
                status_code=400,
                detail="wheel 内没有 *.dist-info/METADATA，不是标准的 Python 包",
            )
        meta_name = dist_infos[0]
        dist_info = meta_name[: -len("/METADATA")]
        pkg_name = dist_info[: -len(".dist-info")]
        version = ""
        try:
            raw = zf.read(meta_name).decode("utf-8", "replace")
            m = re.search(r"^Version:\s*(.+)$", raw, re.M)
            if m:
                version = m.group(1).strip()
            # 包名取 METADATA 的 Name:（规范名），而不是 dist-info 目录名
            # ——后者是 ``<name>-<version>.dist-info``，带着版本号，读起来
            # 像 "wdnapps_demo-0.1.0"。缺失时才回退到去版本后的目录名。
            m = re.search(r"^Name:\s*(.+)$", raw, re.M)
            if m:
                pkg_name = m.group(1).strip()
            else:
                pkg_name = re.sub(r"-\d[^-\s]*$", "", pkg_name)
        except Exception:  # noqa: BLE001
            pass

        # ---- entry point 组 ---------------------------------------------
        ep_name = f"{dist_info}/entry_points.txt"
        card_eps: list[str] = []
        has_ep_file = ep_name in names
        if has_ep_file:
            try:
                cp = configparser.ConfigParser()
                cp.read_string(zf.read(ep_name).decode("utf-8", "replace"))
                # configparser 的 key 小写化，这里只关心段名与条目个数
                for sec in cp.sections():
                    if sec.strip().lower() == _EP_SECTION:
                        card_eps = [k for k in cp.options(sec)]
            except Exception as e:  # noqa: BLE001
                LOG.warning("entry_points.txt 解析失败: %s", e)

        # ---- 卡片 manifest ------------------------------------------------
        manifests = sorted(
            n for n in names
            if n.endswith("/manifest.json") and not n.endswith(".dist-info/manifest.json")
        )

    return {
        "package": pkg_name,
        "version": version,
        "card_entry_points": card_eps,
        "manifests": manifests,
        "has_ep_file": has_ep_file,
    }


def _reject_if_not_an_app(info: dict[str, Any], filename: str) -> None:
    """Reject non-app packages **before** anything is installed."""
    eps = info["card_entry_points"]
    if not eps:
        hint = ""
        if info["has_ep_file"]:
            hint = "（该包有 entry_points.txt，但没有 wdnwebui.cards 组）"
        raise HTTPException(
            status_code=400,
            detail=(
                f"{filename} 不是应用包：未声明 [{CARD_EP_GROUP}] entry point"
                f"{hint}。应用包需要在 pyproject.toml 里写"
                f"[project.entry-points.\"{CARD_EP_GROUP}\"]，"
                "每个卡片一项，键名就是卡片 id。"
            ),
        )
    if not info["manifests"]:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{filename} 不是应用包：声明了 {len(eps)} 个 "
                f"[{CARD_EP_GROUP}] 卡片，但包内没有 manifest.json。"
                "manifest.json 必须和 entry point 指向的卡片模块放在同一目录。"
            ),
        )


# --------------------------------------------------------------------------- #
# 安装                                                                      #
# --------------------------------------------------------------------------- #
def _pip_install(whl: Path) -> tuple[bool, str]:
    """``pip install --no-deps`` into the venv this process runs in.

    ``--force-reinstall``: 同版本重装是有意义的开发动作 —— 改完代码重新
    build 一个同版本号的 wheel 再拖进来，不加这个 pip 会说「already
    satisfied」然后什么都不做，用户会以为没装上。

    ``--no-index``: 不让 pip 碰 PyPI。卡片包的依赖是私有包（wdnhid/wdnav），
    PyPI 上没有；而且我们已经明确不装依赖（--no-deps），索引本来就用不上，
    加上它还能让这次安装在断网板子上也能成功。
    """
    cmd = [
        sys.executable, "-m", "pip", "install",
        "--no-deps", "--no-index", "--force-reinstall",
        "--disable-pip-version-check",
        str(whl),
    ]
    LOG.info("installing app package: %s", cmd[-1])
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=PIP_TIMEOUT_S,
            cwd=str(Path(__file__).resolve().parents[3]),
        )
    except subprocess.TimeoutExpired:
        return False, f"pip 安装超时（>{PIP_TIMEOUT_S}s）"
    except FileNotFoundError as e:
        return False, f"找不到 pip: {e}"

    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        LOG.error("pip install 失败 rc=%s: %s", proc.returncode, out[-2000:])
        return False, out.strip()[-4000:]
    return True, out.strip()[-2000:]


def _rescan() -> list[dict[str, Any]]:
    """Re-run card discovery and return the card list.

    ``manifest.discover()`` recomputes from scratch (there is no cache in
    it), and the plugin modules are already in ``sys.modules`` so the import
    is a dict lookup. ``importlib.invalidate_caches`` matters for the case
    that actually bites us: a *newly* installed package's entry point is not
    in the already-imported ``importlib.metadata`` state.

    ``discover()`` is user-input-driven from here on (the package we just
    installed is arbitrary code), so an unexpected exception inside it must
    not escape as a 500 — by this point the package is *already installed*,
    and a bare "Internal Server Error" leaves the user with a changed venv
    and no idea what happened. Let it propagate, but name the culprit: the
    caller turns this into a "装上了但卡片列表刷不出来" message.
    """
    importlib.invalidate_caches()
    out: list[dict[str, Any]] = []
    for m in discover():
        if m.status != "ok":
            continue
        out.append({
            "id": m.id,
            "title": m.title,
            "status": m.status,
            "preview_video": m.preview_video,
            "description": m.raw.get("description", ""),
            "hidden": m.raw.get("hidden", False),
            "display_order": m.raw.get("display_order", 9999),
            "software_capabilities": list(m.software_capabilities),
            "hardware_capabilities": list(m.hardware_capabilities),
            "capability_groups": [list(g) for g in m.capability_groups],
            "frontend": {
                # 同 main.py：走 .get()。frontend.template 缺失时 _validate
                # 会判 FATAL 把卡剔掉，所以理论上到不了这里 —— 但这是刚装
                # 进来的用户包，理论上不值得赌，缺了就缺了，别让首页崩。
                "template": (m.raw.get("frontend") or {}).get("template", ""),
                "js": m.frontend_js,
                "css": m.frontend_css,
            },
        })
    return out


def _pip_uninstall(pkg: str) -> str:
    """Best-effort removal of a package we just failed to install.

    pip *usually* rolls itself back, but not always — a wheel that blows up
    while writing its RECORD (missing/short RECORD is the common case) can
    leave the package's files in site-packages with a broken dist-info. A
    half-installed card package is worse than no package: ``pip install``
    later refuses to touch it ("no RECORD file found") and every retry
    fails the same way, which reads like the app is permanently broken.
    """
    cmd = [sys.executable, "-m", "pip", "uninstall", "-y",
           "--disable-pip-version-check", pkg]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=180)
        return ((proc.stdout or "") + (proc.stderr or "")).strip()
    except Exception as e:  # noqa: BLE001
        return f"uninstall 调用失败: {e}"


@router.post("/apps/install")
async def install_app(file: UploadFile = File(...)) -> dict:
    """安装一个应用包（wheel）。

    流程：收包 → **先验是不是应用包** → ``pip install --no-deps`` →
    重扫卡片 → 返回新增卡片。装成功但没进应用列表的情况在这里必须堵死：
    entry point 声明了却没 manifest.json、或包压根没声明 entry point，
    都会在安装前被拒，不会留下"装了一半"的 venv。

    非应用包 **不安装**，直接 400 + 说明缺什么。
    """
    filename = (file.filename or "").strip()
    if not filename:
        raise HTTPException(status_code=400, detail="没有文件名")
    if not filename.endswith(".whl"):
        raise HTTPException(
            status_code=400,
            detail=f"只接受 .whl 包，收到的是 {filename}",
        )

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="文件为空")
    if len(raw) > MAX_WHEEL_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"包太大（{len(raw) / 1048576:.1f} MB），上限 "
                   f"{MAX_WHEEL_BYTES / 1048576:.0f} MB",
        )

    tmpdir = tempfile.mkdtemp(prefix="wdnapp-")
    whl = Path(tmpdir) / Path(filename).name
    try:
        whl.write_bytes(raw)

        # ---- 装之前验：非应用包到此为止，不碰 venv --------------------
        info = _wheel_info(whl)
        _reject_if_not_an_app(info, filename)
        LOG.info("接受 %s（%s %s）：卡片 entry point %s，manifest %d 个",
                 filename, info["package"], info["version"],
                 info["card_entry_points"], len(info["manifests"]))

        # ---- 装 -------------------------------------------------------
        before = {c["id"] for c in _rescan()}
        ok, pip_out = _pip_install(whl)
        if not ok:
            # 回滚：pip 自己没清干净的话，这里替它清。留着半装的包会让
            # 用户重试时一直撞同一个错，看起来像"这应用永远装不上"。
            undo = _pip_uninstall(info["package"])
            LOG.warning("安装失败，已回滚 %s: %s", info["package"], undo[-500:])
            raise HTTPException(
                status_code=500,
                detail=f"pip install 失败：\n{pip_out}"
                       + (f"\n\n（已回滚该包：{undo.strip()[-300:]}）" if undo else ""),
            )

        # ---- 装之后重扫 -----------------------------------------------
        try:
            after = _rescan()
        except Exception as e:  # noqa: BLE001
            # 到这一步包**已经装进 venv 了**，不能报 500 了事 —— 那样用户
            # 面对的是"venv 变了 + 一个 500"，两头不靠。这里用 200 + ok:false
            # 明说"装上了、但列表刷不出来"，并把原因（多半是新包 manifest
            # 字段缺失，或它的代码 import 就炸）原样带出去。
            #
            # 不用 HTTPException(status_code=200)：前端只看 r.ok，会把它
            # 当成功，然后因为拿不到 cards 而静默不刷新 —— 比报错更难查。
            LOG.exception("安装成功但重扫卡片失败")
            return {
                "ok": False,
                "installed": True,
                "package": info["package"],
                "version": info["version"],
                "declared_cards": info["card_entry_points"],
                "new_cards": [],
                "missing_cards": info["card_entry_points"],
                "cards": None,
                "message": (
                    f"已安装 {info['package']} {info['version']}，"
                    f"但刷新应用列表失败：{type(e).__name__}: {e}。"
                    "包已就位；请检查该包 manifest 字段是否完整，或重启服务。"
                ),
            }
        new_ids = [c["id"] for c in after if c["id"] not in before]
        declared = info["card_entry_points"]
        missing = [cid for cid in declared if cid not in {c["id"] for c in after}]
        if missing:
            LOG.warning(
                "%s 声明了卡片 %s 但重扫后没出现在列表里（manifest 校验可能没过）",
                info["package"], missing,
            )

        LOG.info("安装完成 %s：新增卡片 %s", info["package"], new_ids or "无")
        return {
            "ok": True,
            "package": info["package"],
            "version": info["version"],
            "declared_cards": declared,
            "new_cards": new_ids,
            "missing_cards": missing,
            "cards": after,
            "pip_output": pip_out,
        }
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)