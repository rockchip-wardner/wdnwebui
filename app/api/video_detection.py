"""Video detection endpoints: REST control + WebSocket frame stream."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import uuid
from pathlib import Path

from fastapi import (
    APIRouter,
    File,
    HTTPException,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)

from app.services.detector import get_detector
from app.services.video_session import get_session


router = APIRouter(tags=["video-detection"])

UPLOAD_DIR = Path(__file__).resolve().parent.parent / "services" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Default root for the server-side file browser. Override at startup with
# VD_FS_ROOT if you want to constrain what the web UI can see (e.g. /mnt).
_DEFAULT_FS_ROOT = os.environ.get("VD_FS_ROOT", "/")
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
_MAX_DEPTH = 8
_MAX_ENTRIES = 2000


@router.get("/video-detection")
async def get_video_detection() -> dict:
    return get_session().snapshot()


@router.get("/video-detection/fs")
async def browse_fs(path: str = "") -> dict:
    """List one directory on the server's filesystem.

    The path is resolved and must stay under ``VD_FS_ROOT`` so the web
    UI cannot wander outside what's intended. Pseudo-filesystems and
    well-known system trees are filtered.
    """
    raw = path.strip() or _DEFAULT_FS_ROOT
    try:
        target = Path(raw).resolve(strict=False)
    except (OSError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=f"bad path: {e}")
    if _is_skipped(str(target)):
        raise HTTPException(
            status_code=403,
            detail=f"路径 {target} 在黑名单中",
        )
    root = Path(_DEFAULT_FS_ROOT).resolve(strict=False)
    if not _is_within(target, root):
        raise HTTPException(
            status_code=400,
            detail=f"path {target} 超出允许的根目录 {root}",
        )
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


def _is_within(p: Path, root: Path) -> bool:
    p_s = str(p)
    r_s = str(root)
    if p_s == r_s:
        return True
    return p_s.startswith(r_s.rstrip("/") + "/")


def _is_skipped(path_str: str) -> bool:
    p = path_str.rstrip("/") or "/"
    for prefix in _SKIP_DIR_PREFIXES:
        base = prefix.rstrip("/")
        if p == base or p.startswith(base + "/"):
            return True
    return False


def _depth_from(target: Path, root: Path) -> int:
    try:
        rel = target.relative_to(root)
    except ValueError:
        return 0
    return len(rel.parts)


@router.post("/video-detection/upload")
async def upload_video(file: UploadFile = File(...)) -> dict:
    if not file.filename:
        raise HTTPException(status_code=400, detail="empty filename")
    ext = Path(file.filename).suffix or ".bin"
    upload_id = uuid.uuid4().hex
    dest = UPLOAD_DIR / f"{upload_id}{ext}"
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
        "upload_id": upload_id,
        "path": str(dest),
        "name": file.filename,
        "size": dest.stat().st_size,
    }


@router.post("/video-detection/open")
async def open_source(payload: dict) -> dict:
    source = (payload or {}).get("source")
    if not source or not isinstance(source, str):
        raise HTTPException(status_code=400, detail="source required")
    try:
        return get_session().open(source)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/video-detection/control")
async def control(payload: dict) -> dict:
    action = (payload or {}).get("action")
    if action == "play":
        return get_session().play()
    if action == "pause":
        return get_session().pause()
    if action == "stop":
        return get_session().close()
    raise HTTPException(status_code=400, detail=f"unknown action: {action!r}")


@router.post("/video-detection/close")
async def close_source() -> dict:
    return get_session().close()


# --------------------------------------------------------------------- #
# rkai VLM detection                                                     #
# --------------------------------------------------------------------- #
@router.get("/video-detection/prompt")
async def get_prompt() -> dict:
    return get_detector().snapshot()


@router.post("/video-detection/prompt")
async def set_prompt(payload: dict) -> dict:
    """Start (or update) VLM detection.

    Body: ``{prompt: str, every_n: int}`` — both required, default
    cadence ~1s @ 30fps if not given.
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
    try:
        return get_detector().start(prompt, every_n=every_n)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/video-detection/prompt/stop")
async def stop_prompt() -> dict:
    return get_detector().stop()


@router.get("/video-detection/events")
async def list_events() -> dict:
    det = get_detector()
    return {
        "snapshot": det.snapshot(),
        "events": det.events(),
        "latest": det.latest(),
    }


@router.post("/video-detection/events/clear")
async def clear_events() -> dict:
    get_detector().clear()
    return {"ok": True}


# --------------------------------------------------------------------- #
@router.websocket("/video-detection/ws")
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

            kind, data = await loop.run_in_executor(
                None, lambda: session.fetch_frame(timeout=0.05)
            )
            if kind == "frame":
                eof_sent = False
                await ws.send_bytes(data)
                continue
            if kind == "eof":
                if not eof_sent:
                    snap = session.snapshot()
                    snap["type"] = "eof"
                    await ws.send_json(snap)
                    eof_sent = True
                await asyncio.sleep(0.2)
                continue
            now = loop.time()
            if now - last_status >= 1.0:
                snap = session.snapshot()
                snap["type"] = "status"
                # also include detector snapshot so the UI can show
                # prompt state without extra round-trips
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