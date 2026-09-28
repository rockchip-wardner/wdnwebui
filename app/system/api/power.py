"""Power control endpoints.

Runs host binaries directly:
- shutdown  -> ``shutdown``
- reboot    -> ``reboot``

The service needs root (or equivalent capabilities) for these to
actually take effect; the OS will return EPERM otherwise and the
error message is bubbled back to the client as a 500.
"""
from __future__ import annotations

import asyncio
import os
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(tags=["power"])


class PowerCommand(BaseModel):
    action: Literal["shutdown", "reboot"]


# Each action is an ordered list of (cmd, post_delay_seconds) tuples.
# Multi-step actions chain with `asyncio.sleep` between steps so the
# event loop stays responsive.
_ACTIONS: dict[str, list[tuple[list[str], float]]] = {
    "shutdown": [(["shutdown"], 0.0)],
    "reboot":   [(["reboot"],   0.0)],
}

_VERBS = {
    "shutdown": "关机",
    "reboot":   "重启",
}


def _read_uptime_seconds() -> float:
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except Exception:
        return 0.0


def _format_uptime(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h} 时 {m:02d} 分 {sec:02d} 秒"
    if m:
        return f"{m} 分 {sec:02d} 秒"
    return f"{sec} 秒"


def _is_root() -> bool:
    try:
        return os.geteuid() == 0
    except Exception:
        return False


async def _run(cmd: list[str]) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as e:
        return 127, f"未找到可执行文件: {cmd[0]} ({e})"
    stdout, stderr = await proc.communicate()
    out = (stderr or stdout or b"").decode(errors="replace").strip()
    return proc.returncode or 0, out


async def _run_action(sequence: list[tuple[list[str], float]]) -> tuple[int, str]:
    total = len(sequence)
    for i, (cmd, delay) in enumerate(sequence, 1):
        rc, out = await _run(cmd)
        if rc != 0:
            label = " ".join(cmd)
            step = f"步骤 {i}/{total}" if total > 1 else "执行失败"
            return rc, f"{step} ({label}) 失败: {out}"
        if delay > 0:
            await asyncio.sleep(delay)
    return 0, ""


@router.get("/power")
async def get_power() -> dict:
    return {
        "uptime_seconds": _read_uptime_seconds(),
        "uptime_human": _format_uptime(_read_uptime_seconds()),
        "is_root": _is_root(),
    }


@router.post("/power")
async def post_power(cmd: PowerCommand) -> dict:
    if cmd.action not in _ACTIONS:
        raise HTTPException(status_code=400, detail=f"不支持的操作: {cmd.action}")

    rc, out = await _run_action(_ACTIONS[cmd.action])
    if rc != 0:
        raise HTTPException(
            status_code=500,
            detail=f"{_VERBS[cmd.action]} 失败: {out}",
        )
    return {"ok": True, "action": cmd.action, "message": f"{_VERBS[cmd.action]} 已执行"}