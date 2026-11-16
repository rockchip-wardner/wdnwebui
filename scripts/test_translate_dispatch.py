#!/usr/bin/env python
"""NPU 多 session 分发自检 —— 「4 个 session 各翻各的，不重复」。

背景
----
网页翻译默认起 **4 个实例**（2 张 1828 协卡 × 每卡 2 份）。这 4 个实例对应
4 个**独立 worker 进程**，每个进程有自己的 rknn3 context 和 per-PID DDS
service —— 所以每个都能**同时**跑一个推理。

``NpuTranslateProvider._translate_many()`` 负责把一批文案分给这 4 个 session。
分配规则是 **按条轮转**：第 ``i`` 条给第 ``i % 4`` 个 session。

为什么必须是「轮转」而不是「成块」
--------------------------------
``ThreadPoolExecutor.map`` 是**按顺序**取任务的。若 items 成块排列（前一半
给 A、后一半给 B），最先跑起来的 N 个任务会全落在 A 上 —— 并发根本没发生
（第一版基准就栽在这，测出「双实例 1.00×」的假象）。

这个脚本断言三件事
-----------------
1. **不重复** —— 每条文案的推理**恰好一次**（不能两个 session 翻同一条，
   那是白白浪费算力）；
2. **不遗漏** —— 24 条全都翻了（不能漏）；
3. **均匀** —— 4 个 session 各拿 1/4（不能全压在同一个上，那就退化成串行）。

用法::

    .venv/bin/python scripts/test_translate_dispatch.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.translate.npu_provider import NpuTranslateProvider  # noqa: E402

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


class FakeSession:
    """假 session —— 记录自己被喂了哪些 prompt。"""

    def __init__(self, name: str):
        self.name = name
        self.seen: list[str] = []

    async def respond(self, prompt=None, max_new_tokens=None):
        self.seen.append(prompt)
        # 提示词形如 "Translate the following text from zh to en:\n<原文>"
        return prompt.split("\n", 1)[-1] + " [en]"


def bodies(n: int) -> list[str]:
    return ["界面文案 %02d" % i for i in range(n)]


def main() -> int:
    print("=" * 72)
    print("NPU 多 session 分发自检")
    print("=" * 72)

    print("\n[1] 24 条 → 4 个 session")
    sessions = [FakeSession("s%d" % i) for i in range(4)]
    prov = NpuTranslateProvider()
    prov._sessions = lambda: sessions          # 注入假 session
    texts = bodies(24)
    outs, misses = prov._translate_many(texts, "zh-CN", "en")

    # ---- 不遗漏 ----
    check("输出条数对得上", len(outs) == 24, str(len(outs)))
    check("每条都出了译文", all(o for o in outs), "")
    check("没有翻失败的条目", misses == set(), str(misses))

    # ---- 每个 session 收到什么 ----
    got = {s.name: list(s.seen) for s in sessions}
    for s in sessions:
        print("      %s 收到 %d 条" % (s.name, len(got[s.name])))

    # ---- 不重复 ----
    all_prompts = [p for s in sessions for p in s.seen]
    check("总推理次数 == 文案条数(没有重复翻译)",
          len(all_prompts) == 24, "推理 %d 次 / 文案 %d 条" % (len(all_prompts), 24))
    check("没有哪条被翻两次", len(set(all_prompts)) == len(all_prompts),
          "unique=%d" % len(set(all_prompts)))

    # ---- 不遗漏(每个原文都出现了) ----
    missing = [t for t in texts if not any(t in p for p in all_prompts)]
    check("没有漏掉任何一条", not missing, str(missing[:3]))

    # ---- 均匀 ----
    counts = [len(s.seen) for s in sessions]
    check("4 个 session 都被用到(并行)", all(c > 0 for c in counts), str(counts))
    check("每个 session 各拿 6 条(均匀)", counts == [6, 6, 6, 6], str(counts))

    # ---- 轮转(而不是成块)----
    # 第 i 条给第 i%4 个 ⇒ s0 拿到 index 0,4,8,12,16,20
    expect = {
        "s0": [0, 4, 8, 12, 16, 20],
        "s1": [1, 5, 9, 13, 17, 21],
        "s2": [2, 6, 10, 14, 18, 22],
        "s3": [3, 7, 11, 15, 19, 23],
    }
    for s in sessions:
        idxs = sorted(int(p.split("界面文案 ")[1].split(" ")[0]) for p in s.seen)
        # ⚠️ 只比**集合**，不比到达顺序 —— 4 个 session 是并发跑的，
        # 同一 session 内部哪条先到取决于调度（老版本这里偶发失败）。
        check("%s 拿到的是轮转出来的 %s" % (s.name, expect[s.name]),
              idxs == expect[s.name], str(idxs))

    print("\n[2] 只有 1 个 session 时退回串行(不报错)")
    one = FakeSession("solo")
    prov1 = NpuTranslateProvider()
    prov1._sessions = lambda: [one]
    outs1, misses1 = prov1._translate_many(bodies(3), "zh-CN", "en")
    check("3 条都翻了", len(outs1) == 3 and all(outs1), str(outs1))
    check("没有失败条目", misses1 == set(), str(misses1))
    check("都落在那一个 session 上", len(one.seen) == 3, str(len(one.seen)))

    print("\n[3] 一个 session 都没有时抛 TranslationError(让链回退外网)")
    from app.core.translate.base import TranslationError
    prov0 = NpuTranslateProvider()
    prov0._sessions = lambda: []
    try:
        prov0._translate_many(bodies(1), "zh-CN", "en")
        check("应当抛 TranslationError", False, "没抛")
    except TranslationError:
        check("应当抛 TranslationError", True)

    print("\n[4] 一条翻不出来 → **只牵连它自己**(其余照常翻)")
    class PickySession(FakeSession):
        """第 7 条（index 6）故意复读原文 —— 模拟「这条它就是翻不出来」。"""

        async def respond(self, prompt=None, max_new_tokens=None):
            self.seen.append(prompt)
            body = prompt.split("\n", 1)[-1]
            if "界面文案 06" in body:
                return body          # 复读原文 -> 文种判定不通过
            return body + " [en]"

    picky = [PickySession("p%d" % i) for i in range(4)]
    prov2 = NpuTranslateProvider()
    prov2._sessions = lambda: picky
    outs2, misses2 = prov2._translate_many(bodies(24), "zh-CN", "en")
    check("输出仍是 24 条(没有整批失败)", len(outs2) == 24, str(len(outs2)))
    check("只有那一条被记为失败", misses2 == {6}, str(misses2))
    check("失败那条保留原文", outs2[6] == "界面文案 06", str(outs2[6]))
    check("其它 23 条都翻出来了",
          all(o.endswith("[en]") for i, o in enumerate(outs2) if i != 6), "")

    print("\n" + "=" * 72)
    print(f"通过 {_PASS} 项,失败 {_FAIL} 项")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
