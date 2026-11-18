#!/usr/bin/env python3
"""回归：网页翻译的**卡位占用估算**必须只算本机实例，不算远端 peer。

背景（2026-11-18 真机现场）
--------------------------
用户在「网页翻译」向导里点「一键开启所需模型」，日志里刷：

    apps.web_translate: web_translate: 卡 [0] 放不下翻译模型（被别的模型占着），只在卡 [1] 上起

但「算力配置」页里 **RK182x 卡上根本没有别的模型** —— 本板一个本地
worker 都没有，3 张 1828 全空。

根因：``_cards_with_room_for_one_copy()`` 遍历 registry 快照的**全部**
实例，只过滤了「不是翻译模型自己」和「不在 running/loading」，**没看
``_source``** ⇒ ``_source == "adopted"``（**别的板子**通过 DDS 暴露的
service）的 ``npu_index``（那是**对端板自己的卡号**）+ 它的
``memory_estimate_mb`` 被当成了**本板**的卡位占用。

真机数据：peer 板 ``172.16.9.147`` 的 ``LocateAnything-3b`` 报
``npu=1 / 5500MB`` ⇒ 本板卡 1 被判占满（5500+2200 > 5120）⇒ 翻译
只剩卡 0 的 2 份（而不是 4 份），日志还说「被别的模型占着」。

⇒ 本脚本用**假 registry**（不碰真机、不起模型）断言：

1. 只有远端 peer 实例时，**所有**本板协卡都算放得下（一张都不跳）；
2. 本机 spawned 实例占着的卡才跳；
3. 本机孤儿（``adopted_local``）实例**也要**算占用 —— 它吃本板显存；
4. ``_conflict()``（卡位冲突提示）同样不认 peer。

用法::

    .venv/bin/python scripts/verify_web_translate_peer_isolation.py
"""
from __future__ import annotations

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from card_dirs import card_dir as _card_dir  # noqa: E402

# 卡片源码目录（插件卡不在 app/apps/ 下）
CARD_DIR = str(_card_dir("web-translate"))

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
# 假 registry：只提供 service.py 用到的那两个方法
# --------------------------------------------------------------------------- #
class FakeRegistry:
    """``model_cards()`` + ``snapshot()`` 就够了（service.py 只用这两个）。"""

    def __init__(self, cards=(0, 1, 2), instances=()):
        self._cards = list(cards)
        self._instances = list(instances)

    def model_cards(self):
        return list(self._cards)

    def snapshot(self):
        return {"instances": [dict(i) for i in self._instances]}


def inst(model_id, npu_index, mem_mb, source="spawned", status="running",
         npu_indices=None, requires_cards=1, label=""):
    """造一条快照里的实例记录（字段与 registry.to_dict() 对齐）。"""
    return {
        "model_id": model_id,
        "label": label or model_id,
        "npu_index": npu_index,
        "npu_indices": list(npu_indices) if npu_indices is not None
                       else [npu_index],
        "status": status,
        "memory_estimate_mb": mem_mb,
        "requires_cards": requires_cards,
        "_source": source,
        "_peer_ip": "172.16.9.147" if source == "adopted" else "",
        "_peer_pid": 3113 if source == "adopted" else 0,
    }


def main() -> int:
    # 延迟 import：卡片模块在插件包里，路径由 card_dirs 解析
    from wdnapps_audio_chat.cards.web_translate import service

    print("=" * 72)
    print("网页翻译：卡位占用估算的 peer 隔离")
    print("=" * 72)
    print(f"  卡片源码目录: {CARD_DIR}")

    # ---------------------------------------------------------------- [1] --
    print("\n[1] 只有远端 peer 实例 ⇒ 本板每一张卡都算放得下")
    peer = inst("LocateAnything-3b", 1, 5500, source="adopted",
                label="[GD]视觉定位模型")
    reg = FakeRegistry(cards=(0, 1, 2), instances=[peer])
    free = service._cards_with_room_for_one_copy(reg)
    check("peer 的 5500MB 没被算到本板任何一张卡上",
          free == {0, 1, 2}, f"free={sorted(free)}")
    slots = service._slots(reg)
    check("槽位表保持满配（卡0×2 + 卡1×2 = 4 份）",
          slots == [(0, 0), (0, 1), (1, 0), (1, 1)], f"slots={slots}")
    check("没有卡被跳过（不是 2 份）", len(slots) == 4, f"slots={slots}")

    # ---------------------------------------------------------------- [2] --
    print("\n[2] 换一张卡号的 peer（npu=0）也一样不影响本板")
    peer0 = inst("LocateAnything-3b", 0, 5500, source="adopted")
    reg = FakeRegistry(cards=(0, 1, 2), instances=[peer0])
    free = service._cards_with_room_for_one_copy(reg)
    check("peer 报 npu=0 时本板卡 0 依然算空",
          free == {0, 1, 2}, f"free={sorted(free)}")
    slots = service._slots(reg)
    check("槽位表仍是 4 份", len(slots) == 4, f"slots={slots}")

    # ---------------------------------------------------------------- [3] --
    print("\n[3] 本机 spawned 实例占着的卡**要**跳（本机显存是真占的）")
    local = inst("qwen3.5-vl-4b", 0, 3500, source="spawned",
                 label="[VLLM]视觉文字多模态v3.5")
    reg = FakeRegistry(cards=(0, 1, 2), instances=[local])
    free = service._cards_with_room_for_one_copy(reg)
    check("卡 0 被排除（3500 + 2200 > 5120）", 0 not in free, f"free={sorted(free)}")
    check("卡 1/2 仍可用", {1, 2} <= free, f"free={sorted(free)}")
    slots = service._slots(reg)
    check("槽位表落到卡 1 的两份",
          slots == [(1, 0), (1, 1)], f"slots={slots}")

    # ---------------------------------------------------------------- [4] --
    print("\n[4] 本机孤儿（adopted_local）也占本板显存 ⇒ 一样要跳")
    orphan = inst("qwen3.5-vl-4b", 0, 3500, source="adopted_local")
    reg = FakeRegistry(cards=(0, 1, 2), instances=[orphan])
    free = service._cards_with_room_for_one_copy(reg)
    check("卡 0 被排除", 0 not in free, f"free={sorted(free)}")

    # ---------------------------------------------------------------- [5] --
    print("\n[5] peer 与本机模型并存：只按本机那份算")
    reg = FakeRegistry(cards=(0, 1, 2),
                       instances=[peer, inst("qwen3.5-vl-4b", 1, 3500)])
    free = service._cards_with_room_for_one_copy(reg)
    check("卡 1 被本机模型排除，卡 0 不受 peer 影响",
          free == {0, 2}, f"free={sorted(free)}")

    # ---------------------------------------------------------------- [6] --
    print("\n[6] 卡位冲突提示（_conflict）同样不认 peer")
    mgr = service.WebTranslateManager()
    peer2 = inst("qwenimage21", 0, 3000, source="adopted",
                 requires_cards=2, label="[IMG]图像生成")
    reg = FakeRegistry(cards=(0, 1, 2), instances=[peer2])
    check("peer 占用 2 张卡**不**报卡位冲突",
          mgr._conflict(reg, service.DEFAULT_MODEL) == "",
          mgr._conflict(reg, service.DEFAULT_MODEL))
    local2 = inst("qwenimage21", 0, 3000, source="spawned",
                  requires_cards=2, label="[IMG]图像生成")
    reg = FakeRegistry(cards=(0, 1, 2), instances=[local2])
    conflict = mgr._conflict(reg, service.DEFAULT_MODEL)
    check("本机模型占 2 张卡**要**报卡位冲突",
          bool(conflict) and "卡位冲突" in conflict, repr(conflict))

    # ---------------------------------------------------------------- [7] --
    print("\n[7] 翻译模型自己的实例不算占用（第二次 enable 不该被自己挡住）")
    mine = inst(service.DEFAULT_MODEL, 0, 2200, source="spawned")
    reg = FakeRegistry(cards=(0, 1, 2), instances=[mine])
    free = service._cards_with_room_for_one_copy(reg)
    check("自己的实例不影响可用卡", free == {0, 1, 2}, f"free={sorted(free)}")

    # ---------------------------------------------------------------- [8] --
    print("\n[8] 负向：把 peer 当成本机（模拟回归前的写法）⇒ 必须被排除")
    # 这一条是「反向验证」：证明上面的断言真的能抓住那个 bug。
    # 用**只有 peer 的那份数据**、按「不看 _source」的老口径算一次 ——
    # 结果必须与正确口径不同（老口径会把卡算满）。
    peer_reg = FakeRegistry(cards=(0, 1, 2), instances=[peer])
    used = {}
    for i in peer_reg.snapshot()["instances"]:
        if i.get("model_id") == service.DEFAULT_MODEL:
            continue
        if i.get("status") not in ("running", "loading"):
            continue
        mem = int(i.get("memory_estimate_mb") or 0)
        for c in (i.get("npu_indices") or [i.get("npu_index")]):
            if c is None:
                continue
            used[int(c)] = used.get(int(c), 0) + mem
    naive = {c for c in (0, 1, 2) if used.get(c, 0) + 2200 <= 5120}
    check("老口径（不看 _source）会把卡算满（= 回归前的样子）",
          naive != {0, 1, 2}, f"naive={sorted(naive)}")
    # 老口径下的槽位表 = 只剩 2 份 —— 正是现场看到的「只起 2 份」
    naive_slots = [(n, r) for (n, r) in service.DEFAULT_SLOTS if n in naive]
    check("老口径下槽位只剩 2 份（现场的「只起 2 份」）",
          len(naive_slots) == 2 and len(service._slots(peer_reg)) == 4,
          f"naive_slots={naive_slots}")

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
