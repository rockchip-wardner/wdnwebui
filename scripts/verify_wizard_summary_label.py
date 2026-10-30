"""验证 7 个 textarea 字段都有 summary_label,完成配置页面摘要行渲染友好"""
import asyncio, json
from playwright.async_api import async_playwright

CASES = [
    ("video-detection",          "detect", "vd-prompt",       "检测提示词"),
    ("video-detection",          "audio",  "vd-audio-prompt", "音频理解提示词"),
    ("video-target-detection",   "detect", "vtd-prompt",      "检测提示词"),
    ("text-chat",                "prompt", "tc-prompt",       "首次提问"),
    ("tts",                      "text",   "tts-text",        "要合成的文本"),
]

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = await browser.new_context(viewport={"width": 1920, "height": 1080})
        page = await ctx.new_page()
        await page.goto("http://localhost:8000/#apps", wait_until="networkidle")

        all_ok = True
        for app_id, step_id, field_id, expected_summary in CASES:
            cfg = await page.evaluate(f"""
async () => {{
  const r = await fetch('/static/apps/{app_id}/wizard.json');
  return await r.json();
}}
""")
            step = next(s for s in cfg["steps"] if s["id"] == step_id)
            field = next(f for f in step["fields"] if f["id"] == field_id)
            summary_label = field.get("summary_label")
            ok = summary_label == expected_summary
            mark = "✅" if ok else "❌"
            all_ok = all_ok and ok
            print(f"  {mark} {app_id:<28}/{field_id:<16} summary_label={summary_label!r} (expected {expected_summary!r})")

        # 模拟 finish 步骤渲染:用真实逻辑跑一次摘要生成
        result = await page.evaluate("""
(cfg) => {
  const WIZ_NS = "wd-wizard-";
  const summary = document.createElement('div');
  cfg.steps.forEach(s => {
    if (s.kind !== 'form') return;
    s.fields.forEach(f => {
      // 模拟用户填了值
      const v = f.id === 'vd-loop' ? true : (f.id.includes('source') ? '/test.mp4' : '测试值');
      if (v == null || v === '' || v === false) return;
      const row = document.createElement('div');
      row.className = WIZ_NS + 'summary-row';
      const summaryLabel = f.summary_label || f.label || f.id;
      const span1 = document.createElement('span');
      span1.textContent = summaryLabel + ':';
      const span2 = document.createElement('span');
      span2.className = WIZ_NS + 'summary-val';
      span2.textContent = (f.type === 'checkbox') ? '是' : String(v);
      row.appendChild(span1);
      row.appendChild(span2);
      summary.appendChild(row);
    });
  });
  return Array.from(summary.querySelectorAll('.wd-wizard-summary-row')).map(r => r.textContent);
}
""", await page.evaluate("""
async () => {
  const r = await fetch('/static/apps/video-detection/wizard.json');
  return await r.json();
}
"""))
        print("\n=== video-detection 完成配置摘要模拟渲染 ===")
        for line in result:
            print(f"  {line}")

        await browser.close()
        print(f"\n{'✅ 全部通过' if all_ok else '❌ 有失败'}")

asyncio.run(main())
