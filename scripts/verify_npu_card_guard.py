#!/usr/bin/env python3
"""回归：启模型前的**卡位校验**（npu_index 越界 / 不是算力卡位）。

背景（v0.29 补）：``POST /api/compute/instances`` 以前只校验
``npu_index >= 0``，**越界的卡号照样往下走** —— 单卡板上传 ``npu=1``，
worker 侧 ``LLMSession(model, npu=1)`` 抛 ``IndexError: npu=1 out of
range``，进程秒退，registry 里留下一个**起不来也删不掉**的僵尸实例
（status=error 却占着卡位），后续启停全被它堵在「过渡态排队」上
（120s → 409 → 向导「一键开启」永远转圈）。

两组断言：

  U1 ``ComputeRegistry.npu_card_error()`` —— 各种卡位布局下的判定
     （越界 / 非算力卡位 / 被 card_count 限掉 / 探测失败不拦）。
  U2 API 层 ``start_instance`` —— 越界必须**立刻 400**，而且
     **不进入过渡态排队**（排队要白等 120s）。

不起服务、不占 NPU、秒级跑完。

用法：.venv/bin/python scripts/verify_npu_card_guard.py
"""
from __future__ import annotations

import asyncio
import contextlib
import sys
import threading

sys.path.insert(0, ".")

from fastapi import HTTPException  # noqa: E402

import app.core.compute.registry as regmod  # noqa: E402
import app.system.api.compute as capi  # noqa: E402
from app.core.compute.registry import ComputeRegistry  # noqa: E402

FAILED: list[str] = []
PASSED = 0

# PCI BDF 形态 = 能跑 rknn 模型的卡；USB 序列号形态 = RGA/VPU 设备。
PCI0 = "0000:01:00.0"
PCI1 = "0000:02:00.0"
USB0 = "180B6D6C00000001"


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"  ✓ {name}" + (f"  ({detail})" if detail else ""))
    else:
        FAILED.append(name)
        print(f"  ✗ {name}" + (f"  ({detail})" if detail else ""))


def make_reg(cards, failed: str = "") -> ComputeRegistry:
    """造一个**不跑 __init__** 的 registry（__init__ 会起 DDS hub）。

    ``list_cards()`` 命中 ``self._cards`` 缓存就直接返回，不会去碰
    /dev/rknn3 —— 所以整个脚本不占硬件、不起服务。

    ``cards`` 为空时**必须**同时把 ``_rknn_probe`` 填上：``list_cards()``
    的缓存判断是 ``if self._cards and ...``，空列表会当成「没缓存」→
    真去探硬件 → 单测会打到真板子。
    """
    reg = ComputeRegistry.__new__(ComputeRegistry)
    reg._lock = threading.RLock()
    reg._cards = [dict(c) for c in cards]
    reg._cards_failed = failed
    reg._rknn_probe = object() if not cards else None
    reg._rknn_device_ids = [] if not cards else None
    return reg


def card(idx: int, dev: str) -> dict:
    return {"npu_index": idx, "device_id": dev}


# --------------------------------------------------------------------------- #
# U1 registry 层的判定
# --------------------------------------------------------------------------- #
def scen_u1():
    print("\n=== U1 npu_card_error() 的判定 ===")

    # (a) 双卡板，两张都是算力卡
    reg = make_reg([card(0, PCI0), card(1, PCI1)])
    check("U1 算力卡位认得两张卡", reg.model_cards() == [0, 1],
          str(reg.model_cards()))
    check("U1 npu0 通过", reg.npu_card_error(0) is None)
    check("U1 npu1 通过", reg.npu_card_error(1) is None)
    err2 = reg.npu_card_error(2)
    check("U1 npu2 判越界", bool(err2) and "越界" in err2, str(err2))
    check("U1 越界原因里带上可用卡号",
          bool(err2) and "0" in err2 and "1" in err2, str(err2))
    err_neg = reg.npu_card_error(-1)
    check("U1 负卡号也判越界", bool(err_neg) and "越界" in err_neg, str(err_neg))
    check("U1 非整数判非法",
          bool(reg.npu_card_error("0")) and "整数" in reg.npu_card_error("0"))

    # (b) 单卡板 —— 真机就是这个布局（RK1828 一张）
    reg1 = make_reg([card(0, PCI0)])
    check("U1 单卡板 npu0 通过", reg1.npu_card_error(0) is None)
    err1 = reg1.npu_card_error(1)
    check("U1 单卡板 npu1 判越界（真机踩过的那条）",
          bool(err1) and "越界" in err1, str(err1))
    check("U1 越界原因说明本机只有 1 张卡",
          bool(err1) and "1 张卡" in err1, str(err1))

    # (c) 卡在，但不是算力卡位（RGA/VPU 设备，USB 序列号形态）
    regm = make_reg([card(0, USB0), card(1, PCI1)])
    check("U1 混合卡只认 PCI 形态", regm.model_cards() == [1],
          str(regm.model_cards()))
    errm = regm.npu_card_error(0)
    check("U1 非算力卡位单独提示", bool(errm) and "不是算力卡位" in errm,
          str(errm))
    check("U1 非算力卡位不是「越界」", bool(errm) and "越界" not in errm,
          str(errm))
    check("U1 真算力卡仍通过", regm.npu_card_error(1) is None)

    # (d) compute.card_count 把可用卡位裁短（双卡板模拟单卡板）
    reg2 = make_reg([card(0, PCI0), card(1, PCI1)])
    orig = regmod.card_count_override
    regmod.card_count_override = lambda: 1
    try:
        errc = reg2.npu_card_error(1)
        check("U1 card_count 限掉的卡位单独提示",
              bool(errc) and "card_count" in errc, str(errc))
        check("U1 card_count=1 时 npu0 仍通过", reg2.npu_card_error(0) is None)
    finally:
        regmod.card_count_override = orig

    # (e) 探测失败 —— **不拦截**（/dev/rknn3 被 worker 占着是常态）
    regf = make_reg([], failed="RKNN probe timeout after 5.0s")
    check("U1 探测失败时不做越界拦截", regf.npu_card_error(99) is None)

    # (f) 探测成功但一张卡都没有
    regz = make_reg([])
    errz = regz.npu_card_error(0)
    check("U1 无卡时报错", bool(errz) and "没有探测到任何 NPU 卡" in errz,
          str(errz))


# --------------------------------------------------------------------------- #
# U2 API 层：越界必须立刻 400，且不排队
# --------------------------------------------------------------------------- #
class StubInst:
    """最小实例桩 —— start_instance 会读它的 npu_index / status。"""

    def __init__(self, npu_index: int = -1, status: str = "not_loaded"):
        self.npu_index = npu_index
        self.status = status
        self._source = "spawned"

    def to_dict(self):
        return {"model_id": "qwen3-vl-4b", "npu_index": self.npu_index,
                "status": self.status}


class StubReg:
    """最小 registry 桩 —— 只实现 start_instance 用到的那几个方法。"""

    def __init__(self, bad_npu: int):
        self.bad_npu = bad_npu
        self.polls = 0
        self.spawns = 0
        self.binds = 0

    def npu_card_error(self, npu_index):
        if npu_index == self.bad_npu:
            return f"npu_index={npu_index} 越界：本机只有 1 张卡（可用卡号 0~0）"
        return None

    def transition_cards(self, model_id, local_only=False):
        self.polls += 1
        return []

    def ensure_worker_for_model(self, model_id, allow_adopt=False,
                                npu_index=None, eager_load=False):
        self.spawns += 1
        if npu_index == self.bad_npu:
            raise AssertionError("越界卡号不该走到 spawn")
        return StubInst()

    def bind_instance(self, inst, n):
        self.binds += 1
        return inst

    @contextlib.contextmanager
    def model_op_lock(self, model_id):
        yield

    def snapshot(self):
        return {"instances": []}


def scen_u2():
    print("\n=== U2 API 层：越界 → 立刻 400，不排队 ===")
    reg = StubReg(bad_npu=1)
    capi.get_registry = lambda: reg
    req = capi.InstanceRequest(model="qwen3-vl-4b", npu_index=[1])
    try:
        asyncio.run(capi.start_instance(req))
        check("U2 越界抛 HTTPException", False, "没抛异常")
    except HTTPException as e:
        check("U2 越界是 400", e.status_code == 400, f"status={e.status_code}")
        check("U2 400 的原因带得上卡号",
              "npu_index=1" in str(e.detail), str(e.detail))
    check("U2 越界没有进过渡态排队（不白等 120s）", reg.polls == 0,
          f"polls={reg.polls}")
    check("U2 越界没有 spawn worker", reg.spawns == 0, f"spawns={reg.spawns}")
    check("U2 越界没有 bind", reg.binds == 0, f"binds={reg.binds}")

    # 合法卡号照常走（别把守卫整个关掉）
    reg2 = StubReg(bad_npu=1)
    capi.get_registry = lambda: reg2
    req2 = capi.InstanceRequest(model="qwen3-vl-4b", npu_index=[0])
    try:
        asyncio.run(capi.start_instance(req2))
        check("U2 合法卡号照常进流程", reg2.polls >= 1, f"polls={reg2.polls}")
    except HTTPException as e:
        check("U2 合法卡号照常进流程", False, f"HTTP {e.status_code} {e.detail}")


def main() -> int:
    scen_u1()
    scen_u2()
    print("\n" + "=" * 60)
    print(f"通过 {PASSED} / {PASSED + len(FAILED)}")
    if FAILED:
        print(f"❌ 失败: {FAILED}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
