#!/usr/bin/env python3
"""验证 wizard unmount 还原 canvas 元素。

精简版:走真实 mount → stepForward → 写 source → 点 打开 → unmount
流程,确认 canvas 在 unmount 后回到 page section 的 vd-stage。
"""
from playwright.sync_api import sync_playwright

VIDEO = "/userdata/test/tmp/video_target_detection/60b0af0d5cc54e9fa22b169864876c2c.mp4"

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
        ctx = browser.new_context()
        page = ctx.new_page()
        page.on("pageerror", lambda e: print(f"  [pageerror] {e}"))
        page.on("console", lambda m: m.type in ("error", "warning") and print(f"  [{m.type}] {m.text[:200]}"))

        page.goto("http://127.0.0.1:8000/", wait_until="domcontentloaded", timeout=15000)
        page.evaluate("Object.keys(localStorage).filter(k => k.includes('wizard')).forEach(k => localStorage.removeItem(k))")
        page.evaluate("try { window.WizardState.unmount(); } catch(_) {}")

        # 进入 vtd app — wizard 自动 mount(env 步骤)
        page.goto("http://127.0.0.1:8000/#video-target-detection", wait_until="domcontentloaded", timeout=15000)
        page.wait_for_selector(".wd-wizard-section", timeout=8000)
        page.wait_for_timeout(800)

        # 手动 mount 到 source(跳 env_check)
        page.evaluate("""
            (async () => {
                try { window.WizardState.unmount(); } catch(_) {}
                const cards = window.__APP_MANIFESTS__?.cards || [];
                const manifest = cards.find(c => c.id === 'video-target-detection');
                const r = await fetch('/static/apps/video-target-detection/wizard.json', {cache:'no-store'});
                const cfg = await r.json();
                await window.WizardState.mount(manifest, cfg);
                window.WizardState.stepForward();
            })()
        """)
        page.wait_for_timeout(800)

        # 写 source
        page.evaluate(f"(() => {{ const s = document.getElementById('vtd-sources'); if (s) s.value = {VIDEO!r}; }})()")
        page.wait_for_timeout(300)

        # 点 打开
        page.locator(".wd-wizard-source-action-btn").filter(has_text="打开").first.click()
        page.wait_for_timeout(3000)

        # 阶段 1:canvas 应被搬到 wizard canvas-wrap
        s1 = page.evaluate("""() => {
            const c = document.getElementById('vtd-grid');
            const wrap = document.querySelector('.wd-wizard-canvas-wrap');
            const inPage = !!(c && wrap && c.parentNode === wrap);
            return {inWrap: inPage, hasWizard: !!document.querySelector('.wd-wizard-section')};
        }""")
        print(f"=== 阶段 1:点 打开 后 canvas 在 wizard 内 ===")
        print(f"  {s1}")
        if not s1['inWrap']:
            print("  ❌ canvas 没搬进 wizard — 修复路径无意义,test 不通过")
            return 1

        # 阶段 2:调 unmount(模拟完成配置)
        page.evaluate("window.WizardState.unmount()")
        page.wait_for_timeout(500)

        s2 = page.evaluate("""() => {
            const c = document.getElementById('vtd-grid');
            const pageSection = document.getElementById('page-video-target-detection');
            const stages = Array.from(document.querySelectorAll('.vd-stage'));
            return {
                exists: !!c,
                // 宫格容器自身就是 .vd-stage（不被 stage 包着），两种都接受。
                inPageStage: stages.some(s => pageSection?.contains(s) && (s === c || s.contains(c))),
                parentClass: c?.parentNode?.className || null,
                hasWizard: !!document.querySelector('.wd-wizard-section'),
                pageSectionVisible: pageSection && !pageSection.classList.contains('hidden'),
            };
        }""")
        print(f"\n=== 阶段 2:wizard unmount 后 ===")
        print(f"  {s2}")

        if s2['exists'] and s2['inPageStage'] and not s2['hasWizard'] and s2['pageSectionVisible']:
            print(f"\n  ✅ canvas 已还原到 page section vd-stage,wizard 已拆,应用页正常")
            print(f"  ✅ Bug 已修复")
            rc = 0
        else:
            print(f"\n  ❌ canvas 没还原")
            rc = 1

        browser.close()
        return rc

if __name__ == "__main__":
    import sys
    sys.exit(main() or 0)