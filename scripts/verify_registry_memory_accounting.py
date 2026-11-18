#!/usr/bin/env python3
"""回归：``registry.snapshot()`` 的显存统计**必须只算本板**，不算远端 peer。

背景（2026-11-18 真机现场）
--------------------------
本板 3 张 1828 **全空**、零本地 worker，但 ``GET /api/compute`` 报::

    per_card_memory_mb: {"0": 3000}
    total_memory_mb:    3000

那 3000 MB 是**别的板子**（peer，``172.16.9.147``）通过 DDS 暴露的
``DepthAnythingV3`` —— 它的 ``npu_index`` 是**对端板自己的卡号**，它的
显存吃在**对端板**上，本板一字节都没用。

根因：``snapshot()`` 算显存时**只看 ``status == "running"``，没看
``_source``** ⇒ peer 的实例被当成本板实例记进了卡位。

⚠️ 这不是"显示问题"：**网页翻译卡**的槽位规划当时就栽在这上面 ——
peer 报 ``npu=1 / 5500MB`` ⇒ 本板卡 1 被判占满（5500+2200 > 5120）⇒
翻译模型只起卡 0 的 2 份（而不是 4 份），日志还打"卡 [1] 放不下翻译
模型（被别的模型占着）"。卡片侧已经修（``web_translate/service.py::
_local_card_occupancy``），**后端快照这侧一直没修**（AGENTS.md 记着
"还没修"）。

⇒ 本脚本断言两件事：

1. **规则本身**（``_local_memory_usage``）—— peer 不占本板卡位、本机孤儿
   （``adopted_local``）要占、多卡实例要平摊、非 running 不计；
2. **端到端**（真的 ``ComputeRegistry.snapshot()``）—— API 出参与规则一致。

另外带一条**反向验证**：按**修复前**的口径（不看 ``_source``）再算一遍，
结果必须与正确口径**不同** —— 证明这些断言真的能抓住那个 bug，而不是
"怎么算都对"。

用法::

    .venv/bin/python scripts/verify_registry_memory_accounting.py
"""
from __future__ import annotations

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_PASS = 0
_FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  ok   {name}")
    else:
        _FAIL += 1
        print(f"  FAIL {name} {extra}")


# --------------------------------------------------------------------------- #
# 假实例
# --------------------------------------------------------------------------- #
def inst(source: str, npu: int, mem_mb: int, cards=None, status: str = "running",
         model_id: str = "DepthAnythingV3") -> dict:
    """造一条快照里的实例记录（字段与 ``ModelInstance.to_dict()`` 对齐）。"""
    return {
        "model_id": model_id,
        "label": model_id,
        "npu_index": npu,
        "npu_indices": list(cards) if cards is not None else [npu],
        "status": status,
        "memory_estimate_mb": mem_mb,
        "_source": source,
        "_peer_ip": "172.16.9.147" if source == "adopted" else "",
    }


def naive_usage(instances) -> dict:
    """**修复前**的口径：只看 ``status == "running"``，不看 ``_source``。

    留着它是为了做反向验证 —— 它必须与正确口径算出**不同的**结果，
    否则说明这个回归脚本抓不住任何东西。
    """
    per_card: dict[int, int] = {}
    total = 0
    for i in instances:
        if i.get("status") != "running":
            continue
        npu = i.get("npu_index")
        per_card[npu] = per_card.get(npu, 0) + int(i.get("memory_estimate_mb") or 0)
        total += int(i.get("memory_estimate_mb") or 0)
    return per_card


# --------------------------------------------------------------------------- #
# [1]~[7] 规则本身
# --------------------------------------------------------------------------- #
def test_rule() -> None:
    from app.core.compute.registry import _local_memory_usage

    # ---------------------------------------------------------------- [1] --
    print("\n[1] 只有远端 peer ⇒ 本板一张卡都不占，显存进 remote")
    peer = inst("adopted", 1, 5500, model_id="LocateAnything-3b")
    per_card, total, remote = _local_memory_usage([peer])
    check("per_card_memory_mb 为空", per_card == {}, f"per_card={per_card}")
    check("total_memory_mb 为 0", total == 0, f"total={total}")
    check("remote_memory_mb = 5500（信息不丢）", remote == 5500, f"remote={remote}")

    # ---------------------------------------------------------------- [2] --
    print("\n[2] peer 报 npu=0 时也一样不占本板卡 0")
    peer0 = inst("adopted", 0, 5500, model_id="LocateAnything-3b")
    per_card, total, remote = _local_memory_usage([peer0])
    check("本板卡 0 依然算空", per_card == {}, f"per_card={per_card}")
    check("total 依然为 0", total == 0, f"total={total}")

    # ---------------------------------------------------------------- [3] --
    print("\n[3] 本机 spawned 实例占卡（本机显存是真占的）")
    local = inst("spawned", 0, 3500, model_id="qwen3.5-vl-4b")
    per_card, total, remote = _local_memory_usage([local])
    check("卡 0 = 3500", per_card == {0: 3500}, f"per_card={per_card}")
    check("total = 3500", total == 3500, f"total={total}")
    check("remote = 0", remote == 0, f"remote={remote}")

    # ---------------------------------------------------------------- [4] --
    print("\n[4] 本机孤儿（adopted_local）也吃本板显存 ⇒ 一样要算")
    orphan = inst("adopted_local", 2, 1600, model_id="qwen3-asr")
    per_card, total, remote = _local_memory_usage([orphan])
    check("卡 2 = 1600", per_card == {2: 1600}, f"per_card={per_card}")
    check("total = 1600", total == 1600, f"total={total}")

    # ---------------------------------------------------------------- [5] --
    print("\n[5] peer 与本机并存 ⇒ 只按本机那份算")
    per_card, total, remote = _local_memory_usage(
        [peer, inst("spawned", 0, 3500, model_id="qwen3.5-vl-4b")])
    check("卡 0 = 3500（peer 的 5500 没进来）",
          per_card == {0: 3500}, f"per_card={per_card}")
    check("total = 3500", total == 3500, f"total={total}")
    check("remote = 5500", remote == 5500, f"remote={remote}")

    # ---------------------------------------------------------------- [6] --
    print("\n[6] 多卡实例（分段式 9B）⇒ 显存平摊到它占的每一张卡")
    nine_b = inst("spawned", 0, 7000, cards=[0, 1], model_id="qwen3.5-vl-9b")
    per_card, total, remote = _local_memory_usage([nine_b])
    check("卡 0 = 3500、卡 1 = 3500（不是 7000/0）",
          per_card == {0: 3500, 1: 3500}, f"per_card={per_card}")
    check("total = 7000", total == 7000, f"total={total}")

    # ---------------------------------------------------------------- [7] --
    print("\n[7] 非 running 的实例不占显存")
    for status in ("loading", "not_loaded", "error"):
        per_card, total, remote = _local_memory_usage(
            [inst("spawned", 0, 3500, status=status, model_id="qwen3.5-vl-4b")])
        check(f"status={status} 不计占用",
              per_card == {} and total == 0, f"per_card={per_card} total={total}")


# --------------------------------------------------------------------------- #
# [8] 端到端：真的 ComputeRegistry.snapshot()
# --------------------------------------------------------------------------- #
def _fake_registry():
    """造一个不碰硬件的 ``ComputeRegistry``（只 stub 探卡与设备拓扑评估）。"""
    from app.core.compute.registry import ComputeRegistry

    reg = ComputeRegistry()
    reg.list_cards = lambda force_refresh=False: [
        {"npu_index": 0, "device_id": "0001:11:00.0", "raw_index": 0},
        {"npu_index": 1, "device_id": "0003:31:00.0", "raw_index": 1},
        {"npu_index": 2, "device_id": "0004:41:00.0", "raw_index": 2},
    ]
    reg.evaluate_device_requirements = (
        lambda model_id, ep_card_count=None: {
            "total_devices": 1, "ok": True, "reason": "",
            "selected": [], "layouts": {},
        })
    reg.effective_requires_cards = lambda model_id, ev=None: 1
    return reg


def _add(reg, source: str, npu: int, model_id: str, mem_mb: int,
         cards=None) -> None:
    from app.core.compute.registry import ModelInstance

    card_tuple = tuple(cards) if cards is not None else (npu,)
    inst_obj = ModelInstance(
        key=(npu, model_id, source, 0),
        npu_index=npu,
        model_id=model_id,
        npu_indices=card_tuple,
        status="running",
        memory_estimate_mb=mem_mb,
        capability_types=["swcap_vision_chat"],
        label=model_id,
        _source=source,
    )
    reg._instances[(npu, model_id, source, 0)] = inst_obj


def test_snapshot_e2e() -> None:
    print("\n[8] 端到端：ComputeRegistry.snapshot() 的出参")
    reg = _fake_registry()
    # peer（别的板子）：报 npu=1 / 5500MB —— 恰好落在本板也有卡 1 的号段里
    _add(reg, "adopted", 1, "LocateAnything-3b", 5500)
    snap = reg.snapshot()

    check("per_card_memory_mb 不含 peer 的卡号",
          snap["per_card_memory_mb"] == {},
          f"per_card={snap['per_card_memory_mb']}")
    check("total_memory_mb = 0", snap["total_memory_mb"] == 0,
          f"total={snap['total_memory_mb']}")
    check("remote_memory_mb = 5500", snap.get("remote_memory_mb") == 5500,
          f"remote={snap.get('remote_memory_mb')}")
    check("本机计数仍是 0（口径一致）",
          snap["local_running_count"] == 0 and snap["remote_running_count"] == 1,
          f"local={snap['local_running_count']} remote={snap['remote_running_count']}")

    # 本机实例 + peer 并存
    reg2 = _fake_registry()
    _add(reg2, "adopted", 1, "LocateAnything-3b", 5500)
    _add(reg2, "spawned", 0, "qwen3.5-vl-4b", 3500)
    snap2 = reg2.snapshot()
    check("本机卡 0 = 3500，peer 没进来",
          snap2["per_card_memory_mb"] == {0: 3500},
          f"per_card={snap2['per_card_memory_mb']}")
    check("total = 3500", snap2["total_memory_mb"] == 3500,
          f"total={snap2['total_memory_mb']}")
    check("remote 仍单独记着 5500",
          snap2.get("remote_memory_mb") == 5500,
          f"remote={snap2.get('remote_memory_mb')}")

    # 本机孤儿
    reg3 = _fake_registry()
    _add(reg3, "adopted_local", 2, "qwen3-asr", 1600)
    snap3 = reg3.snapshot()
    check("本机孤儿占卡 2 = 1600",
          snap3["per_card_memory_mb"] == {2: 1600},
          f"per_card={snap3['per_card_memory_mb']}")


# --------------------------------------------------------------------------- #
# [9] 反向验证：老口径必须算出**不同**的结果
# --------------------------------------------------------------------------- #
def test_negative_control() -> None:
    print("\n[9] 反向验证：修复前的口径（不看 _source）必须算出不同结果")
    peer = inst("adopted", 1, 5500, model_id="LocateAnything-3b")
    naive = naive_usage([peer])
    from app.core.compute.registry import _local_memory_usage
    fixed, _total, _remote = _local_memory_usage([peer])

    check("老口径把 peer 记到本板卡 1 上（= 回归前的样子）",
          naive == {1: 5500}, f"naive={naive}")
    check("修复后本板一张卡都不占", fixed == {}, f"fixed={fixed}")
    check("两者确实不同（否则这个回归抓不住 bug）", naive != fixed,
          f"naive={naive} fixed={fixed}")

    # 老口径下网页翻译卡会怎么判：peer 的 5500 占着卡 1 ⇒ 卡 1 放不下
    # 翻译模型（2200MB）⇒ 只剩卡 0 —— 正是现场的「只起 2 份」。
    card_mem = 5120
    trans_mb = 2200
    naive_free = {c for c in (0, 1, 2)
                  if naive.get(c, 0) + trans_mb <= card_mem}
    fixed_free = {c for c in (0, 1, 2)
                  if fixed.get(c, 0) + trans_mb <= card_mem}
    check("老口径：卡 1 被判放不下翻译模型", naive_free == {0, 2},
          f"naive_free={sorted(naive_free)}")
    check("修复后：三张卡都放得下", fixed_free == {0, 1, 2},
          f"fixed_free={sorted(fixed_free)}")


def main() -> int:
    print("=" * 72)
    print("registry.snapshot()：显存统计的 peer 隔离")
    print("=" * 72)

    test_rule()
    test_snapshot_e2e()
    test_negative_control()

    print("\n" + "=" * 72)
    print(f"通过 {_PASS} / 失败 {_FAIL}")
    print("=" * 72)
    if _FAIL:
        print("❌ 有断言没过")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
