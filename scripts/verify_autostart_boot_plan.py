#!/usr/bin/env python3
"""回归：boot autostart 的判定 —— **本板**这张卡上有没有在跑的 worker。

覆盖 v0.26 修的真机 bug：开机自启声明的两个模型**一个都没起来**。

症状：重启板子后 ``compute.autostart`` 里明明有
``qwen3-vl-4b@npu0`` / ``LocateAnything-3b@npu1``，但本板一个 worker
都没有。日志是::

    boot: autostart qwen3-vl-4b → adopted adopted (peer ... status=not_loaded);
    不发 load —— 算力归对方，本板不动

根因：peer 那边**进程在、模型没加载**（``not_loaded``），autostart 却把
它当成「算力归对方」而放弃本板启动。两块板互相 adopt 对方的空壳 →
谁都不加载的死锁。

而「借 peer 的算力」这条路在开机自启上是脆的：借来的算力会静默失效
（peer 那边 unload 了，本板就没人跑，而本板声明的卡一直空着 —— 症状和
「没自启」一模一样）。所以 autostart 一律**本板自己起**：``npu_index`` 是
本板卡号，声明了就要在这张卡上跑。借 peer 是**运行期**（``get_session``
找不到本机实例时）的特性。

判据见 ``_autostart_plan``。

跑法：``.venv/bin/python scripts/verify_autostart_boot_plan.py``
（纯逻辑，不碰真机状态、不启停任何模型）
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.compute.registry import (  # noqa: E402
    _autostart_needs_load,
    _autostart_plan,
)

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✅ {name}" + (f"  ({detail})" if detail else ""))
    else:
        FAIL += 1
        print(f"  ❌ {name}" + (f"  ({detail})" if detail else ""))


class _Inst:
    """只带 _autostart_plan 用到的字段的假实例。"""

    def __init__(self, model_id: str, npu_index: int, status: str,
                 source: str, peer_ip: str = ""):
        self.model_id = model_id
        self.npu_index = npu_index
        self.npu_indices = (npu_index,) if npu_index >= 0 else ()
        self.status = status
        self._source = source
        self._peer_ip = peer_ip
        self._peer_name = peer_ip

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (f"<{self.model_id} npu={self.npu_index} "
                f"{self.status} {self._source}>")


M = "qwen3-vl-4b"

print("\n【1】本板这张卡上已经在跑 → 跳过")
check("本板 running → skip",
      _autostart_plan([_Inst(M, 0, "running", "spawned")], M, 0) == "skip")
check("本板 loading → skip",
      _autostart_plan([_Inst(M, 0, "loading", "spawned")], M, 0) == "skip")
check("本机孤儿(adopted_local) running → 也算本板在跑",
      _autostart_plan([_Inst(M, 0, "running", "adopted_local")], M, 0)
      == "skip")

print("\n【2】本板有进程但模型没加载 → 本板自己起")
check("本板 not_loaded → spawn_local",
      _autostart_plan([_Inst(M, 0, "not_loaded", "spawned")], M, 0)
      == "spawn_local")
check("本板 not_loaded（没绑卡，-1）→ spawn_local",
      _autostart_plan([_Inst(M, -1, "not_loaded", "spawned")], M, 0)
      == "spawn_local")
check("本板 error → spawn_local",
      _autostart_plan([_Inst(M, 0, "error", "spawned")], M, 0)
      == "spawn_local")
check("本板在**别的卡**上跑着 → 本板这张卡再起一个（同模型跑两张卡）",
      _autostart_plan([_Inst(M, 0, "running", "spawned")], M, 1)
      == "spawn_local")

print("\n【3】peer 不参与判定 —— autostart 声明的是本板")
check("peer **running** → 仍然本板自己起（不借 peer 的算力）",
      _autostart_plan([_Inst(M, -1, "running", "adopted", "1.2.3.4")], M, 0)
      == "spawn_local")
check("peer not_loaded → 本板自己起",
      _autostart_plan([_Inst(M, -1, "not_loaded", "adopted", "1.2.3.4")], M, 0)
      == "spawn_local")
check("peer error → 本板自己起",
      _autostart_plan([_Inst(M, -1, "error", "adopted", "1.2.3.4")], M, 0)
      == "spawn_local")
check("peer 卡 + 本板这张卡在跑 → 仍然 skip",
      _autostart_plan([_Inst(M, 0, "running", "spawned"),
                       _Inst(M, -1, "running", "adopted", "1.2.3.4")], M, 0)
      == "skip")

print("\n【4】多板互相 adopt 空壳的老坑（本次真机症状）")
two_shells = [_Inst("qwen3-vl-4b", -1, "not_loaded", "adopted", "172.16.9.45"),
              _Inst("LocateAnything-3b", -1, "not_loaded", "adopted",
                    "172.16.9.109")]
check("peer 空壳 → 本板 spawn_local（不再「算力归对方」）",
      _autostart_plan(two_shells, "qwen3-vl-4b", 0) == "spawn_local")
check("peer 空壳 → 另一个模型也本板 spawn_local",
      _autostart_plan(two_shells, "LocateAnything-3b", 1) == "spawn_local")

print("\n【5】没有本机实例时一律本板起")
check("registry 里啥都没有 → spawn_local",
      _autostart_plan([], M, 0) == "spawn_local")
check("只有别的模型 → spawn_local",
      _autostart_plan([_Inst("qwen3-tts", 0, "running", "spawned")], M, 0)
      == "spawn_local")

print("\n【6】复用的 worker 要不要补 load RPC")
NP_ARGV = ["python", "compute_worker.py", "--model", M, "--local-only",
           "--npu", "0"]
NO_NP_ARGV = ["python", "compute_worker.py", "--model", M, "--local-only"]
check("带 --npu → 不补（它会自己加载）",
      _autostart_needs_load(NP_ARGV, "not_loaded") is False)
check("没带 --npu + worker not_loaded → 补",
      _autostart_needs_load(NO_NP_ARGV, "not_loaded") is True)
check("没带 --npu + worker 正在 loading → 不补（避免两条 load 叠 session）",
      _autostart_needs_load(NO_NP_ARGV, "loading") is False)
check("没带 --npu + worker running → 不补",
      _autostart_needs_load(NO_NP_ARGV, "running") is False)
check("问不到状态 → 不补（交给探针继续同步）",
      _autostart_needs_load(NO_NP_ARGV, None) is False)

print("\n" + "=" * 56)
print(f"通过 {PASS} / {PASS + FAIL}")
if FAIL:
    print("❌ 有失败项")
    sys.exit(1)
print("✅ 全部通过")
