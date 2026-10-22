#!/usr/bin/env python3
"""回归：模型启停的「过渡态」前置判定。

四个场景（都是 rc=-6 / 显存泄漏 / 误报 404 的来源）：

  S1 开启一个**正在启动中**的模型 → 排队等它加载完 → 再执行
  S2 开启一个**正在关闭中**的模型 → 排队等它关完 → 再执行
  S3 关闭一个**正在启动中**的模型 → 409 拒绝（不等、不硬杀）
  S4 关闭一个**正在关闭中**的模型 → 排队等它关完 → 幂等 200

外加两条不变量：

  I1 已经 running 的模型再「启用」→ **不再 bind**（worker 侧只在
     running 时幂等，loading 时会再起一个 init 线程抢 NPU 内存）
  I2 过渡态标志用完必须清零（漏清 = 该实例永久卡住，比没这标志更糟）

用假 registry 直接驱动 API 层的 handler —— 不起服务、不占 NPU、秒级跑完。
真机验证另见 scripts/verify_compute_transition_real.py。

用法：.venv/bin/python scripts/verify_compute_transition_guard.py
"""
from __future__ import annotations

import asyncio
import contextlib
import sys
import threading
import time

sys.path.insert(0, ".")

from fastapi import HTTPException  # noqa: E402

import app.system.api.compute as capi  # noqa: E402
from app.core.compute.registry import transition_of  # noqa: E402

FAILED: list[str] = []
PASSED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"  ✓ {name}" + (f"  ({detail})" if detail else ""))
    else:
        FAILED.append(name)
        print(f"  ✗ {name}" + (f"  ({detail})" if detail else ""))


class FakeInst:
    def __init__(self, model_id="m", npu_index=0, status="running",
                 source="spawned", caps=("swcap_a",)):
        self.model_id = model_id
        self.npu_index = npu_index
        self.status = status
        self._source = source
        self.capability_types = list(caps)
        self._stop_depth = 0
        self._stop_npu = -1

    def to_dict(self):
        return {"model_id": self.model_id, "npu_index": self.npu_index,
                "status": self.status, "transition": transition_of(self)}


class FakeReg:
    """假 registry —— 手工控制「过渡态」和记录调用。"""

    def __init__(self, inst=None, transitions=(), on_clear=None):
        self.inst = inst
        # transitions: [(npu, kind)] —— transition_cards() 的返回
        self.transitions = list(transitions)
        # 等待多少次之后过渡态消失（模拟「排队等到它结束」）
        self.clear_after = 0
        # 过渡态结束时回调 —— 模拟真实世界里的状态变化
        # （加载完 → running；关完 → 实例被移除）
        self.on_clear = on_clear
        self._polls = 0
        self.bind_calls = 0
        self.stop_calls = 0
        self.spawn_calls = 0
        self.lock = threading.Lock()

    # ---- registry 接口
    def transition_cards(self, model_id):
        self._polls += 1
        if self.clear_after and self._polls > self.clear_after:
            if self.on_clear:
                self.on_clear(self)
            return []
        return list(self.transitions)

    def _instances_of(self, model_id, npu_index=None):
        if self.inst is None:
            return []
        if npu_index is not None and self.inst.npu_index != npu_index:
            return []
        return [self.inst]

    def ensure_worker_for_model(self, model_id, allow_adopt=False,
                                npu_index=None, eager_load=False):
        self.spawn_calls += 1
        if self.inst is None:
            # 实例已被移除（关完了）→ spawn 一个新的空实例
            self.inst = FakeInst(model_id=model_id, npu_index=-1,
                                  status="not_loaded")
        return self.inst

    def bind_instance(self, inst, n):
        self.bind_calls += 1
        inst.npu_index = n
        inst.status = "loading"
        return inst

    def stop_instance_local(self, model_id, npu_index=None):
        self.stop_calls += 1

    def restart_instance_local(self, model_id):
        self.stop_calls += 1

    @contextlib.contextmanager
    def model_op_lock(self, model_id):
        with self.lock:
            yield

    def snapshot(self):
        return {"instances": []}

    def _find_by_model(self, model_id):
        return self.inst


def run(coro):
    return asyncio.run(coro)


def patch(reg):
    capi.get_registry = lambda: reg


# --------------------------------------------------------------------------- #
# S1 开启一个正在启动中的模型 → 排队等它结束
# --------------------------------------------------------------------------- #
def scen_s1():
    print("\n=== S1 开启「正在启动中」的模型 → 排队等它加载完 ===")
    inst = FakeInst(status="loading")
    reg = FakeReg(inst=inst, transitions=[(0, "loading")])
    reg.clear_after = 2          # 轮询 2 次后过渡态消失
    # 排队期间模型加载完成 → running
    reg.on_clear = lambda r: setattr(r.inst, "status", "running")
    patch(reg)
    req = capi.InstanceRequest(model="qwen3-vl-4b", npu_index=[0])
    r = run(capi.start_instance(req))
    check("S1 返回 ok", r["ok"] is True)
    check("S1 等过了过渡态才执行", reg._polls > 1,
          f"polls={reg._polls}")
    # 等完它已经在跑了 → 幂等跳过,不能再 bind(worker 侧 loading 时不
    # 幂等,再 bind 就是一个多余的 init 线程)
    check("S1 排队后不再 bind", reg.bind_calls == 0, f"bind={reg.bind_calls}")
    check("S1 快照带上了该实例", len(r["instances"]) == 1)


# --------------------------------------------------------------------------- #
# S2 开启一个正在关闭中的模型 → 排队等它关完
# --------------------------------------------------------------------------- #
def scen_s2():
    print("\n=== S2 开启「正在关闭中」的模型 → 排队等它关完 ===")
    inst = FakeInst(status="running")
    reg = FakeReg(inst=inst, transitions=[(0, "stopping")])
    reg.clear_after = 2
    # 排队期间关完了 → 实例从 registry 移除
    reg.on_clear = lambda r: setattr(r, "inst", None)
    patch(reg)
    req = capi.InstanceRequest(model="qwen3-vl-4b", npu_index=[0])
    r = run(capi.start_instance(req))
    check("S2 返回 ok", r["ok"] is True)
    check("S2 等过了过渡态", reg._polls > 1, f"polls={reg._polls}")
    check("S2 重新 spawn 了实例", reg.spawn_calls == 1, f"spawn={reg.spawn_calls}")
    check("S2 排队后执行了 bind", reg.bind_calls == 1, f"bind={reg.bind_calls}")


# --------------------------------------------------------------------------- #
# S3 关闭一个正在启动中的模型 → 409 拒绝
# --------------------------------------------------------------------------- #
def scen_s3():
    print("\n=== S3 关闭「正在启动中」的模型 → 409 拒绝 ===")
    inst = FakeInst(status="loading")
    reg = FakeReg(inst=inst, transitions=[(0, "loading")])
    patch(reg)
    req = capi.StopInstanceRequest(model="qwen3-vl-4b", npu_index=0)
    try:
        run(capi.stop_instance(req))
        check("S3 抛 409", False, "没抛异常")
    except HTTPException as e:
        check("S3 是 409", e.status_code == 409, f"status={e.status_code}")
        check("S3 文案提到启动中", "启动中" in str(e.detail), str(e.detail))
    check("S3 没真去停", reg.stop_calls == 0, f"stop={reg.stop_calls}")


# --------------------------------------------------------------------------- #
# S4 关闭一个正在关闭中的模型 → 排队 → 幂等 200（不是 404）
# --------------------------------------------------------------------------- #
def scen_s4():
    print("\n=== S4 关闭「正在关闭中」的模型 → 排队 → 幂等 200 ===")
    inst = FakeInst(status="running")
    reg = FakeReg(inst=inst, transitions=[(0, "stopping")])
    reg.clear_after = 2
    patch(reg)
    req = capi.StopInstanceRequest(model="qwen3-vl-4b", npu_index=0)
    r = run(capi.stop_instance(req))
    check("S4 返回 ok", r.get("ok") is True, str(r)[:80])
    check("S4 等过了过渡态", reg._polls > 1, f"polls={reg._polls}")
    check("S4 没再发第二次 stop", reg.stop_calls == 0, f"stop={reg.stop_calls}")


# --------------------------------------------------------------------------- #
# I1 已经 running 的模型再启用 → 不再 bind
# --------------------------------------------------------------------------- #
def scen_i1():
    print("\n=== I1 已 running 的模型再「启用」→ 幂等，不再 bind ===")
    inst = FakeInst(status="running", npu_index=0)
    reg = FakeReg(inst=inst)
    patch(reg)
    req = capi.InstanceRequest(model="qwen3-vl-4b", npu_index=[0])
    r = run(capi.start_instance(req))
    check("I1 返回 ok", r["ok"] is True)
    check("I1 没有再 bind", reg.bind_calls == 0, f"bind={reg.bind_calls}")
    check("I1 快照里带上了该实例", len(r["instances"]) == 1)


# --------------------------------------------------------------------------- #
# I2 过渡态标志必须清零
# --------------------------------------------------------------------------- #
def scen_i2():
    print("\n=== I2 过渡态标志用完必须清零 ===")
    inst = FakeInst(status="running")
    reg = FakeReg(inst=inst)
    inst._stop_depth = 1
    check("I2 stopping 判据", transition_of(inst) == "stopping")
    inst._stop_depth = 0
    inst.status = "loading"
    check("I2 loading 判据", transition_of(inst) == "loading")
    inst.status = "running"
    check("I2 稳态判据", transition_of(inst) == "")
    check("I2 None 安全", transition_of(None) == "")


# --------------------------------------------------------------------------- #
# 排队超时 → 409（不是无限等）
# --------------------------------------------------------------------------- #
def scen_timeout():
    print("\n=== 排队超时 → 409，不无限等 ===")
    inst = FakeInst(status="loading")
    reg = FakeReg(inst=inst, transitions=[(0, "loading")])
    reg.clear_after = 0          # 永不清空
    patch(reg)
    orig = capi._QUEUE_WAIT_LOADING_S
    capi._QUEUE_WAIT_LOADING_S = 1.0
    try:
        req = capi.InstanceRequest(model="qwen3-vl-4b", npu_index=[0])
        t0 = time.time()
        try:
            run(capi.start_instance(req))
            check("超时抛 409", False, "没抛异常")
        except HTTPException as e:
            check("超时是 409", e.status_code == 409, f"status={e.status_code}")
            check("超时很快返回", time.time() - t0 < 5.0,
                  f"{time.time()-t0:.1f}s")
    finally:
        capi._QUEUE_WAIT_LOADING_S = orig


def main() -> int:
    scen_s1()
    scen_s2()
    scen_s3()
    scen_s4()
    scen_i1()
    scen_i2()
    scen_timeout()
    print("\n" + "=" * 60)
    print(f"通过 {PASSED} / {PASSED + len(FAILED)}")
    if FAILED:
        print(f"❌ 失败: {FAILED}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
