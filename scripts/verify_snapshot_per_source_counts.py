#!/usr/bin/env python3
"""回归测试: ComputeRegistry.snapshot() 的 per-source 计数字段。

背景
----
v0.15 算力配置页顶部汇总条按 _source 分"本机/远端"展示。
snapshot 端一次性产出 6 个计数字段(local/remote ×
running/loading/error)。queued 维度在 v0.15 (cleanup) 删掉:
not_loaded 是默认态,per-card "未启用" badge 已显示,顶部再说
是重复 + "排队"在中文里容易误读。

本测试在 unit 层面覆盖:
  1. 全空 → 全 0
  2. 仅 spawned running → local_running=1, 其余 0
  3. 仅 adopted running → remote_running=1, 其余 0
  4. spawned + adopted 同 status → 各自 _count 计入对应源
  5. mixed status (spawned running + spawned loading + adopted error):
     4 个字段同时有数,互不串
  6. adopted_local 算本机: local_running 包含它
  7. _source 空字符串 → 不计入任何桶(防御性)
  8. running/loading/error 4 类语义互不串
  9. 完整性断言: 6 个字段都在 snapshot dict 里
 10. 完整性断言: queued 字段已删除(防止有人加回来)
"""
import sys
import threading
import traceback

sys.path.insert(0, ".")

from app.core.compute.registry import ComputeRegistry, ModelInstance


def make_inst(npu: int, model_id: str, source: str,
              status: str = "not_loaded", started_at: float = 0.0) -> ModelInstance:
    """构造一个最小可用的 ModelInstance,只填 snapshot 计数字段用到的字段。"""
    return ModelInstance(
        key=(npu, model_id, source),
        npu_index=npu,
        model_id=model_id,
        _source=source,
        status=status,
        started_at=started_at,
        loaded_at=None,
        queued_at=started_at,
    )


def new_registry_with(insts: list[ModelInstance]) -> ComputeRegistry:
    """绕开 ComputeRegistry.__init__ 的 Hub bring-up,只装 _lock + _instances。"""
    reg = ComputeRegistry.__new__(ComputeRegistry)
    reg._lock = threading.RLock()
    reg._instances = {inst.key: inst for inst in insts}
    reg.list_with_source = lambda: [inst.to_dict() for inst in reg._instances.values()]
    reg.list_active_capabilities = lambda: []
    reg.list_supported_models = lambda: sorted({i.model_id for i in reg._instances.values()})
    reg.list_cards = lambda: []
    reg.cards_failed_reason = lambda: ""
    return reg


def case(name, insts, expected: dict) -> bool:
    reg = new_registry_with(insts)
    snap = reg.snapshot()
    got = {
        k: snap.get(k, "MISSING")
        for k in (
            "local_running_count", "remote_running_count",
            "local_loading_count", "remote_loading_count",
            "local_error_count", "remote_error_count",
        )
    }
    ok = got == expected
    flag = "PASS" if ok else "FAIL"
    print(f"  [{flag}] {name}")
    if not ok:
        print(f"        got     = {got}")
        print(f"        expected = {expected}")
    return ok


def main() -> int:
    M = "LocateAnything-3b"
    M2 = "qwen3-vl-4b"
    failed = 0

    zero6 = {k: 0 for k in (
        "local_running_count", "remote_running_count",
        "local_loading_count", "remote_loading_count",
        "local_error_count", "remote_error_count",
    )}

    cases = [
        ("1. 全空 → 全 0",
         [],
         zero6),

        ("2. 仅 spawned running",
         [make_inst(0, M, "spawned", status="running")],
         {"local_running_count": 1, "remote_running_count": 0,
          "local_loading_count": 0, "remote_loading_count": 0,
          "local_error_count": 0, "remote_error_count": 0}),

        ("3. 仅 adopted running",
         [make_inst(0, M, "adopted", status="running")],
         {"local_running_count": 0, "remote_running_count": 1,
          "local_loading_count": 0, "remote_loading_count": 0,
          "local_error_count": 0, "remote_error_count": 0}),

        ("4. spawned running + adopted loading",
         [make_inst(0, M, "spawned", status="running"),
          make_inst(0, M2, "adopted", status="loading")],
         {"local_running_count": 1, "remote_running_count": 0,
          "local_loading_count": 0, "remote_loading_count": 1,
          "local_error_count": 0, "remote_error_count": 0}),

        ("5. mixed (3 spawneds + 1 adopted error)",
         [make_inst(0, M, "spawned", status="running"),
          make_inst(0, M2, "spawned", status="loading"),
          make_inst(0, "qwen3-tts", "spawned", status="error"),
          make_inst(0, "qwen3-vl-embedding-2b", "adopted", status="error")],
         {"local_running_count": 1, "remote_running_count": 0,
          "local_loading_count": 1, "remote_loading_count": 0,
          "local_error_count": 1, "remote_error_count": 1}),

        ("6. adopted_local running 算本机",
         [make_inst(0, M, "adopted_local", status="running")],
         {"local_running_count": 1, "remote_running_count": 0,
          "local_loading_count": 0, "remote_loading_count": 0,
          "local_error_count": 0, "remote_error_count": 0}),

        ("7. _source 空字符串 → 不计入任何桶(防御性)",
         [make_inst(0, M, "", status="running")],
         zero6),

        ("8. running/loading/error 语义互不串",
         # 同 source 多种 status → 各自 _count 独立计数
         [make_inst(0, M, "spawned", status="running"),
          make_inst(0, M2, "spawned", status="loading"),
          make_inst(0, "qwen3-tts", "spawned", status="error")],
         {"local_running_count": 1, "remote_running_count": 0,
          "local_loading_count": 1, "remote_loading_count": 0,
          "local_error_count": 1, "remote_error_count": 0}),
    ]

    for name, insts, expected in cases:
        if not case(name, insts, expected):
            failed += 1

    # 9. 完整性断言: 所有 6 个字段都必须在 snapshot dict 里(不允许缺失)
    print("\n完整性断言: snapshot 必须包含 6 个字段")
    reg = new_registry_with([])
    snap = reg.snapshot()
    must_have = (
        "local_running_count", "remote_running_count",
        "local_loading_count", "remote_loading_count",
        "local_error_count", "remote_error_count",
    )
    missing = [k for k in must_have if k not in snap]
    if not missing:
        print("  [PASS] 全部 6 个字段都在 snapshot dict 里")
    else:
        print(f"  [FAIL] snapshot 缺字段: {missing}")
        failed += 1

    # 10. queued 字段应已删除(防止有人加回来)
    print("\n完整性断言: local_queued_count / remote_queued_count 必须不在 snapshot")
    forbidden = ("local_queued_count", "remote_queued_count")
    leaked = [k for k in forbidden if k in snap]
    if not leaked:
        print("  [PASS] queued 字段已清理")
    else:
        print(f"  [FAIL] queued 字段泄露回 snapshot: {leaked}")
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