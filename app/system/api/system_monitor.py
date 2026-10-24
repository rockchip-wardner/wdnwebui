"""System monitor endpoint.

Returns CPU/memory usage (via psutil), per-thermal-zone temperature read
from sysfs, and per-device NPU telemetry. NPU data is read through the
``wdnai.RknnSmi`` ctypes shim (``/usr/lib/librknnsmi.so``) — the same
C ABI the ``rknn-smi`` CLI uses, but in-process so we don't fork a
subprocess for every poll. The legacy `rknn-smi info` table parser is
kept for fallback (e.g. very old targets where the SO isn't shipped)
but is no longer the default.

Service must run as root (or with sudo) so the NPU driver's sysfs/BAR
interface and ``/sys/class/thermal/`` are readable, AND so the
``/tmp/rknnsmi.lock`` flock taken by ``librknnsmi.so``'s ``init`` can
be acquired (the lock file is created 0666 but ``flock`` against a
file the process can't write fails with EACCES on some kernels).
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import time
from typing import Optional
from datetime import datetime, timezone
from pathlib import Path

import psutil
from fastapi import APIRouter

router = APIRouter(tags=["system-monitor"])

# 旧 fallback 路径：``rknn-smi info`` 子进程。默认不再走，保留是因为
# 极少数目标（如 5.10 BSP 之前不带 librknnsmi.so 的镜像）仍然只能靠
# CLI 拿数。前端永远看到相同 shape 的 JSON。
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

# ---------------------------------------------------------------------------
# NPU 温度 / 功耗读取
#
# RK1828（RK3588 + PCIe AI 协处理器板）上，**设备级** SMI 命令要过 PCIe
# 通道问 EP 固件要遥测，而这块固件**不应答** —— rknn-smi 先打
#   [ERROR] [nsmi_pcie.c:651] Receive error, command: 12   （温度）
#   [ERROR] [nsmi_pcie.c:651] Receive error, command: 7    （功耗）
# 然后 rknnsmi_get_device_temperature() 直接返回 -1（GENERIC）。
# 前端于是永远拿不到 NPU 温度（卡片上温度恒为空），journal 里每 3 秒
# 刷一屏 ERROR。
#
# 实测（2026-10-24，本机双 RK1828 板）：
#   rknnsmi_get_device_temperature(0) → 必失败（RknnSmiCallError GENERIC）
#   rknnsmi_get_chip_temperature(0,0)  → 成功，raw = 43 / 45
#   rknn-smi info CLI                   → Temp(C) 40 / 42（CLI 走的就是 chip 级）
#   rknnsmi_get_device_power(0)        → PCIe 失败，但**库内部**回退到板级
#                                        INA226（hwmon）→ 仍返回 ~5300 mW
#
# **设备级→chip 级的回退已经由 wdnai 承担**（RknnSmi.get_device_temperature
# 失败时自动回退，并记住该卡设备级不通 → 后续不再触发注定失败的 PCIe
# 命令）。所以本仓这里就是一次普通调用，不需要自己猜单位、不需要自己
# 实现「设备级 / chip 级」这层平台语义。
#
# 功耗那边 wdnai 没法兜：设备级功耗命令（cmd 7）同样过不去 PCIe，库内部
# 回退到板级 INA226（hwmon）——**值是对的**（两张卡读到的是同一路板级
# 传感器，本来就不是逐卡功率），但每次调用都会先打一行 C 层的
# "Receive error, command: 7"。功耗是慢变量，缓存几个轮询周期即可。
# ---------------------------------------------------------------------------
_POWER_CACHE: dict[int, tuple[float, float]] = {}   # index → (mW, 取值时刻)
_POWER_TTL_S = 30.0                           # 慢变量；tooltip 显示，30s 足够新鲜


def _read_power_mw(smi, idx: int) -> Optional[float]:
    """读 NPU 功耗（mW），带短 TTL 缓存。

    设备级功耗命令（cmd 7）在这块固件上同样过不去 PCIe，rknn-smi 库内部
    会回退到板级 INA226（hwmon）——**值是对的**（注意两张卡读到的是同一路
    板级传感器，本来就不是逐卡功率），但每次调用都会先打一行 C 层的
    "Receive error, command: 7"。功耗是慢变量，缓存几个轮询周期即可。
    """
    import logging

    now = time.time()
    hit = _POWER_CACHE.get(idx)
    if hit is not None and now - hit[1] < _POWER_TTL_S:
        return hit[0]
    try:
        val = smi.get_device_power(idx)
    except Exception as e:  # noqa: BLE001
        logging.getLogger(__name__).debug(
            "rknnsmi get_device_power(%d) failed: %s: %s",
            idx, type(e).__name__, e)
        return None
    if val is None:
        return None
    val = float(val)
    _POWER_CACHE[idx] = (val, now)
    return val


# ---------------------------------------------------------------------------
# 失败日志限流
#
# PCIe 链路偶尔会进入一段坏状态（典型触发：NPU 被 SIGTERM 打断、卡复位），
# 此时一整轮 SMI 查询会集体失败（get_memory_info / get_chip_health /
# get_chip_list …，ret=-1 / -8）。这类失败**自己会恢复**，但前端每 3 秒
# 轮询一次，逐次记 WARNING 就是刷屏。
#
# 所以同一个 (操作, 设备) 的失败最多每 _SMI_WARN_TTL_S 记一条 WARNING，
# 其余降到 DEBUG —— 真要追时看 DEBUG 就够。
# ---------------------------------------------------------------------------
_SMI_WARN_AT: dict[str, float] = {}
_SMI_WARN_TTL_S = 60.0


def _smi_warn_once(key: str, msg: str, *args) -> None:
    """同一个 key 的失败最多每 60s 记一条 WARNING，其余记 DEBUG。"""
    import logging

    lg = logging.getLogger(__name__)
    now = time.time()
    if now - _SMI_WARN_AT.get(key, 0.0) >= _SMI_WARN_TTL_S:
        _SMI_WARN_AT[key] = now
        lg.warning(msg, *args)
    else:
        lg.debug(msg, *args)


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
        # subprocess 可能在 wait_for 取消过程中已经退出 / 被 asyncio 关闭，
        # 直接 kill() / wait() 会抛 ProcessLookupError。需要 try/except 兜住
        # 否则 500 错误泄漏到 /api/system-monitor 整个 endpoint。
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await proc.wait()
        except ProcessLookupError:
            pass
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


def _wdnai_canonical_device_index(bus_id: str | None) -> int | None:
    """Resolve a ``bus_id`` (PCI BDF, e.g. ``0001:11:00.0``) to its
    enumerate-index in ``RKNN3Lite().get_devices_id()``.

    wdnai (and therefore 算力配置页) uses ``enumerate(get_devices_id())``
    as the canonical ``npu_index``; the SMI driver numbers devices by
    its own internal order, which doesn't always match. This helper
    bridges the two so 系统信息 / 算力卡信息 and 算力配置 label the same
    physical card with the same ``#K``. Returns ``None`` when wdnai is
    unavailable (non-RK dev box) or ``bus_id`` is missing/unmatched —
    callers fall back to the rknn-smi native idx in that case.

    Caches the ``RKNN3Lite`` probe and the resolved lookup map at
    module scope. ``/api/system-monitor`` calls this once per
    smi device per poll; without the cache each call instantiates
    a new ``RKNN3Lite`` (which opens ``/dev/rknn3`` and contends with
    the worker's inference on RK3588).
    """
    if not bus_id:
        return None
    global _wdnai_dev_ids
    if _wdnai_dev_ids is None:
        try:
            # wdnai ≥ 0.4: ``RKNN3Lite`` is the vendored ctypes shim;
            # the rknn3lite / rknn3-toolkit-lite wheel is no longer
            # installed.
            from wdnai._librknn import RKNN3Lite
            _wdnai_probe = RKNN3Lite()
            _wdnai_dev_ids = _wdnai_probe.get_devices_id()
        except Exception:
            return None
    for i, x in enumerate(_wdnai_dev_ids):
        raw = x if isinstance(x, (bytes, bytearray)) else str(x).encode("utf-8")
        if raw.decode("utf-8", errors="replace") == bus_id:
            return i
    return None


_wdnai_probe = None
_wdnai_dev_ids: Optional[list] = None


def _align_npu_indices(devices: list[dict]) -> list[dict]:
    """Re-index each smi row to the wdnai-canonical ``npu_index``.

    Sorts the returned list by the canonical index (smi-only
    orphans come last, in their original order) so the UI sees
    ``#0, #1, #2, ...`` in the same order the 算力配置 page does. The
    rknn-smi native idx is preserved as ``rknn_smi_idx`` for debugging.
    Orphans (cards wdnai can't see, e.g. smi mis-scan) get a
    negative ``index`` and an ``orphan=True`` flag so they render
    after the canonical rows without colliding on the same ``#N``.
    """
    aligned: list[tuple[int, int, int, dict]] = []
    for smi_order, dev in enumerate(devices):
        canonical = _wdnai_canonical_device_index(dev.get("bus_id"))
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


async def _collect_npu_via_smi() -> list[dict]:
    """Read per-device NPU telemetry via :class:`wdnai.RknnSmi`.

    Returns a list of dicts in the same shape as
    :func:`_parse_rknn_smi_table` produced (so the downstream
    :func:`_align_npu_indices` reordering and the frontend rendering
    paths need no changes):

        {index, id, type, bus_id, status, health, power_mw, temp_c,
         npu_percent, mem_used_mb, mem_total_mb, mem_raw}

    Each per-device probe is wrapped in its own try/except so a
    single bad device (e.g. one that just rebooted and has no chip
    info yet) doesn't blank the whole card grid. Returns an empty
    list if the SO can't be opened at all; callers should fall back
    to the CLI parser.
    """
    from wdnai import RknnSmi  # local import: wdnai pulls cv2 + gguf

    def _probe() -> list[dict]:
        smi = RknnSmi()
        devs = smi.get_device_list()
        out: list[dict] = []
        for d in devs:
            idx = int(d["index"])
            row: dict = {
                "index": idx,
                "id": None,
                "type": d.get("mode"),
                "bus_id": None,
                "status": "Online",
                "health": None,
                "power_mw": None,
                "temp_c": None,
                "npu_percent": None,
                "mem_used_mb": None,
                "mem_total_mb": None,
                "mem_raw": None,
                "rknn_smi_idx": idx,
            }
            # device_info / chip_info — best-effort, log on failure
            try:
                info = smi.get_device_info(idx)
                if info.get("product_name"):
                    row["id"] = info["product_name"]
            except Exception as e:  # noqa: BLE001
                _smi_warn_once(
                    f"device_info:{idx}",
                    "rknnsmi get_device_info(%d) failed: %s: %s",
                    idx, type(e).__name__, e,
                )
            try:
                chips = smi.get_chip_list(idx)
                chip_id = chips[0] if chips else 0
            except Exception as e:  # noqa: BLE001
                _smi_warn_once(
                    f"chip_list:{idx}",
                    "rknnsmi get_chip_list(%d) failed: %s: %s",
                    idx, type(e).__name__, e,
                )
                chip_id = 0
            try:
                pcie = smi.get_pcie_info(idx, chip_id)
                if pcie.get("bus_id"):
                    row["bus_id"] = pcie["bus_id"]
            except Exception:
                # USB / ETH devices don't expose PCIe BDF; leave None.
                pass
            # 温度：wdnai 已内建「设备级失败 → 回退 chip 级」并记住该卡
            # 设备级不通（见文件头的说明），这里就是一次普通调用。
            try:
                row["temp_c"] = smi.get_device_temperature(idx)
            except Exception as e:  # noqa: BLE001
                _smi_warn_once(
                    f"device_temp:{idx}",
                    "rknnsmi get_device_temperature(%d) failed: %s: %s",
                    idx, type(e).__name__, e,
                )
            # 功耗：值来自库内部的 hwmon 回退（PCIe 路径坏），带 TTL 缓存。
            row["power_mw"] = _read_power_mw(smi, idx)
            try:
                npu = smi.get_npu_info(idx, chip_id)
                row["npu_percent"] = float(npu.get("util_rate_pct", 0) or 0)
            except Exception as e:  # noqa: BLE001
                _smi_warn_once(
                    f"npu_info:{idx}:{chip_id}",
                    "rknnsmi get_npu_info(%d, %d) failed: %s: %s",
                    idx, chip_id, type(e).__name__, e,
                )
            try:
                mem = smi.get_memory_info(idx, chip_id)
                total = int(mem.get("total_size", 0))
                free = int(mem.get("free_size", 0))
                if total > 0:
                    used = max(0, total - free)
                    row["mem_total_mb"] = round(total / (1024 * 1024), 1)
                    row["mem_used_mb"] = round(used / (1024 * 1024), 1)
                    row["mem_raw"] = (
                        f"{int(used / (1024 * 1024))} / "
                        f"{int(total / (1024 * 1024))}"
                    )
            except Exception as e:  # noqa: BLE001
                _smi_warn_once(
                    f"memory_info:{idx}:{chip_id}",
                    "rknnsmi get_memory_info(%d, %d) failed: %s: %s",
                    idx, chip_id, type(e).__name__, e,
                )
            try:
                health = smi.get_chip_health(idx, chip_id)
                row["health"] = health.name
            except Exception as e:  # noqa: BLE001
                _smi_warn_once(
                    f"chip_health:{idx}:{chip_id}",
                    "rknnsmi get_chip_health(%d, %d) failed: %s: %s",
                    idx, chip_id, type(e).__name__, e,
                )
            out.append(row)
        return out

    try:
        return await asyncio.to_thread(_probe)
    except Exception as e:  # noqa: BLE001
        # RknnSmi init failed (no SO, no root, no driver). Let caller
        # try the CLI fallback. We do NOT raise — the endpoint should
        # still return a valid (degraded) JSON shape.
        raise _SmiUnavailable(str(e))


class _SmiUnavailable(Exception):
    """Marker: in-process RknnSmi shim could not be initialized.

    Handled by :func:`_collect_npu` to decide between the
    wdnai RknnSmi path (preferred) and the ``rknn-smi info`` CLI
    fallback (legacy targets without ``librknnsmi.so``).
    """


async def _collect_npu() -> dict:
    """Aggregate NPU device telemetry, with graceful degradation.

    Tries the in-process :class:`wdnai.RknnSmi` ctypes shim first
    (no subprocess, no per-poll fork). If the shim can't load or
    ``rknnsmi_init`` fails (e.g. SO not present on very old images,
    or the process is missing the privileges to grab
    ``/tmp/rknnsmi.lock``), falls back to the legacy
    ``rknn-smi info`` subprocess + table parser so the UI keeps
    showing data instead of going dark.

    Either way the response shape is the same — frontend rendering
    doesn't care which path produced the data.
    """
    try:
        devices = _align_npu_indices(await _collect_npu_via_smi())
        return {
            "available": True,
            "source": "wdnai.RknnSmi",
            "devices": devices,
            "raw_output": "",
            "stderr": "",
        }
    except _SmiUnavailable as e:
        fallback_reason = f"wdnai.RknnSmi unavailable: {e}"
    except Exception as e:  # noqa: BLE001
        # Per-device errors we can't predict — also fall back to the
        # CLI rather than blanking the UI on a single broken probe.
        fallback_reason = f"wdnai.RknnSmi runtime error: {type(e).__name__}: {e}"

    # --- legacy CLI fallback (deprecated; kept for very old images
    # without librknnsmi.so). The legacy parser is still loaded because
    # the v0.4 dependency upgrade doesn't *guarantee* the SO is on
    # the target — only the typical wdnai install path does.
    if shutil.which(_RKNNSMI_BIN) is None:
        return {
            "available": False,
            "source": _RKNNSMI_BIN,
            "reason": (
                f"{fallback_reason}; and {_RKNNSMI_BIN} not in PATH"
            ),
            "devices": [],
            "raw_output": "",
            "stderr": "",
        }
    stdout, stderr, rc = await _run_rknn_smi_info()
    if rc != 0 or not stdout.strip():
        return {
            "available": False,
            "source": _RKNNSMI_BIN,
            "reason": (
                f"{fallback_reason}; CLI exit={rc}, empty output"
            ),
            "devices": [],
            "raw_output": stdout,
            "stderr": stderr,
        }
    devices = _align_npu_indices(_parse_rknn_smi_table(stdout))
    return {
        "available": True,
        "source": f"{_RKNNSMI_BIN} (fallback: {fallback_reason})",
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


def _sample_disk() -> dict:
    """Snapshot 根挂载点的磁盘用量。

    wdnwebui 在嵌入式板上模型权重、日志、uploads 全都往根盘或
    ``/userdata`` 写 —— 满盘会让 rknn 加载失败、demo 写文件失败、
    journald 卡死。系统信息页顶部给一个"磁盘剩余"卡，比事后看
    dmesg 里 "no space left" 强。
    """
    path = "/"
    try:
        du = psutil.disk_usage(path)
    except FileNotFoundError:
        return {
            "available": False,
            "reason": f"{path} not mounted",
            "path": path,
        }
    except OSError as exc:
        return {
            "available": False,
            "reason": f"disk_usage({path}): {exc.strerror or exc}",
            "path": path,
        }
    total_mb = du.total / 1024 / 1024
    free_mb = du.free / 1024 / 1024
    used_mb = du.used / 1024 / 1024
    return {
        "available": True,
        "path": path,
        "total_mb": round(total_mb, 1),
        "used_mb": round(used_mb, 1),
        "free_mb": round(free_mb, 1),
        "percent": du.percent,
    }


def _read_proc_field(pid: int, name: str) -> str | None:
    """Read a single field from ``/proc/<pid>/status`` (e.g. ``PPid``,
    ``State``, ``VmRSS``). Returns the raw value as a string, or
    ``None`` if the process vanished or the field is missing.
    """
    try:
        with open(f"/proc/{pid}/status", "rb") as f:
            data = f.read().decode("utf-8", errors="replace")
    except (OSError, FileNotFoundError):
        return None
    prefix = name + ":"
    for line in data.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return None


def _read_proc_comm(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/comm", "rb") as f:
            return f.read().rstrip(b"\x00").rstrip().decode("utf-8", errors="replace")
    except (OSError, FileNotFoundError):
        return ""


def _read_proc_cmdline(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().rstrip(b"\x00").decode("utf-8", errors="replace")
    except (OSError, FileNotFoundError):
        return ""


def _read_proc_start_time(pid: int) -> float | None:
    """Return process start time as Unix epoch (seconds)."""
    stat = _read_proc_field(pid, "Name")
    if stat is None:
        return None
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            raw = f.read().decode("utf-8", errors="replace")
    except (OSError, FileNotFoundError):
        return None
    # /proc/[pid]/stat 字段以空格分隔，但 comm 字段用括号包起来（可能含空格）。
    # 找最后一个 ')' 然后再 split。
    rparen = raw.rfind(")")
    if rparen < 0:
        return None
    rest = raw[rparen + 1:].strip()
    # rest 的字段 3 = starttime (clock ticks since boot)
    fields = rest.split()
    if len(fields) < 22:
        return None
    try:
        start_ticks = int(fields[20])  # 0-indexed within rest: field 21 - 1
    except (ValueError, IndexError):
        return None
    try:
        clk_tck = os.sysconf("SC_CLK_TCK")
    except (ValueError, OSError):
        clk_tck = 100
    try:
        with open("/proc/stat", "rb") as f:
            data = f.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    btime = None
    for line in data.splitlines():
        if line.startswith("btime "):
            try:
                btime = float(line.split()[1])
            except (ValueError, IndexError):
                pass
            break
    if btime is None:
        return None
    return btime + start_ticks / clk_tck


def _parse_worker_model_id(cmdline: str) -> str | None:
    """Extract ``--model <id>`` from a ``compute_worker.py`` cmdline.

    Accepts the argv-style string (``compute_worker.py --model foo``
    joined by nulls) or a human-friendly form with spaces.
    """
    if not cmdline:
        return None
    parts = re.split(r"\x00|\s+", cmdline)
    for i, p in enumerate(parts):
        if p == "--model" and i + 1 < len(parts):
            return parts[i + 1] or None
    return None


def _collect_workers() -> dict:
    """Enumerate every ``compute_worker.py --model <id>`` process on
    the host (any user, any owner — we read ``/proc`` which the kernel
    exposes to all readers, see ``proc(5)``).

    这是「系统信息 → Worker 进程」表格的真相源：完全独立于
    ``ComputeRegistry`` 内部状态。注册中心看的是「我接管的实例」，
    这里看的是「OS 上实际在跑的所有 worker」—— 两边对不上 = 有
    孤儿泄漏；这就是我们要捕获的现场。
    """
    own_pid = os.getpid()
    workers: list[dict] = []
    try:
        entries = os.listdir("/proc")
    except OSError as exc:
        return {
            "available": False,
            "reason": f"listdir /proc: {exc.strerror or exc}",
            "workers": [],
        }
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == own_pid:
            continue
        cmdline = _read_proc_cmdline(pid)
        if "compute_worker.py" not in cmdline:
            continue
        model_id = _parse_worker_model_id(cmdline)
        if model_id is None:
            continue
        state_field = _read_proc_field(pid, "State") or ""
        ppid = _read_proc_field(pid, "PPid")
        rss_field = _read_proc_field(pid, "VmRSS")
        rss_mb = None
        if rss_field:
            m = re.match(r"(\d+)\s*kB", rss_field)
            if m:
                try:
                    rss_mb = round(int(m.group(1)) / 1024, 1)
                except ValueError:
                    pass
        started_at = _read_proc_start_time(pid)
        # cmdline 里把每个 argv 用空格拼起来就够了（用户可读形式），
        # 实际原始形式是 null 分隔。
        cmdline_human = cmdline.replace("\x00", " ").strip()
        workers.append({
            "pid": pid,
            "ppid": int(ppid) if ppid and ppid.isdigit() else None,
            "comm": _read_proc_comm(pid),
            "model_id": model_id,
            "state": state_field.split()[0] if state_field else "",
            "rss_mb": rss_mb,
            "started_at": started_at,
            "cmdline": cmdline_human,
        })
    workers.sort(key=lambda w: (w["model_id"] or "", w["pid"]))
    return {
        "available": True,
        "source": "/proc/*/cmdline",
        "workers": workers,
    }


def _collect_discovery() -> dict:
    """Snapshot the FastDDS SIMPLE discovery state of the main process's
    Hub. Source of truth for the 「系统信息 → 在网设备」section.

    Returns ``{protocol, multicast_group, own_guid, peer_count,
    peers:[{guid, name, ip, discovered_at}], wdndds_initial_peers}``.
    ``ip`` is the first UDPv4 unicast locator the peer announced
    (from FastDDS ParticipantBuiltinTopicData); empty string when
    the peer didn't advertise one (e.g. SHM-only).
    """
    try:
        from app.core.compute.registry import get_registry
        reg = get_registry()
        peers = reg.list_peers()
        own_guid = reg.own_guid()
    except Exception as e:  # noqa: BLE001
        return {
            "protocol": "SIMPLE",
            "multicast_group": "239.255.0.1:7400",
            "own_guid": "",
            "peer_count": 0,
            "peers": [],
            "reason": f"registry unavailable: {type(e).__name__}: {e}",
        }
    initial_peers = [
        p.strip() for p in
        (os.environ.get("WDNDDS_INITIAL_PEERS", "") or "").split(",")
        if p.strip()
    ]
    return {
        "protocol": "SIMPLE",
        "multicast_group": "239.255.0.1:7400",
        "own_guid": own_guid,
        "peer_count": len(peers),
        "peers": sorted(peers, key=lambda p: p.get("name") or ""),
        "wdndds_initial_peers": initial_peers,
    }


@router.get("/system-monitor")
async def get_system_monitor() -> dict:
    base = await asyncio.to_thread(_sample_cpu_mem)
    base["disk"] = await asyncio.to_thread(_sample_disk)
    base["workers"] = await asyncio.to_thread(_collect_workers)
    base["discovery"] = await asyncio.to_thread(_collect_discovery)
    base["thermal"] = await asyncio.to_thread(_collect_thermal)
    base["npu"] = await _collect_npu()
    base["placeholder"] = False
    base["section"] = "system-monitor"
    base["collected_at"] = datetime.now(timezone.utc).isoformat()
    return base
    return {"running": False, "stopped": True}