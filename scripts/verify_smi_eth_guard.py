"""回归防护：SMI 采集**不能**被远程（ETH）设备拖死，NPU 遥测要能降级。

背景（2026-11-03 实测，RK3588 真机）：``/api/system-monitor`` 60s 超时都
不返回，``py-spy dump`` 抓到 6 个线程全停在 ``get_device_info`` 的
``device_id=2`` —— SMI 设备表是 ``[0:pcie, 1:pcie, 2:eth]``，那个 eth 是
经网口接入的另一台 RK 板。``rknnsmi_get_device_info(2)`` **永不返回**
（C 层无超时）。

而它跑在 ``asyncio.to_thread`` 里 —— 用的是 event loop 的默认线程池（上限
12）。前端每 3s 轮询一次 → 每轮漏一个永久卡死的线程 → 约 36s 内默认池
被占满 → 全站所有 ``to_thread`` 一起排队阻塞。

修复四层（``app/system/api/system_monitor.py``）：

  1. 主修：``_smi_is_remote()`` 认出 ETH 设备 → 一个 SMI 命令都不发；
  2. 兜底：专用线程池（不占全站默认池）+ 整体超时 → 降级返回，不挂起；
  3. 熔断：超时后一段时间内不再提交新任务（卡死的线程杀不掉）；
  4. 缓存：NPU 遥测 10s TTL —— 慢变量，不该每 3s 轮询都真采。

本脚本**不占 NPU**：用假的 ``wdnai.RknnSmi`` 截获全部调用。

  1. ETH 设备**不触发任何** SMI 命令；
  2. PCIe 设备照常采集遥测；
  3. 第二次采集命中缓存（不再调 SMI）；
  4. SMI 挂死时端点**降级返回**而不是挂起，熔断期内立即返回。

用法::

    python -m scripts.verify_smi_eth_guard
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import types
from contextlib import contextmanager
from pathlib import Path

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
    """假的 ``wdnai.RknnSmi`` —— 记录每个被调用的方法。

    返回值按各方法的真实形状给（power 是数字、chip_list 是列表…），
    免得 fake 自己的类型错误把被测逻辑淹掉。
    """

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

    def __init__(self, devs, calls, hang: bool = False):
        self._devs = devs
        self._calls = calls
        self.hang = hang

    def get_device_list(self):
        return self._devs

    def _record(self, name, *args):
        self._calls.append((name, args))
        if self.hang:
            # 模拟 C 层不返回（真实情况是永久阻塞）。睡 6s：比测试用的
            # 1s 超时长，又不至于把脚本拖太久。
            time.sleep(6)
        return self._RET.get(name, {})

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda *a, **k: self._record(name, *a)


@contextmanager
def _fake_smi(devs, hang: bool = False):
    """装上假的 ``wdnai.RknnSmi``，退出时还原。yield 记录到的调用列表。"""
    calls: list[tuple[str, tuple]] = []
    fake_mod = types.ModuleType("wdnai")
    fake_mod.RknnSmi = lambda: FakeSmi(devs, calls, hang=hang)
    prev = sys.modules.get("wdnai")
    sys.modules["wdnai"] = fake_mod
    try:
        yield calls
    finally:
        if prev is None:
            sys.modules.pop("wdnai", None)
        else:
            sys.modules["wdnai"] = prev


DEVS = [
    {"index": 0, "mode": "pcie", "global_id": 0, "internal_id": 0},
    {"index": 1, "mode": "pcie", "global_id": 1, "internal_id": 1},
    {"index": 2, "mode": "eth", "global_id": 0, "internal_id": 0},
]


def main() -> int:
    print("== verify_smi_eth_guard ==")

    # ---- 1. mode 判定
    check(m._smi_is_remote("eth") is True, "eth → 远程")
    check(m._smi_is_remote("ETH") is True, "ETH（大小写）→ 远程")
    check(m._smi_is_remote("pcie") is False, "pcie → 非远程")
    check(m._smi_is_remote("usb") is False, "usb → 非远程")
    check(m._smi_is_remote(None) is False, "None → 非远程")
    check(m._smi_is_remote("") is False, "空串 → 非远程")

    # ---- 2. ETH 设备不发任何 SMI 命令
    with _fake_smi(DEVS) as calls:
        rows = asyncio.run(m._collect_npu_via_smi())

    eth_calls = [c for c in calls if c[1] and c[1][0] == 2]
    check(len(eth_calls) == 0, "ETH 设备不触发任何 SMI 命令", f"eth 调用={eth_calls}")
    eth_rows = [r for r in rows if r.get("type") == "eth"]
    check(len(eth_rows) == 1 and eth_rows[0]["status"] == "Remote",
          "ETH 设备仍出现在结果里（标记为 Remote）", f"eth_rows={eth_rows}")
    check(len([r for r in rows if r.get("type") == "pcie"]) == 2,
          "PCIe 设备照常出现在结果里")
    pcie_dev_ids = {c[1][0] for c in calls if c[1]}
    check(pcie_dev_ids <= {0, 1}, "只有 PCIe 设备被发了命令",
          f"被调用的 device_id={sorted(pcie_dev_ids)}")

    # ---- 3. 缓存命中
    m._npu_cache, m._npu_cache_ts = None, 0.0
    with _fake_smi(DEVS) as calls:
        asyncio.run(m._collect_npu())
        n_first = len(calls)
        asyncio.run(m._collect_npu())
        n_second = len(calls)
    check(n_first > 0, "第一次采集真的调了 SMI", f"calls={n_first}")
    check(n_second == n_first, "第二次命中缓存（不再调 SMI）",
          f"第一次 {n_first} 次 → 第二次共 {n_second} 次")

    # ---- 4. SMI 挂死 → 降级返回，不挂起
    old_timeout, m._NPU_SMI_TIMEOUT_S = m._NPU_SMI_TIMEOUT_S, 1.0
    m._npu_smi_trip_until = 0.0
    try:
        with _fake_smi(DEVS, hang=True):
            t0 = time.perf_counter()
            res = asyncio.run(m._collect_npu_uncached())
            dt = time.perf_counter() - t0
    finally:
        m._NPU_SMI_TIMEOUT_S = old_timeout
    check(dt < 5.0, f"SMI 挂死时在超时内返回（实测 {dt:.2f}s）", f"{dt:.2f}s")
    check(res.get("available") is False, "SMI 挂死时降级为 available=False",
          f"res={res.get('available')}")
    check("超时" in (res.get("reason") or ""), "降级结果带超时原因",
          f"reason={res.get('reason')!r}")

    # ---- 5. 超时后熔断打开 → 后续请求**立即**降级（不再等一轮超时）
    try:
        with _fake_smi(DEVS, hang=True):
            t0 = time.perf_counter()
            res2 = asyncio.run(m._collect_npu_uncached())
            dt2 = time.perf_counter() - t0
    finally:
        m._NPU_SMI_TIMEOUT_S = old_timeout
        m._npu_smi_trip_until = 0.0
    check(dt2 < 0.5, f"熔断期内立即降级返回（实测 {dt2 * 1000:.0f}ms）",
          f"{dt2 * 1000:.0f}ms")
    check(res2.get("available") is False and "熔断" in (res2.get("reason") or ""),
          "熔断期返回降级结果并说明原因", f"res={res2}")

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} 条")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
