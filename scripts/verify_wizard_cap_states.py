#!/usr/bin/env python3
"""UI 回归：向导 env 步的「软件能力」就绪态判定。

背景
----
env 步原来只有三态：✓ 绿 / ⚠ 黄（卡位只起一半）/ ✗ 红。而「✗ 红」的判据
其实混了两件事：

1. **能力级** —— 板上有没有任何运行中的实例提供这个能力；
2. **模型槽位级** —— cfg 里指定的那个 model_id 在它要求的卡位上起没起。

文本对话就踩了这个：cfg 配的是 ``qwen3.5-vl-9b@npu0+npu1``，板上跑的却是
``qwen3.5-vl-4b``（``text_chat`` 的 ``PREFERRED_MODELS`` 里 4B 明确是回退）。
4B 提供 ``swcap_text_chat``，功能**完全可用**，env 步却判成 ✗ 红、
禁用「下一步」、逼用户先点「一键开启所需模型」。

现在分成四态：

===========  ==========================================  ======  ==========
态           条件                                        图形    阻塞下一步
===========  ==========================================  ======  ==========
``ok``       cfg 指定的模型全开或在启动中                ✓ 绿    否
``alt``      cfg 指定的模型没开，但**别的实例**提供该能力  ⚠ 黄    否
``warn``     cfg 指定的模型开了但卡位只起一半            ⚠ 黄    否
``fail``     没有任何实例提供该能力，或 cfg 槽位上的
             worker 进了 ``error`` 终态                  ✗ 红    是
===========  ==========================================  ======  ==========

判据是「**能力可用吗**」而不是「cfg 指定的模型就位吗」—— 卡片按 capability
取 session（``get_session``），不认 model_id。所以 ``alt`` / ``warn`` 都是
「本机有 running/loading 实例提供这个 cap」，能力是通的，都放行「下一步」；
只有 ``fail`` 挡路。``alt`` 与 ``warn`` 的区别只在**配置匹配度**上，所以
都画 ⚠ 黄。

``error`` 终态优先于一切：那是**故障**不是「还没起」，放行的话用户一路走到
finish 步才撞「启动失败」。

本脚本断言
----------
1. ``_capStates`` 四态判定（纯逻辑，不依赖板子状态）。
2. 端到端：文本对话向导 env 步显示 ⚠ 而不是 ✗，且「下一步」可点。

端到端那部分**必须 mock /api/compute**：要验的是「cfg 指定的模型没起、但
别的实例提供该能力」这个 alt 场景，跟板子此刻真跑什么模型无关。读真机
瞬时状态的话，换个 autostart 配置就假失败 —— 实测 autostart 只起 vtd 卡的
``qwen3-vl-4b`` + ``qwen2.5-omni-3b``，没人提供 ``swcap_text_chat`` →
判 ✗ 红叉，那是**正确**判定，不是回归。

跑法：``.venv/bin/python scripts/verify_wizard_cap_states.py``
（需要服务在跑；不启停任何模型，不占 NPU）
"""
from __future__ import annotations

import json
import os
import sys

from playwright.sync_api import sync_playwright

BASE = os.environ.get("WDNWEBUI_BASE", "http://127.0.0.1:8000")
_FAILED: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {label}")
    if not ok:
        if detail:
            print(f"       {detail}")
        _FAILED.append(label)
    return ok


# alt 场景：cfg 指定的是 qwen3.5-vl-9b@[0,1]（文本对话的真实配置），
# 板上跑的却是 qwen3.5-vl-4b —— 它提供 swcap_text_chat，能力可用，
# 但用的不是配置里指定的那个模型 → 判 alt。
ALT_SCENE = [
    {
        "model_id": "qwen3.5-vl-4b", "npu_index": 0, "npu_indices": [0],
        "status": "running", "_source": "spawned",
        "capability_types": ["swcap_text_chat", "swcap_vision_chat", "swcap_tool_call"],
    },
]


# ---------------------------------------------------------------------------
# 1) 纯逻辑：_capStates 的四态
# ---------------------------------------------------------------------------
print("=== 1. _capStates 四态判定（纯函数）===")

with sync_playwright() as pw:
    browser = pw.chromium.launch(args=["--no-sandbox", "--mute-audio"])
    ctx = browser.new_context(viewport={"width": 1600, "height": 1200})
    page = ctx.new_page()
    errs: list[str] = []
    page.on("pageerror", lambda e: errs.append("pageerror: " + str(e)))
    page.on("console",
            lambda m: errs.append("console.error: " + m.text) if m.type == "error" else None)

    def on_compute(route):
        route.fulfill(status=200, headers={"Content-Type": "application/json"},
                      body=json.dumps({
                          "instances": ALT_SCENE,
                          "card_count": 2,
                          "model_cards": [0, 1],
                          "cards": [{"npu_index": 0}, {"npu_index": 1}],
                      }))

    def on_instances(route):
        route.fulfill(status=200, headers={"Content-Type": "application/json"},
                      body=json.dumps({"ok": True, "instances": [], "errors": []}))

    def on_active_app(route):
        # **必须 mock**：框架进卡片时会 PUT /api/compute/active-app，后端会
        # 把承载该卡 caps 的 (model, npu) 增量写进 compute.autostart ——
        # 回归脚本不该改生产配置。不 mock 时它还会因「本机没人提供
        # swcap_text_chat」回 409，在 console 里刷一堆资源加载失败。
        route.fulfill(status=200, headers={"Content-Type": "application/json"},
                      body=json.dumps({"ok": True}))

    # route 必须装在 goto 之前 —— 框架加载时会自动进「上次打开的卡片」，
    # 向导 mount 里的 _probeEnv 立刻拉 /api/compute。
    page.route("**/api/compute/instances", on_instances)
    page.route("**/api/compute/active-app", on_active_app)
    page.route("**/api/compute", on_compute)
    page.goto(f"{BASE}/#apps", wait_until="networkidle")
    page.wait_for_timeout(4000)

    # _capStates(swRequired, swMissing, slots, groupFail)
    #   slots: [{cap, model_id, npu_index, up, err}]
    #   groupFail 是 **Set**（判定用 groupFail.has）—— 参数经 JSON 传不进 Set，
    #   在页面侧包一层转换。
    def cap_state(sw_missing, slots, group_fail=None):
        return page.evaluate(
            """([missing, slots, gf]) => {
              const fn = window.WizardState && window.WizardState.__capStates;
              if (!fn) return null;
              return fn(["swcap_text_chat"], missing, slots, new Set(gf || []));
            }""",
            [sw_missing, slots, group_fail or []],
        )

    def slot(model, npu, up=False, err=False):
        return {"cap": "swcap_text_chat", "model_id": model,
                "npu_index": npu, "up": up, "err": err}

    # ok：cfg 指定的模型起来了
    st = cap_state([], [slot("qwen3.5-vl-9b", 0, up=True)])
    check(st == {"swcap_text_chat": "ok"},
          "cfg 指定的模型起来了 → ok", json.dumps(st, ensure_ascii=False))

    # alt：cfg 的槽位一个都没 up，但**别人提供该能力** → alt
    st = cap_state([], [slot("qwen3.5-vl-9b", 0, up=False)])
    check(st == {"swcap_text_chat": "alt"},
          "cfg 槽位没起但别人提供该能力 → alt（不是 fail）",
          json.dumps(st, ensure_ascii=False))

    # alt：多卡位全没起也是 alt（不是 warn）
    st = cap_state([], [slot("qwen3.5-vl-9b", 0, up=False),
                        slot("qwen3.5-vl-9b", 1, up=False)])
    check(st == {"swcap_text_chat": "alt"},
          "cfg 槽位全没起但能力有人提供 → alt",
          json.dumps(st, ensure_ascii=False))

    # warn：cfg 模型起了但卡位只起一半 → warn（能力可用，不阻塞）
    st = cap_state([], [slot("qwen3.5-vl-9b", 0, up=True),
                        slot("qwen3.5-vl-9b", 1, up=False)])
    check(st == {"swcap_text_chat": "warn"},
          "卡位只起一半 → warn（能力可用，不挡下一步）",
          json.dumps(st, ensure_ascii=False))

    # fail：没有任何实例提供该能力
    st = cap_state(["swcap_text_chat"], [slot("qwen3.5-vl-9b", 0, up=False)])
    check(st == {"swcap_text_chat": "fail"},
          "没人提供该能力 → fail",
          json.dumps(st, ensure_ascii=False))

    # fail：配置槽位上的实例进了 error 终态 → fail（故障，不能被顶替掩盖）
    st = cap_state([], [slot("qwen3.5-vl-9b", 0, up=False, err=True)])
    check(st == {"swcap_text_chat": "fail"},
          "cfg 槽位上的 worker 挂了（error） → fail",
          json.dumps(st, ensure_ascii=False))

    # 组不满足 → fail（哪怕有别的实例提供其中一项）
    # groupFail 的元素是**拆开的单个 cap 名** —— env 侧把 "a + b" 拆成
    # {"a", "b"} 再塞进 Set（见 _envView），这里要跟它同口径。
    st = cap_state([], [slot("qwen3.5-vl-9b", 0, up=False)],
                    group_fail=["swcap_text_chat", "swcap_tool_call"])
    check(st == {"swcap_text_chat": "fail"},
          "能力组没被同一实例满足 → fail（alt 不越权）",
          json.dumps(st, ensure_ascii=False))

    # ---- _modelSlots：同一卡位上多个实例时必须挑**活着的**那个 ----------
    # 分段模型（npu_indices=[0,1]）与另一个同模型的实例都会匹配卡位 1，
    # 数组里 error 的那条排在前面。取首个就会把活着的判成故障（fail）。
    slots = page.evaluate("""() => {
      const fn = window.WizardState && window.WizardState.__modelSlots;
      if (!fn) return null;
      return fn({swcap_to_model: {swcap_text_chat: {model_id: "m", npu_index: [0, 1]}}},
                [{model_id: "m", npu_index: 0, nppu_indices: [0, 1], status: "error"},
                 {model_id: "m", npu_index: 1, nppu_indices: [1], status: "running"}]);
    }""")
    s1 = next((s for s in (slots or []) if s["npu_index"] == 1), None)
    check(s1 is not None and s1["up"] is True and s1["err"] is False,
          "同卡位多个实例 → 挑活着的那个（不误判成 error 故障）",
          json.dumps(slots, ensure_ascii=False))

    # ----------------------------------------------------------------- #
    # 2) 端到端：文本对话向导 env 步
    # ----------------------------------------------------------------- #
    print("\n=== 2. 端到端：文本对话向导 env 步（alt 场景，mock）===")
    page.evaluate("() => localStorage.clear()")
    page.evaluate("() => { location.hash = '#text-chat'; }")
    page.wait_for_timeout(4000)
    page.evaluate("""() => {
      const t = Array.from(document.querySelectorAll('button'))
        .find(x => (x.getAttribute('data-wizard-mode') || '') !== '');
      if (t) t.click();
    }""")
    page.wait_for_timeout(5000)

    rows = page.evaluate("""() => Array.from(document.querySelectorAll('[class*="env-row"]'))
        .map(r => ({text: r.innerText.trim(), cls: r.className, title: r.title}))""")
    cap_rows = [r for r in rows if "swcap_" in r["text"]]
    print("    能力行:", json.dumps(cap_rows, ensure_ascii=False))

    if cap_rows:
        row = cap_rows[0]
        check(row["text"].startswith("⚠"),
              "env 步：能力显示 ⚠（不是 ✗ 红叉）", row["text"])
        check("env-warn" in row["cls"],
              "env 步：走黄色 warn 样式", row["cls"])
    else:
        check(False, "env 步：找到 swcap_* 能力行",
              json.dumps(rows, ensure_ascii=False))

    nxt = page.evaluate("""() => {
      const b = document.querySelector('#wd-wizard-next-btn');
      return b ? {disabled: b.disabled} : null;
    }""")
    check(nxt is not None and nxt["disabled"] is False,
          "env 步：「下一步」可点（不必先点「一键开启所需模型」）",
          json.dumps(nxt, ensure_ascii=False))

    # 用户要求：alt（配置模型没开、别人提供能力）时**仍要**给
    # 「一键开启所需模型」按钮 —— 想换成配置里指定的那个模型时唯一的入口。
    bring = page.evaluate("""() => {
      const b = document.querySelector('#wd-wizard-env-bring-btn');
      return b ? {present: true, disabled: b.disabled} : {present: false};
    }""")
    check(bring.get("present") is True,
          "env 步：alt 态仍出现「一键开启所需模型」",
          json.dumps(bring, ensure_ascii=False))

    # 用户要求：alt 态**不要**那个「能力由其它模型提供」的说明块。
    notice = page.evaluate("""() => {
      const n = Array.from(document.querySelectorAll('[class*="notice"]'))
        .map(e => e.innerText.trim()).filter(Boolean);
      return n;
    }""")
    check(not any("其它模型" in t for t in notice),
          "env 步：alt 态不再出现「能力由其它模型提供」提示",
          json.dumps(notice, ensure_ascii=False))

    check(not errs, "页面无 JS 报错", json.dumps(errs, ensure_ascii=False))
    browser.close()

print("\n" + "=" * 60)
if _FAILED:
    print(f"❌ {len(_FAILED)} 条未通过：")
    for f in _FAILED:
        print("   -", f)
    sys.exit(1)
print("✅ 全部通过")
