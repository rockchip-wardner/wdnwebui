"""Coprocessor management endpoint.

WebSocket ``/api/coprocessor/ws`` runs the same reset sequence as the
on-device sample ``/userdata/samples/reset_182x.sh`` directly, streaming
each step's stdout / stderr to the client so the user can watch the
NPU come back up:

    sudo rknn-smi reset -t hw -d 0    # ×4 (the original script repeats
                                     # the reset for chips that don't
                                     # always take it on the first try)
    sudo rknn3_startup restart

The commands are inlined (and wrapped in ``sudo`` to match the original
sample) rather than shelling out to a script file because the sample
isn't guaranteed to exist on every target. Each subprocess call is
bounded by ``_STEP_TIMEOUT_S``; on expiry the process tree is killed
and a timeout message is reported so the WS closes even when a step is
wedged.

WS frame types:
- ``step_start`` — ``{index, total, command, message}``
- ``step_end``   — ``{index, total, command, exit, stdout, stderr,
                     duration_ms}``
- ``done``       — ``{ok, failed_step}``
- ``error``      — ``{detail}`` (only on unexpected exceptions)
"""
from __future__ import annotations

import asyncio
import os
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect


router = APIRouter(tags=["coprocessor"])


_STEPS: list[list[str]] = [
    ["sudo", "rknn-smi", "reset", "-t", "hw", "-d", "0"],
    ["sudo", "rknn-smi", "reset", "-t", "hw", "-d", "0"],
    ["sudo", "rknn-smi", "reset", "-t", "hw", "-d", "0"],
    ["sudo", "rknn-smi", "reset", "-t", "hw", "-d", "0"],
    ["sudo", "rknn3_startup", "restart"],
]

_STEP_TIMEOUT_S = 60.0


async def _run_step(cmd: list[str], timeout: float) -> tuple[int, str, str]:
    """Spawn ``cmd`` and capture stdout / stderr. ``start_new_session``
    puts the child in its own process group so we can kill the whole
    tree on timeout (rknn3_startup forks a transfer proxy). stdin is
    closed so a misplaced ``sudo`` prompt never wedges the call."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except FileNotFoundError as e:
        return 127, "", f"未找到可执行文件: {cmd[0]} ({e})"
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError:
        try:
            os.killpg(proc.pid, 9)
        except (ProcessLookupError, PermissionError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        return 124, "", f"执行超时 ({timeout:.0f}s): {' '.join(cmd)}"
    rc = proc.returncode or 0
    return (
        rc,
        (stdout_b or b"").decode(errors="replace").strip(),
        (stderr_b or b"").decode(errors="replace").strip(),
    )


@router.websocket("/coprocessor/ws")
async def coprocessor_ws(ws: WebSocket) -> None:
    """Run the reset sequence and stream each step's output to the
    client. Closes once every step finishes (or one fails)."""
    await ws.accept()
    total = len(_STEPS)
    failed_step: int | None = None
    try:
        ok = True
        for i, cmd in enumerate(_STEPS, 1):
            label = " ".join(cmd)
            await ws.send_json({
                "type": "step_start",
                "index": i,
                "total": total,
                "command": cmd,
                "message": f"步骤 {i}/{total}: {label}",
            })
            t0 = time.monotonic()
            rc, out, err = await _run_step(cmd, _STEP_TIMEOUT_S)
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            await ws.send_json({
                "type": "step_end",
                "index": i,
                "total": total,
                "command": cmd,
                "exit": rc,
                "stdout": out,
                "stderr": err,
                "duration_ms": elapsed_ms,
            })
            if rc != 0:
                ok = False
                failed_step = i
                break
        await ws.send_json({"type": "done", "ok": ok, "failed_step": failed_step})
    except WebSocketDisconnect:
        return
    except Exception as e:
        try:
            await ws.send_json({"type": "error", "detail": str(e)})
        except Exception:
            pass
    finally:
        try:
            await ws.close()
        except Exception:
            pass