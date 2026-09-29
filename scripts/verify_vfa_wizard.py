#!/usr/bin/env python3
"""验证 视频文件分析 wizard mount + stepForward + applyWizardSnapshot。

不依赖真模型启动(直接 mount wizard,绕过 env)。
"""
from playwright.sync_api import sync_playwright

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
        ctx = browser.new_context()
        page = ctx.new_page()
        page.on("pageerror", lambda e: print(f"  [pageerror] {e}"))

        page.goto("http://127.0.0.1:8000/#video-file-analysis", wait_until="domcontentloaded", timeout=15000)
        page.wait_for_selector(".wd-wizard-section", timeout=8000)
        page.wait_for_timeout(500)

        ok = 0
        # 测试 1:基本 mount
        basic = page.evaluate("""() => ({
            sections: document.querySelectorAll('.wd-wizard-section').length,
            dots: document.querySelectorAll('.wd-wizard-step-dot').length,
            title: document.querySelector('.wd-wizard-title')?.textContent,
            ImApp: typeof window.ImApp_video_file_analysis?.applyWizardSnapshot,
        })""")
        print(f"=== 1) wizard mount ===")
        print(f"  {basic}")
        if basic['sections'] == 1 and basic['dots'] == 5 and basic['ImApp'] == 'function':
            print(f"  ✅ wizard mount OK")
            ok += 1
        else:
            print(f"  ❌ FAIL")

        # 测试 2:跳到 source 步骤 → 浏览 button 自动出现
        page.evaluate("window.WizardState.stepForward()")
        page.wait_for_timeout(400)
        s2 = page.evaluate("""() => ({
            stepTitle: document.querySelector('.wd-wizard-step-title')?.textContent,
            hasSourceInput: !!document.querySelector('[data-field-id="vfa-source"]'),
            hasBrowseBtn: !!document.querySelector('.wd-wizard-browse-btn'),
            stepDotsCount: document.querySelectorAll('.wd-wizard-step-dot').length,
        })""")
        print(f"\n=== 2) source step ===")
        print(f"  {s2}")
        if s2['hasSourceInput'] and s2['hasBrowseBtn'] and s2['stepTitle'] == '视频源':
            print(f"  ✅ source 步骤 OK")
            ok += 1
        else:
            print(f"  ❌ FAIL")

        # 测试 3:跳到 prompt + sample 步骤
        page.evaluate("window.WizardState.stepForward()")
        page.wait_for_timeout(300)
        s3a = page.evaluate("""() => ({
            stepTitle: document.querySelector('.wd-wizard-step-title')?.textContent,
            hasPromptTextarea: !!document.querySelector('[data-field-id="vfa-prompt"]'),
        })""")
        print(f"\n=== 3) prompt step ===")
        print(f"  {s3a}")
        if s3a['hasPromptTextarea'] and s3a['stepTitle'] == '分析要求':
            print(f"  ✅ prompt 步骤 OK")
            ok += 1
        else:
            print(f"  ❌ FAIL")

        page.evaluate("window.WizardState.stepForward()")
        page.wait_for_timeout(300)
        s3b = page.evaluate("""() => ({
            stepTitle: document.querySelector('.wd-wizard-step-title')?.textContent,
            hasEveryNInput: !!document.querySelector('[data-field-id="vfa-every-n"]'),
        })""")
        print(f"\n=== 4) sample step ===")
        print(f"  {s3b}")
        if s3b['hasEveryNInput'] and s3b['stepTitle'] == '采样间隔':
            print(f"  ✅ sample 步骤 OK")
            ok += 1
        else:
            print(f"  ❌ FAIL")

        # 测试 5:finish 步骤
        page.evaluate("window.WizardState.stepForward()")
        page.wait_for_timeout(300)
        s4 = page.evaluate("""() => ({
            stepTitle: document.querySelector('.wd-wizard-step-title')?.textContent,
            hasFinishBtn: !!document.getElementById('wd-wizard-finish-btn'),
            hasFinishErrorHidden: !!document.getElementById('wd-wizard-finish-error'),
        })""")
        print(f"\n=== 5) finish step ===")
        print(f"  {s4}")
        if s4['hasFinishBtn'] and s4['stepTitle'] == '完成配置':
            print(f"  ✅ finish 步骤 OK")
            ok += 1
        else:
            print(f"  ❌ FAIL")

        # 测试 6:applyWizardSnapshot(opts) 写值但不点 start(因为缺字段会走 fast-return)
        page.evaluate("""(async () => {
                // 模拟 wizard finish 走 applyWizardSnapshot
                try { window.WizardState.unmount(); } catch(_) {}
                const cards = window.__APP_MANIFESTS__?.cards || [];
                const manifest = cards.find(c => c.id === 'video-file-analysis');
                await window.WizardState.mount(manifest, {steps:[{kind:'env_check'},{kind:'form',fields:[{id:'vfa-source'}]},{kind:'finish'}]});
            })()
        """)
        page.wait_for_timeout(800)
        result = page.evaluate("""(async () => {
            // 等页面 DOM 就绪
            await new Promise(r => setTimeout(r, 300));
            const r = await window.ImApp_video_file_analysis.applyWizardSnapshot({source: '/tmp/test.mp4'});
            const sourceVal = document.getElementById('vfa-source')?.value;
            const promptVal = document.getElementById('vfa-prompt')?.value;
            const everyNVal = document.getElementById('vfa-every-n')?.value;
            return {result: r, sourceVal, promptVal, everyNVal};
        })""")
        print(f"\n=== 6) applyWizardSnapshot (仅 source) ===")
        print(f"  {result}")
        # source_set 应为 true,started 应为 false(缺 prompt + everyN)
        if result['result']['source_set'] and not result['result']['started'] and result['sourceVal'] == '/tmp/test.mp4':
            print(f"  ✅ 部分字段写回 OK,未启动分析")
            ok += 1
        else:
            print(f"  ❌ FAIL")

        browser.close()
        print(f"\n=== 总结: {ok}/6 ===")
        if ok == 6:
            print("✅ 全部通过")
            return 0
        return 1

if __name__ == "__main__":
    import sys
    sys.exit(main() or 0)