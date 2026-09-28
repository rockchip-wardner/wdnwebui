#!/usr/bin/env python3
"""Smoke test: 5 个挂载 wizard 的应用都能 mount wizard + ImApp_<id> 暴露正确。"""
from playwright.sync_api import sync_playwright
import sys, time

APPS = ["text-chat", "tts", "video-detection", "video-target-detection", "video-target-localization"]

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
        page = browser.new_context().new_page()
        page.on("pageerror", lambda e: print(f"  [pageerror] {e}"))

        ok_count = 0
        for app_id in APPS:
            url = f"http://127.0.0.1:8000/#{app_id}"
            print(f"\n=== {app_id} ===")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=15000)
                # 等 wizard mount + stepper 渲染
                page.wait_for_selector(".wd-wizard-section", timeout=8000)
                page.wait_for_selector(".wd-wizard-step-dot", timeout=5000)
                sections = page.locator(".wd-wizard-section").count()
                dots = page.locator(".wd-wizard-step-dot").count()
                # 检查 ImApp_<id> 是否暴露
                ns = app_id.replace("-", "_")
                imapp_ok = page.evaluate(f"typeof window.ImApp_{ns}?.applyWizardSnapshot === 'function'")
                print(f"  wizard sections={sections}, dots={dots}, ImApp_{ns}.applyWizardSnapshot={imapp_ok}")
                if sections == 1 and dots >= 2 and imapp_ok:
                    ok_count += 1
                    print(f"  ✅ {app_id} OK")
                else:
                    print(f"  ❌ {app_id} FAIL")
            except Exception as e:
                print(f"  ❌ {app_id}: {e}")

        browser.close()
        print(f"\n=== 总结: {ok_count}/{len(APPS)} ===")
        if ok_count == len(APPS):
            print("✅ 全部通过")
        else:
            print(f"❌ {len(APPS) - ok_count} 失败")
            sys.exit(1)

if __name__ == "__main__":
    main()