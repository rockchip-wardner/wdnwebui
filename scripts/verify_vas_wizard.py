#!/usr/bin/env python3
"""验证 视频分析检索 wizard mount + stepForward + applyWizardSnapshot。

不依赖真模型启动(直接 mount wizard,绕过 env)。
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
        if basic['sections'] == 1 and basic['dots'] == 6 and basic['ImApp'] == 'function':
            print(f"  ✅ wizard mount OK")
            ok += 1
        else:
            print(f"  ❌ FAIL")

        # 测试 2:跳到 source 步骤 → 浏览 button 自动出现
        page.evaluate("window.WizardState.stepForward()")
        page.wait_for_timeout(400)
        s2 = page.evaluate("""() => ({
            stepTitle: document.querySelector('.wd-wizard-step-title')?.textContent,
            hasSourceInput: !!document.querySelector('[data-field-id="vcs-source"]'),
            hasBrowseBtn: !!document.querySelector('.wd-wizard-browse-btn'),
            stepDotsCount: document.querySelectorAll('.wd-wizard-step-dot').length,
            // 判据三字段（算法 / 阈值 / 帧间隔）在 5dcd790 之后并进了
            // source 步骤，原先独立的「采样间隔」步骤已不存在 —— 这里断言
            // 它们在 source 步内可见，否则 wizard 能过而配置项根本没渲染。
            hasJudgeMode: !!document.querySelector('[data-field-id="vas-judge-mode"]'),
            hasJudgeThreshold: !!document.querySelector('[data-field-id="vas-judge-threshold"]'),
            hasJudgeEveryN: !!document.querySelector('[data-field-id="vas-judge-every-n"]'),
        })""")
        print(f"\n=== 2) source step ===")
        print(f"  {s2}")
        if (s2['hasSourceInput'] and s2['hasBrowseBtn'] and s2['stepTitle'] == '视频源'
                and s2['hasJudgeMode'] and s2['hasJudgeThreshold'] and s2['hasJudgeEveryN']):
            print(f"  ✅ source 步骤 OK")
            ok += 1
        else:
            print(f"  ❌ FAIL")

        # 测试 3:prompt 步骤
        page.evaluate("window.WizardState.stepForward()")
        page.wait_for_timeout(300)
        s3a = page.evaluate("""() => ({
            stepTitle: document.querySelector('.wd-wizard-step-title')?.textContent,
            hasPromptTextarea: !!document.querySelector('[data-field-id="vas-prompt"]'),
        })""")
        print(f"\n=== 3) prompt step ===")
        print(f"  {s3a}")
        if s3a['hasPromptTextarea'] and s3a['stepTitle'] == '分析要求':
            print(f"  ✅ prompt 步骤 OK")
            ok += 1
        else:
            print(f"  ❌ FAIL")

        # 测试 4:search 步骤(检索问题)
        page.evaluate("window.WizardState.stepForward()")
        page.wait_for_timeout(300)
        s3c = page.evaluate("""() => ({
            stepTitle: document.querySelector('.wd-wizard-step-title')?.textContent,
            hasSearchTextarea: !!document.querySelector('[data-field-id="vcs-text"]'),
        })""")
        print(f"\n=== 4) search step ===")
        print(f"  {s3c}")
        if s3c['hasSearchTextarea'] and s3c['stepTitle'] == '检索问题':
            print(f"  ✅ search 步骤 OK")
            ok += 1
        else:
            print(f"  ❌ FAIL")

        # 测试 5:chunk 步骤(切片参数)
        page.evaluate("window.WizardState.stepForward()")
        page.wait_for_timeout(300)
        s3d = page.evaluate("""() => ({
            stepTitle: document.querySelector('.wd-wizard-step-title')?.textContent,
            hasChunkSelect: !!document.querySelector('[data-field-id="vcs-chunk-seconds"]'),
            hasOverlapSelect: !!document.querySelector('[data-field-id="vcs-chunk-overlap"]'),
        })""")
        print(f"\n=== 5) chunk step ===")
        print(f"  {s3d}")
        if s3d['hasChunkSelect'] and s3d['hasOverlapSelect'] and s3d['stepTitle'] == '切片参数':
            print(f"  ✅ chunk 步骤 OK")
            ok += 1
        else:
            print(f"  ❌ FAIL")

        # 测试 6:finish 步骤
        page.evaluate("window.WizardState.stepForward()")
        page.wait_for_timeout(300)
        s4 = page.evaluate("""() => ({
            stepTitle: document.querySelector('.wd-wizard-step-title')?.textContent,
            hasFinishBtn: !!document.getElementById('wd-wizard-finish-btn'),
            hasFinishErrorHidden: !!document.getElementById('wd-wizard-finish-error'),
        })""")
        print(f"\n=== 6) finish step ===")
        print(f"  {s4}")
        if s4['hasFinishBtn'] and s4['stepTitle'] == '完成配置':
            print(f"  ✅ finish 步骤 OK")
            ok += 1
        else:
            print(f"  ❌ FAIL")

        # 测试 7:applyWizardSnapshot(opts) 写值但不点 start(因为缺字段会走 fast-return)
        page.evaluate("""(async () => {
                // 模拟 wizard finish 走 applyWizardSnapshot
                try { window.WizardState.unmount(); } catch(_) {}
                const cards = window.__APP_MANIFESTS__?.cards || [];
                const manifest = cards.find(c => c.id === 'video-analysis-search');
                await window.WizardState.mount(manifest, {steps:[{kind:'env_check'},{kind:'form',fields:[{id:'vcs-source'}]},{kind:'finish'}]});
            })()
        """)
        page.wait_for_timeout(800)
        result = page.evaluate("""(async () => {
            // 等页面 DOM 就绪
            await new Promise(r => setTimeout(r, 300));
            const r = await window.ImApp_video_analysis_search.applyWizardSnapshot({source: '/tmp/test.mp4'});
            const sourceVal = document.getElementById('vcs-source')?.value;
            const promptVal = document.getElementById('vas-prompt')?.value;
            const everyNVal = document.getElementById('vas-every-n')?.value;
            return {result: r, sourceVal, promptVal, everyNVal};
        })""")
        print(f"\n=== 7) applyWizardSnapshot (仅 source) ===")
        print(f"  {result}")
        # source_set 应为 true,started 应为 false(缺 prompt + everyN)
        if result['result']['source_set'] and not result['result']['started'] and result['sourceVal'] == '/tmp/test.mp4':
            print(f"  ✅ 部分字段写回 OK,未启动分析")
            ok += 1
        else:
            print(f"  ❌ FAIL")

        browser.close()
        # 分母 = 断言条数。「采样间隔」步骤在 5dcd790 并进 source 步后被删，
        # 这里从 8 降到 7；判定必须跟着用同一个常量，否则全绿时反而红着退。
        total = 7
        print(f"\n=== 总结: {ok}/{total} ===")
        if ok == total:
            print("✅ 全部通过")
            return 0
        return 1

if __name__ == "__main__":
    import sys
    sys.exit(main() or 0)
