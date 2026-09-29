#!/usr/bin/env python3
"""测试 自然流程 (无 stale state 注入)是否也有 bug。"""
from playwright.sync_api import sync_playwright

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
        ctx = browser.new_context()
        page = ctx.new_page()
        page.on("console", lambda m: print(f"  [{m.type}] {m.text}") if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: print(f"  [pageerror] {e}"))

        print("\n=== 1. 加载首页 ===")
        page.goto("http://127.0.0.1:8000/", wait_until="domcontentloaded", timeout=15000)

        # 清掉 wizard localStorage
        page.evaluate("Object.keys(localStorage).filter(k => k.includes('wizard')).forEach(k => localStorage.removeItem(k))")
        page.reload(wait_until="domcontentloaded")

        print(f"  _activeApp (initial) = {page.evaluate('window.DemoGuard._activeApp')!r}")

        print("\n=== 2. 进入 视频目标定位 (直接 hash) ===")
        page.goto("http://127.0.0.1:8000/#video-target-localization", wait_until="domcontentloaded", timeout=15000)
        page.wait_for_selector(".wd-wizard-section", timeout=8000)
        print(f"  ✅ wizard mounted, _activeApp = {page.evaluate('window.DemoGuard._activeApp')!r}")

        print("\n=== 3. 点 下一步 x3 + 完成配置 ===")
        for i in range(3):
            btn = page.locator(".wd-wizard-primary-btn").filter(has_text="下一步").first
            if btn.count() == 0:
                break
            btn.click()
            page.wait_for_timeout(500)
        finish_btn = page.locator(".wd-wizard-primary-btn").filter(has_text="完成配置").first
        finish_btn.click()
        page.wait_for_timeout(2500)

        active_after = page.evaluate("window.DemoGuard._activeApp")
        cur_hash = page.evaluate("location.hash")
        print(f"  _activeApp = {active_after!r}, hash = {cur_hash!r}")

        print("\n=== 4. 点 应用列表 button ===")
        page.click('.nav-item[data-page="apps"]')
        page.wait_for_timeout(1500)

        new_hash = page.evaluate("location.hash")
        new_active = page.evaluate("window.DemoGuard._activeApp")
        wizard_visible = page.locator(".wd-wizard-section").count()
        page_apps_visible = page.evaluate("""() => {
            const el = document.getElementById("page-apps");
            return el && !el.classList.contains("hidden");
        }""")
        print(f"  hash = {new_hash!r}, _activeApp = {new_active!r}, wizard sections = {wizard_visible}, #page-apps visible = {page_apps_visible}")

        if new_hash == "#video-detection":
            print("  ❌ BUG 自然重现")
        elif new_hash == "#apps":
            print("  ✅ 自然流程正确")

        browser.close()

if __name__ == "__main__":
    main()