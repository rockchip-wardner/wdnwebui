"""方案 A 验证:title + desc 全宽左对齐垂直堆叠"""
import asyncio
from playwright.async_api import async_playwright

EXPECTED = {
    "video-detection":              "对实时视频流逐帧理解，输出画面与声音的文字描述。",
    "video-target-detection":       "对实时视频流持续检测指定目标是否出现，命中时自动告警并描述。",
    "video-target-localization":    "输入任意文字描述，从图像中圈出对应目标所在区域。",
    "tts":                          "把中文文本转成自然语音。",
    "video-analysis-search":        "上传视频做逐帧分析与摘要，并支持自然语言检索片段。",
    "text-chat":                    "与通用大模型多轮对话，支持上下文连续追问。",
}

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = await browser.new_context(viewport={"width": 1920, "height": 1080})
        page = await ctx.new_page()
        await page.goto("http://localhost:8000/#apps", wait_until="networkidle")
        await page.wait_for_selector(".app-card", timeout=5000)
        await page.wait_for_timeout(300)

        # 1) 6 个 desc 都正确渲染
        cards = await page.evaluate("""
() => Array.from(document.querySelectorAll('.app-card')).map(a => ({
  id: a.dataset.page,
  title: a.querySelector('.app-card-title')?.textContent,
  desc: a.querySelector('.app-card-desc')?.textContent,
}))
""")
        print(f"可见卡片: {len(cards)}")
        ok = True
        for c in cards:
            mark = "✅" if EXPECTED.get(c["id"]) == c["desc"] else "❌"
            print(f"  {mark} {c['id']:<32} | {c['title']}")
            if EXPECTED.get(c["id"]) == c["desc"]:
                print(f"        {c['desc']}")
            else:
                print(f"        got: {c['desc']!r}")
                print(f"        exp: {EXPECTED.get(c['id'])!r}")
            ok = ok and (EXPECTED.get(c["id"]) == c["desc"])

        # 2) 布局:title 和 desc 全宽、垂直堆叠
        layout = await page.evaluate("""
() => {
  const card = document.querySelector('.app-card');
  const title = card.querySelector('.app-card-title');
  const desc = card.querySelector('.app-card-desc');
  const tcs = getComputedStyle(title);
  const dcs = getComputedStyle(desc);
  const tb = title.getBoundingClientRect();
  const db = desc.getBoundingClientRect();
  return {
    title_textAlign: tcs.textAlign,
    title_fontSize: tcs.fontSize,
    desc_textAlign: dcs.textAlign,
    desc_fontSize: dcs.fontSize,
    desc_color: dcs.color,
    title_left: Math.round(tb.left),
    desc_left: Math.round(db.left),
    title_top: Math.round(tb.top),
    desc_top: Math.round(db.top),
    title_right: Math.round(tb.right),
    desc_right: Math.round(db.right),
    card_right: Math.round(card.getBoundingClientRect().right),
    vertical_stack: db.top > tb.bottom - 5,
  };
}
""")
        print(f"\n布局:")
        for k, v in layout.items():
            print(f"  {k}: {v}")

        # 3) hover 验证
        first = page.locator(".app-card").first
        await first.hover()
        await page.wait_for_timeout(300)
        hover = await page.evaluate("""
() => {
  const t = document.querySelector('.app-card:hover .app-card-title');
  const d = document.querySelector('.app-card:hover .app-card-desc');
  return { title_color: getComputedStyle(t).color, desc_color: getComputedStyle(d).color };
}
""")
        print(f"\nhover 颜色: {hover}")

        await browser.close()
        print("\n" + ("✅ 全部通过" if ok else "❌ 有失败"))

asyncio.run(main())
