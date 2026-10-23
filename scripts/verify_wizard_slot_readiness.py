#!/usr/bin/env python3
"""回归：向导 env 步的就绪判据必须是 **(模型, 卡位) 对**级，不是模型级。

用户实测 BUG（视频目标监控 / vtd 卡）：qwen3-vl-4b 跑在 RK182x#0、
qwen2.5-omni-3b 跑在 RK182x#1，点「视频目标监控」进向导模式：

  第 1 步「运行环境检查」→ 显示「✓ swcap_vision_chat 已就绪」
  → **不显示「一键开启所需模型」按钮**
  → 一路走到最后一步「完成配置」→ 点完成 → 永久等待
    「[VLM]视觉描述v3 启动在 RK182x#1」（npu1 上跑的是 omni，不是它）

根因：env 步只按 **capability（模型级）** 判就绪。vtd 的 swcap_to_model 是
``{swcap_vision_chat: {model_id: qwen3-vl-4b, npu_index: [0, 1]}}`` ——
同一个模型要**两张卡都跑**。qwen3-vl-4b 在 npu0 上 running → capability
命中 → 判成就绪 → 卡位缺口被完全忽略。正确粒度是 (模型, 卡位) 对，与
finish 步的等待（_waitForModelsRunning）同口径。

本脚本守住七条：
  B1 半就绪（只起 npu0）→ ⚠ 黄 + 出现「一键开启所需模型」按钮
  B2 半就绪 → **不再**暴露模型名 / 卡号明细行（用户不需要知道这么具体）
  B3 半就绪 → 「下一步」禁用（走不到完成配置页死等）
  B4 全就绪（两张卡都有）→ ✓ 绿、**不**出现按钮，「下一步」可用
  B5 peer adopt 的实例不算本机就绪（远端算力本机无权用）
  B6 点按钮 → DELETE 带 npu_index 腾掉 omni@#1，POST 带 npu_index=[0,1]
  B7 三态齐全：完全没开 → ✗ 红；正在启动中 → ✓ 绿（就绪在途）

全 mock，不占 NPU、不碰真机。

用法：.venv/bin/python scripts/verify_wizard_slot_readiness.py
"""
from __future__ import annotations

import json
import sys
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000"
APP = "video-target-detection"
NS = "wd-wizard-"


def _inst(model_id, npu, caps, status="running", source="spawned"):
    return {
        "model_id": model_id, "npu_index": npu, "status": status,
        "_source": source, "capability_types": caps, "transition": "",
        "pid": 1000 + npu,
    }


# 用户场景：qwen3-vl-4b 跑在 #0，omni 跑在 #1（vtd 想要的是 qwen3-vl-4b 两张卡）
USER_SCENE = [
    _inst("qwen3-vl-4b", 0, ["swcap_vision_chat"]),
    _inst("qwen2.5-omni-3b", 1, ["swcap_audio_chat"]),
]
# 全就绪：qwen3-vl-4b 两张卡都在
ALL_READY = [
    _inst("qwen3-vl-4b", 0, ["swcap_vision_chat"]),
    _inst("qwen3-vl-4b", 1, ["swcap_vision_chat"]),
]
# peer adopt：#1 上那份是远端板子的，本机无权用
PEER_ONLY = [
    _inst("qwen3-vl-4b", 0, ["swcap_vision_chat"]),
    _inst("qwen3-vl-4b", 1, ["swcap_vision_chat"], source="adopted"),
]
# 完全没开：本机一个实例都没有
NONE_UP: list = []
# 正在启动中：两个卡位都在 loading —— 也算「就绪在途」
LOADING = [
    _inst("qwen3-vl-4b", 0, ["swcap_vision_chat"], status="loading"),
    _inst("qwen3-vl-4b", 1, ["swcap_vision_chat"], status="loading"),
]

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


def open_wizard(browser, instances):
    """开一个页面 + 装好 mock，返回 (page, ctx, calls)。

    mock 必须在 page.goto() **之前**装好：框架在页面加载时会自动进入
    「上次打开的卡片」，向导 mount 里的 _probeEnv 会立刻拉 /api/compute。
    route 装晚了那次请求就打到真后端了（板子上真在跑的模型会污染判据）。
    """
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    page = ctx.new_page()
    page.on("pageerror", lambda e: print(f"  [pageerror] {e}"))
    calls: list[tuple[str, dict]] = []

    def on_compute(route):
        route.fulfill(status=200,
                      headers={"Content-Type": "application/json"},
                      body=json.dumps({
                          "instances": instances,
                          "card_count": 2,
                          "model_cards": [0, 1],
                          "cards": [{"npu_index": 0}, {"npu_index": 1}],
                      }))

    def on_instances(route):
        body = route.request.post_data_json or {}
        calls.append((route.request.method, body))
        route.fulfill(status=200,
                      headers={"Content-Type": "application/json"},
                      body=json.dumps({"ok": True, "instances": [],
                                       "errors": []}))

    page.route("**/api/compute/instances", on_instances)
    page.route("**/api/compute", on_compute)

    page.goto(BASE, wait_until="domcontentloaded", timeout=20000)
    page.wait_for_function(
        "() => window.WizardState && window.ROUTER && window.WizardRegistry",
        timeout=15000)
    # 清掉「已完成 / 上次停留步骤」的痕迹，保证每次都从 env 步开始
    page.evaluate("""(id) => {
        const base = 'wdnwebui.wizard.' + id + '.';
        for (const k of Object.keys(localStorage))
            if (k.startsWith(base)) localStorage.removeItem(k);
    }""", APP)
    page.evaluate("(id) => { window.location.hash = '#' + id; }", APP)
    page.wait_for_timeout(500)
    # 显式走「向导模式」入口 → force mount，从第 1 步开始
    page.evaluate("(id) => { window.enterWizardMode(id); }", APP)
    page.wait_for_selector(f"#{NS}root-{APP}:not(.hidden)", timeout=20000)
    page.wait_for_selector(f"#{NS}root-{APP} .{NS}env-row", timeout=20000)
    page.wait_for_timeout(400)   # 等后台刷新那一轮 settle
    return page, ctx, calls


def env_rows(page) -> list[str]:
    return page.eval_on_selector_all(
        f"#{NS}root-{APP} .{NS}env-row",
        "els => els.map(e => e.textContent.trim())")


def cap_state(page, cap: str) -> str:
    """软件能力行的三态：ok / warn / fail（认不出来返回 "?"）。

    env 步有两张卡（硬件能力 / 软件能力），硬件行不含 swcap_ 前缀，
    按能力名精确挑出软件能力行。
    """
    cls = page.eval_on_selector_all(
        f"#{NS}root-{APP} .{NS}env-row",
        """(els, cap) => {
            const e = els.find(x => x.textContent.includes(cap));
            return e ? e.className : null;
        }""",
        cap)
    if not cls:
        return "?"
    for st in ("ok", "warn", "fail"):
        if f"env-{st}" in cls:
            return st
    return "?"


def no_model_detail_rows(rows: list[str]) -> bool:
    """软件能力区不再出现模型名 / 卡号明细行。"""
    return not any("qwen3-vl-4b" in r or "qwen2.5-omni-3b" in r
                   or "RK182x #" in r for r in rows)


def bring_btn_count(page) -> int:
    return page.locator(f"#{NS}root-{APP} #{NS}env-bring-btn").count()


def next_disabled(page) -> bool | None:
    return page.evaluate(
        f"() => {{ const b = document.querySelector('#{NS}root-{APP} "
        f"#{NS}next-btn'); return b ? b.disabled : null; }}")


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])

        # ---- 场景 A：用户场景（只起 npu0，#1 上是 omni）--------------------
        print("\n=== A: 半就绪（qwen3-vl-4b 只在 #0，#1 上是 omni） ===")
        page, ctx, calls = open_wizard(browser, USER_SCENE)
        rows = env_rows(page)
        print("  env 行: " + " | ".join(rows))
        check("B1 半就绪 → 能力行显示 ⚠（黄色告警）",
              cap_state(page, "swcap_vision_chat") == "warn",
              f"state={cap_state(page, 'swcap_vision_chat')}")
        check("B1 出现「一键开启所需模型」按钮",
              bring_btn_count(page) == 1, f"count={bring_btn_count(page)}")
        check("B2 不再显示模型名 / 卡号明细行",
              no_model_detail_rows(rows), f"rows={rows}")
        check("B3 「下一步」禁用（走不到完成配置页死等）",
              next_disabled(page) is True, f"disabled={next_disabled(page)}")

        # ---- 场景 B：点「一键开启」→ 腾卡 + 两张卡都起 ---------------------
        print("\n=== B: 点「一键开启所需模型」 ===")
        page.click(f"#{NS}root-{APP} #{NS}env-bring-btn")
        for _ in range(60):
            if any(m == "POST" for m, _ in calls):
                break
            page.wait_for_timeout(200)
        page.wait_for_timeout(800)
        print(f"  请求: {json.dumps(calls, ensure_ascii=False)}")
        deletes = [b for m, b in calls if m == "DELETE"]
        posts = [b for m, b in calls if m == "POST"]
        check("B6 DELETE 腾掉 omni@#1（带卡号）",
              any(d.get("model") == "qwen2.5-omni-3b" and d.get("npu_index") == 1
                  for d in deletes), f"deletes={deletes}")
        check("B6 DELETE 不碰本应用需要的 qwen3-vl-4b@#0",
              not any(d.get("model") == "qwen3-vl-4b" for d in deletes),
              f"deletes={deletes}")
        check("B6 POST 两张卡都启（npu_index=[0,1]）",
              any(p.get("model") == "qwen3-vl-4b"
                  and p.get("npu_index") == [0, 1] for p in posts),
              f"posts={posts}")
        ctx.close()

        # ---- 场景 C：两张卡都就绪 ------------------------------------------
        print("\n=== C: 全就绪（qwen3-vl-4b 两张卡都在） ===")
        page, ctx, _ = open_wizard(browser, ALL_READY)
        rows = env_rows(page)
        print("  env 行: " + " | ".join(rows))
        check("B4 能力行显示 ✓（绿色）",
              cap_state(page, "swcap_vision_chat") == "ok",
              f"state={cap_state(page, 'swcap_vision_chat')}")
        check("B4 不出现「一键开启所需模型」按钮",
              bring_btn_count(page) == 0, f"count={bring_btn_count(page)}")
        check("B4 「下一步」可用",
              next_disabled(page) is False, f"disabled={next_disabled(page)}")
        check("B4 不再显示模型名 / 卡号明细行",
              no_model_detail_rows(rows), f"rows={rows}")
        ctx.close()

        # ---- 场景 D：peer adopt 的实例不算本机就绪 --------------------------
        print("\n=== D: #1 上是 peer adopt 的实例 ===")
        page, ctx, _ = open_wizard(browser, PEER_ONLY)
        rows = env_rows(page)
        print("  env 行: " + " | ".join(rows))
        check("B5 peer adopt 不算就绪 → 半就绪 ⚠",
              cap_state(page, "swcap_vision_chat") == "warn",
              f"state={cap_state(page, 'swcap_vision_chat')}")
        check("B5 peer adopt 不算就绪 → 仍出现「一键开启」按钮",
              bring_btn_count(page) == 1, f"count={bring_btn_count(page)}")
        check("B5 「下一步」禁用",
              next_disabled(page) is True, f"disabled={next_disabled(page)}")
        ctx.close()

        # ---- 场景 E：完全没开启 ------------------------------------------
        print("\n=== E: 完全没开启（本机一个实例都没有） ===")
        page, ctx, _ = open_wizard(browser, NONE_UP)
        rows = env_rows(page)
        print("  env 行: " + " | ".join(rows))
        check("B7 没开启 → 能力行显示 ✗（红色）",
              cap_state(page, "swcap_vision_chat") == "fail",
              f"state={cap_state(page, 'swcap_vision_chat')}")
        check("B7 没开启 → 出现「一键开启」按钮",
              bring_btn_count(page) == 1, f"count={bring_btn_count(page)}")
        check("B7 没开启 → 「下一步」禁用",
              next_disabled(page) is True, f"disabled={next_disabled(page)}")
        ctx.close()

        # ---- 场景 F：正在启动中 -------------------------------------------
        print("\n=== F: 两个卡位都在启动中（loading） ===")
        page, ctx, _ = open_wizard(browser, LOADING)
        rows = env_rows(page)
        print("  env 行: " + " | ".join(rows))
        check("B7 启动中 → 能力行显示 ✓（就绪在途）",
              cap_state(page, "swcap_vision_chat") == "ok",
              f"state={cap_state(page, 'swcap_vision_chat')}")
        check("B7 启动中 → 不出现「一键开启」按钮",
              bring_btn_count(page) == 0, f"count={bring_btn_count(page)}")
        check("B7 启动中 → 「下一步」可用（finish 步会等 running）",
              next_disabled(page) is False, f"disabled={next_disabled(page)}")
        ctx.close()

        browser.close()

    print("\n" + "=" * 60)
    print(f"通过 {PASSED} / {PASSED + len(FAILED)}")
    if FAILED:
        print(f"❌ 失败: {FAILED}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
