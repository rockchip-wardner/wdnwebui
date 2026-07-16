"""System monitor endpoint.

Returns CPU/memory usage (via psutil) and per-device NPU telemetry parsed
from ``rknn-smi info``. Service must run as root (or with sudo) so the
NPU driver's sysfs/BAR interface is readable.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
from datetime import datetime, timezone

import psutil
from fastapi import APIRouter

router = APIRouter(tags=["system-monitor"])

_RKNNSMI_BIN = "rknn-smi"
_RKNNSMI_TIMEOUT_S = 5
_BLOCK_SEP = re.compile(r"^\+[-=]+\+\s*$", re.MULTILINE)


async def _run_rknn_smi_info() -> tuple[str, str, int]:
    """Run ``rknn-smi info``; return (stdout, stderr, returncode)."""
    proc = await asyncio.create_subprocess_exec(
        _RKNNSMI_BIN, "info",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=_RKNNSMI_TIMEOUT_S
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return "", f"timeout after {_RKNNSMI_TIMEOUT_S}s", -1
    return (
        stdout.decode(errors="replace"),
        stderr.decode(errors="replace"),
        proc.returncode if proc.returncode is not None else -1,
    )


def _to_int(s: str) -> int | None:
    s = s.strip()
    if not s or s.upper() == "NA":
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def _to_float(s: str) -> float | None:
    s = s.strip().rstrip("%")
    if not s or s.upper() == "NA":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_rknn_smi_table(text: str) -> list[dict]:
    """Parse ``rknn-smi info`` table output.

    Each device occupies one block delimited by ``+---+``. Within the
    block, the first pipe-delimited row carries
    ``[idx, status, health, power_mw, npu_pct]`` (idx+status share the
    first cell), and the second row carries
    ``[chip_name, bus_id, temp_c, memory]`` (chip_name is the second
    token of the first cell).

    Field names mirror what the table reports.
    """
    devices: list[dict] = []
    blocks = _BLOCK_SEP.split(text)

    for block in blocks:
        data_rows: list[list[str]] = []
        for line in block.splitlines():
            stripped = line.strip()
            if not stripped.startswith("|"):
                continue
            inner = stripped.strip("|")
            cells = [c.strip() for c in inner.split("|")]
            if not cells:
                continue
            first_token = cells[0].split()[0].lower() if cells[0] else ""
            if first_token in ("device", "chip", "rknn-smi"):
                continue
            try:
                int(cells[0].split()[0])
            except (ValueError, IndexError):
                continue
            data_rows.append(cells)

        i = 0
        while i < len(data_rows):
            top = data_rows[i]
            top_tokens = top[0].split()
            try:
                idx = int(top_tokens[0])
            except (ValueError, IndexError):
                i += 1
                continue
            status = " ".join(top_tokens[1:]) if len(top_tokens) > 1 else ""
            top_rest = [status] + top[1:]
            i += 1

            if i < len(data_rows):
                bot = data_rows[i]
                bot_tokens = bot[0].split()
                looks_like_new_device = (
                    bool(bot_tokens)
                    and bot_tokens[0].isdigit()
                    and len(bot_tokens) == 1
                )
                if looks_like_new_device:
                    chip_name = ""
                    bot_rest = bot[1:]
                    # don't consume: leave for next iteration
                else:
                    chip_name = " ".join(bot_tokens[1:]) if len(bot_tokens) > 1 else (bot_tokens[0] if bot_tokens else "")
                    bot_rest = bot[1:]
                    i += 1
            else:
                chip_name = ""
                bot_rest = []

            cols = top_rest + [chip_name] + bot_rest
            cols = (cols + [""] * 8)[:8]
            power_mw = _to_int(cols[2])
            npu_percent = _to_float(cols[3])
            temp_c = _to_float(cols[6])
            mem_str = cols[7] if len(cols) > 7 else ""
            mem_used, mem_total = _parse_memory(mem_str)
            devices.append({
                "index": idx,
                "id": chip_name or None,
                "type": None,
                "bus_id": cols[5] if len(cols) > 5 else None,
                "status": cols[0] or None,
                "health": cols[1] if len(cols) > 1 else None,
                "power_mw": power_mw,
                "temp_c": temp_c,
                "npu_percent": npu_percent,
                "mem_used_mb": mem_used,
                "mem_total_mb": mem_total,
                "mem_raw": mem_str,
            })
    return devices


def _parse_memory(s: str) -> tuple[float | None, float | None]:
    """Parse '32 / 5120' or '32/5120' into (used, total) in MB."""
    if not s:
        return None, None
    parts = re.split(r"\s*/\s*", s.strip())
    if len(parts) != 2:
        return None, None
    try:
        return float(parts[0]), float(parts[1])
    except ValueError:
        return None, None


async def _collect_npu() -> dict:
    if shutil.which(_RKNNSMI_BIN) is None:
        return {
            "available": False,
            "source": _RKNNSMI_BIN,
            "reason": f"{_RKNNSMI_BIN} not found in PATH",
            "devices": [],
            "raw_output": "",
            "stderr": "",
        }
    stdout, stderr, rc = await _run_rknn_smi_info()
    if rc != 0 or not stdout.strip():
        return {
            "available": False,
            "source": _RKNNSMI_BIN,
            "reason": f"exit={rc}; empty output",
            "devices": [],
            "raw_output": stdout,
            "stderr": stderr,
        }
    return {
        "available": True,
        "source": _RKNNSMI_BIN,
        "devices": _parse_rknn_smi_table(stdout),
        "raw_output": stdout,
        "stderr": stderr,
    }


def _sample_cpu_mem() -> dict:
    cpu = psutil.cpu_percent(interval=0.1)
    per_core = psutil.cpu_percent(interval=None, percpu=True)
    vm = psutil.virtual_memory()
    load_avg = None
    if hasattr(os, "getloadavg"):
        try:
            la = os.getloadavg()
            load_avg = {"1": la[0], "5": la[1], "15": la[2]}
        except OSError:
            pass
    total_mb = vm.total / 1024 / 1024
    used_mb = vm.used / 1024 / 1024
    return {
        "cpu": {
            "percent": cpu,
            "per_core": per_core,
            "count": psutil.cpu_count(logical=True) or len(per_core),
            "load_avg": load_avg,
        },
        "memory": {
            "total_mb": round(total_mb, 1),
            "used_mb": round(used_mb, 1),
            "available_mb": round(total_mb - used_mb, 1),
            "percent": vm.percent,
        },
    }


@router.get("/system-monitor")
async def get_system_monitor() -> dict:
    base = await asyncio.to_thread(_sample_cpu_mem)
    base["npu"] = await _collect_npu()
    base["placeholder"] = False
    base["section"] = "system-monitor"
    base["collected_at"] = datetime.now(timezone.utc).isoformat()
    return base