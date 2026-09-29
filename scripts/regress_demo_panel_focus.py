#!/usr/bin/env python3
"""回归测试:stale active + 应用列表 跳 目标是否正确(覆盖所有有 wizard 的 app)。

注:只测能"点到底"的 wizard(app 接受空表单就能 next),其他 app 在 helper text
里有相似 fixture 但需填表,不在本次回归范围(核心 bug fix 仅依赖 framework 逻辑)。
"""
from playwright.sync_api import sync_playwright

# (stale active page, target page, expected hash after click 应用列表)
# 要求 target 必须有 wizard.json 且能空表单 next → finish。
SCENARIOS = [
    ("video-detection",        "video-target-localization", "#apps"),
    ("video-target-detection", "video-target-localization", "#apps"),
    ("video-target-detection", "video-detection",           "#apps"),
]

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
        ctx = browser.new_context()
        page = ctx.new_page()
        page.on("pageerror", lambda e: print(f"  [pageerror] {e}"))

        page.goto("http://127.0.0.1:8000/", wait_until="domcontentloaded", timeout=15000)
        page.evaluate("Object.keys(localStorage).filter(k => k.includes('wizard')).forEach(k => localStorage.removeItem(k))")

        ok = 0
        for stale, target, expected in SCENARIOS:
            print(f"\n=== stale={stale}, target={target} ===")
            try:
                # 注入 stale active (模拟"之前在跑")
                page.evaluate(f"""
                    window.DemoGuard.report("{stale}", {{
                        session: {{ state: "playing" }},
                        detector: {{ enabled: true }}
                    }});
                """)
                active0 = page.evaluate("window.DemoGuard._activeApp")
                print(f"  injected: active = {active0!r}")

                # 进入目标 app
                page.goto(f"http://127.0.0.1:8000/#{target}", wait_until="domcontentloaded", timeout=15000)
                page.wait_for_selector(".wd-wizard-section", timeout=8000)
                active1 = page.evaluate("window.DemoGuard._activeApp")
                print(f"  after nav: active = {active1!r} (stale 已清)")

                # 完成配置 — 反复点 next,直到只剩 finish 按钮
                for _ in range(10):
                    next_btn = page.locator(".wd-wizard-primary-btn").filter(has_text="下一步").first
                    if next_btn.count() == 0:
                        break
                    if not next_btn.is_enabled():
                        # 必填字段未填 — 跳过该场景
                        print(f"  ⚠️ next disabled (需填表),skip")
                        raise RuntimeError("next disabled")
                    next_btn.click()
                    page.wait_for_timeout(400)
                finish_btn = page.locator(".wd-wizard-primary-btn").filter(has_text="完成配置").first
                finish_btn.click()
                page.wait_for_timeout(2500)

                # 点应用列表
                page.click('.nav-item[data-page="apps"]')
                page.wait_for_timeout(1200)

                new_hash = page.evaluate("location.hash")
                wizard_n = page.locator(".wd-wizard-section").count()
                apps_visible = page.evaluate("""() => {
                    const el = document.getElementById("page-apps");
                    return el && !el.classList.contains("hidden");
                }""")
                print(f"  after click: hash = {new_hash!r}, wizard={wizard_n}, #page-apps visible={apps_visible}")

                if new_hash == expected and wizard_n == 0 and apps_visible:
                    print(f"  ✅ {stale} → {target} → 应用列表 ✓")
                    ok += 1
                else:
                    print(f"  ❌ {stale} → {target}: 期望 hash={expected},wizard=0; 实际 hash={new_hash},wizard={wizard_n}")

                # cleanup
                page.goto("http://127.0.0.1:8000/", wait_until="domcontentloaded", timeout=10000)
                page.evaluate(f"window.DemoGuard._cache && delete window.DemoGuard._cache['{stale}']")
            except Exception as e:
                print(f"  ⚠️ skip: {e}")

        browser.close()
        print(f"\n=== 总结: {ok}/{len(SCENARIOS)} ===")
        if ok == len(SCENARIOS):
            print("✅ 全部通过")
        else:
            print(f"❌ {len(SCENARIOS) - ok} 失败")

if __name__ == "__main__":
    main()