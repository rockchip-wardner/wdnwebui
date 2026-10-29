#!/usr/bin/env python3
"""回归：停止本机实例时不能因同名 peer 副本而失败。

场景：本机和 LAN peer 同时提供同一个 model，且 npu_index 相同。向导
「一键开启所需模型」先 DELETE 本机非必需模型腾卡；旧实现
stop_instance_local() 会把 peer 的 adopted 实例也放进 targets，停完本地
副本后遇到 adopted 抛错，API 返回 400，整个一键开启因此失败。

本脚本只验证选择/保护逻辑，不启动 worker / NPU。
"""
import sys
import threading
import traceback

sys.path.insert(0, ".")

from app.core.compute.registry import ComputeRegistry, ModelInstance


def make_inst(npu, model_id, source):
    return ModelInstance(
        key=(npu, model_id, source),
        npu_index=npu,
        npu_indices=(npu,) if npu >= 0 else (),
        model_id=model_id,
        _source=source,
    )


def make_registry(insts):
    reg = ComputeRegistry.__new__(ComputeRegistry)
    reg._lock = threading.RLock()
    reg._instances = {i.key: i for i in insts}
    reg._recently_stopped = {}
    reg.stopped = []
    reg._stop_inst = lambda model_id, inst: reg.stopped.append((model_id, inst))
    return reg


def expect_error(label, fn, needle):
    try:
        fn()
    except RuntimeError as exc:
        ok = needle in str(exc)
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}: {exc}")
        return ok
    except Exception as exc:  # noqa: BLE001
        print(f"  [FAIL] {label}: unexpected {type(exc).__name__}: {exc}")
        return False
    print(f"  [FAIL] {label}: expected RuntimeError containing {needle!r}")
    return False


def main():
    model = "qwen2.5-omni-3b"
    local = make_inst(1, model, "spawned")
    peer = make_inst(1, model, "adopted")
    local_other_card = make_inst(0, model, "spawned")
    remote_only = make_inst(0, "qwen3-vl-4b", "adopted")

    passed = 0
    total = 0

    print("1. local + peer on same card: stop local, skip peer")
    reg = make_registry([local, peer])
    reg.stop_instance_local(model, 1)
    total += 1
    if reg.stopped == [(model, local)]:
        print("  [PASS] only local instance stopped")
        passed += 1
    else:
        print(f"  [FAIL] stopped={reg.stopped!r}")

    print("2. local on two cards, peer duplicate: only requested card")
    reg = make_registry([local, peer, local_other_card])
    reg.stop_instance_local(model, 1)
    total += 1
    if reg.stopped == [(model, local)]:
        print("  [PASS] npu_index filter still applies")
        passed += 1
    else:
        print(f"  [FAIL] stopped={reg.stopped!r}")

    print("3. adopted_local + peer: stop local orphan")
    orphan = make_inst(1, model, "adopted_local")
    reg = make_registry([orphan, peer])
    reg.stop_instance_local(model, 1)
    total += 1
    if reg.stopped == [(model, orphan)]:
        print("  [PASS] adopted_local stopped")
        passed += 1
    else:
        print(f"  [FAIL] stopped={reg.stopped!r}")

    print("4. peer only: still refuse")
    reg = make_registry([remote_only])
    total += 1
    if expect_error(
            "only adopted", lambda: reg.stop_instance_local("qwen3-vl-4b", 0),
            "远端 peer"):
        passed += 1

    print("5. missing model: still report not in registry")
    reg = make_registry([])
    total += 1
    if expect_error(
            "missing", lambda: reg.stop_instance_local("qwen3-vl-4b", 0),
            "不在 registry"):
        passed += 1

    print()
    if passed == total:
        print(f"✅ All {total} cases passed")
        return 0
    print(f"❌ {passed}/{total} passed")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(2)
