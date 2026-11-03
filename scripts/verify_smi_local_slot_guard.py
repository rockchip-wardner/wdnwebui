"""回归防护：SMI 采集只探**本机**的 PCIe 卡，且不发注定失败的命令。

背景（2026-11-03 实测，RK3588 真机）
------------------------------------
上游 `fb634ad` 已经修好了「远程（ETH）NPU 的 SMI 命令永不返回 → 端点挂死」
（专用线程池 + 整体超时 + 熔断 + 10s TTL 缓存）。但它认远程设备靠的是
``get_device_list()`` 里的 ``mode`` 字段，而**那个字段跟这个库的版本对不上**：

    slot 0  mode=0x0      → 0001:11:00.0   （PCIe，本机）
    slot 1  mode=0x10001  → 0003:31:00.0   （PCIe，本机）
    slot 2  mode=0x20002  → 0004:41:00.0   （PCIe，本机）
    slot 3  mode=0x3      → ETH peer      ← **不是本机卡**

即 PCIe 设备填的是 ``0`` / ``0x10001`` / ``0x20002``（不是
``CommMode.PCIE == 0``），而 ETH 槽位有时填 ``2``（上游能认出）、有时填
``0x3``（``"unknown(3)"``，**上游认不出**）。认不出时的后果实测：

* 对端不应答 → ``nsmi_eth.c`` 的 socket 没有超时 → **永久阻塞**；
* 对端应答 → 回的是**垃圾**（``received error status 344100``、
  ``Data size exceeds max allowed size: 28 > 4``）→ C 层每轮刷 10 行
  ERROR（实测 69 行 / 37 秒），偶尔还被当成温度读出来（显示成 59℃）。

所以补一条**槽位规则**：只探前 N 个槽位，N = 本机 ``/dev/pcie-rkep-*``
节点数（N 和 NSMI 的 PCIe 槽位数一致 —— 同一套 PCIe 枚举）。

另外两条同源的「不发注定失败的命令」：

* **chip 列表为空 → 跳过全部 chip 级命令**（npu 占用 / 显存 / health）。
  原来 fallback 到 ``chip_id = 0`` 照发不误 → 每卡每轮白发 3 条。
* **温度这条路不通 → TTL 内不再发温度命令**（SDK 的回退只在 chip 级
  **成功**时才记住「设备级不通」，chip 级也失败时它每次都重发 cmd 12）。
* **PCIe 相关命令全灭 → 静默期**（期间一个命令都不发）。

本脚本**不占 NPU**：用假的 ``wdnai.RknnSmi`` 截获全部调用。

用法::

    python -m scripts.verify_smi_local_slot_guard
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import types
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.system.api import system_monitor as m  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        print(f"  [OK]   {label}")
    else:
        print(f"  [FAIL] {label}" + (f" — {detail}" if detail else ""))
        FAILS.append(label)


class FakeSmi:
    """假的 ``wdnai.RknnSmi`` —— 记录每个被调用的方法。"""

    _RET = {
        "get_device_info": {"device_id": 0, "product_name": "SA0-F",
                            "manufacturer": "", "serial_num": "60901653"},
        "get_chip_list": [0],
        "get_pcie_info": {"bus_id": "0001:11:00.0"},
        "get_device_temperature": 42.0,
        "get_device_power": 4500.0,
        "get_npu_info": {"util_rate_pct": 10.0},
        "get_memory_info": {"total_size": 5 * 1024 ** 3,
                            "free_size": 4 * 1024 ** 3},
        "get_chip_health": types.SimpleNamespace(name="OK"),
    }

    def __init__(self, devs, calls, *, fail_all=False, empty_chips=False,
                 temp_fails=False):
        self._devs = devs
        self._calls = calls
        self._fail_all = fail_all
        self._empty_chips = empty_chips
        self._temp_fails = temp_fails

    def get_device_list(self):
        return self._devs

    def _record(self, name, *args):
        self._calls.append((name, args))
        if self._fail_all:
            raise RuntimeError(f"rknnsmi {name} failed: TIMEOUT (-7)")
        if name == "get_chip_list" and self._empty_chips:
            return []
        if name == "get_device_temperature" and self._temp_fails:
            raise RuntimeError("rknnsmi get_device_temperature failed: TIMEOUT (-7)")
        return self._RET.get(name, {})

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda *a, **k: self._record(name, *a)


@contextmanager
def _fake_smi(devs, **kw):
    calls: list[tuple[str, tuple]] = []
    fake_mod = types.ModuleType("wdnai")
    fake_mod.RknnSmi = lambda: FakeSmi(devs, calls, **kw)
    prev = sys.modules.get("wdnai")
    sys.modules["wdnai"] = fake_mod
    try:
        yield calls
    finally:
        if prev is None:
            sys.modules.pop("wdnai", None)
        else:
            sys.modules["wdnai"] = prev


#: 本机 3 张 PCIe 卡 + 1 个 mdns 发现的对端机器（mode 认不出来的那种）
DEVS_LOCAL3 = [
    {"index": 0, "mode": "pcie", "global_id": 0, "internal_id": 0},
    {"index": 1, "mode": "unknown(65537)", "global_id": 0, "internal_id": 0},
    {"index": 2, "mode": "unknown(131074)", "global_id": 2, "internal_id": 0},
    {"index": 3, "mode": "unknown(3)", "global_id": 0, "internal_id": 0},
]

#: 只有 1 张 PCIe 卡 + 1 个 ETH peer（上游那个 mode == "eth" 的形态）
DEVS_1PCIE = [
    {"index": 0, "mode": "pcie", "global_id": 0, "internal_id": 0},
    {"index": 1, "mode": "eth", "global_id": 0, "internal_id": 0},
]


def _reset_state() -> None:
    """清掉跨用例的模块级状态。"""
    m._npu_cache, m._npu_cache_ts = None, 0.0
    m._npu_smi_trip_until = 0.0
    m._SMI_QUIET_UNTIL = 0.0
    m._TEMP_STATE.clear()


def _collect() -> list[dict]:
    """跑一轮采集；静默期/熔断期会抛 _SmiTimeout（真实流程由
    _collect_npu_uncached 兜住降级），这里吞掉返回空列表。"""
    try:
        return asyncio.run(m._collect_npu_via_smi())
    except m._SmiTimeout:
        return []


def main() -> int:
    print("== verify_smi_local_slot_guard ==")

    # ---- 1. 槽位规则：mode 认不出的对端机器也不发命令 -------------------
    _reset_state()
    orig_count = m._local_pcie_ep_count
    m._local_pcie_ep_count = lambda: 3          # 本机 3 张 PCIe 卡
    try:
        with _fake_smi(DEVS_LOCAL3) as calls:
            rows = _collect()
    finally:
        m._local_pcie_ep_count = orig_count
    remote_calls = [c for c in calls if c[1] and c[1][0] == 3]
    check(not remote_calls, "槽位 ≥ 本机 EP 数的设备不触发任何 SMI 命令",
          f"remote 调用={remote_calls}")
    remote_rows = [r for r in rows if r.get("index") == 3]
    check(len(remote_rows) == 1 and remote_rows[0]["status"] == "Remote",
          "对端机器仍出现在结果里（标记为 Remote）", f"rows={remote_rows}")
    check(len([r for r in rows if r.get("status") == "Online"]) == 3,
          "本机 3 张 PCIe 卡照常采集", f"rows={rows}")

    # ---- 2. 槽位规则不误伤上游能认出的 eth ---------------------------
    _reset_state()
    m._local_pcie_ep_count = lambda: 1          # 本机 1 张 PCIe 卡
    try:
        with _fake_smi(DEVS_1PCIE) as calls:
            rows = _collect()
    finally:
        m._local_pcie_ep_count = orig_count
    check(not [c for c in calls if c[1] and c[1][0] == 1],
          "mode == eth 的设备仍然一个命令都不发",
          f"calls={[c for c in calls if c[1] and c[1][0] == 1]}")

    # ---- 3. chip 列表为空 → 跳过 chip 级命令 --------------------------
    _reset_state()
    with _fake_smi(DEVS_1PCIE, empty_chips=True) as calls:
        rows = _collect()
    chip_cmds = [c[0] for c in calls
                 if c[0] in ("get_npu_info", "get_memory_info", "get_chip_health")]
    check(not chip_cmds, "chip 列表为空 → chip 级命令一条都不发",
          f"chip 调用={chip_cmds}")
    check(len(rows) == 2, "行数不变（两张卡都在）", f"rows={len(rows)}")

    # ---- 4. 温度不通 → TTL 内不再发温度命令 --------------------------
    _reset_state()
    with _fake_smi(DEVS_1PCIE, temp_fails=True) as calls:
        _collect()
        n_first = len([c for c in calls if c[0] == "get_device_temperature"])
        _collect()
        n_second = len([c for c in calls if c[0] == "get_device_temperature"])
    check(n_first == 1, "第一轮发了温度命令", f"n={n_first}")
    check(n_second == 1, "后面一轮不再发温度命令（TTL 内）", f"n={n_second}")

    # ---- 5. PCIe 命令全灭 → 静默期（一个命令都不发）-------------------
    _reset_state()
    with _fake_smi(DEVS_1PCIE, fail_all=True) as calls:
        _collect()
        n_first = len(calls)
        _collect()
        n_second = len(calls)
    check(n_first > 0, "第一轮确实发了命令", f"calls={n_first}")
    check(n_second == n_first, "静默期内一个命令都不再发",
          f"第一轮 {n_first} → 第二轮共 {n_second}")

    # ---- 6. 静默期内降级返回（带原因，不挂起）-------------------------
    _reset_state()
    m._SMI_QUIET_UNTIL = time.monotonic() + 60
    t0 = time.perf_counter()
    try:
        res = asyncio.run(m._collect_npu_uncached())
    finally:
        m._SMI_QUIET_UNTIL = 0.0
    dt = time.perf_counter() - t0
    check(dt < 0.5, f"静默期内立即返回（实测 {dt * 1000:.0f}ms）", f"{dt:.3f}s")
    check(res.get("available") is False, "静默期内降级为 available=False",
          f"res={res.get('available')}")
    check("无响应" in (res.get("reason") or ""), "降级结果说明原因",
          f"reason={res.get('reason')!r}")

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} 条")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
