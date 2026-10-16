#!/usr/bin/env python3
"""复现 back-home "退出应用" 后重进 vtd 不进 wizard 的 bug。

策略:不模拟整个 wizard 流程(慢 + env_check 可能阻塞),
直接往 localStorage 灌"已完成的 wizard"状态,等效于"用户刚走完 wizard
到达功能页"的最终态。然后走 back-home + 重进这两个关键链路。
"""
import sys
from playwright.sync_api import sync_playwright


# Single-quoted JS helpers, no embedded single quotes inside.
def js_page_visible(page_id):
    return """() => {
        const el = document.getElementById('%s');
        return el ? !el.classList.contains('hidden') : false;
    }""" % page_id


def js_ls_filter(prefix=None):
    if prefix is None:
        return "() => Object.keys(localStorage)"
    return "() => Object.keys(localStorage).filter(k => k.includes('%s'))" % prefix


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-gpu"],
        )
        ctx = browser.new_context()
        page = ctx.new_page()

        logs = []
        page.on("console", lambda m: logs.append((m.type, m.text))
                if m.type in ("warning", "error", "log") else None)
        page.on("pageerror", lambda e: logs.append(("pageerror", str(e))))

        def dump(label):
            print(f"\n--- console: {label} ---")
            for typ, txt in logs[-30:]:
                print(f"  [{typ}] {txt}")
            logs.clear()

        # ============================================================
        # 0. clean slate + 模拟 "刚完成 wizard" 状态
        # ============================================================
        print("=" * 60)
        print(" 0. clean + 模拟 completed wizard 状态")
        print("=" * 60)
        page.goto("http://127.0.0.1:8000/", wait_until="domcontentloaded",
                  timeout=15000)

        # 把 wizard 的 localStorage 灌成"已完成的 wizard"状态
        page.evaluate("""() => {
            for (const k of Object.keys(localStorage)) {
                if (k.indexOf('wizard') >= 0) localStorage.removeItem(k);
            }
            localStorage.setItem(
                'wdnwebui.wizard.video-target-detection.values',
                JSON.stringify({
                    'vtd-sources': 'rtsp://localhost/0',
                    'vtd-loop': true,
                    'vtd-prompt': '请检测画面中的目标',
                }));
            localStorage.setItem(
                'wdnwebui.wizard.video-target-detection.step', '4');
            localStorage.setItem(
                'wdnwebui.wizard.video-target-detection.completed', '1');
        }""")

        page.reload(wait_until="domcontentloaded")
        page.wait_for_timeout(500)

        # 先访问 vtd 一次让 framework 加载它的 JS。
        page.goto("http://127.0.0.1:8000/#video-target-detection",
                  wait_until="domcontentloaded", timeout=15000)
        page.wait_for_timeout(2500)
        dump("after first vtd entry")

        # 现在 wizard 应该被跳过(isCompleted=true),落到功能页
        wizard_count_1 = page.locator(".wd-wizard-section").count()
        page_vtd_visible_1 = page.evaluate(js_page_visible(
            "page-video-target-detection"))
        print(f"  wizard sections = {wizard_count_1} (期望 0)")
        print(f"  page-vtd visible = {page_vtd_visible_1} (期望 true)")
        ls_1 = page.evaluate(js_ls_filter("video-target-detection"))
        print(f"  ls = {ls_1}")
        assert len(ls_1) == 3, f"unexpected ls: {ls_1}"
        print("  [OK] 模拟 wizard 已完成,落到功能页")

        # ============================================================
        # 3. 点右上角 "退出应用"
        # ============================================================
        print("\n" + "=" * 60)
        print(" 3. 点 '退出应用' (back-home btn)")
        print("=" * 60)

        # 模拟 demo_guard 报告让 label 变 "退出应用"
        page.evaluate("""() => {
            if (window.DemoGuard && DemoGuard.report) {
                DemoGuard.report('video-target-detection', {
                    session: { state: 'playing' },
                });
            }
        }""")
        page.wait_for_timeout(500)

        back_btn = page.locator(
            "#page-video-target-detection .back-home-btn").filter(
            has_text="退出应用").first
        print(f"  back-home '退出应用' (scoped to vtd) count = {back_btn.count()}")
        if back_btn.count() == 0:
            back_btn = page.locator(
                "#page-video-target-detection .back-home-btn").filter(
                has_text="返回首页").first
            print(f"  (fallback) back-home '返回首页' count = {back_btn.count()}")

        back_btn.click()
        page.wait_for_timeout(2500)
        dump("after back-home click")

        ls_2 = page.evaluate(js_ls_filter("video-target-detection"))
        completed_2 = page.evaluate("""() => localStorage.getItem(
            'wdnwebui.wizard.video-target-detection.completed')""")
        hash_2 = page.evaluate("location.hash")
        print(f"  hash after exit = {hash_2!r}")
        print(f"  ls after exit = {ls_2}")
        print(f"  completed after exit = {completed_2!r} (期望 null)")

        if completed_2 is None and not ls_2:
            print("  [OK] back-home 已清空 vtd 的 wizard 状态")
        else:
            print(f"  [FAIL] back-home 没清干净!completed={completed_2!r}")
            print("  —— 这是 wizard 不弹的根因")

        # ============================================================
        # 4. 重进 视频目标监控
        # ============================================================
        print("\n" + "=" * 60)
        print(" 4. 重进 视频目标监控")
        print("=" * 60)
        # 从 home page 上点 card
        page.wait_for_selector(".app-card", timeout=5000)
        card = page.locator(".app-card").filter(
            has_text="视频目标监控").first
        print(f"  card count = {card.count()}")
        card.click()
        page.wait_for_timeout(3500)
        dump("after re-enter")

        wizard_count_3 = page.locator(".wd-wizard-section").count()
        page_vtd_visible_3 = page.evaluate(js_page_visible(
            "page-video-target-detection"))
        hash_3 = page.evaluate("location.hash")
        completed_3 = page.evaluate("""() => localStorage.getItem(
            'wdnwebui.wizard.video-target-detection.completed')""")
        print(f"  hash = {hash_3!r}")
        print(f"  wizard sections = {wizard_count_3}")
        print(f"  page-vtd visible = {page_vtd_visible_3}")
        print(f"  completed after re-enter = {completed_3!r}")

        # ============================================================
        # 5. 结论
        # ============================================================
        print("\n" + "=" * 60)
        print(" 5. 结论")
        print("=" * 60)
        if wizard_count_3 > 0:
            print("  [OK] wizard 弹出 —— 修复生效")
        elif page_vtd_visible_3 and wizard_count_3 == 0:
            print("  [FAIL] BUG 复现: feature page visible, wizard 不弹")
            print("     检查 back-home 后的 console warn:")
            for typ, txt in logs[-15:]:
                if "DEBUG" in txt:
                    print(f"     [{typ}] {txt}")
        else:
            print(f"  ⚠️  unexpected: wizard={wizard_count_3}, "
                  f"page_visible={page_vtd_visible_3}")

        page.screenshot(path="/tmp/vtd_after_reenter.png", full_page=True)
        print(f"\n  截图: /tmp/vtd_after_reenter.png")

        browser.close()


if __name__ == "__main__":
    main()