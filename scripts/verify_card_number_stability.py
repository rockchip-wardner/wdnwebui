#!/usr/bin/env python3
"""回归：**算力卡号（npu_index）在对端机器上下线时保持稳定**。

背景（真机踩到）
----------------
``RKNN3Lite.get_devices_id()`` 返回的驱动枚举列表里混着三类设备：

* 本机 PCIe EP 协卡 —— device_id 是 PCI BDF（``0001:11:00.0``）
* mdns 发现的对端机器 —— device_id 是 ``IP:端口``（``172.16.9.139:11900``）
* 本机 RGA / VPU 设备 —— device_id 是 USB 序列号形态

对端机器**随时会上下线**（实验室里另一台板子，IP 还从 .139 变成过 .159）。
它一旦在，就占住列表最前面的下标 —— 本机协卡的下标整体后移 1。

旧实现直接拿驱动枚举下标当「卡号」，于是：

* ``config.jsonc`` 的 ``compute.autostart[].npu_index``（用户写死的稳定
  句柄）在对端上线后**失效** → 那条 autostart 被
  ``_crop_autostart_for_cards`` 静默丢弃（只留一行 INFO 日志）→ **少起
  一个实例**，用户在 UI 上完全看不出来；
* 更糟的是主进程与 worker 各枚举各的，枚举结果不一致时，**worker 会把
  模型加载到另一张卡上**（最坏情况：加载到远端板子）。

修复：卡号 = **本机 EP 协卡按 PCI BDF 排序后的序号**（0 起），规则只有
``app/core/compute/cards.py`` 一处实现，主进程 / worker / 系统监控共用。

断言分五组：

  A 卡号规则本身（peer 在 / 不在 → 卡号与卡→设备映射完全一致）
  B registry 的枚举 → 编号（``list_cards`` / ``model_cards``）
  C autostart 裁剪（peer 在场时 npu0/1 **不再被丢弃**；真越界仍被裁）
  D worker 按卡号选卡（两种枚举解析到**同一张物理卡**）
  E 系统监控的卡号对齐（``bus_id → 卡号`` 在两种枚举下一致）

不起服务、不占 NPU、秒级跑完。

用法：.venv/bin/python scripts/verify_card_number_stability.py
"""
from __future__ import annotations

import inspect
import logging
import sys
import threading

sys.path.insert(0, ".")

from app.core.compute import cards as cards_mod  # noqa: E402
from app.core.compute.registry import ComputeRegistry  # noqa: E402
from app.core.compute.workers import _base as worker_base  # noqa: E402
from app.system.api import system_monitor  # noqa: E402

FAILED: list[str] = []
PASSED = 0

# 真机形态：3 张本机 EP 协卡 + 1 台 mdns 对端机器 + 3 个 RGA/VPU 设备。
PCI0 = b"0001:11:00.0"
PCI1 = b"0003:31:00.0"
PCI2 = b"0004:41:00.0"
PEER = b"172.16.9.139:11900"
PEER2 = b"172.16.9.159:11900"
RGA0 = b"180B6D6C67616C6161535051564B4349"
RGA1 = b"180B6D626F646C62665350515644787F"

#: 对端离线 / 对端在线（插在列表最前面）两种枚举。
ENUM_NO_PEER = [PCI0, PCI1, PCI2, RGA0, RGA1]
ENUM_PEER = [PEER, PCI0, PCI1, PCI2, RGA0, RGA1]
#: 对端在线且 IP 变了（.139 → .159）—— 证明编号不依赖对端的具体身份。
ENUM_PEER2 = [PEER2, PCI0, PCI1, PCI2, RGA0, RGA1]


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"  ✓ {name}" + (f"  ({detail})" if detail else ""))
    else:
        FAILED.append(name)
        print(f"  ✗ {name}" + (f"  ({detail})" if detail else ""))


def make_reg(devs) -> ComputeRegistry:
    """造一个**不跑 __init__** 的 registry，枚举结果由 ``devs`` 桩掉。

    ``list_cards()`` 的缓存分支是 ``if self._cards and ...`` —— 空列表
    会被当成「没缓存」；所以这里让 ``_cards`` 为空、``_rknn_probe`` 非
    None、``_rknn_device_ids`` 给桩值 → 既不碰 /dev/rknn3 也不起 DDS。
    """
    reg = ComputeRegistry.__new__(ComputeRegistry)
    reg._lock = threading.RLock()
    reg._cards = []
    reg._cards_failed = ""
    reg._rknn_probe = object()
    reg._rknn_device_ids = list(devs)
    reg._autostart_skipped = []
    return reg


# --------------------------------------------------------------------------- #
# A 卡号规则
# --------------------------------------------------------------------------- #
def scen_a():
    print("\n=== A 卡号规则：peer 在 / 不在，编号必须一致 ===")

    ids_no = cards_mod.local_ep_device_ids(ENUM_NO_PEER)
    ids_pe = cards_mod.local_ep_device_ids(ENUM_PEER)
    ids_pe2 = cards_mod.local_ep_device_ids(ENUM_PEER2)

    check("A1 对端离线数出 3 张协卡", ids_no == ["0001:11:00.0", "0003:31:00.0",
                                                "0004:41:00.0"], str(ids_no))
    check("A2 对端在线仍是同样 3 张、同样顺序", ids_pe == ids_no, str(ids_pe))
    check("A3 对端换 IP 也不影响", ids_pe2 == ids_no, str(ids_pe2))
    check("A4 卡号恒为 0..N-1",
          cards_mod.card_numbers(ENUM_PEER) == [0, 1, 2],
          str(cards_mod.card_numbers(ENUM_PEER)))
    check("A5 对端形态不是协卡", not cards_mod.is_ep_device_id(PEER))
    check("A6 RGA/VPU 形态不是协卡", not cards_mod.is_ep_device_id(RGA0))
    check("A7 对端形态能认出是远端", cards_mod.is_remote_endpoint(PEER))

    # 卡号 → 设备：两种枚举下必须指向同一张物理卡
    same = all(
        cards_mod.card_no_to_device_id(ENUM_NO_PEER, n)
        == cards_mod.card_no_to_device_id(ENUM_PEER, n)
        == cards_mod.card_no_to_device_id(ENUM_PEER2, n)
        for n in range(3)
    )
    check("A8 卡号→设备在三种枚举下一致", same,
          str([cards_mod.card_no_to_device_id(ENUM_PEER, n) for n in range(3)]))

    # 驱动原始下标确实会漂 —— 这正是必须用卡号的原因
    raw_no = cards_mod.card_no_to_raw_index(ENUM_NO_PEER, 0)
    raw_pe = cards_mod.card_no_to_raw_index(ENUM_PEER, 0)
    check("A9 驱动原始下标确实会漂（所以不能拿它当卡号）",
          raw_no != raw_pe, f"无对端={raw_no} 有对端={raw_pe}")
    check("A10 越界卡号解析不出设备",
          cards_mod.card_no_to_device_id(ENUM_PEER, 9) is None)


# --------------------------------------------------------------------------- #
# B registry 的枚举 → 编号
# --------------------------------------------------------------------------- #
def scen_b():
    print("\n=== B registry.list_cards() 的编号 ===")

    r_no = make_reg(ENUM_NO_PEER)
    r_pe = make_reg(ENUM_PEER)

    c_no = [(c["npu_index"], c["device_id"]) for c in r_no.list_cards()]
    c_pe = [(c["npu_index"], c["device_id"]) for c in r_pe.list_cards()]
    check("B1 对端在与否，卡号→设备映射完全相同", c_no == c_pe, f"{c_no} vs {c_pe}")
    check("B2 cards 只含本机协卡（无 RGA/VPU、无对端）",
          all(cards_mod.is_ep_device_id(d) for _, d in c_pe), str(c_pe))
    check("B3 model_cards 恒为 [0,1,2]",
          r_no.model_cards() == [0, 1, 2] and r_pe.model_cards() == [0, 1, 2],
          f"{r_no.model_cards()} / {r_pe.model_cards()}")
    check("B4 card#0 始终是 0001:11:00.0",
          r_pe.list_cards()[0]["device_id"] == "0001:11:00.0")
    check("B5 raw_index 记录了驱动下标（诊断用）",
          r_pe.list_cards()[0]["raw_index"] == 1,
          str(r_pe.list_cards()[0]["raw_index"]))
    check("B6 card_device_id() 能按卡号取到物理身份",
          r_pe.card_device_id(1) == "0003:31:00.0", str(r_pe.card_device_id(1)))
    check("B7 card_device_id() 对越界卡号返回 None",
          r_pe.card_device_id(9) is None)


# --------------------------------------------------------------------------- #
# C autostart 裁剪
# --------------------------------------------------------------------------- #
def scen_c():
    print("\n=== C autostart：不再静默丢实例 ===")

    entries = [{"model": "DepthAnythingV3", "npu_index": 0},
               {"model": "DepthAnythingV3", "npu_index": 1}]

    # 对端在线 —— 旧实现在这里会丢掉 npu0
    r_pe = make_reg(ENUM_PEER)
    kept = r_pe._crop_autostart([dict(e) for e in entries])
    check("C1 对端在线时两条 autostart 都保留（修复前会丢 npu0）",
          len(kept) == 2, str(kept))
    check("C2 没有条目被记为跳过", r_pe._autostart_skipped == [],
          str(r_pe._autostart_skipped))

    r_no = make_reg(ENUM_NO_PEER)
    kept_no = r_no._crop_autostart([dict(e) for e in entries])
    check("C3 对端离线结果相同", kept_no == kept, str(kept_no))

    # 真越界：卡号 9 不存在 → 必须被裁掉，且**要留痕**
    r_bad = make_reg(ENUM_PEER)
    kept_bad = r_bad._crop_autostart([{"model": "DepthAnythingV3",
                                        "npu_index": 9}])
    check("C4 越界卡号被裁掉", kept_bad == [], str(kept_bad))
    check("C5 被裁条目被记录（供 UI 提示）",
          len(r_bad._autostart_skipped) == 1
          and r_bad._autostart_skipped[0]["npu_index"] == 9,
          str(r_bad._autostart_skipped))
    check("C6 记录里带中文原因",
          bool(r_bad._autostart_skipped
               and "算力卡位" in r_bad._autostart_skipped[0]["reason"]),
          str(r_bad._autostart_skipped))
    # snapshot 里必须真的带上这个字段（静态断言：stub registry 跑不起
    # 完整 snapshot —— 它要 instances / tools / capabilities 全套状态）。
    src = inspect.getsource(ComputeRegistry.snapshot)
    check("C7 snapshot 暴露 autostart_skipped",
          '"autostart_skipped"' in src)

    # 同卡两个模型 → 也要被裁掉并留痕
    r_dup = make_reg(ENUM_PEER)
    kept_dup = r_dup._crop_autostart([
        {"model": "A", "npu_index": 0},
        {"model": "B", "npu_index": 0},
    ])
    check("C8 同卡两个模型只留一条", len(kept_dup) == 1, str(kept_dup))
    check("C9 同卡冲突也留痕",
          len(r_dup._autostart_skipped) == 1, str(r_dup._autostart_skipped))

    # 越界卡号在 API 层仍被拒（别把守卫整个关掉）
    r_g = make_reg(ENUM_PEER)
    err = r_g.npu_card_error(9)
    check("C10 越界卡号仍被拒", bool(err) and "越界" in err, str(err))
    check("C11 合法卡号仍通过", r_g.npu_card_error(0) is None)


# --------------------------------------------------------------------------- #
# D worker 按卡号选卡
# --------------------------------------------------------------------------- #
def scen_d():
    print("\n=== D worker：按卡号解析到同一张物理卡 ===")

    orig_probe = worker_base._probe_devices

    def stub(devs):
        return lambda: list(devs)

    try:
        # (1) 主进程把物理身份一起发过来 → worker 直接按身份选卡
        kw, desc = worker_base._resolve_card_targets([1], "0003:31:00.0")
        check("D1 有 device_id 时按物理身份选卡",
              kw.get("device_id") == "0003:31:00.0", f"{kw} / {desc}")

        # (2) 只有卡号 → worker 自己枚举（两种枚举）→ 必须解析到同一张卡
        worker_base._probe_devices = stub(ENUM_NO_PEER)
        kw_no, _ = worker_base._resolve_card_targets([1], None)
        worker_base._probe_devices = stub(ENUM_PEER)
        kw_pe, _ = worker_base._resolve_card_targets([1], None)
        check("D2 只有卡号时 worker 自己解析出 device_id",
              kw_no.get("device_id") == "0003:31:00.0"
              and kw_pe.get("device_id") == "0003:31:00.0",
              f"{kw_no} / {kw_pe}")
        check("D3 两种枚举解析到同一张卡",
              kw_no.get("device_id") == kw_pe.get("device_id"))

        # (3) 多卡：卡号 → 本进程原始下标（peer 在线时下标整体后移 1）
        worker_base._probe_devices = stub(ENUM_PEER)
        kw_m, desc_m = worker_base._resolve_card_targets([0, 1], None)
        check("D4 多卡换算成本进程原始下标",
              kw_m.get("npu_indices") == [1, 2], f"{kw_m} / {desc_m}")
        worker_base._probe_devices = stub(ENUM_NO_PEER)
        kw_m2, _ = worker_base._resolve_card_targets([0, 1], None)
        check("D4b 对端离线时多卡下标不同但指向同样的物理卡",
              kw_m2.get("npu_indices") == [0, 1], f"{kw_m2}")

        # (4) 卡号在 worker 侧不存在 → 明确报错，不静默换卡
        worker_base._probe_devices = stub([PCI0])
        try:
            worker_base._resolve_card_targets([2], None)
            check("D5 卡号不存在时报错而非换卡", False, "没抛异常")
        except RuntimeError as e:
            check("D5 卡号不存在时报错而非换卡", "不存在" in str(e), str(e))

        # (5) 枚举整体失败 → 退回按卡号直传（此时 wdnai 自己也会用同一份
        #     枚举，报错更贴近原文），但必须留 WARNING
        worker_base._probe_devices = lambda: None
        kw_f, desc_f = worker_base._resolve_card_targets([0], None)
        check("D6 枚举失败时退回按卡号直传",
              kw_f.get("npu") == 0, f"{kw_f} / {desc_f}")
    finally:
        worker_base._probe_devices = orig_probe


# --------------------------------------------------------------------------- #
# E 系统监控的卡号对齐
# --------------------------------------------------------------------------- #
def scen_e():
    print("\n=== E 系统监控：bus_id → 卡号 ===")

    orig = system_monitor._wdnai_dev_ids
    try:
        system_monitor._wdnai_dev_ids = list(ENUM_NO_PEER)
        idx_no = system_monitor._wdnai_canonical_device_index("0003:31:00.0")
        system_monitor._wdnai_dev_ids = list(ENUM_PEER)
        idx_pe = system_monitor._wdnai_canonical_device_index("0003:31:00.0")
        system_monitor._wdnai_dev_ids = list(ENUM_PEER2)
        idx_pe2 = system_monitor._wdnai_canonical_device_index("0003:31:00.0")
        check("E1 对端在与否，bus_id→卡号一致",
              idx_no == idx_pe == idx_pe2 == 1, f"{idx_no}/{idx_pe}/{idx_pe2}")
        check("E2 第一张卡是 #0",
              system_monitor._wdnai_canonical_device_index("0001:11:00.0") == 0)
        check("E3 未知 bus_id 返回 None",
              system_monitor._wdnai_canonical_device_index("dead:be:00.0") is None)
        check("E4 对端自身不是协卡 → None",
              system_monitor._wdnai_canonical_device_index("172.16.9.139:11900")
              is None)
    finally:
        system_monitor._wdnai_dev_ids = orig


def main() -> int:
    logging.basicConfig(level=logging.ERROR)
    scen_a()
    scen_b()
    scen_c()
    scen_d()
    scen_e()
    print("\n" + "=" * 60)
    print(f"通过 {PASSED} / {PASSED + len(FAILED)}")
    if FAILED:
        print(f"❌ 失败: {FAILED}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
