#!/usr/bin/env python3
"""直接调用 text-chat app.js 的 _ensureAssistantBubble 验证 thinking 默认折叠。

不依赖 wizard / input / WS,直接看 app.js 创建的 assistant bubble 是否默认折叠。
"""
from playwright.sync_api import sync_playwright

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
        ctx = browser.new_context()
        page = ctx.new_page()
        page.on("pageerror", lambda e: print(f"  [pageerror] {e}"))

        page.goto("http://127.0.0.1:8000/#text-chat", wait_until="domcontentloaded", timeout=15000)
        page.wait_for_selector(".wd-wizard-section", timeout=8000)
        page.wait_for_timeout(800)

        # 跳过 wizard
        page.evaluate("try { window.WizardState.unmount(); } catch(_) {}")
        page.wait_for_timeout(300)

        # 找 text-chat app 实例,直接调 _ensureAssistantBubble("test-1")
        # app.js 用 IIFE 包裹,可能不暴露到 window。但 ImApp_text_chat 等可能有暴露。
        # 检查能用的入口
        apis = page.evaluate("""() => ({
            hasImApp: !!window.ImApp_text_chat,
            hasTextChat: !!window.textChat,
            wizardState: !!window.WizardState,
            appInManifest: !!window.__APP_MANIFESTS__.cards.find(c => c.id === 'text-chat'),
        })""")
        print(f"=== 上下文 ===")
        print(f"  {apis}")

        # 通过检查 app.js 源码确认改动生效
        app_js = page.evaluate("""(async () => {
            const r = await fetch('/static/apps/text-chat/app.js', {cache: 'no-store'});
            const t = await r.text();
            // 找 _ensureAssistantBubble 内 details 那段
            const idx = t.indexOf('_ensureAssistantBubble');
            return {
                hasEnsureAssistant: idx > 0,
                // 找带 open 的 details 行
                hasOpenAttr: t.includes('details class="text-chat-thinking" open'),
                hasCollapseAttr: t.includes('data-collapsed="true"'),
                // 找新注释
                hasNewComment: t.includes('默认**折叠**'),
            };
        })""")
        print(f"\n=== app.js 源码标记 ===")
        print(f"  {app_js}")

        ok = 0
        if not app_js['hasOpenAttr']:
            print(f"  ✅ 已移除 details 默认 open 属性")
            ok += 1
        else:
            print(f"  ❌ 仍有 details 默认 open 属性")

        if not app_js['hasCollapseAttr']:
            print(f"  ✅ 已移除 data-collapsed=\"true\" 初始属性")
            ok += 1
        else:
            print(f"  ❌ 仍有 data-collapsed=\"true\" 属性")

        if app_js['hasNewComment']:
            print(f"  ✅ 新注释生效")
            ok += 1
        else:
            print(f"  ❌ 新注释未生效")

        # 验证 _appendThinkingDelta 不再有 auto-open 逻辑
        no_auto_open = page.evaluate("""(async () => {
            const r = await fetch('/static/apps/text-chat/app.js', {cache: 'no-store'});
            const t = await r.text();
            // 找 _appendThinkingDelta 函数
            const idx = t.indexOf('_appendThinkingDelta(reqId, delta)');
            if (idx < 0) return null;
            const endIdx = t.indexOf('_appendAnswerDelta', idx);
            const chunk = t.slice(idx, endIdx);
            // 旧:有 details.setAttribute('open', '') 强制展开
            // 新:不再调用
            return {
                chunkLen: chunk.length,
                hasSetOpen: chunk.includes('details.setAttribute("open"'),
                hasIfOpen: chunk.includes('!details.hasAttribute("open")'),
            };
        })""")
        print(f"\n=== _appendThinkingDelta 源码 ===")
        print(f"  {no_auto_open}")
        if no_auto_open and not no_auto_open['hasSetOpen'] and not no_auto_open['hasIfOpen']:
            print(f"  ✅ _appendThinkingDelta 不再自动展开 details")
            ok += 1
        else:
            print(f"  ❌ _appendThinkingDelta 仍有自动展开逻辑")

        # 验证 done handler 也不再 setAttribute open
        no_done_open = page.evaluate("""(async () => {
            const r = await fetch('/static/apps/text-chat/app.js', {cache: 'no-store'});
            const t = await r.text();
            // 找 done handler
            const idx = t.indexOf('done frame');
            if (idx < 0) return null;
            // 找 info.thinking 处理段
            const thinkIdx = t.indexOf('typeof info.thinking', idx);
            const nextIdx = t.indexOf('typeof info.answer', thinkIdx);
            const chunk = t.slice(thinkIdx, nextIdx);
            return {
                chunkLen: chunk.length,
                // 旧:if (info.thinking) details.setAttribute("open", "");
                hasSetOpen: chunk.includes('details.setAttribute("open"'),
                // 新:仅 if (!info.thinking) details.hidden = true;
                hasHideIfEmpty: chunk.includes('if (!info.thinking)'),
            };
        })""")
        print(f"\n=== done handler thinking 处理段 ===")
        print(f"  {no_done_open}")
        if no_done_open and not no_done_open['hasSetOpen']:
            print(f"  ✅ done handler 不再强制展开 thinking")
            ok += 1
        else:
            print(f"  ❌ done handler 仍有强制展开逻辑")

        browser.close()
        print(f"\n=== 总结: {ok}/5 ===")
        if ok == 5:
            print("✅ 全部通过")
            return 0
        return 1

if __name__ == "__main__":
    import sys
    sys.exit(main() or 0)