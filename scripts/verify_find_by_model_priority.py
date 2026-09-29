#!/usr/bin/env python3
"""回归测试: ComputeRegistry._find_by_model 在同 model_id 多 source 共存时的优先级。

背景
----
v0.8.30+ 修了 wizard 一键开启误关网络 peer NPU 的 bug(Layer 1 + 2),但暴露
一个 secondary bug:用户在自己的本机上启了同 model_id(spawned),同时 LAN
上有另一台板子也在提供该 model_id(adopted),_find_by_model 按 _instances
插入顺序返回,DDS 通常先发现远端 adopted → 用户点"禁用" → 命中 adopted →
Layer 2 403 → 误以为"我本机的关不掉"。

修复: _find_by_model 优先本机(spawned / adopted_local),adopted 仅作 fallback。

测试矩阵(全部要 pass):
  1. 只有 spawned             → spawned
  2. 只有 adopted             → adopted (fallback)
  3. 只有 adopted_local       → adopted_local
  4. spawned + adopted        → spawned (本机优先)
  5. adopted + spawned        → spawned (顺序无关)
  6. spawned + adopted + adopted_local → spawned (本机优先,任一本机 OK)
  7. adopted + adopted_local  → adopted_local (本机优先)
  8. 不同 model_id 不互相干扰
"""
import sys
import threading
import traceback

sys.path.insert(0, ".")

from app.core.compute.registry import ComputeRegistry, ModelInstance


def make_inst(npu: int, model_id: str, source: str, peer_name: str = "") -> ModelInstance:
    """构造一个最小可用的 ModelInstance,只填 _find_by_model 用到的字段。"""
    return ModelInstance(
        key=(npu, model_id, source),
        npu_index=npu,
        model_id=model_id,
        _source=source,
        _peer_name=peer_name,
    )


def new_registry_with(insts: list[ModelInstance]) -> ComputeRegistry:
    """绕开 ComputeRegistry.__init__ 的 Hub bring-up,只装 _lock + _instances。"""
    reg = ComputeRegistry.__new__(ComputeRegistry)
    reg._lock = threading.RLock()
    reg._instances = {inst.key: inst for inst in insts}
    return reg


def case(name: str, insts: list[ModelInstance], query: str, expected: str) -> bool:
    reg = new_registry_with(insts)
    got = reg._find_by_model(query)
    actual = got._source if got else None
    ok = actual == expected
    flag = "PASS" if ok else "FAIL"
    print(f"  [{flag}] {name}: query={query!r} → {actual!r} (expected {expected!r})")
    if not ok:
        print(f"        full instance: {got!r}")
    return ok


def main() -> int:
    M = "LocateAnything-3b"
    spawned = make_inst(0, M, "spawned")
    adopted_local = make_inst(0, M, "adopted_local", peer_name="orphan-on-self")
    adopted = make_inst(0, M, "adopted", peer_name="PeerBoard-X")
    # 不同 model_id,确保不会被错选
    other = make_inst(0, "OtherModel-7b", "spawned")

    cases = [
        ("1. only spawned",
         [spawned], M, "spawned"),
        ("2. only adopted (fallback)",
         [adopted], M, "adopted"),
        ("3. only adopted_local",
         [adopted_local], M, "adopted_local"),
        ("4. spawned + adopted (any order)",
         [spawned, adopted], M, "spawned"),
        ("5. adopted + spawned (reversed order)",
         [adopted, spawned], M, "spawned"),
        ("6. spawned + adopted + adopted_local",
         [spawned, adopted, adopted_local], M, "spawned"),
        ("7. adopted_local + adopted (no spawned)",
         [adopted_local, adopted], M, "adopted_local"),
        ("8. model_id isolation: querying 'OtherModel-7b'",
         [spawned, adopted, other], "OtherModel-7b", "spawned"),
    ]

    print(f"Running {len(cases)} cases against _find_by_model priority:")
    failed = 0
    for name, insts, q, exp in cases:
        if not case(name, insts, q, exp):
            failed += 1

    # 9. query 不存在时返回 None
    print("\nEdge case: query 找不到")
    reg = new_registry_with([spawned])
    got = reg._find_by_model("NonExistent-99b")
    if got is None:
        print("  [PASS] non-existent model_id returns None")
    else:
        print(f"  [FAIL] expected None, got {got!r}")
        failed += 1

    # 10. (回归)反向断言: 旧实现(只看第一个匹配)在新场景下会失败,确保
    # 改完不会回退 — 用 [adopted, spawned] 顺序查 spawned,旧实现会返回 adopted
    print("\nRegression guard: 旧实现会坏的场景")
    reg = new_registry_with([adopted, spawned])  # adopted 先插入(模拟 DDS 先发现远端)
    got = reg._find_by_model(M)
    if got is not None and got._source == "spawned":
        print("  [PASS] even with adopted-first insertion, returns spawned")
    else:
        print(f"  [FAIL] expected spawned, got {got!r}")
        failed += 1

    print()
    if failed == 0:
        print(f"✅ All {len(cases) + 2} cases passed")
        return 0
    print(f"❌ {failed} case(s) failed")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(2)
