#!/usr/bin/env python3
"""Smoke test: 5 个挂载 wizard 的应用都能 mount wizard + ImApp_<id> 暴露正确。

每个 app 用独立 browser context(全新 storage / cookies / cache):
  - 避免上一个 app 的 localStorage / sessionStorage / service worker /
    WebSocket 连接残留影响下一个 app 的 page.goto + wizard mount。
  - 历史踩坑:复用 context 时,第 4-5 个 app 的 page.goto 会偶发
    timeout 15s (DOMContentLoaded 不触发),根因未深查但症状与
    playwright 在前一次 navigation 的等待队列未排空有关。
"""
from playwright.sync_api import sync_playwright
import sys, time

APPS = ["text-chat", "tts", "video-detection", "video-file-analysis", "video-target-detection", "video-target-localization"]

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])

        ok_count = 0
        for app_id in APPS:
            url = f"http://127.0.0.1:8000/#{app_id}"
            print(f"\n=== {app_id} ===")
            # 每个 app 独立 context — 干净 storage,避免 cross-test 干扰
            ctx = browser.new_context()
            page = ctx.new_page()
            page.on("pageerror", lambda e, app=app_id: print(f"  [{app}][pageerror] {e}"))
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
            finally:
                ctx.close()

        browser.close()
        print(f"\n=== 总结: {ok_count}/{len(APPS)} ===")
        if ok_count == len(APPS):
            print("✅ 全部通过")
        else:
            print(f"❌ {len(APPS) - ok_count} 失败")
            sys.exit(1)

if __name__ == "__main__":
    main()