"""Coprocessor management endpoint.

WebSocket ``/api/coprocessor/ws`` runs the same reset sequence as the
on-device sample ``/userdata/samples/reset_182x.sh`` directly, streaming
each step's stdout / stderr to the client so the user can watch the
NPU come back up:

    pcie_upgrade_tool -s <bdf> uf /lib/firmware/rknn3_rk1820.img -reset
        # per card — explicit "烧写" step, previously hidden inside
        # `rknn3_startup restart`
    rknnsmi_hw_reset_device(0)          # ×4 (some chips don't take
                                       # the reset on the first try)
    rknn3_startup start
    <in-process> restart all spawned compute_worker processes

The webui process is **not** root, and we deliberately do NOT shell out
to ``sudo`` anywhere in this module — the legacy reset_182x.sh assumed
root (it inlined `sudo rknn-smi reset -t hw -d 0` and `sudo rknn3_startup
restart`); the v0.6 refactor moves everything possible into
non-privileged paths:

  * hw_reset goes through ``wdnai.RknnSmi.hw_reset_device(...)``
    (ctypes over ``/usr/lib/librnsmi.so``); requires the process to
    hold the ``/tmp/rknnsmi.lock`` flock (writable by group) and to
    have r/w on ``/dev/pcie-rkep-*`` (mode 660 root:video, and the
    webui user is in the video group).
  * burn goes through ``pcie_upgrade_tool`` directly (not via
    ``rknn3_startup``); passes a tmp dir to dodge the loader.bin
    write to ``/usr/bin``.
  * rknn3_startup start still requires ``/sys/bus/pci/rescan`` write
    (root-only on most targets). When wdnwebui runs as a non-root user
    the script's rescan will EACCES; the step's stderr surfaces the
    exact reason and the WS frame is marked ``accepted=False`` so
    the operator sees the gap. Production deploys that need a clean
    rescan run wdnwebui as root (matches the AGENTS.md
    "Service must run as root" expectation for the rknn3 driver).

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
# 单卡 pcie_upgrade_tool uf 实测 1-2 GB PCIe DMA 写 3-4s 完成，给 30s
# 富余（5.10 BSP 之前老镜像偶尔会卡在 bootloader 提取，30s 内会自然
# 失败退出；超过 30s 视为卡死，kill 子进程 + step 失败）。
_BURN_TIMEOUT_S = 30.0


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


# ---- in-process step: 烧写 RK1828 firmware 到每张卡 ----
_RK1828_FIRMWARE_PATH = "/lib/firmware/rknn3_rk1820.img"
_PCIE_UPGRADE_BIN = "pcie_upgrade_tool"


def _parse_pcie_addresses(text: str) -> list[str]:
    """Extract PCIe BDF addresses from `pcie_upgrade_tool ld` output.

    The tool prints lines like ``Addr=0000:01:00.0 [1d87:182a]``;
    we pull the BDF and dedup. Empty / unparseable output → [].
    """
    import re as _re
    out: list[str] = []
    for line in text.splitlines():
        m = _re.search(r"Addr=([0-9a-fA-F:.]+)", line)
        if m:
            bdf = m.group(1)
            if bdf not in out:
                out.append(bdf)
    return out


def _burn_firmware_step() -> tuple[int, str, str]:
    """Run ``pcie_upgrade_tool uf <fw> -reset`` on every RK1828 card.

    This is the "烧写命令" the user wants visible in the reset flow.
    Old paths hid it inside ``rknn3_startup restart`` (which calls
    `pcie_upgrade_tool` per-device as part of its ``start`` action) —
    but when wdnwebui runs as a non-root user that script silently
    no-ops on `/sys/bus/pci/rescan` and the burn never happens.
    Surfacing it as its own WS step means:

    * the UI shows the firmware-upload progress + per-card exit codes
    * non-root failures (e.g. "Cannot new file /usr/bin/loader.bin"
      when pcie_upgrade_tool can't write its working dir, or EACCES
      on /sys/bus/pci/rescan) are visible in step_end.stderr instead
      of being swallowed
    * operators can run just this step if they only need to reflash
      without doing a full hw-reset + worker-restart

    Returns (rc, stdout, stderr):
      rc=0  step ran to completion (tool present, at least one card
             attempted). Per-card failure details are surfaced in
             stdout but DO NOT fail the step — burning is a
             best-effort reflash; the next two steps (hw_reset via
             librknnsmi + rknn3_startup userspace restart) are the
             authoritative recovery path and must always run, even
             if every card failed to flash here. The only way this
             step returns rc=1 is if pcie_upgrade_tool / the firmware
             image is missing entirely, in which case the next steps
             would also be useless.
      rc=1  pcie_upgrade_tool not on PATH, firmware image missing, or
             `ld` returned no devices (the latter usually means the
             kernel driver isn't bound — i.e. environment broken, no
             point continuing)
    """
    import shutil as _shutil
    import subprocess as _sp
    import tempfile

    if _shutil.which(_PCIE_UPGRADE_BIN) is None:
        return 1, "", f"未找到 {_PCIE_UPGRADE_BIN}（不在 PATH 中）"
    if not os.path.isfile(_RK1828_FIRMWARE_PATH):
        return 1, "", f"未找到 firmware 镜像: {_RK1828_FIRMWARE_PATH}"

    # 1) 列出所有 RK1828 BDF。
    try:
        ld = _sp.run(
            [_PCIE_UPGRADE_BIN, "ld"],
            capture_output=True, text=True, timeout=10,
        )
    except _sp.TimeoutExpired:
        return 1, "", f"{_PCIE_UPGRADE_BIN} ld 超时 (10s)"
    except Exception as e:  # noqa: BLE001
        return 1, "", f"{_PCIE_UPGRADE_BIN} ld 失败: {type(e).__name__}: {e}"

    bdfs = _parse_pcie_addresses(ld.stdout)
    if not bdfs:
        return (
            1,
            ld.stdout,
            (ld.stderr or "")
            + "\n未在 pcie_upgrade_tool ld 输出中找到任何 Addr= 行",
        )

    # 2) pcie_upgrade_tool 在提取 bootloader 阶段会尝试把 loader.bin
    # 写到它自己的 CWD；/usr/bin 是 root 写的，所以非 root 跑会爆
    # "Cannot new file /usr/bin/loader.bin"。给个可写的 tmp dir 让它
    # 落地，行为不变（loader 走 DMA 不影响）。
    with tempfile.TemporaryDirectory(prefix="wdnwebui-burn-") as tmp:
        out_lines: list[str] = [
            f"检测到 {len(bdfs)} 张 NPU: {', '.join(bdfs)}",
            f"使用 tmp dir: {tmp}",
            f"firmware: {_RK1828_FIRMWARE_PATH}",
        ]
        ok_count = 0
        fail_count = 0
        for bdf in bdfs:
            cmd = [
                _PCIE_UPGRADE_BIN, "-s", bdf, "uf",
                _RK1828_FIRMWARE_PATH, "-reset", tmp,
            ]
            out_lines.append(f"\n>>> {' '.join(cmd)}")
            try:
                p = _sp.run(
                    cmd, capture_output=True, text=True,
                    timeout=_BURN_TIMEOUT_S, cwd=tmp,
                )
            except _sp.TimeoutExpired:
                fail_count += 1
                out_lines.append(
                    f"!!! 超时 ({_BURN_TIMEOUT_S}s): {bdf}"
                )
                continue
            except Exception as e:  # noqa: BLE001
                fail_count += 1
                out_lines.append(
                    f"!!! 异常: {type(e).__name__}: {e}"
                )
                continue
            tail = (p.stdout or "").strip().splitlines()[-5:]
            out_lines.append(
                f"rc={p.returncode}  (last 5 lines: {tail})"
            )
            if p.returncode == 0:
                ok_count += 1
            else:
                fail_count += 1
                err_tail = (p.stderr or "").strip()
                if err_tail:
                    out_lines.append(f"stderr: {err_tail[-200:]}")

        out_lines.append(
            f"\n汇总: {ok_count}/{len(bdfs)} 烧写成功, "
            f"{fail_count} 失败"
        )
        if fail_count > 0:
            out_lines.append(
                "\n注意: 烧写失败不阻塞后续步骤 —— hw_reset / "
                "rknn3_startup 是真正能把卡救回来的复位动作。烧写"
                "失败可后续手动 `pcie_upgrade_tool -s <bdf> uf ... -reset` 重试。"
            )
        # 步骤永远 rc=0，让 hw_reset / rknn3_startup / worker_restart
        # 一定有机会跑。这些才是从烧写失败状态恢复的真正手段。烧写
        # 失败 → 顶多记在 stdout，UI 标 accepted=True。
        return 0, "\n".join(out_lines), ""


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
    # 1) 烧写 RK1828 firmware 到每张卡。旧路径隐藏在
    # `rknn3_startup restart` 的 start 函数里，非 root 跑 webui 时
    # 那个脚本会静默 no-op，用户看不到。把烧写独立成 step 后：
    #   - UI 直接展示每张卡的 pcie_upgrade_tool 进度和 rc
    #   - 失败（loader.bin 写不出 / rescan EACCES）有 stderr 可看
    #   - 操作员可以单独跑这步做 reflash
    # pcie_upgrade_tool 提取 bootloader 时会往 CWD 写 loader.bin，
    # /usr/bin 是 root 写的，webui 进程无权写 —— 强制给一个 tmpdir。
    # 烧写是 best-effort：单卡/全卡失败都返回 rc=0（见 _burn_fire
    # docstring）。后续 hw_reset + rknn3_startup 才是真正把卡救回来
    # 的复位动作；卡死在烧写这一关会让后续步骤根本起不来。
    (_burn_firmware_step, {0}),
    # 2) NPU 硬件复位：旧路径是 4 次 `rknn-smi reset -t hw -d 0`（带
    # sudo）。改用 wdnai.RknnSmi 后内部循环 4 次，单条 step 一次性
    # 跑完，避免前端看到 4 行几乎一样的 step_start。retry 节奏
    # （每次间隔 0.5s）跟旧 reset_182x.sh 对齐 —— 上游注释："某些
    # 板子第一次复位不生效,需要多试几次"。
    (_hw_reset_npu_step, {0}),
    # 3) 重启 rknn3 userspace（rknn3_transfer_proxy 守护 + 重新打开
    # /dev/pcie-rkep-*）。现在烧写已经单独跑了，这一步只剩
    # userspace half，所以从 ``restart`` 改成 ``start`` —— 不需要
    # 再 stop 一次（rknn3_transfer_proxy 在烧写 + hw reset 期间
    # 状态本来就未刷新；新 start 直接接管）。
    #
    # 不要在前面加 sudo —— wdnwebui 进程不是 root，且本模块全程
    # 走非特权路径（hw_reset 用 librknnsmi ctypes / 烧写用
    # pcie_upgrade_tool）。rknn3_startup 内部如果需要 root
    # （/sys/bus/pci/rescan），就让它失败，stderr 会把原因发给
    # 前端；如果目标机器 webui 是以 root 跑的，rknn3_startup 会
    # 正常完成。
    (["rknn3_startup", "start"], {0}),
    # 4) 重启全部 spawned compute_worker。必须放在 rknn3_startup 之
    # 后：worker 重启时会重新打开 rknn3 驱动。没有 spawned worker 时
    # 这条 step 走 rc=0 + stdout="无 spawned worker，跳过"，UI 标
    # ok 不影响整体 done。rc=1（任一 worker 失败）视为整步失败，
    # 让用户在 WS done 帧看到。
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
                # 用 callable 的 ``__name__`` 当 label，方便前端
                # 区分 _burn_firmware_step / _hw_reset_npu_step /
                # _restart_compute_workers_step，而不是全部写成
                # "重启算力 worker"。原写死在 v0.8 引入 burn /
                # hw_reset 之后没更新。
                label = getattr(cmd, "__name__", repr(cmd))
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