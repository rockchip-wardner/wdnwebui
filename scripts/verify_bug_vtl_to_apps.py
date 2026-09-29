#!/usr/bin/env python3
"""Reproduce bug: 视频目标定位 完成配置 后,点 应用列表 跳转到 音视频描述 向导。

Run:
  .venv/bin/python scripts/verify_bug_vtl_to_apps.py
"""
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

        # 清掉 wizard localStorage,确保纯净测试
        page.evaluate("Object.keys(localStorage).filter(k => k.includes('wizard')).forEach(k => localStorage.removeItem(k))")

        # 模拟"之前玩过音视频描述"场景 — 手动注入 stale video-detection cache
        print("\n=== 2. 模拟之前玩过音视频描述(注入 stale state) ===")
        page.evaluate("""
            window.DemoGuard.report("video-detection", {
                session: { state: "playing" },
                detector: { enabled: true }
            });
        """)
        page.wait_for_timeout(300)

        active_before = page.evaluate("window.DemoGuard._activeApp")
        print(f"  _activeApp = {active_before!r}")

        print("\n=== 3. 进入 视频目标定位 ===")
        page.goto("http://127.0.0.1:8000/#video-target-localization", wait_until="domcontentloaded", timeout=15000)
        page.wait_for_selector(".wd-wizard-section", timeout=8000)
        print("  ✅ wizard mounted")

        print("\n=== 4. 点击 下一步 x3 + 完成配置 ===")
        for i in range(3):
            btn = page.locator(".wd-wizard-primary-btn").filter(has_text="下一步").first
            if btn.count() == 0:
                # 可能在 finish 步骤了
                break
            btn.click()
            page.wait_for_timeout(500)

        # 完成配置按钮
        finish_btn = page.locator(".wd-wizard-primary-btn").filter(has_text="完成配置").first
        finish_btn.click()
        page.wait_for_timeout(2000)  # 等 finish 跑完(可能 _waitForModelsRunning)

        # 检查 wizard 是否 unmount + app page 显示
        wizard_count = page.locator(".wd-wizard-section").count()
        page_visible = page.evaluate("""
            () => {
                const el = document.getElementById("page-video-target-localization");
                return el && !el.classList.contains("hidden");
            }
        """)
        print(f"  wizard sections={wizard_count}, page visible={page_visible}")

        active_after = page.evaluate("window.DemoGuard._activeApp")
        cur_page = page.evaluate("location.hash")
        print(f"  _activeApp = {active_after!r}, current hash = {cur_page!r}")

        print("\n=== 5. 点击 应用列表 button (sidebar .nav-item[data-page='apps']) ===")
        # 点击 sidebar nav-item
        page.click('.nav-item[data-page="apps"]')
        page.wait_for_timeout(1500)

        new_hash = page.evaluate("location.hash")
        new_active = page.evaluate("window.DemoGuard._activeApp")
        wizard_visible = page.locator(".wd-wizard-section").count()
        page_apps_visible = page.evaluate("""
            () => {
                const el = document.getElementById("page-apps");
                return el && !el.classList.contains("hidden");
            }
        """)
        print(f"  hash = {new_hash!r}")
        print(f"  _activeApp = {new_active!r}")
        print(f"  wizard sections = {wizard_visible}")
        print(f"  #page-apps visible = {page_apps_visible}")

        print("\n=== 6. 诊断 ===")
        if new_hash == "#video-detection" and wizard_visible > 0:
            print("  ❌ BUG 已重现:hash 跳到 #video-detection + wizard 自动挂载")
        elif new_hash == "#apps" and page_apps_visible:
            print("  ✅ 正常:导航到 #apps,无 wizard 挂载")
        else:
            print(f"  ❓ 不符合预期")

        # save evidence
        page.screenshot(path="/tmp/vtl-bug-screenshot.png", full_page=True)
        print("  screenshot → /tmp/vtl-bug-screenshot.png")

        browser.close()

if __name__ == "__main__":
    main()