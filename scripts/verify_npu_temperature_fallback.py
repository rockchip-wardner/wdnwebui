#!/usr/bin/env python3
"""回归：NPU 温度读不到 + C 层 ERROR 刷屏。

故障现象（本机双 RK1828 板，2026-10-24）：

    [ERROR] [nsmi_pcie.c:651] Receive error, command: 12     ← 设备级温度
    [ERROR] [rk_nsmi.c:1143] rknnsmi receive failed, ret=-1
    [WARNING] system_monitor: rknnsmi get_device_temperature(0) failed: ...
    [ERROR] [nsmi_pcie.c:651] Receive error, command: 7      ← 设备级功耗
    [WARN]  [rk_nsmi.c:906] rknnsmi receive failed, ret=-1
    [INFO]  [rk_nsmi.c:923] Device[0] get device power from hwmon node, power: 5143 mW

设备级 SMI 命令要过 PCIe 问 EP 固件要遥测，这块固件**不应答** → 温度直接
拿不到（卡片上温度恒为空），journal 每 3 秒刷一屏 ERROR。

分工（v0.30 之后）：

* **wdnai（SDK）** 负责「设备级失败 → 回退 chip 级」并记住该卡设备级不通
  （`RknnSmi.get_device_temperature`）—— 平台语义归 SDK 一层；
  chip 级的单位换算也在 SDK 里修好（整度，不再 ÷1000）。
* **wdnwebui** 只做两件事：一次普通调用拿温度；功耗加 TTL 缓存（cmd 7 的
  C 层噪音 wdnai 兜不住 —— 它的 hwmon 回退在库内部，每次调用仍会先打一行
  "Receive error, command: 7"）。

用例：

  T1 温度一次调用就拿到（SDK 兜住了）
  T2 webui **不碰** chip 级 API —— 那层平台语义归 SDK
  T3 温度彻底拿不到 → temp_c 置空，不抛异常、不刷 WARNING
  T4 功耗有 TTL 缓存 → TTL 内不再打 API（压 cmd 7 的噪音）
  T5 同一个 (操作, 设备) 的失败最多每 60s 记一条 WARNING（PCIe 瞬态）

不占 NPU、不起服务、不碰真机。
跑法：``.venv/bin/python scripts/verify_npu_temperature_fallback.py``
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wdnai  # noqa: E402

from app.system.api import system_monitor as sm  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✅ {name}" + (f"  ({detail})" if detail else ""))
    else:
        FAIL += 1
        print(f"  ❌ {name}" + (f"  ({detail})" if detail else ""))


class FakeSmiError(RuntimeError):
    """模拟 rknnsmi 的 RknnSmiCallError（GENERIC (-1)）。"""


class FakeSmi:
    """假 RknnSmi —— 模型**修复后**的 SDK 对外行为。

    SDK 侧已经把「设备级失败 → 回退 chip 级」兜住了，所以从 webui 看，
    ``get_device_temperature()`` 要么给值、要么抛异常；webui 不该再去碰
    ``get_chip_temperature()``。

    ``device_temp_ok=False`` 用来模拟「两级都不通」。
    """

    def __init__(self, *, device_temp_ok=True, temp_value=43.0,
                 power_ok=True, power_value=5343, memory_ok=True):
        self.device_temp_ok = device_temp_ok
        self.temp_value = temp_value
        self.power_ok = power_ok
        self.power_value = power_value
        self.memory_ok = memory_ok
        self.calls: list[str] = []

    # --- 枚举 / 静态信息 ---------------------------------------------------
    def get_device_list(self):
        return [{"index": 0, "mode": "pcie"}]

    def get_device_info(self, idx):
        return {"product_name": "RK1828"}

    def get_chip_list(self, idx):
        return [0]

    def get_pcie_info(self, idx, chip_id):
        return {"bus_id": "0001:11:00.0"}

    def get_npu_info(self, idx, chip_id):
        return {"util_rate_pct": 12}

    def get_memory_info(self, idx, chip_id):
        self.calls.append("mem")
        if not self.memory_ok:
            raise FakeSmiError("rknnsmi get_memory_info failed: GENERIC (-1)")
        return {"total_size": 5 * 1024 ** 3, "free_size": 1024 ** 3}

    def get_chip_health(self, idx, chip_id):
        return type("H", (), {"name": "OK"})()

    # --- 遥测 --------------------------------------------------------------
    def get_device_temperature(self, idx):
        self.calls.append("dev_temp")
        if not self.device_temp_ok:
            raise FakeSmiError("rknnsmi get_device_temperature failed: GENERIC (-1)")
        return self.temp_value

    def get_chip_temperature(self, idx, chip_id):
        self.calls.append("chip_temp")
        return self.temp_value

    def get_device_power(self, idx):
        self.calls.append("power")
        if not self.power_ok:
            raise FakeSmiError("rknnsmi get_device_power failed: GENERIC (-1)")
        return self.power_value


def install(fake: FakeSmi) -> FakeSmi:
    """把 wdnai.RknnSmi 换成假实现（_collect_npu_via_smi 内部才 import）。"""
    wdnai.RknnSmi = lambda *a, **k: fake
    return fake


def reset_caches() -> None:
    """清缓存 —— 旧代码里这些缓存还不存在，所以要容错（好让脚本在修复前
    是「红」而不是「崩」）。"""
    for name in ("_POWER_CACHE", "_SMI_WARN_AT"):
        cache = getattr(sm, name, None)
        if isinstance(cache, dict):
            cache.clear()


class _WarnCatcher(logging.Handler):
    def __init__(self):
        super().__init__()
        self.warnings: list[str] = []

    def emit(self, record):
        if record.levelno >= logging.WARNING:
            self.warnings.append(record.getMessage())


def collect() -> list[dict]:
    return asyncio.run(sm._collect_npu_via_smi())


# --------------------------------------------------------------------------- #
def t1_t2_temperature() -> None:
    reset_caches()
    fake = install(FakeSmi())
    rows = collect()
    check("T1a 采集到一行设备数据", len(rows) == 1, f"rows={len(rows)}")
    check("T1b 温度一次调用就拿到（SDK 兜住了设备级→chip 级回退）",
          rows and rows[0].get("temp_c") == 43.0,
          f"temp_c={rows[0].get('temp_c') if rows else None!r}")
    check("T1c 温度调用的是设备级 API",
          fake.calls.count("dev_temp") == 1,
          f"calls={fake.calls}")
    check("T2 webui 不碰 chip 级 API（平台语义归 SDK 一层）",
          "chip_temp" not in fake.calls,
          f"calls={fake.calls}")


def t3_temperature_unavailable() -> None:
    reset_caches()
    fake = install(FakeSmi(device_temp_ok=False))
    catcher = _WarnCatcher()
    lg = logging.getLogger(sm.__name__)
    lg.addHandler(catcher)
    old_level = lg.level
    lg.setLevel(logging.DEBUG)
    try:
        rows = collect()
    finally:
        lg.removeHandler(catcher)
        lg.setLevel(old_level)
    check("T3a 温度彻底拿不到时置空", rows and rows[0].get("temp_c") is None,
          f"temp_c={rows[0].get('temp_c') if rows else None!r}")
    check("T3b 不抛异常（其余字段照常）",
          rows and rows[0].get("npu_percent") == 12,
          f"npu_percent={rows[0].get('npu_percent') if rows else None!r}")
    check("T3c 不刷 WARNING（第一次失败记一条，之后降 DEBUG）",
          len(catcher.warnings) <= 1,
          f"warnings={catcher.warnings[:2]}")


def t4_power_cache() -> None:
    reset_caches()
    fake = install(FakeSmi())
    rows = collect()
    check("T4a 功耗取到值",
          rows and rows[0].get("power_mw") == 5343.0,
          f"power_mw={rows[0].get('power_mw') if rows else None!r}")
    n_after_first = fake.calls.count("power")
    rows = collect()
    check("T4b TTL 内第二次不再打功耗 API（压 cmd 7 噪音）",
          fake.calls.count("power") == n_after_first,
          f"power calls={fake.calls.count('power')}")
    reset_caches()
    rows = collect()
    check("T4c 缓存清掉后会重新取值",
          fake.calls.count("power") == n_after_first + 1,
          f"power calls={fake.calls.count('power')}")


def t5_warning_rate_limit() -> None:
    """PCIe 链路瞬态坏掉时，同一个操作不该每次轮询都记 WARNING。"""
    reset_caches()
    fake = install(FakeSmi(memory_ok=False))
    catcher = _WarnCatcher()
    lg = logging.getLogger(sm.__name__)
    lg.addHandler(catcher)
    old_level = lg.level
    lg.setLevel(logging.DEBUG)
    try:
        collect()
        collect()
        collect()
    finally:
        lg.removeHandler(catcher)
        lg.setLevel(old_level)
    mem_warns = [w for w in catcher.warnings if "get_memory_info" in w]
    check("T5a 连续 3 轮失败只记 1 条 WARNING（限流）",
          len(mem_warns) == 1,
          f"warnings={len(mem_warns)}")
    check("T5b 其余 WARNING 不是这条操作刷出来的",
          len(catcher.warnings) == len(mem_warns),
          f"all={catcher.warnings[:3]}")


def main() -> int:
    print("=== NPU 温度（SDK 兜底）+ 功耗缓存 + WARNING 限流 回归 ===")
    t1_t2_temperature()
    t3_temperature_unavailable()
    t4_power_cache()
    t5_warning_rate_limit()
    print(f"\n{'PASS' if FAIL == 0 else 'FAIL'}: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
