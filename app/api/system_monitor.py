"""System monitor endpoint.

Returns CPU/memory usage (via psutil), per-thermal-zone temperature read
from sysfs, and per-device NPU telemetry parsed from ``rknn-smi info``.
Service must run as root (or with sudo) so the NPU driver's sysfs/BAR
interface and ``/sys/class/thermal/`` are readable.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

import psutil
from fastapi import APIRouter

router = APIRouter(tags=["system-monitor"])

_RKNNSMI_BIN = "rknn-smi"
_RKNNSMI_TIMEOUT_S = 5
_BLOCK_SEP = re.compile(r"^\+[-=]+\+\s*$", re.MULTILINE)
_THERMAL_ROOT = Path("/sys/class/thermal")
_THERMAL_NAME_OVERRIDES = {
    "soc-thermal":      "SoC",
    "bigcore0-thermal": "大核 0",
    "bigcore1-thermal": "大核 1",
    "littlecore-thermal": "小核",
    "center-thermal":   "中心",
    "gpu-thermal":      "GPU",
    "npu-thermal":      "NPU",
    "cpu-thermal":      "CPU",
    "cpu0-thermal":     "CPU 0",
    "cpu1-thermal":     "CPU 1",
    "x86_pkg_temp":     "x86 整包",
}
_SKIP_THERMAL_TYPES = {"acpitz", "acpi"}


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


def _rkai_canonical_device_index(bus_id: str | None) -> int | None:
    """Resolve a ``bus_id`` (PCI BDF, e.g. ``0001:11:00.0``) to its
    enumerate-index in ``RKNN3Lite().get_devices_id()``.

    rkai (and therefore 算力配置页) uses ``enumerate(get_devices_id())``
    as the canonical ``npu_index``; ``rknn-smi info`` numbers devices by
    its own internal order, which doesn't always match. This helper
    bridges the two so 系统信息 / 算力卡信息 and 算力配置 label the same
    physical card with the same ``#K``. Returns ``None`` when rkai is
    unavailable (non-RK dev box) or ``bus_id`` is missing/unmatched —
    callers fall back to the rknn-smi native idx in that case.
    """
    if not bus_id:
        return None
    try:
        from rknn3lite.api import RKNN3Lite
        ids = RKNN3Lite().get_devices_id()
    except Exception:
        return None
    for i, x in enumerate(ids):
        raw = x if isinstance(x, (bytes, bytearray)) else str(x).encode("utf-8")
        if raw.decode("utf-8", errors="replace") == bus_id:
            return i
    return None


def _align_npu_indices(devices: list[dict]) -> list[dict]:
    """Re-index each rknn-smi row to the rkai-canonical ``npu_index``.

    Sorts the returned list by the canonical index (rknn-smi-only
    orphans come last, in their original order) so the UI sees
    ``#0, #1, #2, ...`` in the same order the 算力配置 page does. The
    rknn-smi native idx is preserved as ``rknn_smi_idx`` for debugging.
    Orphans (cards rkai can't see, e.g. rknn-smi mis-scan) get a
    negative ``index`` and an ``orphan=True`` flag so they render
    after the canonical rows without colliding on the same ``#N``.
    """
    aligned: list[tuple[int, int, int, dict]] = []
    for smi_order, dev in enumerate(devices):
        canonical = _rkai_canonical_device_index(dev.get("bus_id"))
        if canonical is None:
            dev["rknn_smi_idx"] = dev.get("index")
            dev["index"] = -(smi_order + 1)
            dev["orphan"] = True
            key = (1, 0, smi_order, dev)
            aligned.append(key)
            continue
        dev["rknn_smi_idx"] = dev.get("index")
        dev["index"] = canonical
        dev.pop("orphan", None)
        key = (0, canonical, smi_order, dev)
        aligned.append(key)
    aligned.sort(key=lambda t: (t[0], t[1], t[2]))
    return [t[3] for t in aligned]


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
    devices = _align_npu_indices(_parse_rknn_smi_table(stdout))
    return {
        "available": True,
        "source": _RKNNSMI_BIN,
        "devices": devices,
        "raw_output": stdout,
        "stderr": stderr,
    }


def _read_int(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (FileNotFoundError, PermissionError, ValueError, OSError):
        return None


def _collect_thermal() -> dict:
    if not _THERMAL_ROOT.is_dir():
        return {
            "available": False,
            "reason": f"{_THERMAL_ROOT} not found",
            "sensors": [],
        }
    sensors: list[dict] = []
    try:
        zone_dirs = sorted(
            (p for p in _THERMAL_ROOT.iterdir() if p.name.startswith("thermal_zone")),
            key=lambda p: int(p.name[len("thermal_zone"):]) if p.name[len("thermal_zone"):].isdigit() else p.name,
        )
    except OSError as exc:
        return {
            "available": False,
            "reason": f"enumerate {_THERMAL_ROOT}: {exc.strerror or exc}",
            "sensors": [],
        }
    for zone in zone_dirs:
        try:
            index = int(zone.name[len("thermal_zone"):])
        except ValueError:
            continue
        type_path = zone / "type"
        temp_path = zone / "temp"
        if not temp_path.is_file():
            continue
        raw_temp = _read_int(temp_path)
        if raw_temp is None:
            continue
        stype = type_path.read_text().strip() if type_path.is_file() else ""
        if stype in _SKIP_THERMAL_TYPES:
            continue
        sensors.append({
            "index": index,
            "name": stype or zone.name,
            "label": _THERMAL_NAME_OVERRIDES.get(stype, stype or zone.name),
            "temp_c": round(raw_temp / 1000.0, 1),
        })
    if not sensors:
        return {
            "available": False,
            "reason": "no thermal_zone sensors readable",
            "sensors": [],
        }
    return {
        "available": True,
        "source": str(_THERMAL_ROOT),
        "sensors": sensors,
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
    base["thermal"] = await asyncio.to_thread(_collect_thermal)
    base["npu"] = await _collect_npu()
    base["placeholder"] = False
    base["section"] = "system-monitor"
    base["collected_at"] = datetime.now(timezone.utc).isoformat()
    return base