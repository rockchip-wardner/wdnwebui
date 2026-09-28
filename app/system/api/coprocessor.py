"""Coprocessor management endpoint.

WebSocket ``/api/coprocessor/ws`` runs the same reset sequence as the
on-device sample ``/userdata/samples/reset_182x.sh`` directly, streaming
each step's stdout / stderr to the client so the user can watch the
NPU come back up:

    sudo rknn-smi reset -t hw -d 0    # ×4 (the original script repeats
                                     # the reset for chips that don't
                                     # always take it on the first try)
    sudo rknn3_startup restart
    <in-process> restart all spawned compute_worker processes

The commands are inlined (and wrapped in ``sudo`` to match the original
sample) rather than shelling out to a script file because the sample
isn't guaranteed to exist on every target. Each subprocess call is
bounded by ``_STEP_TIMEOUT_S``; on expiry the process tree is killed
and a timeout message is reported so the WS closes even when a step is
wedged.

The compute-worker restart step (``restart_all_local``) runs
in-process and uses the Q3 4-step per-instance flow (unload → shutdown
→ wait/kill → ensure_worker_for_model). It must happen **after**
``rknn3_startup restart`` so the new rknn3 driver userspace is live
when workers re-open it; doing it before would race the reset.

``rknn-smi reset -t hw -d 0`` frequently exits with code 255 when the
target NPU is already in the expected post-reset state — that's a
benign "nothing to reset" reply, not a failure. Each entry in
``_STEPS`` carries an ``acceptable_exits`` set so such non-zero but
expected exit codes don't abort the rest of the sequence. The WS
``step_end`` frame includes an ``accepted`` flag the UI uses to colour
the line (danger vs. warn).

WS frame types:
- ``step_start`` — ``{index, total, command, message}``
- ``step_end``   — ``{index, total, command, exit, stdout, stderr,
                     duration_ms, accepted}``
- ``done``       — ``{ok, failed_step}``
- ``error``      — ``{detail}`` (only on unexpected exceptions)
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Awaitable, Callable, Sequence, Union

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.compute.registry import ComputeRegistry, get_registry


router = APIRouter(tags=["coprocessor"])


_RKNNSMI_RESET_OK = {0, 255}


# ---- in-process step: 重启全部 spawned compute_worker ----
async def _restart_compute_workers_step() -> tuple[int, str, str]:
    """Drive :meth:`ComputeRegistry.restart_all_local` from inside the
    WS loop. Returns ``(exit, stdout, stderr)`` so it slots into the
    same step_start/step_end frames as the shell commands.

    exit = 0  → 至少一个 worker 重启成功 **或** 没有需要重启的 worker
    exit = 1  → 有 worker 重启失败（stdout 列出来龙去脉）
    """
    try:
        reg: ComputeRegistry = get_registry()
    except Exception as e:  # noqa: BLE001
        return 1, "", f"无法访问 ComputeRegistry: {e}"
    try:
        rc, lines = await asyncio.to_thread(reg.restart_all_local)
    except Exception as e:  # noqa: BLE001
        return 1, "", f"restart_all_local 抛异常: {type(e).__name__}: {e}"
    out = "\n".join(lines) if lines else ""
    return int(rc or 0), out, ""


# ---- 步骤表 ----
# 每条可以是 shell (cmd: list[str]) 或 in-process (cmd: callable)。可调
# 用对象必须是 ``() -> tuple[int, str, str]`` 或 async 版本。
_ShellStep = list[str]
_CallableStep = Callable[[], Union[tuple[int, str, str], Awaitable[tuple[int, str, str]]]]
_Step = Union[_ShellStep, _CallableStep]


# ---- 步骤表 ----
# 每条可以是 shell (cmd: list[str]) 或 in-process (cmd: callable)。
# callable 必须是 ``() -> tuple[int, str, str]`` 或 async 版本。
_ShellStep = list[str]
_CallableStep = Callable[[], Union[tuple[int, str, str], Awaitable[tuple[int, str, str]]]]
_Step = Union[_ShellStep, _CallableStep]


_STEPS: list[tuple[_Step, set[int]]] = [
    (["sudo", "rknn-smi", "reset", "-t", "hw", "-d", "0"], _RKNNSMI_RESET_OK),
    (["sudo", "rknn-smi", "reset", "-t", "hw", "-d", "0"], _RKNNSMI_RESET_OK),
    (["sudo", "rknn-smi", "reset", "-t", "hw", "-d", "0"], _RKNNSMI_RESET_OK),
    (["sudo", "rknn-smi", "reset", "-t", "hw", "-d", "0"], _RKNNSMI_RESET_OK),
    (["sudo", "rknn3_startup", "restart"], {0}),
    # 必须放在 rknn3_startup 之后：worker 重启时会重新打开 rknn3 驱动。
    # 没有 spawned worker 时这条 step 走 rc=0 + stdout="无 spawned
    # worker，跳过"，UI 标 ok 不影响整体 done。rc=1（任一 worker
    # 失败）视为整步失败，让用户在 WS done 帧看到。
    (_restart_compute_workers_step, {0}),
]

_STEP_TIMEOUT_S = 60.0


async def _run_step(cmd: _Step, timeout: float) -> tuple[int, str, str, Sequence[str]]:
    """Dispatch a step:
    * shell ``cmd`` (list[str])  → spawn + capture (with timeout)
    * callable ``cmd``           → await 直接拿 (rc, out, err)

    Returns ``(rc, stdout, stderr, command_repr)``. The command_repr is
    passed back to the WS frame so the UI can echo what ran."""
    if isinstance(cmd, list):
        command_repr: Sequence[str] = list(cmd)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except FileNotFoundError as e:
            return 127, "", f"未找到可执行文件: {cmd[0]} ({e})", command_repr
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
            return 124, "", f"执行超时 ({timeout:.0f}s): {' '.join(cmd)}", command_repr
        rc = proc.returncode or 0
        return (
            rc,
            (stdout_b or b"").decode(errors="replace").strip(),
            (stderr_b or b"").decode(errors="replace").strip(),
            command_repr,
        )

    # callable path
    command_repr = ["<in-process>", getattr(cmd, "__name__", repr(cmd))]
    try:
        ret = cmd()
        if asyncio.iscoroutine(ret):
            ret = await ret
        rc, out, err = ret
    except Exception as e:  # noqa: BLE001
        return 1, "", f"in-process 步骤抛异常: {type(e).__name__}: {e}", command_repr
    return int(rc or 0), str(out or "").strip(), str(err or "").strip(), command_repr


@router.websocket("/coprocessor/ws")
async def coprocessor_ws(ws: WebSocket) -> None:
    """Run the reset sequence and stream each step's output to the
    client. Closes once every step finishes (or one fails)."""
    await ws.accept()
    total = len(_STEPS)
    failed_step: int | None = None
    try:
        ok = True
        for i, (cmd, acceptable) in enumerate(_STEPS, 1):
            if isinstance(cmd, list):
                label = " ".join(cmd)
            else:
                label = "重启算力 worker（spawned 本地进程）"
            await ws.send_json({
                "type": "step_start",
                "index": i,
                "total": total,
                "command": (cmd if isinstance(cmd, list) else [label]),
                "message": f"步骤 {i}/{total}: {label}",
            })
            t0 = time.monotonic()
            rc, out, err, command_repr = await _run_step(cmd, _STEP_TIMEOUT_S)
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            accepted = rc in acceptable
            await ws.send_json({
                "type": "step_end",
                "index": i,
                "total": total,
                "command": list(command_repr),
                "exit": rc,
                "stdout": out,
                "stderr": err,
                "duration_ms": elapsed_ms,
                "accepted": accepted,
            })
            if not accepted:
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