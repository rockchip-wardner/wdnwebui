"""Coprocessor management endpoint.

WebSocket ``/api/coprocessor/ws`` runs the same reset sequence as the
on-device sample ``/userdata/samples/reset_182x.sh`` directly, streaming
each step's stdout / stderr to the client so the user can watch the
NPU come back up:

    rknnsmi_hw_reset_device(0)          # ×4 (some chips don't take
                                       # the reset on the first try)
    sudo rknn3_startup restart
    <in-process> restart all spawned compute_worker processes

The NPU reset step is **not** a shell-out to ``rknn-smi reset -t hw``:
wdnwebui is required to avoid invoking the rknn-smi CLI per the
"全面禁止使用 rknn-smi 命令查询 NPU 状态" constraint. Instead we call
``wdnai.RknnSmi.hw_reset_device(...)`` which is the ctypes shim over
``/usr/lib/librknnsmi.so``. This requires the webui process to have
permission to ``open(O_RDWR | O_CREAT) /tmp/rknnsmi.lock`` and to talk
to the NPU driver — same privileges the legacy ``sudo rknn-smi``
invocation assumed.

Each step is bounded by ``_STEP_TIMEOUT_S``; on expiry the step is
marked failed and the WS closes.

The compute-worker restart step (``restart_all_local``) runs
in-process and uses the Q3 4-step per-instance flow (unload → shutdown
→ wait/kill → ensure_worker_for_model). It must happen **after**
``rknn3_startup restart`` so the new rknn3 driver userspace is live
when workers re-open it; doing it before would race the reset.

``rknnsmi_hw_reset_device`` returns ``RKNNSMI_SUCCESS`` (=0) on
success, and a negative ``RKNNSMI_ERR_*`` code otherwise. Code
``-4 (RKNNSMI_ERR_STATE)`` and a few other transient codes are
treated as "the chip is already in the expected post-reset state" —
the same benign case the legacy ``rknn-smi reset -t hw -d 0`` rc=255
covered. Each entry in ``_STEPS`` carries an ``acceptable_codes`` set
so such non-success but expected codes don't abort the rest of the
sequence. The WS ``step_end`` frame includes an ``accepted`` flag
the UI uses to colour the line (danger vs. warn).

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


# rknnsmi 返回的 success 码 = 0；STATE = -4 (chip 已经在预期状态
# —— 跟旧路径 rknn-smi 退出码 255 等价)；其余负码是真正的错误。
_RKNNSMI_RESET_OK = {0, -4}
_RESET_DEVICE_ID = 0
# 重试次数：旧 reset_182x.sh 把 rknn-smi reset 跑 4 次。新路径直接重试
# 同一次调用 4 次比维护 4 个 _STEPS 条目更直观，也避免 WS 客户端看到
# 4 段几乎一样的 step_start。原始脚本的"重试理由"——某些板子第一次不
# 生效——我们保留这个语义（timeout 内的循环重试）。
_HW_RESET_ATTEMPTS = 4


# ---- in-process step: 硬件复位 NPU（替代旧 sudo rknn-smi reset -t hw）----
def _hw_reset_npu_step() -> tuple[int, str, str]:
    """Drive ``rknnsmi_hw_reset_device`` N times (legacy = 4).

    Returns ``(exit, stdout, stderr)`` so it slots into the same
    step_start/step_end frames as the shell commands.

    exit = 0   → 至少一次 reset 成功（其余可能命中 RKNNSMI_ERR_STATE，
                  跟旧 rknn-smi rc=255 等价)
    exit = 1   → 所有重试都返回非可接受错误码

    The :class:`wdnai.RknnSmi` shim wraps ``/usr/lib/librknnsmi.so``;
    no subprocess is involved.
    """
    from wdnai import RknnSmi, RknnSmiError
    try:
        smi = RknnSmi()
    except Exception as e:  # noqa: BLE001
        return 1, "", f"RknnSmi 初始化失败: {type(e).__name__}: {e}"

    last_code: int | None = None
    log_lines: list[str] = []
    for attempt in range(1, _HW_RESET_ATTEMPTS + 1):
        try:
            smi.hw_reset_device(_RESET_DEVICE_ID)
            log_lines.append(
                f"rknnsmi_hw_reset_device({_RESET_DEVICE_ID}) "
                f"attempt {attempt}/{_HW_RESET_ATTEMPTS}: OK"
            )
            return 0, "\n".join(log_lines), ""
        except RknnSmiError as e:
            last_code = e.code
            log_lines.append(
                f"rknnsmi_hw_reset_device({_RESET_DEVICE_ID}) "
                f"attempt {attempt}/{_HW_RESET_ATTEMPTS}: "
                f"{e.enum.name} ({e.code})"
            )
            if last_code in _RKNNSMI_RESET_OK:
                # 已经在预期状态 — 跟旧 rknn-smi rc=255 同义。
                return 0, "\n".join(log_lines), ""
            # 其他错：等 0.5s 再试一次，模拟旧 reset_182x.sh 的节奏。
            time.sleep(0.5)
        except Exception as e:  # noqa: BLE001
            log_lines.append(
                f"rknnsmi_hw_reset_device({_RESET_DEVICE_ID}) "
                f"attempt {attempt}/{_HW_RESET_ATTEMPTS}: "
                f"{type(e).__name__}: {e}"
            )
            last_code = -1
            time.sleep(0.5)

    return (
        1,
        "\n".join(log_lines),
        f"硬件复位 {_HW_RESET_ATTEMPTS} 次均失败 (last code={last_code})",
    )


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
    # NPU 硬件复位：旧路径是 4 次 `sudo rknn-smi reset -t hw -d 0`。
    # 改用 wdnai.RknnSmi 后内部循环 4 次，单条 step 一次性跑完，
    # 避免前端看到 4 行几乎一样的 step_start。retry 节奏（每次间隔
    # 0.5s）跟旧 reset_182x.sh 对齐 —— 上游注释："某些板子第一次
    # 复位不生效,需要多试几次"。
    (_hw_reset_npu_step, {0}),
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