#!/usr/bin/env python3
"""回归：探针把「上一轮残留的 error」当成本次终态 → 实例永远停在「启动失败」。

真机症状（2026-10-24，RK3588 双卡板）：worker 里 `tokenizers` 缺失导致
加载失败 → 用户装好包**重试** → worker 侧其实 34.5s 就 `LLMSession ready`，
但算力页一直显示「启动失败」，错误文案还是**上一轮**的那句；能力也取不到
（`功能未启用: swcap_vision_chat`）。

机制（三个动作凑在一起）：

  1. worker 侧 load 失败但**进程还活着** → state 停在
     ("error", 旧错误)（workers/_base.py:555-556）。
  2. 重试复用同一个 worker → bind 乐观写 status="loading"，然后**先**起
     probe（registry.py:2742），**后**发 load RPC（registry.py:2893）。
     probe 的 info 调用持着 `inst._probe_lock`，load RPC 也要抢这把锁
     → probe 那条 info **先**到 worker，worker 回的是**上一轮**的 error。
  3. probe 把这个陈旧 error 当成终态 → 写 status="error" 并 `return`
     （探针是「一次性」的）→ 没人再探 → 实例永远卡在「启动失败」，
     哪怕 worker 其实加载成功了。

修法：worker 的 load_handler 是**同步**把 state["status"]="loading" 写好
才回 load 的 RPC（workers/_base.py:494 → :562），所以「load 被确认」≡
「worker 侧状态已经是本轮的」。ack 之前 worker 报上来的 error 必然属于
上一轮 → 主进程用 `inst._load_pending` 标出这个窗口，探针**忽略**该
error 并继续探。

用例：

  T1 探针忽略「load 确认前」的陈旧 error —— 状态不塌成 error，
     且随后能收敛到 running
  T2 真 error（load 已确认）**仍然**是终态 —— 防止修过头：
     status=error + 文案 + 探针退出
  T3 bind 的标志生命周期：load 未确认时为 True、ack 后归零
  T4 走真 bind_instance 的端到端：上一轮 error 残留 → 重试 → running

不占 NPU、不起 DDS、不碰真机。
跑法：``.venv/bin/python scripts/verify_probe_stale_error.py``
"""
from __future__ import annotations

import os
import sys
import threading
import time

os.environ["WDNWEBUI_COMPUTE_MODE"] = "inprocess"   # 不起 DDS Hub

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.compute.registry import ComputeRegistry, ModelInstance  # noqa: E402

PASS = 0
FAIL = 0

STALE_ERROR = "ModuleNotFoundError: No module named 'tokenizers'"
FRESH_ERROR = "RuntimeError: rknn3_init failed"


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✅ {name}" + (f"  ({detail})" if detail else ""))
    else:
        FAIL += 1
        print(f"  ❌ {name}" + (f"  ({detail})" if detail else ""))


# --------------------------------------------------------------------------- #
# 桩件                                                                        #
# --------------------------------------------------------------------------- #
class ScriptedRpc:
    """按剧本回状态的假 RpcClient。

    ``states`` 是一条状态序列：``"loading"`` 这种纯状态串，或
    ``("error", "错误文案")`` 这种带 init_error 的二元组。用完重复最后一条。
    记录 ``seen``（每次读到的状态）和 ``pending_seen``（每次读时
    ``inst._load_pending`` 的值）—— 后者用来验「标志必须在起探针之前
    置位」。
    """

    def __init__(self, states, *, inst=None, load_result=None):
        self._states = list(states)
        self._lock = threading.Lock()
        self._i = 0
        self.seen: list[str] = []
        self.pending_seen: list[bool] = []
        self.load_calls = 0
        self.inst = inst
        self.load_result = load_result or {"ok": True,
                                           "data": {"status": "loading",
                                                    "npu_index": 0}}

    def _next_state(self):
        with self._lock:
            cur = self._states[min(self._i, len(self._states) - 1)]
            self._i += 1
            if isinstance(cur, (tuple, list)):
                status, err = cur[0], cur[1]
            else:
                status, err = cur, ""
            self.seen.append(status)
            if self.inst is not None:
                self.pending_seen.append(getattr(self.inst, '_load_pending', None))
            return status, err

    def call(self, payload, timeout_ms=None):
        action = (payload or {}).get("action")
        if action == "load":
            with self._lock:
                self.load_calls += 1
            return dict(self.load_result)
        if action == "info":
            st, err = self._next_state()
            return {"ok": True,
                    "data": {"status": st, "init_error": err, "npu_index": 0,
                             "model_dir": "/tmp/fake-model-dir"}}
        return {"ok": True, "data": {}}


class GatedRpc(ScriptedRpc):
    """load RPC 先卡在闸门上 —— 用来把「ack 之前」这个窗口拉长到可控。

    真机上 worker 的 load RPC 立即回 {"status":"loading"}，ack 窗口只有几
    毫秒；这里让 load 挂着，测试就能确定性地观察 ack 前后标志的变化。
    """

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.gate = threading.Event()
        self.load_entered = threading.Event()

    def call(self, payload, timeout_ms=None):
        if (payload or {}).get("action") == "load":
            self.load_entered.set()
            self.gate.wait(timeout=10)
        return super().call(payload, timeout_ms=timeout_ms)


def make_inst(reg: ComputeRegistry, model_id="qwen3-vl-4b", npu=0):
    """造一个已登记在 registry 里的实例（key 用 spawn 口径）。"""
    inst = ModelInstance(key=(npu, model_id, "spawned"),
                         npu_index=npu,
                         model_id=model_id,
                         status="not_loaded",
                         _source="spawned")
    reg._instances[inst.key] = inst
    return inst


def wait_until(pred, timeout=5.0, interval=0.05) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return False


# --------------------------------------------------------------------------- #
# T1 / T2 —— 探针的判定逻辑（直接驱动真 _probe_instance）                      #
# --------------------------------------------------------------------------- #
def t1_stale_error_ignored() -> None:
    """load 未确认时读到陈旧 error → 忽略它，之后收敛到 running。"""
    reg = ComputeRegistry()
    inst = make_inst(reg)
    rpc = ScriptedRpc([("error", STALE_ERROR), "loading", "running"], inst=inst)
    inst.rpc_client = rpc
    # 「本次 load 已发起、还没被 worker 确认」
    inst._load_pending = True

    t = threading.Thread(target=reg._probe_instance, args=(inst,), daemon=True)
    t.start()

    ok = wait_until(lambda: inst.status == "running", timeout=6.0)
    check("T1a 陈旧 error 不把状态打回 error（load 确认前）",
          inst.status != "error",
          f"status={inst.status!r}")
    check("T1b 陈旧 error 之后仍收敛到 running",
          ok and inst.status == "running",
          f"status={inst.status!r} seen={rpc.seen}")
    check("T1c 陈旧 error 不写进 inst.error",
          inst.error == "",
          f"error={inst.error!r}")
    check("T1d 探针没有自杀（仍在运行）",
          t.is_alive(),
          f"alive={t.is_alive()}")


def t2_real_error_still_terminal() -> None:
    """load 已确认后 worker 报 error → 仍然是终态（别修过头）。"""
    reg = ComputeRegistry()
    inst = make_inst(reg)
    rpc = ScriptedRpc([("error", FRESH_ERROR)], inst=inst)
    inst.rpc_client = rpc
    inst._load_pending = False          # load 已被 worker 确认

    t = threading.Thread(target=reg._probe_instance, args=(inst,), daemon=True)
    t.start()

    ok = wait_until(lambda: inst.status == "error", timeout=6.0)
    check("T2a load 确认后的 error 仍然是终态",
          ok and inst.status == "error",
          f"status={inst.status!r}")
    check("T2b 错误文案取的是 worker 这次的 init_error",
          inst.error == FRESH_ERROR,
          f"error={inst.error!r}")
    t.join(timeout=2.0)
    check("T2c 真终态下探针退出（一次性线程的原有语义）",
          not t.is_alive(),
          f"alive={t.is_alive()}")


# --------------------------------------------------------------------------- #
# T3 —— bind 的标志生命周期                                                    #
# --------------------------------------------------------------------------- #
def t3_flag_lifecycle() -> None:
    reg = ComputeRegistry()
    inst = make_inst(reg)
    rpc = GatedRpc(["loading", "running"], inst=inst)
    inst.rpc_client = rpc

    reg.bind_instance(inst, 0)

    check("T3a load 尚未被 worker 确认时 _load_pending 为 True",
          rpc.load_entered.wait(timeout=3.0) and getattr(inst, '_load_pending', None) is True,
          f"_load_pending={getattr(inst, '_load_pending', None)!r}")
    check("T3b 起探针之前标志就已置位（探针第一次 info 读时为 True）",
          bool(rpc.pending_seen) and rpc.pending_seen[0] is True,
          f"pending_seen={rpc.pending_seen[:3]}")

    rpc.gate.set()                      # 放行 load → worker 回 ack
    ok = wait_until(lambda: getattr(inst, '_load_pending', None) is False, timeout=5.0)
    check("T3c load ack 之后 _load_pending 归零",
          ok,
          f"_load_pending={getattr(inst, '_load_pending', None)!r}")

    ok = wait_until(lambda: inst.status == "running", timeout=6.0)
    check("T3d 端到端：重试后收敛到 running",
          ok and inst.status == "running",
          f"status={inst.status!r} seen={rpc.seen}")


# --------------------------------------------------------------------------- #
# T4 —— 真 bind_instance 的端到端（上一轮 error 残留 → 重试 → running）          #
# --------------------------------------------------------------------------- #
def t4_bind_retry_end_to_end() -> None:
    reg = ComputeRegistry()
    inst = make_inst(reg)
    # 上一轮的残留：worker 进程还活着，state 停在 ("error", 旧错误)
    rpc = ScriptedRpc([("error", STALE_ERROR), "loading", "running"], inst=inst)
    inst.rpc_client = rpc

    reg.bind_instance(inst, 0)

    ok = wait_until(lambda: inst.status == "running", timeout=8.0)
    check("T4a 重试后收敛到 running（没被陈旧 error 拖死）",
          ok and inst.status == "running",
          f"status={inst.status!r} seen={rpc.seen}")
    check("T4b 最终状态不是 error", inst.status != "error",
          f"status={inst.status!r}")
    check("T4c 没有残留的错误文案", inst.error == "",
          f"error={inst.error!r}")
    if rpc.pending_seen and rpc.pending_seen[0] is False:
        print("     ℹ️  这次 load 先于探针首读完成（另一种时序），"
              "T4 只断言最终收敛 —— 陈旧 error 的判定由 T1 覆盖")


def main() -> int:
    print("=== 探针陈旧 error 判定回归 ===")
    t1_stale_error_ignored()
    t2_real_error_still_terminal()
    t3_flag_lifecycle()
    t4_bind_retry_end_to_end()
    print(f"\n{'PASS' if FAIL == 0 else 'FAIL'}: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
