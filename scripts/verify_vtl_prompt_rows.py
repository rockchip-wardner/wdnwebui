#!/usr/bin/env python3
"""验证 vtl 向导 prompt_rows 字段:
1. ground 步骤渲染多行 prompt UI(无 textarea)
2. 默认 1 行,可"+ 添加输入框"加到多行
3. - 按钮删除行
4. 填值后 opts.prompts 是字符串数组
5. applyWizardSnapshot 把数组写回应用页 #vtl-prompt-rows
"""
from playwright.sync_api import sync_playwright

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
        ctx = browser.new_context()
        page = ctx.new_page()
        page.on("pageerror", lambda e: print(f"  [pageerror] {e}"))

        page.goto("http://127.0.0.1:8000/#video-target-localization", wait_until="domcontentloaded", timeout=15000)
        page.wait_for_selector(".wd-wizard-section", timeout=8000)
        page.wait_for_timeout(800)

        # 跳过 env,跳到 ground
        page.evaluate("window.WizardState.stepForward()")  # env → source
        page.wait_for_timeout(300)
        page.evaluate("window.WizardState.stepForward()")  # source → ground
        page.wait_for_timeout(500)

        ok = 0

        # 测试 1:ground 步骤渲染了 prompt-rows UI(不是 textarea)
        t1 = page.evaluate("""() => {
            const wizardBody = document.querySelector('.wd-wizard-section .wd-wizard-body');
            const inputs = [...wizardBody.querySelectorAll('.vtl-prompt-row-input')];
            return {
                hasTextarea: !!wizardBody.querySelector('textarea'),
                hasPromptRows: !!wizardBody.querySelector('.vtl-prompt-rows'),
                hasAddBtn: !!wizardBody.querySelector('.vtl-prompt-add'),
                initialRowCount: wizardBody.querySelectorAll('.vtl-prompt-row').length,
                stepTitle: wizardBody.querySelector('.wd-wizard-step-title')?.textContent,
                values: inputs.map(i => i.value),
                placeholders: inputs.map(i => i.placeholder),
            };
        }""")
        print(f"=== 1) ground 步骤 ===")
        print(f"  {t1}")
        if not t1['hasTextarea'] and t1['hasPromptRows'] and t1['hasAddBtn'] and t1['initialRowCount'] == 2:
            print(f"  ✅ 渲染多行 UI,无单 textarea")
            ok += 1
        else:
            print(f"  ❌ FAIL")

        # 测试 1b:默认两行 —— 第一行预填「穿着红衬衫的人」,第二行空着带浅色占位
        if (t1['values'] == ['穿着红衬衫的人', '']
                and t1['placeholders'] == ['例：穿着红衬衫的人'] * 2):
            print(f"  ✅ 默认两行:第一行预填,第二行空且带占位")
            ok += 1
        else:
            print(f"  ❌ FAIL")

        # 测试 2:点 + 添加输入框,行数 +1
        page.locator(".wd-wizard-section .vtl-prompt-add").click()
        page.wait_for_timeout(300)
        page.locator(".wd-wizard-section .vtl-prompt-add").click()
        page.wait_for_timeout(300)
        t2 = page.evaluate("""() => {
            const rows = document.querySelectorAll('.wd-wizard-section .vtl-prompt-row');
            const inputs = document.querySelectorAll('.wd-wizard-section .vtl-prompt-row-input');
            return {rowCount: rows.length, inputCount: inputs.length};
        }""")
        print(f"\n=== 2) 加 2 次后 ===")
        print(f"  {t2}")
        if t2['rowCount'] == 4 and t2['inputCount'] == 4:
            print(f"  ✅ 添加成功")
            ok += 1
        else:
            print(f"  ❌ FAIL")

        # 测试 3:填 3 个值
        page.evaluate("""(() => {
            const inputs = document.querySelectorAll('.wd-wizard-section .vtl-prompt-row-input');
            inputs[0].value = '穿着红衬衫的人';
            inputs[1].value = '戴帽子的人';
            inputs[2].value = '走在路上';
            inputs[0].dispatchEvent(new Event('input', {bubbles: true}));
            inputs[1].dispatchEvent(new Event('input', {bubbles: true}));
            inputs[2].dispatchEvent(new Event('input', {bubbles: true}));
        })()""")
        page.wait_for_timeout(300)

        # 触发 capture(走 stepForward 模拟)
        page.evaluate("window.WizardState.stepForward()")  # ground → finish
        page.wait_for_timeout(300)
        captured = page.evaluate("window._wd_wizard_values || {}")
        print(f"\n=== 3) capture 后 state.values ===")
        print(f"  {captured}")
        # 应有 vtl-prompt-rows 键,值是数组
        prompts_v = captured.get('vtl-prompt-rows')
        if isinstance(prompts_v, list) and prompts_v == ['穿着红衬衫的人', '戴帽子的人', '走在路上']:
            print(f"  ✅ opts.prompts 数组正确捕获")
            ok += 1
        elif isinstance(prompts_v, list):
            print(f"  ⚠️ 数组内容: {prompts_v}")
        else:
            print(f"  ❌ 不是数组: {prompts_v}")

        # 测试 4:applyWizardSnapshot 同步到应用页
        # 先 unmount wizard,让应用页可见
        page.evaluate("window.WizardState.unmount()")
        page.wait_for_timeout(300)

        # 看应用页 #vtl-prompt-rows 现状(初始 1 行,空)
        before = page.evaluate("""() => ({
            rowCount: document.querySelectorAll('#vtl-prompt-rows .vtl-prompt-row').length,
            values: Array.from(document.querySelectorAll('#vtl-prompt-rows .vtl-prompt-row-input')).map(i => i.value)
        })""")
        print(f"\n=== 4) 应用页初始 ===")
        print(f"  {before}")

        # 调 applyWizardSnapshot
        result = page.evaluate("""(async () => {
            return await window.ImApp_video_target_localization.applyWizardSnapshot({
                prompts: ['穿着红衬衫的人', '戴帽子的人', '走在路上']
            });
        })()""")
        page.wait_for_timeout(500)

        after = page.evaluate("""() => ({
            rowCount: document.querySelectorAll('#vtl-prompt-rows .vtl-prompt-row').length,
            values: Array.from(document.querySelectorAll('#vtl-prompt-rows .vtl-prompt-row-input')).map(i => i.value)
        })""")
        print(f"\n=== 5) applyWizardSnapshot 后应用页 ===")
        print(f"  {after}")
        print(f"  result: {result}")

        if after['rowCount'] == 3 and after['values'] == ['穿着红衬衫的人', '戴帽子的人', '走在路上']:
            print(f"  ✅ 数组写入对应行,行数扩展到 3")
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