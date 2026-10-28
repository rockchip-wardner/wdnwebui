#!/usr/bin/env python3
"""回归：autostart 的「一卡一模型」不变量 + ``--npu`` 是否真的下发。

覆盖 v0.25 修的 4 个真机 bug（详见 AGENTS.md「一卡一模型是不变量」）：

  1. ``--npu`` 的卡号 0 是 falsy → autostart 的 npu0 条目**不带 --npu**，
     worker 起来了却永远不加载模型。
  2. boot 裁剪只在**单卡**时生效 → 双卡板同卡多条照样全起，撞 rc=-6。
  3. ``set_active_app`` 增量追加 autostart 时**不维护卡位唯一性** →
     同卡挂好几个模型，配置无限膨胀。
  4. dedup 把「未绑卡(-1)」的实例当成「这张卡上已经有了」→ 同模型配两张
     卡时第二条被静默吞掉。

跑法：``.venv/bin/python scripts/verify_autostart_card_uniqueness.py``
（纯逻辑，不碰真机状态、不启停任何模型）
"""
from __future__ import annotations

import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.compute.registry import (  # noqa: E402
    _crop_autostart_for_cards,
    _instance_reusable,
    _merge_autostart_entry,
    _wants_eager_load,
    _worker_npu_arg,
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


def ids(entries: list[dict]) -> str:
    return ",".join(f"{e['model']}@{e['npu_index']}" for e in entries)


print("\n【1】_wants_eager_load / _worker_npu_arg —— worker 到底带不带 --npu")
check("eager_load=None（autostart 路径）→ 要 worker 自己加载",
      _wants_eager_load(None) is True)
check("eager_load=False（算力页启用路径）→ 不要",
      _wants_eager_load(False) is False)
check("eager_load=True 显式覆盖 → 要",
      _wants_eager_load(True) is True)
check("**0 号卡也要带 --npu**（卡号 0 不是「不要」）",
      _worker_npu_arg(0, None) == ["--npu", "0"])
check("1 号卡带 --npu 1", _worker_npu_arg(1, None) == ["--npu", "1"])
check("没给卡号 → 不带", _worker_npu_arg(None, None) == [])
check("eager_load=False（算力页走 bind）→ 不带",
      _worker_npu_arg(0, False) == [])

print("\n【2】_crop_autostart_for_cards —— boot 裁剪（每卡一条）")
dirty = [
    {"model": "qwen3-vl-4b", "npu_index": 0},
    {"model": "LocateAnything-3b", "npu_index": 0},
    {"model": "qwen3-vl-embedding-2b", "npu_index": 0},
    {"model": "qwen3.5-vl-4b", "npu_index": 0},
    {"model": "qwen3-vl-4b", "npu_index": 1},
    {"model": "qwen2.5-omni-3b", "npu_index": 1},
    {"model": "LocateAnything-3b", "npu_index": 1},
]
kept, dropped = _crop_autostart_for_cards(dirty, [0, 1])
check("7 条脏配置裁成每卡一条", ids(kept) == "qwen3-vl-4b@0,qwen3-vl-4b@1",
      ids(kept))
check("同卡第 2~4 条被丢弃", len(kept) == 2 and len(dropped) == 5,
      f"kept={len(kept)} dropped={len(dropped)}")

# 真机现状：2 张 PCI 卡 + 2 个 USB 设备（跑不了 rknn 模型）
kept2, dropped2 = _crop_autostart_for_cards(
    [{"model": "qwen3-vl-4b", "npu_index": 0},
     {"model": "qwen3-vl-4b", "npu_index": 2},
     {"model": "qwen3-vl-4b", "npu_index": 3}],
    [0, 1])
check("指向非算力卡（USB 形态）的条目被丢弃",
      ids(kept2) == "qwen3-vl-4b@0" and len(dropped2) == 2, ids(kept2))
check("model_cards 为空时原样返回（不误杀）",
      ids(_crop_autostart_for_cards(dirty, [])[0]) == ids(dirty))
check("双卡各一条 → 一条都不裁",
      ids(_crop_autostart_for_cards(
          [{"model": "a", "npu_index": 0}, {"model": "b", "npu_index": 1}],
          [0, 1])[0]) == "a@0,b@1")

print("\n【3】_merge_autostart_entry —— set_active_app 维护卡位唯一性")
merged, added, stale = _merge_autostart_entry([], "qwen3-vl-4b", 0)
check("空配置 → 新增一条", added and ids(merged) == "qwen3-vl-4b@0",
      ids(merged))

merged, added, stale = _merge_autostart_entry(
    [{"model": "qwen3-vl-4b", "npu_index": 0}], "qwen3-vl-4b", 0)
check("同 (model, card) 已存在 → 幂等，不新增", not added and not stale,
      ids(merged))

merged, added, stale = _merge_autostart_entry(
    [{"model": "LocateAnything-3b", "npu_index": 0}], "qwen3-vl-4b", 0)
check("同卡不同模型 → 旧条目被替换",
      ids(merged) == "qwen3-vl-4b@0" and added
      and ids(stale) == "LocateAnything-3b@0", ids(merged))
check("替换后该卡只剩一条",
      len([e for e in merged if e["npu_index"] == 0]) == 1)

merged, added, stale = _merge_autostart_entry(
    [{"model": "qwen3-vl-4b", "npu_index": 0},
     {"model": "qwen2.5-omni-3b", "npu_index": 1}],
    "LocateAnything-3b", 0)
check("双卡组合下只动同卡那一条，另一张卡不受影响",
      ids(merged) == "LocateAnything-3b@0,qwen2.5-omni-3b@1", ids(merged))

merged, added, stale = _merge_autostart_entry(
    [{"model": "qwen3-vl-4b", "npu_index": 0},
     {"model": "LocateAnything-3b", "npu_index": 0}], "qwen3-vl-4b", 0)
check("同卡已有多条历史沉积 → 全部清掉，只留本条目",
      ids(merged) == "qwen3-vl-4b@0" and len(stale) == 1, ids(merged))

merged, added, stale = _merge_autostart_entry(
    [{"model": "qwen3-vl-4b", "npu_index": 0}],
    "qwen3-vl-4b", 1)
check("同模型换卡 → 两条都在（『同模型跑多张卡』是合法配置）",
      ids(merged) == "qwen3-vl-4b@0,qwen3-vl-4b@1" and added, ids(merged))

print("\n【4】_instance_reusable —— dedup 判定（同模型多卡不被合并）")
check("不指定卡 → 一律复用",
      _instance_reusable(-1, "spawned", None, None) is True)
check("已绑在同卡 → 复用",
      _instance_reusable(0, "spawned", 0, None) is True)
check("已绑在**别的**卡 → 不复用（『同模型跑多张卡』要的正是这个）",
      _instance_reusable(0, "spawned", 1, None) is False)
check("未绑卡(-1) + 算力页路径(eager=False) → 复用（随后 bind 补卡号）",
      _instance_reusable(-1, "spawned", 0, False) is True)
check("未绑卡(-1) + autostart 路径(eager=None) → **不复用**"
      "（它没带 --npu，复用了就等于这条卡位被吞掉）",
      _instance_reusable(-1, "spawned", 0, None) is False)
check("未绑卡(-1) + eager_load=True → 不复用",
      _instance_reusable(-1, "spawned", 0, True) is False)
check("LAN peer 实例(adopted) → 一律复用，不动对端算力",
      _instance_reusable(-1, "adopted", 0, True) is True)

print("\n【5】不变量：任何合并序列之后，每张卡最多一条")
seq = [
    ("qwen3-vl-4b", 0), ("LocateAnything-3b", 0), ("qwen3-vl-4b", 0),
    ("qwen2.5-omni-3b", 1), ("LocateAnything-3b", 1),
    ("qwen3-vl-embedding-2b", 0), ("qwen3.5-vl-4b", 0),
    ("qwen3-vl-4b", 1),
]
acc: list[dict] = []
for m, c in seq:
    acc, _, _ = _merge_autostart_entry(acc, m, c)
per_card: dict[int, int] = {}
for e in acc:
    per_card[e["npu_index"]] = per_card.get(e["npu_index"], 0) + 1
check("7 次追加后每张卡仍只有一条",
      all(v == 1 for v in per_card.values()), str(per_card))
check("合并结果 = " + ids(acc), True, ids(acc))

print("\n【6】set_active_app 集成 —— 真的写回 config 并清理同卡陈旧条目")


def _check_set_active_app_integration():
    """set_active_app 真的写回 config 并清理同卡陈旧条目。"""
    from app.core.compute import registry as reg_mod

    class _FakeConfig:
        def __init__(self, section: dict):
            self.section = section

        def get_section(self, name: str, default=None):
            return self.section.get(name, default)

        def set_section(self, name: str, value) -> None:
            self.section[name] = value

    dirty_cfg = _FakeConfig({"compute": {
        "autostart": [
            {"model": "qwen3-vl-4b", "npu_index": 0},      # 陈旧：npu0 现在跑别的
            {"model": "LocateAnything-3b", "npu_index": 1},  # 陈旧：npu1 现在跑别的
        ],
        "active_app": None,
    }})
    orig_get_config = reg_mod.get_config
    reg_mod.get_config = lambda: dirty_cfg
    try:
        reg = reg_mod.ComputeRegistry.__new__(reg_mod.ComputeRegistry)
        reg._lock = threading.RLock()
        # 一个 running 的本地实例：LocateAnything-3b 跑在 npu0
        reg._instances = {
            (-1, "LocateAnything-3b", "spawned"): reg_mod.ModelInstance(
                key=(-1, "LocateAnything-3b", "spawned"),
                npu_index=0, model_id="LocateAnything-3b",
                capability_types=["swcap_vision_grounding"],
                instance_name="LocateAnything-3b", label="LocateAnything-3b",
                memory_estimate_mb=3000, status="running",
                queued_at=0.0, started_at=0.0, _source="spawned"),
        }
        out = reg.set_active_app("video-target-localization", ["swcap_vision_grounding"])
    finally:
        reg_mod.get_config = orig_get_config

    written = dirty_cfg.get_section("compute")["autostart"]
    check("config 写回后每卡只有一条",
          sorted({e["npu_index"] for e in written}) == [0, 1]
          and len(written) == 2, ids(written))
    check("npu0 的陈旧条目被换成当前在跑的模型",
          any(e["npu_index"] == 0 and e["model"] == "LocateAnything-3b"
              for e in written), ids(written))
    check("返回值带 autostart_replaced（可观测）",
          isinstance(out.get("autostart_replaced"), list)
          and len(out["autostart_replaced"]) == 1,
          str(out.get("autostart_replaced")))
    check("active_app 一并落盘",
          dirty_cfg.get_section("compute").get("active_app")
          == "video-target-localization")


_check_set_active_app_integration()

print("\n【7】set_active_app 多卡模型 —— 9B 写回 [0, 1]")


def _check_set_active_app_multicard():
    from app.core.compute import registry as reg_mod

    class _FakeConfig:
        def __init__(self, section: dict):
            self.section = section

        def get_section(self, name: str, default=None):
            return self.section.get(name, default)

        def set_section(self, name: str, value) -> None:
            self.section[name] = value

    cfg = _FakeConfig({"compute": {"autostart": [], "active_app": None}})
    orig_get_config = reg_mod.get_config
    reg_mod.get_config = lambda: cfg
    try:
        reg = reg_mod.ComputeRegistry.__new__(reg_mod.ComputeRegistry)
        reg._lock = threading.RLock()
        reg._instances = {
            (0, "qwen3.5-vl-9b", "spawned"): reg_mod.ModelInstance(
                key=(0, "qwen3.5-vl-9b", "spawned"),
                npu_index=0, npu_indices=(0, 1),
                model_id="qwen3.5-vl-9b",
                capability_types=["swcap_text_chat", "swcap_vision_chat",
                                  "swcap_tool_call"],
                instance_name="qwen3.5-vl-9b", label="qwen3.5-vl-9b",
                memory_estimate_mb=7000, status="running",
                queued_at=0.0, started_at=0.0, _source="spawned"),
        }
        out = reg.set_active_app("text-chat", ["swcap_text_chat"])
    finally:
        reg_mod.get_config = orig_get_config

    written = cfg.get_section("compute")["autostart"]
    check("9B autostart 写回 [0, 1]",
          written == [{"model": "qwen3.5-vl-9b", "npu_index": [0, 1]}],
          str(written))
    check("9B extended 记录多卡",
          out.get("autostart_extended")
          == [{"capability": "swcap_text_chat", "model": "qwen3.5-vl-9b",
               "npu_index": [0, 1]}],
          str(out.get("autostart_extended")))


_check_set_active_app_multicard()

print("\n" + "=" * 56)
print(f"通过 {PASS} / {PASS + FAIL}")
if FAIL:
    print("❌ 有失败项")
    sys.exit(1)
print("✅ 全部通过")
