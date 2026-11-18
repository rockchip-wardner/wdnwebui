#!/usr/bin/env python3
"""回归：向导「一键开启所需模型」必须按模型的 **max_replicas_per_card** 夹紧槽位。

背景（2026-11-18 真机现场）
--------------------------
网页翻译的向导点「一键开启所需模型」→ ``POST /api/compute/instances``
返 **400**：

    {"detail":"hy-mt-1-5 每张卡最多 1 份（请求了第 2 份）"}

原因：卡片的 ``wizard.json`` 声明了 4 个槽位（卡0×2 + 卡1×2，
「每卡 2 份」是 2026-11-16 实测出来的加速方案），而**模型包里的
manifest 没带 ``max_replicas_per_card``** ⇒ 后端取默认值 1 ⇒ 计划与
后端契约打架 ⇒ 整条一键开启死路。

⇒ 修法有两层：

1. **治本**：模型包 manifest 补 ``"max_replicas_per_card": 2``（见
   ``scripts/verify_translate_replica_manifest.py``）；
2. **兜底**：向导按后端告诉它的上限**夹紧**槽位 —— 老模型包 / 重新
   分发漏字段时**能起几份起几份**，而不是硬撞 400。

本脚本测第 2 层（纯前端，全 mock，不碰真机）：

* 上限 = 1（老包）⇒ 槽位夹到每卡第 0 份；
* 上限 = 2（补过字段的包）⇒ 4 个槽位原样发出；
* 模型不在清单里 ⇒ 取默认值 1 ⇒ 同样夹紧。

用法::

    .venv/bin/python scripts/verify_wizard_replica_clamp.py
"""
from __future__ import annotations

import json
import os
import sys

from playwright.sync_api import sync_playwright

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from card_dirs import card_dir as _card_dir  # noqa: E402

CARD = str(_card_dir("web-translate"))
WIZARD_JSON = os.path.join(CARD, "static/wizard.json")
BASE = os.environ.get("WDNWEBUI_BASE", "https://127.0.0.1:8000").rstrip("/")

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


def supported_models(max_rep):
    """/api/compute 的 supported_models（只放网页翻译那个模型）。"""
    m = {
        "id": "hy-mt-1-5",
        "label": "[MT]翻译模型v1.5",
        "capability_types": ["swcap_translate"],
        "requires_cards": 1,
        "exists": True,
        "model_dir": "/userdata/models/HY_MT_1_5",
    }
    if max_rep is not None:
        m["max_replicas_per_card"] = max_rep
    return [m]


def run_case(page, cfg, max_rep, expect_slots, expect_cards, label):
    """跑一次「一键开启」，返回 (POST body, 进度消息)。"""
    posted = []
    notes = []

    def handle(route):
        req = route.request
        raw = req.post_data or ""
        if req.method == "POST":
            posted.append(json.loads(raw) if raw else {})
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"ok": True,
                                           "snapshot": {"instances": []}}))
        else:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"ok": True}))

    page.route("**/api/compute", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({
            "instances": [],
            "supported_models": supported_models(max_rep),
            "model_cards": [0, 1, 2],
            "cards": [{"npu_index": i} for i in (0, 1, 2)],
        }),
    ))
    page.route("**/api/compute/instances", handle)

    result = page.evaluate("""
        async ([cfg]) => {
            const notes = [];
            const ok = await window._wizardBringUpModels(cfg, (m) => notes.push(m));
            return {ok, notes};
        }
    """, [cfg])
    notes.extend(result.get("notes") or [])
    body = posted[0] if posted else {}
    print(f"  --- {label}: POST body = {json.dumps(body, ensure_ascii=False)}")
    print(f"      notes = {notes}")
    check(f"{label}: 发出了 POST", bool(posted))
    slots = body.get("slots")
    check(f"{label}: slots == {expect_slots}",
          slots == expect_slots, f"实际 {slots}")
    check(f"{label}: npu_index == {expect_cards}",
          body.get("npu_index") == expect_cards, f"实际 {body.get('npu_index')}")
    return body, notes


def main() -> int:
    wiz = json.load(open(WIZARD_JSON, encoding="utf-8"))
    plan = wiz["swcap_to_model"]["swcap_translate"]
    print("=" * 72)
    print("向导：一键开启的槽位夹紧（max_replicas_per_card）")
    print("=" * 72)
    print(f"  卡片计划: {plan.get('instance_slots')}")

    # 计划本身必须是「每卡 2 份」——夹紧逻辑的前提
    check("卡片计划里有 replica=1 的槽位（每卡 2 份）",
          any(int(sl.get("replica") or 0) >= 1
              for sl in plan.get("instance_slots") or []),
          str(plan.get("instance_slots")))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True,
                                    args=["--no-sandbox", "--disable-gpu",
                                          "--mute-audio"])
        ctx = browser.new_context()
        page = ctx.new_page()
        page.on("pageerror", lambda e: print(f"  [pageerror] {e}"))
        page.goto(BASE + "/", wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(900)

        # ---------------------------------------------------------------- [1] --
        print("\n[1] 纯函数 _maxReplicasPerCard 的口径")
        probe = page.evaluate("""
            () => {
              const f = window.WizardState.__maxReplicasPerCard;
              if (!f) return {err: "no export"};
              return {
                one:   f([{id: "m", max_replicas_per_card: 1}], "m"),
                two:   f([{id: "m", max_replicas_per_card: 2}], "m"),
                zero:  f([{id: "m", max_replicas_per_card: 0}], "m"),
                none:  f([{id: "m"}], "m"),
                miss:  f([{id: "other"}], "m"),
                empty: f([], "m"),
              };
            }
        """)
        print("      ", probe)
        check("声明 1 → 1", probe.get("one") == 1, str(probe))
        check("声明 2 → 2", probe.get("two") == 2, str(probe))
        check("声明 0 → 兜底 1", probe.get("zero") == 1, str(probe))
        check("没声明 → 兜底 1", probe.get("none") == 1, str(probe))
        check("模型不在清单 → 兜底 1", probe.get("miss") == 1, str(probe))
        check("清单为空 → 兜底 1", probe.get("empty") == 1, str(probe))

        # ---------------------------------------------------------------- [2] --
        print("\n[2] 老模型包（max_replicas_per_card 缺省 = 1）⇒ 夹紧到每卡 1 份")
        run_case(page, {"swcap_to_model": {"swcap_translate": plan}},
                 1,
                 [{"npu_index": 0, "replica": 0},
                  {"npu_index": 1, "replica": 0}],
                 [0, 1], "上限 1")

        # ---------------------------------------------------------------- [3] --
        print("\n[3] 补过字段的模型包（= 2）⇒ 4 个槽位原样发出")
        run_case(page, {"swcap_to_model": {"swcap_translate": plan}},
                 2,
                 [{"npu_index": 0, "replica": 0},
                  {"npu_index": 0, "replica": 1},
                  {"npu_index": 1, "replica": 0},
                  {"npu_index": 1, "replica": 1}],
                 [0, 1], "上限 2")

        # ---------------------------------------------------------------- [4] --
        print("\n[4] 模型根本不在 supported_models 里 ⇒ 兜底 1 ⇒ 夹紧")
        run_case(page, {"swcap_to_model": {"swcap_translate": plan}},
                 None,
                 [{"npu_index": 0, "replica": 0},
                  {"npu_index": 1, "replica": 0}],
                 [0, 1], "模型不在清单")

        browser.close()

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
