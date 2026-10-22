#!/usr/bin/env python3
"""回归：向导「一键开启所需模型」必须按 **(模型, 卡位) 对**腾卡。

用户实测 BUG（音视频描述 / vd 卡）：两张 RK182X 卡都加载了
[VLM]视觉描述v3 (qwen3-vl-4b) 时，点「一键开启所需模型」→
[Omni]多感知描述v2.5 (qwen2.5-omni-3b) 起不来
（npu1 被 qwen3-vl-4b 占着 3500MB，omni 要 2500MB，5120 装不下 →
worker rc=-6）。

根因：第 1 步「停非必需」按 **capability（模型级）** 判要不要停。
qwen3-vl-4b 的 capability 是 swcap_vision_chat，在 vd 卡的
swcap_to_model 里 → 判成「需要」→ 两张卡上的都不停 → npu1 腾不出来。

正确粒度是 **(模型, 卡位) 对**：vd 卡要的是
(qwen3-vl-4b, npu0) + (qwen2.5-omni-3b, npu1)，npu1 上那个
qwen3-vl-4b 是多余的，必须停。

本脚本守住三条：
  A1 多余的那张卡被停掉 → DELETE body 带 npu_index=1
  A2 本应用需要的卡**不能**被停 → 不发 qwen3-vl-4b @ npu0 的 DELETE
  A3 DELETE 必须带 npu_index（不带 = 停该模型所有卡，会误停需要的卡）

全 mock，不占 NPU、不碰真机。

用法：.venv/bin/python scripts/verify_wizard_multicard_bringup.py
"""
from __future__ import annotations

import json
import sys
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000"
APP = "video-detection"

# 两张卡都跑 qwen3-vl-4b —— 复现用户场景
INSTANCES = [
    {"model_id": "qwen3-vl-4b", "npu_index": 0, "status": "running",
     "_source": "spawned", "capability_types": ["swcap_vision_chat"],
     "transition": "", "pid": 111},
    {"model_id": "qwen3-vl-4b", "npu_index": 1, "status": "running",
     "_source": "spawned", "capability_types": ["swcap_vision_chat"],
     "transition": "", "pid": 222},
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


def main() -> int:
    deletes: list[dict] = []
    posts: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_context(viewport={"width": 1400, "height": 900}).new_page()
        page.on("pageerror", lambda e: print(f"  [pageerror] {e}"))

        def on_compute(route):
            route.fulfill(status=200,
                          headers={"Content-Type": "application/json"},
                          body=json.dumps({
                              "instances": INSTANCES,
                              "card_count": 2,
                              "model_cards": [0, 1],
                              "cards": [{"npu_index": 0}, {"npu_index": 1}],
                          }))

        def on_instances(route):
            body = route.request.post_data_json or {}
            if route.request.method == "DELETE":
                deletes.append(body)
            else:
                posts.append(body)
            route.fulfill(status=200,
                          headers={"Content-Type": "application/json"},
                          body=json.dumps({"ok": True, "instances": [],
                                           "errors": []}))

        # **必须在 goto 之前装好 mock** —— 框架在页面加载时就会自动进入
        # 「上次打开的卡片」，向导 mount 里的 _probeEnv 会立刻拉
        # /api/compute。route 装晚了,那次请求就打到真后端了(板子上
        # 真的在跑 omni → swcap_audio_chat ✓ → 「一键开启」按钮
        # 根本不渲染)。
        page.route("**/api/compute/instances", on_instances)
        page.route("**/api/compute", on_compute)

        page.goto(BASE, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_function(
            "() => window.WizardState && window.ROUTER && window.WizardRegistry",
            timeout=15000)
        page.evaluate("""(id) => {
            const base = 'wdnwebui.wizard.' + id + '.';
            for (const k of Object.keys(localStorage))
                if (k.startsWith(base)) localStorage.removeItem(k);
        }""", APP)

        # 进卡片 → 向导 env 步
        page.evaluate("(id) => { window.location.hash = '#' + id; }", APP)
        page.wait_for_selector(f"#wd-wizard-root-{APP}:not(.hidden)", timeout=20000)
        page.wait_for_selector("#wd-wizard-stepper .wd-wizard-step-dot", timeout=20000)
        page.wait_for_selector("#wd-wizard-env-bring-btn", timeout=20000)

        print("\n=== 点「一键开启所需模型」 ===")
        page.click("#wd-wizard-env-bring-btn")
        # 等它把两个阶段都跑完（DELETE 循环 + POST 循环）
        for _ in range(60):
            if deletes and len(posts) >= 2:
                break
            page.wait_for_timeout(500)

        print(f"  DELETE 请求: {json.dumps(deletes, ensure_ascii=False)}")
        print(f"  POST  请求: {json.dumps(posts, ensure_ascii=False)}")

        # ---- 断言
        stopped = [(d.get("model"), d.get("npu_index")) for d in deletes]
        check("A1 多余的 qwen3-vl-4b@npu1 被停掉",
              ("qwen3-vl-4b", 1) in stopped, f"stopped={stopped}")
        check("A2 本应用需要的 qwen3-vl-4b@npu0 没被停",
              ("qwen3-vl-4b", 0) not in stopped, f"stopped={stopped}")
        check("A3 每条 DELETE 都带 npu_index",
              all(d.get("npu_index") is not None for d in deletes),
              f"deletes={deletes}")
        check("A3 只发了 1 条 DELETE（多余的只有一张卡）",
              len(deletes) == 1, f"deletes={deletes}")
        started = [(d.get("model"), d.get("npu_index")) for d in posts]
        check("A4 两个需要的模型都被启",
              ("qwen3-vl-4b", 0) in started and ("qwen2.5-omni-3b", 1) in started,
              f"started={started}")
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
