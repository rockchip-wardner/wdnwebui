#!/usr/bin/env python3
"""验证:空 errBox 隐藏,有内容时显示。

直接 mount 到 finish 步骤(绕过 env/source/detect/audio 等步骤
依赖真实模型状态),专注于验证 CSS :empty 规则效果。
"""
from playwright.sync_api import sync_playwright

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
        ctx = browser.new_context()
        page = ctx.new_page()
        page.on("pageerror", lambda e: print(f"  [pageerror] {e}"))

        page.goto("http://127.0.0.1:8000/", wait_until="domcontentloaded", timeout=15000)
        page.evaluate("Object.keys(localStorage).filter(k => k.includes('wizard')).forEach(k => localStorage.removeItem(k))")
        page.evaluate("try { window.WizardState.unmount(); } catch(_) {}")

        # 直接 mount + stepForward 到 finish 步骤(5 步:env → source → detect → audio → finish)
        page.evaluate("""
            (async () => {
                const cards = window.__APP_MANIFESTS__?.cards || [];
                const manifest = cards.find(c => c.id === 'video-detection');
                const r = await fetch('/static/apps/video-detection/wizard.json', {cache:'no-store'});
                const cfg = await r.json();
                await window.WizardState.mount(manifest, cfg);
                for (let i = 0; i < 4; i++) window.WizardState.stepForward();
            })()
        """)
        page.wait_for_timeout(800)

        # 检查 finish 步骤渲染
        step_title = page.evaluate("""() => {
            const t = document.querySelector('.wd-wizard-step-title');
            return t?.textContent || null;
        }""")
        print(f"current step: {step_title}")

        # 测试 1:errBox 空时 → display: none
        s1 = page.evaluate("""() => {
            const e = document.getElementById('wd-wizard-finish-error');
            if (!e) return {found: false};
            const s = getComputedStyle(e);
            return {found: true, text: e.textContent, display: s.display, height: e.offsetHeight};
        }""")
        print(f"\n=== 1) 空 errBox ===")
        print(f"  {s1}")
        t1 = s1['found'] and s1['display'] == 'none'
        print(f"  {'✅' if t1 else '❌'} {'隐藏' if t1 else '未隐藏'}")

        # 测试 2:写文字后 → errBox 显示
        page.evaluate("document.getElementById('wd-wizard-finish-error').textContent = '等待模型启动中... (1 个)';")
        page.wait_for_timeout(200)
        s2 = page.evaluate("""() => {
            const e = document.getElementById('wd-wizard-finish-error');
            if (!e) return {found: false};
            const s = getComputedStyle(e);
            return {found: true, text: e.textContent, display: s.display, height: e.offsetHeight};
        }""")
        print(f"\n=== 2) 有文字 errBox ===")
        print(f"  {s2}")
        t2 = s2['found'] and s2['display'] != 'none' and s2['height'] > 0
        print(f"  {'✅' if t2 else '❌'} {'显示' if t2 else '未显示'}")

        # 测试 3:清空后 → 重新隐藏
        page.evaluate("document.getElementById('wd-wizard-finish-error').textContent = '';")
        page.wait_for_timeout(200)
        s3 = page.evaluate("""() => {
            const e = document.getElementById('wd-wizard-finish-error');
            if (!e) return {found: false};
            const s = getComputedStyle(e);
            return {found: true, text: e.textContent, display: s.display, height: e.offsetHeight};
        }""")
        print(f"\n=== 3) 清空后 ===")
        print(f"  {s3}")
        t3 = s3['found'] and s3['display'] == 'none'
        print(f"  {'✅' if t3 else '❌'} {'重新隐藏' if t3 else '未隐藏'}")

        browser.close()
        if t1 and t2 and t3:
            print(f"\n✅ 3/3 通过")
            return 0
        return 1

if __name__ == "__main__":
    import sys
    sys.exit(main() or 0)