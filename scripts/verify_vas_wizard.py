#!/usr/bin/env python3
"""验证 监控分析检索 wizard mount + stepForward + applyWizardSnapshot。

不依赖真模型启动(直接 mount wizard,绕过 env)。

向导步骤(与 wizard.json 一致):env → source(含判据) → search → finish。
"""
from playwright.sync_api import sync_playwright

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
        ctx = browser.new_context()
        page = ctx.new_page()
        page.on("pageerror", lambda e: print(f"  [pageerror] {e}"))

        page.goto("http://127.0.0.1:8000/#video-analysis-search", wait_until="domcontentloaded", timeout=15000)
        page.wait_for_selector(".wd-wizard-section", timeout=8000)
        page.wait_for_timeout(500)

        ok = 0
        # 测试 1:基本 mount
        basic = page.evaluate("""() => ({
            sections: document.querySelectorAll('.wd-wizard-section').length,
            dots: document.querySelectorAll('.wd-wizard-step-dot').length,
            title: document.querySelector('.wd-wizard-title')?.textContent,
            ImApp: typeof window.ImApp_video_analysis_search?.applyWizardSnapshot,
        })""")
        print(f"=== 1) wizard mount ===")
        print(f"  {basic}")
        if basic['sections'] == 1 and basic['dots'] == 4 and basic['ImApp'] == 'function':
            print(f"  ✅ wizard mount OK")
            ok += 1
        else:
            print(f"  ❌ FAIL")

        # 测试 2:source 步骤(视频源；判据参数已固化 → 不应再出现判据字段)
        page.evaluate("window.WizardState.stepForward()")
        page.wait_for_timeout(400)
        s2 = page.evaluate("""() => ({
            stepTitle: document.querySelector('.wd-wizard-step-title')?.textContent,
            hasSourceInput: !!document.querySelector('[data-field-id="vcs-source"]'),
            hasBrowseBtn: !!document.querySelector('.wd-wizard-browse-btn'),
            hasJudgeMode: !!document.querySelector('[data-field-id="vas-judge-mode"]'),
        })""")
        print(f"\n=== 2) source step ===")
        print(f"  {s2}")
        if (s2['hasSourceInput'] and s2['hasBrowseBtn']
                and s2['stepTitle'] == '视频源'
                and not s2['hasJudgeMode']):
            print(f"  ✅ source 步骤 OK(且无判据字段)")
            ok += 1
        else:
            print(f"  ❌ FAIL")

        # 测试 3:search 步骤(检索问题)
        page.evaluate("window.WizardState.stepForward()")
        page.wait_for_timeout(300)
        s3 = page.evaluate("""() => ({
            stepTitle: document.querySelector('.wd-wizard-step-title')?.textContent,
            hasSearchTextarea: !!document.querySelector('[data-field-id="vcs-text"]'),
            hasStalePromptField: !!document.querySelector('[data-field-id="vas-prompt"]'),
        })""")
        print(f"\n=== 3) search step ===")
        print(f"  {s3}")
        if s3['hasSearchTextarea'] and s3['stepTitle'] == '检索问题' and not s3['hasStalePromptField']:
            print(f"  ✅ search 步骤 OK(且无分析提问字段)")
            ok += 1
        else:
            print(f"  ❌ FAIL")

        # 测试 4:finish 步骤
        page.evaluate("window.WizardState.stepForward()")
        page.wait_for_timeout(300)
        s4 = page.evaluate("""() => ({
            stepTitle: document.querySelector('.wd-wizard-step-title')?.textContent,
            hasFinishBtn: !!document.getElementById('wd-wizard-finish-btn'),
            hasFinishErrorHidden: !!document.getElementById('wd-wizard-finish-error'),
            hasChunkField: !!document.querySelector('[data-field-id="vcs-chunk-seconds"]'),
        })""")
        print(f"\n=== 4) finish step ===")
        print(f"  {s4}")
        if (s4['hasFinishBtn'] and s4['stepTitle'] == '完成配置'
                and not s4['hasChunkField']):
            print(f"  ✅ finish 步骤 OK(且无切片参数字段)")
            ok += 1
        else:
            print(f"  ❌ FAIL")

        # 测试 5:applyWizardSnapshot 写回源
        page.evaluate("""(async () => {
                try { window.WizardState.unmount(); } catch(_) {}
                const cards = window.__APP_MANIFESTS__?.cards || [];
                const manifest = cards.find(c => c.id === 'video-analysis-search');
                await window.WizardState.mount(manifest, {steps:[{kind:'env_check'},{kind:'form',fields:[{id:'vcs-source'}]},{kind:'finish'}]});
            })()
        """)
        page.wait_for_timeout(800)
        result = page.evaluate("""(async () => {
            await new Promise(r => setTimeout(r, 300));
            const r = await window.ImApp_video_analysis_search.applyWizardSnapshot({source: '/tmp/test.mp4'});
            const sourceVal = document.getElementById('vcs-source')?.value;
            return {result: r, sourceVal};
        })""")
        print(f"\n=== 5) applyWizardSnapshot (仅 source) ===")
        print(f"  {result}")
        # prompt 固化 → 有 source 即可启动分析(started=true)
        if result['result']['source_set'] and result['sourceVal'] == '/tmp/test.mp4':
            print(f"  ✅ 源字段写回 OK")
            ok += 1
        else:
            print(f"  ❌ FAIL")

        browser.close()
        print(f"\n=== 总结: {ok}/5 ===")
        if ok == 5:
            print("✅ 全部通过")
            return 0
        return 1

if __name__ == "__main__":
    import sys
    sys.exit(main() or 0)
