#!/usr/bin/env python3
"""回归：模型启停的「过渡态」前置判定。

四个场景（都是 rc=-6 / 显存泄漏 / 误报 404 的来源）：

  S1 开启一个**正在启动中**的模型 → 排队等它加载完 → 再执行
  S2 开启一个**正在关闭中**的模型 → 排队等它关完 → 再执行
  S3 关闭一个**正在启动中**的模型 → 排队等它加载完 → 再停（腾卡必需：
     拒绝的话卡上那份继续占显存 → 新模型 rc=-6 起不来）
  S4 关闭一个**正在关闭中**的模型 → 排队等它关完 → 幂等 200
  S5 等的那次启动**失败**了 → 幂等 200（卡本来就是空的，不是 404）

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
from app.core.compute.registry import ComputeRegistry, transition_of  # noqa: E402

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
        self.npu_indices = (npu_index,) if npu_index >= 0 else ()
        self.status = status
        self._source = source
        self.capability_types = list(caps)
        self._stop_depth = 0
        self._stop_npu = -1
        # bind_instance / _probe_instance 用到
        self._probe_lock = threading.Lock()
        self._probe_thread = None
        self._probe_gen = 0
        self._bind_inflight = False
        self.error = ""
        self.started_at = 0.0
        self.key = (npu_index, model_id, source)
        self.proc = None
        self.rpc_client = None
        self.load_time_sec = None
        self.loaded_at = None
        self.finished_at = None

    def to_dict(self):
        return {"model_id": self.model_id, "npu_index": self.npu_index,
                "status": self.status, "transition": transition_of(self)}


class FakeReg:
    """假 registry —— 手工控制「过渡态」和记录调用。"""

    def __init__(self, inst=None, transitions=(), on_clear=None):
        self.inst = inst
        # transitions: [(npu, kind)] —— transition_cards() 的返回
        self.transitions = list(transitions)
        # 每次 transition_cards() 收到的 local_only 参数（API 层必须传
        # True —— 过渡态守卫只看本机实例，见 S6）
        self.local_only_flags: list[bool] = []
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
    def transition_cards(self, model_id, local_only=False):
        self._polls += 1
        self.local_only_flags.append(bool(local_only))
        if self.clear_after and self._polls > self.clear_after:
            if self.on_clear:
                self.on_clear(self)
            return []
        return list(self.transitions)

    def npu_card_error(self, npu_index):
        """卡位校验（v0.29）—— 本脚本只关心过渡态，假 registry 一律放行。

        越界卡号的真实行为另见 scripts/verify_npu_card_guard.py。
        """
        return None

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

    def was_recently_stopped(self, model_id, npu_index=None, window_s=10.0):
        return False

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
# S3 关闭一个正在启动中的模型 → 排队等它加载完 → 再停
# --------------------------------------------------------------------------- #
def scen_s3():
    print("\n=== S3 关闭「正在启动中」的模型 → 排队等它加载完 → 再停 ===")
    inst = FakeInst(status="loading")
    reg = FakeReg(inst=inst, transitions=[(0, "loading")])
    reg.clear_after = 2          # 轮询 2 次后过渡态消失
    # 排队期间模型加载完成 → running（显存已归还，随后的 unload 正常）
    reg.on_clear = lambda r: setattr(r.inst, "status", "running")
    patch(reg)
    req = capi.StopInstanceRequest(model="qwen3-vl-4b", npu_index=0)
    r = run(capi.stop_instance(req))
    check("S3 返回 ok", r.get("ok") is True, str(r)[:80])
    check("S3 等过了过渡态", reg._polls > 1, f"polls={reg._polls}")
    check("S3 等完真的停了", reg.stop_calls == 1, f"stop={reg.stop_calls}")


# --------------------------------------------------------------------------- #
# S5 等的那次启动失败了 → 幂等 200（卡本来就是空的，不是 404）
# --------------------------------------------------------------------------- #
def scen_s5():
    print("\n=== S5 等的那次启动失败 → 幂等 200（不是 404）===")
    inst = FakeInst(status="loading")
    reg = FakeReg(inst=inst, transitions=[(0, "loading")])
    reg.clear_after = 2
    # 加载失败 → 实例被移出 registry（卡本来就是空的）
    reg.on_clear = lambda r: setattr(r, "inst", None)
    patch(reg)
    req = capi.StopInstanceRequest(model="qwen3-vl-4b", npu_index=0)
    r = run(capi.stop_instance(req))
    check("S5 返回 ok（不是 404）", r.get("ok") is True, str(r)[:80])
    check("S5 没去停（实例已经没了）", reg.stop_calls == 0,
          f"stop={reg.stop_calls}")


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


# --------------------------------------------------------------------------- #
# S6 远端 peer(adopted)的过渡态**不能**挡住本机的启停
# --------------------------------------------------------------------------- #
def scen_s6():
    print("\n=== S6 peer 的 loading 不挡本机启停（单板机 + peer 卡住） ===")
    # (a) 真实 registry 的过滤：peer 的过渡态不进本机视图
    real_reg = ComputeRegistry.__new__(ComputeRegistry)   # 不跑 __init__
    peer = FakeInst(status="loading", npu_index=0, source="adopted")
    local = FakeInst(status="running", npu_index=0, source="spawned")
    real_reg._instances_of = lambda mid, npu_index=None: [peer, local]
    all_cards = real_reg.transition_cards("qwen3-vl-4b")
    local_cards = real_reg.transition_cards("qwen3-vl-4b", local_only=True)
    check("S6 全量视图看得到 peer 的过渡态", (0, "loading") in all_cards,
          str(all_cards))
    check("S6 本机视图不含 peer 的过渡态", local_cards == [], str(local_cards))

    # (b) API 层必须按 local_only=True 问 —— 否则上面的过滤用不上
    inst = FakeInst(status="running", npu_index=0)
    reg = FakeReg(inst=inst, transitions=[])      # 本机没有过渡态
    patch(reg)
    req = capi.InstanceRequest(model="qwen3-vl-4b", npu_index=[0])
    r = run(capi.start_instance(req))
    check("S6 启模型成功", r["ok"] is True)
    check("S6 API 层按 local_only 问过渡态",
          bool(reg.local_only_flags) and all(reg.local_only_flags),
          str(reg.local_only_flags))

    # (c) 本机自己的 loading 仍然要挡住（别把守卫整个关掉）
    inst2 = FakeInst(status="loading", npu_index=0)
    reg2 = FakeReg(inst=inst2, transitions=[(0, "loading")])
    reg2.clear_after = 1
    reg2.on_clear = lambda rr: setattr(rr.inst, "status", "running")
    patch(reg2)
    r2 = run(capi.start_instance(capi.InstanceRequest(model="qwen3-vl-4b",
                                                      npu_index=[0])))
    check("S6 本机的 loading 仍然排队等（没被误放行）", reg2._polls > 1,
          f"polls={reg2._polls}")
    check("S6 本机 loading 排完照常启", r2["ok"] is True)


class FakeRpc:
    """假 RPC client —— load 立即回 loading（worker 的 init 在它自己线程里跑）。"""

    def __init__(self, reply=None):
        self.reply = reply or {"ok": True, "data": {"status": "loading"}}
        self.calls = 0

    def call(self, payload, timeout_ms=None):
        self.calls += 1
        return dict(self.reply)


# --------------------------------------------------------------------------- #
# S7 bind 之后必须有**活着的** probe —— 否则实例永久卡在 loading
# --------------------------------------------------------------------------- #
def scen_s7():
    print("\n=== S7 bind 后 probe 必须活着（否则永久 loading → 启停 409） ===")
    reg = ComputeRegistry.__new__(ComputeRegistry)
    reg._lock = threading.Lock()
    reg._instances = {}
    reg._probe_instance = lambda inst: time.sleep(2.0)   # 假 probe：睡一会儿

    inst = FakeInst(model_id="qwen3-vl-4b", npu_index=-1, status="error")

    # (a) 没有 probe → 必须起一个
    reg._ensure_probe_thread(inst)
    check("S7 无 probe 时起了新的", inst._probe_thread is not None
          and inst._probe_thread.is_alive())
    first = inst._probe_thread
    reg._ensure_probe_thread(inst)
    check("S7 probe 还活着时不重复起", inst._probe_thread is first)

    # (b) probe 已死 → 必须换一个（这正是「加载失败过一次后再点启用」的路径）
    first.join()
    reg._ensure_probe_thread(inst)
    check("S7 probe 已死时换新的", inst._probe_thread is not first
          and inst._probe_thread.is_alive())

    # (c) 端到端：bind 一个「加载失败过(error)」的实例 → 必须有活 probe 接管
    reg2 = ComputeRegistry.__new__(ComputeRegistry)
    reg2._lock = threading.Lock()
    reg2._instances = {}
    reg2._probe_instance = lambda i: time.sleep(3.0)
    inst2 = FakeInst(model_id="qwen3-vl-4b", npu_index=-1, status="error")
    inst2.rpc_client = FakeRpc()
    dead = threading.Thread(target=lambda: None)
    dead.start(); dead.join()                 # 一个已结束的线程
    inst2._probe_thread = dead
    reg2._instances[inst2.key] = inst2
    out = reg2.bind_instance(inst2, 0)
    check("S7 bind 后 status=loading", out.status == "loading", out.status)
    check("S7 bind 后有活着的 probe 接管",
          out._probe_thread is not None and out._probe_thread.is_alive())
    check("S7 load RPC 已发出（走 loading 分支）",
          inst2.rpc_client.calls >= 1, f"calls={inst2.rpc_client.calls}")


def main() -> int:
    scen_s1()
    scen_s2()
    scen_s3()
    scen_s4()
    scen_s5()
    scen_s6()
    scen_s7()
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
