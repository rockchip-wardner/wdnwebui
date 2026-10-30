#!/usr/bin/env python3
"""验证 监控反查 wizard mount + stepForward + applyWizardSnapshot + **向导跑完自动播放**。

不依赖真模型启动（直接 mount wizard，绕过 env）。

向导步骤（与 wizard.json 一致）：env → source → search → finish。

第 5 项是**真实 UI 全流程**（向导里填源 + 检索问题 → 点「完成配置」），
断言的是本次改动的核心：**向导跑完画面就在播**（后端 /source 快照
state=playing + 画布真有帧 + DemoGuard 认得「预览在跑」）。

第 6 项是**契约层**：直接调 applyWizardSnapshot，断言返回 source_opened
且预览真的开着。

素材：默认用 /userdata/assets/uploads/rk-15.mp4（12s），可用环境变量
VR_TEST_VIDEO 覆盖；找不到就用卡片自带的 static/preview.mp4。
"""
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

REPO = Path(__file__).resolve().parent.parent

CANDIDATES = [
    os.environ.get("VR_TEST_VIDEO", ""),
    "/userdata/assets/uploads/rk-15.mp4",
    "/userdata/assets/uploads/rk-1.mp4",
]


def pick_video() -> str:
    for c in CANDIDATES:
        if c and Path(c).is_file():
            return c
    # 兜底：卡片自带的演示素材
    fallback = (REPO / ".venv/src/wdnapps-video-analyzer/src/wdnapps_video_analyzer"
                / "cards/video_retrieval/static/preview.mp4")
    if fallback.is_file():
        return str(fallback)
    raise SystemExit("找不到测试视频（设置 VR_TEST_VIDEO 或放一个 mp4 到 uploads）")


# 画布判据：统计非黑像素 + 画布像素尺寸（2×2 = 页面还被向导藏着，
# 画面没真正落到页面上 —— 向导挂载时 page section 是 hidden 的）。
CANVAS_PROBE = """() => {
    const c = document.getElementById('vr-canvas');
    if (!c) return {err: 'no canvas'};
    const ctx = c.getContext('2d');
    const d = ctx.getImageData(0, 0, c.width, c.height).data;
    let nonblack = 0;
    for (let i = 0; i < d.length; i += 4) {
        if (d[i] + d[i+1] + d[i+2] > 12) nonblack++;
    }
    return {w: c.width, h: c.height, nonblack,
            total: d.length / 4,
            parentW: c.parentElement ? c.parentElement.clientWidth : -1};
}"""

# 后端预览快照（/source = 共享 VideoSession 的预览那一路）
SOURCE_SNAPSHOT = """async () => {
    const r = await fetch('/api/video-retrieval/source');
    return await r.json();
}"""


def main():
    video = pick_video()
    print(f"测试素材: {video}")
    ok = 0
    total = 6

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
        ctx = browser.new_context()
        page = ctx.new_page()
        page.on("pageerror", lambda e: print(f"  [pageerror] {e}"))

        page.goto("http://127.0.0.1:8000/#video-retrieval", wait_until="domcontentloaded", timeout=15000)
        page.wait_for_selector(".wd-wizard-section", timeout=8000)
        page.wait_for_timeout(500)

        # 测试 1:基本 mount
        basic = page.evaluate("""() => ({
            sections: document.querySelectorAll('.wd-wizard-section').length,
            dots: document.querySelectorAll('.wd-wizard-step-dot').length,
            title: document.querySelector('.wd-wizard-title')?.textContent,
            ImApp: typeof window.ImApp_video_retrieval?.applyWizardSnapshot,
        })""")
        print("=== 1) wizard mount ===")
        print(f"  {basic}")
        if basic['sections'] == 1 and basic['dots'] == 4 and basic['ImApp'] == 'function':
            print("  ✅ wizard mount OK")
            ok += 1
        else:
            print("  ❌ FAIL")

        # 测试 2:source 步骤（视频源；无判据 / 无分析字段）
        page.evaluate("window.WizardState.stepForward()")
        page.wait_for_timeout(400)
        s2 = page.evaluate("""() => ({
            stepTitle: document.querySelector('.wd-wizard-step-title')?.textContent,
            hasSourceInput: !!document.querySelector('[data-field-id="vr-source"]'),
            hasLoopInput: !!document.querySelector('[data-field-id="vr-loop"]'),
            hasBrowseBtn: !!document.querySelector('.wd-wizard-browse-btn'),
            hasJudgeMode: !!document.querySelector('[data-field-id="vr-judge-mode"]'),
            hasAnalyzeMode: !!document.querySelector('[data-field-id="vr-mode-analyze"]'),
        })""")
        print("\n=== 2) source step ===")
        print(f"  {s2}")
        if (s2['hasSourceInput'] and s2['hasLoopInput'] and s2['hasBrowseBtn']
                and s2['stepTitle'] == '视频源'
                and not s2['hasJudgeMode'] and not s2['hasAnalyzeMode']):
            print("  ✅ source 步骤 OK（无判据 / 无分析字段）")
            ok += 1
        else:
            print("  ❌ FAIL")

        # 测试 3:search 步骤（检索问题）
        page.evaluate("window.WizardState.stepForward()")
        page.wait_for_timeout(300)
        s3 = page.evaluate("""() => ({
            stepTitle: document.querySelector('.wd-wizard-step-title')?.textContent,
            hasQueryGroups: !!document.querySelector('[data-field-id="vr-query-groups"] .qg-text'),
            hasStalePromptField: !!document.querySelector('[data-field-id="vr-prompt"]'),
        })""")
        print("\n=== 3) search step ===")
        print(f"  {s3}")
        if s3['hasQueryGroups'] and s3['stepTitle'] == '检索问题' and not s3['hasStalePromptField']:
            print("  ✅ search 步骤 OK")
            ok += 1
        else:
            print("  ❌ FAIL")

        # 测试 4:finish 步骤
        page.evaluate("window.WizardState.stepForward()")
        page.wait_for_timeout(300)
        s4 = page.evaluate("""() => ({
            stepTitle: document.querySelector('.wd-wizard-step-title')?.textContent,
            hasFinishBtn: !!document.getElementById('wd-wizard-finish-btn'),
            hasFinishErrorHidden: !!document.getElementById('wd-wizard-finish-error'),
            hasChunkField: !!document.querySelector('[data-field-id="vr-chunk-seconds"]'),
        })""")
        print("\n=== 4) finish step ===")
        print(f"  {s4}")
        if (s4['hasFinishBtn'] and s4['stepTitle'] == '完成配置'
                and not s4['hasChunkField']):
            print("  ✅ finish 步骤 OK（无切片参数字段）")
            ok += 1
        else:
            print("  ❌ FAIL")

        # ------------------------------------------------------------------ #
        # 测试 5:真实 UI 全流程 —— 向导跑完画面就在播（本次改动的核心）
        # ------------------------------------------------------------------ #
        print("\n=== 5) 向导全流程 → 自动播放视频 ===")
        # 回到 source 步填值（前面 stepForward 已经走到 finish，这里重挂一次真向导）
        page.evaluate("""async () => {
            try { window.WizardState.unmount(); } catch(_) {}
            const manifest = (window.__APP_MANIFESTS__?.cards || [])
                .find(c => c.id === 'video-retrieval');
            await window.WizardRegistry.tryMount(manifest, {force: true});
        }""")
        page.wait_for_timeout(800)
        # env → source
        page.evaluate("window.WizardState.stepForward()")
        page.wait_for_timeout(400)
        page.evaluate("""(src) => {
            const set = (id, v) => {
                const el = document.querySelector(`[data-field-id="${id}"]`);
                const input = el?.matches?.('input[type=checkbox]') ? el
                    : el?.querySelector?.('input[type=checkbox]') || el;
                if (!input) return;
                if (input.type === 'checkbox') input.checked = v;
                else input.value = v;
                input.dispatchEvent(new Event('input', {bubbles: true}));
                input.dispatchEvent(new Event('change', {bubbles: true}));
            };
            set('vr-source', src);
            set('vr-loop', true);
        }""", video)
        page.wait_for_timeout(300)
        # source → search（检索问题在这一步才渲染出来，必须走到这一步再填）
        page.evaluate("window.WizardState.stepForward()")
        page.wait_for_timeout(400)
        filled = page.evaluate("""() => {
            const el = document.querySelector('[data-field-id="vr-query-groups"] .qg-text');
            if (!el) return false;
            el.value = '视频里有没有人';
            el.dispatchEvent(new Event('input', {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
            return el.value.length > 0;
        }""")
        print(f"  检索问题已填: {filled}")
        page.wait_for_timeout(200)
        # search → finish
        page.evaluate("window.WizardState.stepForward()")
        page.wait_for_timeout(300)
        at_finish = page.evaluate(
            "() => document.querySelector('.wd-wizard-step-title')?.textContent")
        print(f"  当前步骤: {at_finish}")
        page.evaluate("document.getElementById('wd-wizard-finish-btn')?.click()")
        # 等向导跑完（预览打开 + 检索启动）
        page.wait_for_timeout(4000)

        s5 = page.evaluate("""async () => {
            const r = await fetch('/api/video-retrieval/source');
            const snap = await r.json();
            const c = document.getElementById('vr-canvas');
            const ctx = c?.getContext('2d');
            let nonblack = 0;
            if (c && ctx) {
                const d = ctx.getImageData(0, 0, c.width, c.height).data;
                for (let i = 0; i < d.length; i += 4) {
                    if (d[i] + d[i+1] + d[i+2] > 12) nonblack++;
                }
            }
            return {
                srcState: snap.state,
                srcSource: snap.source,
                srcLoop: snap.loop,
                canvasW: c?.width, canvasH: c?.height,
                nonblack,
                guardActive: window.DemoGuard?.isActive?.('video-retrieval'),
                wizardGone: !document.querySelector('.wd-wizard-section'),
                pageVisible: !document.getElementById('page-video-retrieval')
                                 ?.classList.contains('hidden'),
                errText: document.getElementById('vr-error-text')?.textContent || '',
            };
        }""")
        print(f"  {s5}")
        if (s5['srcState'] == 'playing' and s5['srcSource'] == video
                and s5['nonblack'] > 0 and s5['canvasW'] > 2
                and s5['guardActive'] is True
                and s5['wizardGone'] and s5['pageVisible']):
            print("  ✅ 向导跑完：视频在播 + 画布有帧 + DemoGuard 认得在跑")
            ok += 1
        else:
            print("  ❌ FAIL")

        # ------------------------------------------------------------------ #
        # 测试 6:契约层 —— applyWizardSnapshot 直接调用
        # ------------------------------------------------------------------ #
        print("\n=== 6) applyWizardSnapshot 契约 ===")
        # 先关掉上一轮预览，保证这一项测的是"新开"
        page.evaluate("""async () => {
            await fetch('/api/video-retrieval/source/close', {method: 'POST'});
        }""")
        page.wait_for_timeout(500)
        r6 = page.evaluate("""async (src) => {
            try {
                const r = await window.ImApp_video_retrieval.applyWizardSnapshot(
                    {source: src, loop: true});
                return {ok: true, r};
            } catch (e) {
                return {ok: false, err: String(e && (e.message || e))};
            }
        }""", video)
        page.wait_for_timeout(1500)
        s6 = page.evaluate(CANVAS_PROBE)
        print(f"  返回: {r6}")
        print(f"  画布: {s6}")
        snap6 = page.evaluate(SOURCE_SNAPSHOT)
        print(f"  后端: state={snap6.get('state')} source={snap6.get('source')}")
        if (snap6.get('state') == 'playing' and snap6.get('source') == video
                and s6.get('nonblack', 0) > 0):
            print("  ✅ applyWizardSnapshot 打开视频源 OK")
            ok += 1
        else:
            print("  ❌ FAIL")

        # 收尾：关掉预览，别把解码线程留在后台
        page.evaluate("""async () => {
            await fetch('/api/video-retrieval/source/close', {method: 'POST'});
        }""")
        browser.close()

    print(f"\n=== 总结: {ok}/{total} ===")
    if ok == total:
        print("✅ 全部通过")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main() or 0)
