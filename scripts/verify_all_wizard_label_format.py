"""统一验证:6 个 wizard.json 中所有 textarea 字段都改成 'label 即示例 + 删 placeholder'"""
import asyncio, json
from playwright.async_api import async_playwright

CASES = [
    ("video-detection",          "detect", "vd-prompt"),
    ("video-detection",          "audio",  "vd-audio-prompt"),
    ("video-target-detection",   "detect", "vtd-prompt"),
    ("text-chat",                "prompt", "tc-prompt"),
    ("tts",                      "text",   "tts-text"),
]

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = await browser.new_context(viewport={"width": 1920, "height": 1080})
        page = await ctx.new_page()
        await page.goto("http://localhost:8000/#apps", wait_until="networkidle")

        all_ok = True
        for app_id, step_id, field_id in CASES:
            cfg = await page.evaluate(f"""
async () => {{
  const r = await fetch('/static/apps/{app_id}/wizard.json');
  return await r.json();
}}
""")
            step = next(s for s in cfg["steps"] if s["id"] == step_id)
            field = next(f for f in step["fields"] if f["id"] == field_id)
            label = field.get("label")
            has_ph_key = "placeholder" in field
            ok = (
                isinstance(label, str) and label.startswith("例")
                and not has_ph_key
            )
            mark = "✅" if ok else "❌"
            all_ok = all_ok and ok
            print(f"  {mark} {app_id:<28}/{field_id:<16} = {label!r:<55} (has placeholder key: {has_ph_key})")

        await browser.close()
        print(f"\n{'✅ 全部通过' if all_ok else '❌ 有失败'}")

asyncio.run(main())
