#!/usr/bin/env python3
"""回归：本地算力的启停**不能**波及远端 peer(网络算力)的实例。

实例 ``_source``：``spawned`` / ``adopted_local`` 是本机的，``adopted`` 是
**别的板子**通过 LAN 提供的 service —— 卸载/停止 peer 等于去动另一块板子的
NPU 显存，每块板子只管自己的本地算力。

断言
----
1. stop-all 只卸本机实例，peer 一个都不碰。
2. DELETE /compute/instances 目标是 peer → 403，且不发任何 unload。
3. DELETE /compute/instances 目标是本机 → 成功，且不波及 peer。
4. restart 本机模型 → 不 adopt peer，起的是本机 worker。
5. restart_all_local(「重启所有协处理器」第 4 步) → peer 自身不被重启。

**不碰真机**：假 registry(真 ``ModelInstance`` + 真 ``ComputeRegistry`` 方法)，
``_unload_one`` 换成只记录，``_spawn_compute_worker`` / RPC 全 mock ——
零 RPC、零 spawn、零 NPU 占用。

跑法：``.venv/bin/python scripts/verify_peer_isolation.py``
"""
from __future__ import annotations

import asyncio
import sys
import threading

from fastapi import HTTPException

sys.path.insert(0, ".")

from app.core.compute.registry import ComputeRegistry, ModelInstance
from app.system.api import compute as capi

_FAILED: list[str] = []


def _pick_models() -> tuple[str, str]:
    """挑「本机模型」与「peer 模型」两个 model id。

    ⚠️ **不要写死模型名** —— 目录里的模型随分发变化（``qwen3-vl-4b``
    已经改名成 ``qwen3.5-vl-4b``），写死的名字会让这个回归在换包后直接
    抛 pydantic 校验错误（``未知 model: 'qwen3-vl-4b'``），**连断言都
    跑不到** —— 脚本看上去「失败」，其实是被前置校验挡在门外。

    ``StopInstanceRequest`` 的校验用的是 ``MODEL_PRESETS``（= 本板**已
    安装**的模型目录），所以这里从它里面挑，保证一定能过校验。
    """
    from app.core.compute.registry import MODEL_PRESETS

    ids = sorted(MODEL_PRESETS.keys())
    if len(ids) < 2:
        raise SystemExit("本板装的模型少于 2 个，跑不了这个回归")
    peer, local = ids[0], ids[1]
    return local, peer


LOCAL_MODEL, PEER_MODEL = _pick_models()


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {label}")
    if not ok:
        if detail:
            print(f"       {detail}")
        _FAILED.append(label)


class FakeProc:
    """最小假 subprocess —— 只为让「只收 proc is not None」的筛选通过,
    不发任何信号。"""

    pid = 4242

    def poll(self):
        return None

    def terminate(self):
        pass

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


def make_reg() -> ComputeRegistry:
    """真 registry 对象,只注入实例表 —— 方法全是真的。

    场景:一块本板 + 一块远端 peer 板。
      - 本板:<local model> @npu0 (spawned)
      - peer:<peer model> @npu1 (adopted)

    ⚠️ 模型名**从 ``MODEL_PRESETS`` 动态取**（见 :func:`_pick_models`）——
    写死会随模型包改名而失效。
    """
    reg = ComputeRegistry.__new__(ComputeRegistry)
    reg._lock = threading.RLock()
    reg._model_locks = {}
    reg._recently_stopped = {}
    reg._instances = {}
    # 设备拓扑评估跟本回归无关（它会去探真卡）—— mock 掉，保持脚本不碰硬件
    reg._dev_req_cache = {}
    reg.evaluate_device_requirements = (
        lambda model_id, ep_card_count=None: {
            "total_devices": 1, "ok": True, "reason": "",
            "selected": [], "layouts": {},
        })
    # snapshot() 依赖一堆 probe/卡表内部状态,本脚本不看快照 → 直接覆盖
    reg.snapshot = lambda: {"instances": []}
    for m, npu, src in ((LOCAL_MODEL, 0, "spawned"),
                        (PEER_MODEL, 1, "adopted")):
        inst = ModelInstance(key=(npu, m, src), model_id=m, npu_index=npu,
                             _source=src, status="running")
        reg._instances[inst.key] = inst
    return reg


def instrument(reg: ComputeRegistry) -> list:
    """把 _unload_one 换成只记录 —— 记录 = 「谁会被真的 unload」。"""
    hits: list = []
    reg._unload_one = lambda inst, teardown: hits.append(
        (inst.model_id, inst.npu_index, inst._source))
    return hits


def make_restart_reg() -> tuple[ComputeRegistry, list, list]:
    """restart 场景的 registry + 两个记录器。

    ``_adopt_existing_worker`` / ``_spawn_compute_worker`` 全 mock —— 只记
    「有没有走 adopt」与「spawn 的 allow_adopt 是什么」,不真起进程。
    """
    reg = make_reg()
    reg._instances[(0, LOCAL_MODEL, "spawned")].proc = FakeProc()
    adopted_calls: list = []
    spawn_calls: list = []

    def fake_spawn(model_id, **kw):
        spawn_calls.append((model_id, kw.get("allow_adopt")))
        return ModelInstance(key=(-1, model_id, "spawned"), model_id=model_id,
                              npu_index=-1, _source="spawned",
                              status="not_loaded")

    reg._adopt_existing_worker = lambda m: adopted_calls.append(m)
    reg._spawn_compute_worker = fake_spawn
    return reg, adopted_calls, spawn_calls


def main() -> int:
    # ------------------------------------------------------------------ #
    print("=== 1. POST /api/compute/stop-all ===")
    reg = make_reg()
    hits = instrument(reg)
    capi.get_registry = lambda: reg
    res = asyncio.run(capi.stop_all_instances())
    print("    返回 stopped =", res["stopped"])
    print("    返回 skipped =", res["skipped"])
    print("    实际被 unload 的实例 =", hits)
    peer_hit = [h for h in hits if h[2] == "adopted"]
    check(not peer_hit, "stop-all 不 unload 远端 peer 的实例",
          f"却 unload 了 {peer_hit}")
    check(PEER_MODEL not in res["stopped"],
          "stop-all 的 stopped 列表不含 peer 模型", f"stopped={res['stopped']}")
    check(any(h[2] == "spawned" for h in hits),
          "stop-all 仍会卸本机实例", f"hits={hits}")

    # ------------------------------------------------------------------ #
    print("\n=== 2. DELETE /api/compute/instances（目标是 peer 实例）===")
    reg = make_reg()
    hits = instrument(reg)
    capi.get_registry = lambda: reg
    try:
        asyncio.run(capi.stop_instance(
            capi.StopInstanceRequest(model=PEER_MODEL, npu_index=1)))
        check(False, "DELETE peer 实例应被 403 拒绝", "却返回成功")
    except HTTPException as e:
        check(e.status_code == 403,
              "DELETE peer 实例被 403 拒绝（本机无权停）",
              f"status={e.status_code}")
    check(not hits, "DELETE peer 时没有发出任何 unload", f"hits={hits}")

    # ------------------------------------------------------------------ #
    print("\n=== 3. DELETE /api/compute/instances（目标是本机实例）===")
    reg = make_reg()
    hits = instrument(reg)
    capi.get_registry = lambda: reg
    try:
        asyncio.run(capi.stop_instance(
            capi.StopInstanceRequest(model=LOCAL_MODEL, npu_index=0)))
        check(True, "DELETE 本机实例成功")
    except HTTPException as e:
        check(False, "DELETE 本机实例应成功",
              f"status={e.status_code} detail={e.detail[:80]}")
    peer_hit = [h for h in hits if h[2] == "adopted"]
    check(not peer_hit, "DELETE 本机实例时没有波及 peer", f"hits={hits}")
    check(any(h[2] == "spawned" for h in hits),
          "DELETE 本机实例确实卸了本机那个", f"hits={hits}")

    # ------------------------------------------------------------------ #
    # 不 mock _stop_inst_body —— 它末尾会把实例 pop 出 registry,那是
    # restart 语义的一部分(no-op 掉会让 ensure 复用旧实例,测不到 spawn)。
    print("\n=== 4. restart_instance_local（算力页「重启服务」）===")
    reg, adopted_calls, spawn_calls = make_restart_reg()
    reg.restart_instance_local(LOCAL_MODEL)
    check(not adopted_calls,
          "重启本机模型不 adopt peer（allow_adopt=False）",
          f"adopted_calls={adopted_calls}")
    check(bool(spawn_calls) and spawn_calls[0][1] is False,
          "重启本机模型起的是本机 worker", f"spawn_calls={spawn_calls}")

    # ------------------------------------------------------------------ #
    print("\n=== 5. restart_all_local（「重启所有协处理器」第 4 步）===")
    reg, adopted_calls, spawn_calls = make_restart_reg()
    rc, lines = reg.restart_all_local()
    print("    restart_all_local ->", lines)
    check(not adopted_calls,
          "重启所有协处理器时不 adopt peer", f"adopted_calls={adopted_calls}")
    check(not any(PEER_MODEL in ln for ln in lines),
          "peer 自身不被重启（只收 proc is not None 的本机 worker）",
          f"lines={lines}")

    print("\n" + "=" * 60)
    if _FAILED:
        print(f"❌ {len(_FAILED)} 条未通过：")
        for f in _FAILED:
            print("   -", f)
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
