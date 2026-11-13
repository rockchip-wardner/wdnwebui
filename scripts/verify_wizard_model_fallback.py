#!/usr/bin/env python3
"""回归：「首选模型没装 ⇒ 回退到本板装着的那个」这条链。

背景
----
``swcap_to_model`` 里钉的 ``model_id`` 是**偏好**，不是唯一解 —— 卡片是按
**capability** 取 session 的（``get_session_for_model(cap, model_id)`` 只是
按模型取而已，能力不够时它会失败）。

文本对话就踩了这个：``wizard.json`` 钉的是 ``qwen3.5-vl-9b``（占 2 张卡），
本板只装了 ``qwen3.5-vl-4b``。旧逻辑只查「钉的 model_id 装没装」⇒ 报
「⚠ 本板未安装所需模型」并**禁用**「一键开启所需模型」⇒ 用户走进死胡同
（其实 4B 也能提供 ``swcap_text_chat``）。

本脚本守住三条链：

1. **候选列表**（``swcap_to_model.models``）—— 声明「这个能力我接受哪些
   模型、按这个顺序挑」；``model_id`` 保持兼容（= 首选）。
2. **前端解析**（``state.js``）—— 钉的没装 → 退到能用的那个；卡位按
   **替代模型自己的** ``requires_cards`` 重算（9B 占 2 张、4B 占 1 张）。
3. **运行期解析**（插件的 ``service.py``）—— 候选 = 声明的偏好 + 本板
   **已安装**的 provider（后端动态列）+ 正在跑的 provider ⇒ **新增模型
   不必改代码**。

三段跑法
--------
* 第 1 段（纯 Python）：运行期解析 —— 不需要浏览器。
* 第 2 段（纯函数）：前端解析 —— 需要浏览器，但**不依赖板子状态**。
* 第 3 段（端到端）：env 步的提示与按钮 —— mock ``/api/compute``，
  不启停任何模型、不占 NPU。

⚠️ mock ``/api/compute/active-app``（PUT **和** DELETE）：框架进卡片时会
写 ``compute.active_app``，后端还会把承载该卡 caps 的 ``(model, npu)``
增量写进 ``compute.autostart`` —— 回归脚本不该改生产配置。

跑法::

    .venv/bin/python scripts/verify_wizard_model_fallback.py
"""
from __future__ import annotations

import json

import sys
from pathlib import Path

# ``app`` 包在仓库根 —— 脚本目录不是它，先把根加进 sys.path。
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000"
CARD = "text-chat"
CAP = "swcap_text_chat"

_FAILED: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {label}")
    if not ok:
        if detail:
            print(f"       {detail}")
        _FAILED.append(label)
    return ok


# --------------------------------------------------------------------------- #
# 第 1 段：运行期解析（纯 Python）
# --------------------------------------------------------------------------- #
print("=== 1. 运行期动态 provider 解析（插件 service.py）===")

from wdnapps_audio_chat.cards.text_chat import service as svc  # noqa: E402
from app.core.compute.registry import get_registry  # noqa: E402

check(svc.CAPABILITY == CAP,
      "service.CAPABILITY = swcap_text_chat", str(svc.CAPABILITY))

declared = svc._declared_models()
print(f"    声明的偏好 = {declared}")
check(len(declared) >= 2,
      "声明的偏好至少两个（首选 + 回退）", str(declared))
check(declared[0] == "qwen3.5-vl-9b",
      "首选仍是 qwen3.5-vl-9b（wizard.json 的 model_id）", str(declared))
check("qwen3.5-vl-4b" in declared,
      "回退 qwen3.5-vl-4b 在候选里", str(declared))

providers = svc._provider_models()
print(f"    已装 provider = {providers}")
check(bool(providers),
      "后端能动态列出本板已装、且带该 capability 的 provider", str(providers))

candidates = svc._candidate_models()
print(f"    候选顺序 = {candidates}")
check(list(candidates[:len(declared)]) == list(declared),
      "候选顺序 = 声明的偏好（向导启哪个，运行期就先用哪个）", str(candidates))
for p in providers:
    check(p in candidates,
          f"已装 provider {p} 进候选池（新增模型不必改代码）", str(candidates))

# 运行期源文件里**不许**再留「偏好写死」的痕迹（旧形态是
# ``PREFERRED_MODELS = ("qwen3.5-vl-9b", "qwen3.5-vl-4b")``）。
#
# ⚠️ 不做「整个文件不许出现模型 id」的检查：service.py 里还有 9B 专用的
# thinking 解析器分支（``if model_id == "qwen3.5-vl-9b"``）—— 那是
# **模型特有的输出格式**，不是模型偏好，换模型就要换解析器，属于另一件事。
src = Path(svc.__file__).read_text(encoding="utf-8")
check("PREFERRED_MODELS" not in src,
      "service.py 里没有写死的偏好常量 PREFERRED_MODELS",
      "found PREFERRED_MODELS")
check("get_session_for_model" in src and "_candidate_models()" in src,
      "会话选择走候选列表（_candidate_models）而不是常量",
      "")

# 行为断言（比静态正则强）：**模拟装一个新模型** —— 它必须自动进候选池，
# 不需要改任何代码。这才是「新增模型不必改代码」的真身。
reg = get_registry()
_orig_list = reg.list_supported_models
NEW_MODEL = "qwen3.5-vl-99b"


def _patched_list():
    rows = [dict(r) for r in _orig_list()]
    rows.append({"id": NEW_MODEL, "exists": True, "requires_cards": 1,
                 "capability_types": [CAP]})
    return rows


reg.list_supported_models = _patched_list
try:
    cand_new = svc._candidate_models()
finally:
    reg.list_supported_models = _orig_list
print(f"    装上新模型后的候选 = {cand_new}")
check(NEW_MODEL in cand_new,
      f"模拟装上 {NEW_MODEL} → 自动进候选池（不用改代码）",
      str(cand_new))
check(list(cand_new[:len(declared)]) == list(declared),
      "新模型排在声明的偏好**之后**（声明的顺序仍然是权威）",
      str(cand_new))

# 后端列出的 provider 必须真的带这个 capability（别把别的能力的模型捡进来）
supported = get_registry().list_supported_models()
by_id = {m["id"]: m for m in supported}
for p in providers:
    caps = by_id.get(p, {}).get("capability_types") or []
    check(CAP in caps,
          f"provider {p} 确实声明了 {CAP}", str(caps))

# 候选里的「已装」部分必须真的装了（declared 里的首选没装是允许的）
for p in providers:
    check(by_id.get(p, {}).get("exists") is not False,
          f"provider {p} 的模型目录存在", str(by_id.get(p, {}).get("model_dir")))

# --------------------------------------------------------------------------- #
# 第 2 段 + 第 3 段：浏览器
# --------------------------------------------------------------------------- #
with sync_playwright() as pw:
    browser = pw.chromium.launch(args=["--no-sandbox", "--mute-audio"])
    ctx = browser.new_context(viewport={"width": 1600, "height": 1200})
    page = ctx.new_page()
    errs: list[str] = []
    page.on("pageerror", lambda e: errs.append("pageerror: " + str(e)))
    page.on("console",
            lambda m: errs.append("console.error: " + m.text) if m.type == "error" else None)

    # ---- 第 2 段：纯函数解析 ------------------------------------------- #
    print("\n=== 2. 前端解析（纯函数，不依赖板子状态）===")

    # 先进一次应用页 —— framework.js / wizard/state.js 加载后
    # window.WizardState 才有值（纯函数断言要在页面里跑）。
    page.goto(f"{BASE}/#apps", wait_until="domcontentloaded", timeout=20000)
    page.wait_for_timeout(2500)
    has_ws = page.evaluate("typeof (window.WizardState || {}).__resolvePin === 'function'")
    check(has_ws is True,
          "window.WizardState 暴露了解析函数（__resolvePin 等）",
          str(has_ws))

    # 候选顺序：models[] → model_id → 其余已装 provider
    cand = page.evaluate(
        """([pin, models]) => {
          const fn = window.WizardState && window.WizardState.__candidateModels;
          if (!fn) return null;
          const prov = window.WizardState.__installedProviders(models);
          return fn("swcap_text_chat", pin, prov);
        }""",
        [{"model_id": "qwen3.5-vl-9b",
          "models": ["qwen3.5-vl-9b", "qwen3.5-vl-4b"],
          "npu_index": [0, 1]},
         [{"id": "qwen3.5-vl-4b", "exists": True,
           "capability_types": ["swcap_text_chat"], "requires_cards": 1},
          {"id": "qwen3.5-vl-8b", "exists": True,
           "capability_types": ["swcap_text_chat"], "requires_cards": 1}]],
    )
    print(f"    候选顺序 = {cand}")
    check(cand == ["qwen3.5-vl-9b", "qwen3.5-vl-4b", "qwen3.5-vl-8b"],
          "候选顺序 = models[] 声明 → 其余已装 provider", str(cand))

    # 老卡片（没写 models[]）→ 只认 model_id，行为不变
    cand_legacy = page.evaluate(
        """([pin, models]) => {
          const fn = window.WizardState.__candidateModels;
          const prov = window.WizardState.__installedProviders(models);
          return fn("swcap_text_chat", pin, prov);
        }""",
        [{"model_id": "qwen3.5-vl-9b", "npu_index": [0, 1]},
         [{"id": "qwen3.5-vl-4b", "exists": True,
           "capability_types": ["swcap_text_chat"], "requires_cards": 1}]],
    )
    check(cand_legacy == ["qwen3.5-vl-9b", "qwen3.5-vl-4b"],
          "没写 models[] 的老卡片：model_id 优先，其余已装 provider 跟上",
          str(cand_legacy))

    # 首选没装 → 退到 4B，卡位按 4B 的 requires_cards(=1) 重算
    pin = page.evaluate(
        """([models]) => {
          const fn = window.WizardState.__resolvePin;
          const prov = window.WizardState.__installedProviders(models);
          return fn("swcap_text_chat",
                    {model_id: "qwen3.5-vl-9b",
                     models: ["qwen3.5-vl-9b", "qwen3.5-vl-4b"],
                     npu_index: [0, 1]}, prov, [0, 1, 2]);
        }""",
        [[{"id": "qwen3.5-vl-4b", "exists": True,
           "capability_types": ["swcap_text_chat"], "requires_cards": 1}]],
    )
    print(f"    解析结果 = {pin}")
    check(pin and pin["model_id"] == "qwen3.5-vl-4b",
          "首选没装 → 退到本板装着的 4B", str(pin))
    check(pin and pin["swapped"] is True,
          "标记 swapped（提示要说明「换成了谁」）", str(pin))
    check(pin and pin["npu_index"] == [0],
          "卡位按 4B 的 requires_cards(=1) 重算（不再占 2 张）", str(pin))

    # 首选装了 → 保持 2 张卡
    pin9 = page.evaluate(
        """([models]) => {
          const fn = window.WizardState.__resolvePin;
          const prov = window.WizardState.__installedProviders(models);
          return fn("swcap_text_chat",
                    {model_id: "qwen3.5-vl-9b",
                     models: ["qwen3.5-vl-9b", "qwen3.5-vl-4b"],
                     npu_index: [0, 1]}, prov, [0, 1, 2]);
        }""",
        [[{"id": "qwen3.5-vl-9b", "exists": True,
           "capability_types": ["swcap_text_chat"], "requires_cards": 2},
          {"id": "qwen3.5-vl-4b", "exists": True,
           "capability_types": ["swcap_text_chat"], "requires_cards": 1}]],
    )
    check(pin9 and pin9["model_id"] == "qwen3.5-vl-9b"
          and pin9["npu_index"] == [0, 1]
          and pin9["swapped"] is False,
          "首选装了 → 用它，卡位保持 2 张", str(pin9))

    # 一个 provider 都没有 → 真缺
    pin_none = page.evaluate(
        """([models]) => {
          const fn = window.WizardState.__resolvePin;
          const prov = window.WizardState.__installedProviders(models);
          return fn("swcap_text_chat",
                    {model_id: "qwen3.5-vl-9b",
                     models: ["qwen3.5-vl-9b", "qwen3.5-vl-4b"],
                     npu_index: [0, 1]}, prov, [0, 1, 2]);
        }""",
        [[]],
    )
    check(pin_none is None,
          "一个 provider 都没有 → 真缺（null）", str(pin_none))

    # ---- 第 3 段：端到端（mock /api/compute）--------------------------- #
    print("\n=== 3. 端到端：env 步的提示与「一键开启」按钮（mock）===")

    def run_scenario(scene: dict) -> dict:
        """按给定场景跑一遍 env 步，返回页面上的关键结论。"""
        state = {"scene": scene}

        def on_compute(route):
            route.fulfill(status=200, headers={"Content-Type": "application/json"},
                          body=json.dumps(state["scene"]))

        def on_instances(route):
            route.fulfill(status=200, headers={"Content-Type": "application/json"},
                          body=json.dumps({"ok": True, "instances": [], "errors": []}))

        def on_active_app(route):
            route.fulfill(status=200, headers={"Content-Type": "application/json"},
                          body=json.dumps({"ok": True}))

        page.route("**/api/compute/instances", on_instances)
        page.route("**/api/compute/active-app", on_active_app)
        page.route("**/api/compute", on_compute)
        try:
            page.goto(f"{BASE}/#apps", wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(600)
            page.evaluate("() => localStorage.clear()")
            page.evaluate(f"() => {{ location.hash = '#{CARD}'; }}")
            page.wait_for_timeout(4500)
            page.evaluate("""() => {
              const t = Array.from(document.querySelectorAll('button'))
                .find(x => (x.getAttribute('data-wizard-mode') || '') !== '');
              if (t) t.click();
            }""")
            page.wait_for_timeout(4500)
            return page.evaluate(
                """() => {
                  const notices = Array.from(
                      document.querySelectorAll('[class*="single-notice"]'))
                    .map(e => e.innerText.trim());
                  const rows = Array.from(
                      document.querySelectorAll('[class*="env-row"]'))
                    .map(r => ({text: r.innerText.trim(), cls: r.className}));
                  const b = document.querySelector('#wd-wizard-env-bring-btn');
                  const n = document.querySelector('#wd-wizard-next-btn');
                  return {
                    notices: notices,
                    capRows: rows.filter(r => r.text.indexOf("swcap_") >= 0),
                    bring: b ? {present: true, disabled: b.disabled,
                                title: b.title} : {present: false},
                    next: n ? {disabled: n.disabled} : null,
                  };
                }"""
            )
        finally:
            page.unroute("**/api/compute")
            page.unroute("**/api/compute/instances")
            page.unroute("**/api/compute/active-app")

    # 场景 A：9B **没装**，4B 装了且在跑（跑在 npu2，不是钉的 npu0）
    scene_a = {
        "instances": [
            {"model_id": "qwen3.5-vl-4b", "npu_index": 2, "npu_indices": [2],
             "status": "running", "_source": "spawned",
             "capability_types": ["swcap_text_chat", "swcap_vision_chat",
                                  "swcap_tool_call"]},
        ],
        "card_count": 3,
        "model_cards": [0, 1, 2],
        "cards": [{"npu_index": 0}, {"npu_index": 1}, {"npu_index": 2}],
        # 9B 不在 preset_order 里 = 本板没装
        "preset_order": ["qwen3.5-vl-4b"],
        "supported_models": [
            {"id": "qwen3.5-vl-4b", "exists": True, "requires_cards": 1,
             "capability_types": ["swcap_text_chat", "swcap_vision_chat",
                                  "swcap_tool_call"]},
        ],
    }
    res_a = run_scenario(scene_a)
    print("    A 提示块 =", json.dumps(res_a["notices"], ensure_ascii=False))
    print("    A 能力行 =", json.dumps(res_a["capRows"], ensure_ascii=False))

    check(not any("本板未安装所需模型" in t for t in res_a["notices"]),
          "A：首选没装但有替代 → 不再报「本板未安装所需模型」",
          json.dumps(res_a["notices"], ensure_ascii=False))
    check(any("已改用本板可用的模型" in t for t in res_a["notices"]),
          "A：出现「已改用本板可用的模型」提示（说清楚换成了谁）",
          json.dumps(res_a["notices"], ensure_ascii=False))
    check(any("qwen3.5-vl-9b" in t and "qwen3.5-vl-4b" in t
              for t in res_a["notices"]),
          "A：提示里点明了首选与实际用的模型",
          json.dumps(res_a["notices"], ensure_ascii=False))
    check(res_a["bring"].get("present") is True,
          "A：「一键开启所需模型」按钮出现", json.dumps(res_a["bring"]))
    check(res_a["bring"].get("disabled") is False,
          "A：按钮**可用**（旧逻辑在这里禁用 → 死路）",
          json.dumps(res_a["bring"]))
    check(res_a["next"] is not None and res_a["next"]["disabled"] is False,
          "A：「下一步」可点", json.dumps(res_a["next"]))

    # 场景 B：一个 provider 都没有 → 真缺，按钮禁用（不能假装能开）
    scene_b = {
        "instances": [],
        "card_count": 3,
        "model_cards": [0, 1, 2],
        "cards": [{"npu_index": 0}, {"npu_index": 1}, {"npu_index": 2}],
        "preset_order": [],
        "supported_models": [],
    }
    res_b = run_scenario(scene_b)
    print("    B 提示块 =", json.dumps(res_b["notices"], ensure_ascii=False))

    check(any("本板未安装所需模型" in t for t in res_b["notices"]),
          "B：一个 provider 都没有 → 仍报「本板未安装所需模型」",
          json.dumps(res_b["notices"], ensure_ascii=False))
    check(not any("已改用本板可用的模型" in t for t in res_b["notices"]),
          "B：没有替代 → 不出现「已改用」提示",
          json.dumps(res_b["notices"], ensure_ascii=False))
    check(res_b["bring"].get("present") is True
          and res_b["bring"].get("disabled") is True,
          "B：按钮禁用（点了也起不来，后端会 422）", json.dumps(res_b["bring"]))

    hard = [e for e in errs if not e.startswith("console.error:")]
    check(not hard, "页面无 pageerror", json.dumps(hard[:3], ensure_ascii=False))
    browser.close()

print("\n" + "=" * 60)
if _FAILED:
    print(f"❌ {len(_FAILED)} 条未通过：")
    for f in _FAILED:
        print("   -", f)
    sys.exit(1)
print("✅ 全部通过")
