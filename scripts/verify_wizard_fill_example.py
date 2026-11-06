#!/usr/bin/env python3
"""回归:向导里提示词字段的「一键填入」按钮。

约定(见 verify_all_wizard_label_format.py):提示词类 textarea 字段的
label 就是「例:…」示例本身。例子后面跟一个「一键填入」按钮,点一下把
示例写进下面的输入框 —— 省得用户照着手抄一遍。

本脚本守住三条:
  F1 提示词字段的 label 旁边有「一键填入」按钮
  F2 点它 → 输入框的值 = 示例(去掉「例:」前缀)
  F3 点它 → 值**立刻**被采集进 state.values(不等用户手动改一下)

用法:.venv/bin/python scripts/verify_wizard_fill_example.py
"""
from __future__ import annotations

import asyncio
import sys

from playwright.async_api import async_playwright

BASE = "http://127.0.0.1:8000"
NS = "wd-wizard-"

CASES = [
    ("video-detection",        "vd-prompt"),
    ("video-detection",        "vd-audio-prompt"),
    ("video-target-detection", "vtd-prompt"),
    ("text-chat",              "tc-prompt"),
    ("tts",                    "tts-text"),
]

FAILED: list[str] = []
PASSED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"  ✓ {name}" + (f"  ({detail})" if detail else ""))
    else:
        FAILED.append(name)
        print(f"  ✗ {name}" + (f"  ({detail})" if detail else ""))


async def main() -> int:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--mute-audio"])
        ctx = await browser.new_context(viewport={"width": 1500, "height": 1000})
        page = await ctx.new_page()
        page.on("pageerror", lambda e: print(f"  [pageerror] {e}"))

        for app_id, field_id in CASES:
            print(f"\n=== {app_id} / {field_id} ===")
            # 清掉上次痕迹 → 进向导模式 → 一步步走到该字段
            await page.goto(BASE, wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(800)
            # 从 wizard.json 取该字段的 label(=示例文本)
            cfg = await page.evaluate(f"""
async () => {{
  const r = await fetch('/static/apps/{app_id}/wizard.json');
  return await r.json();
}}
""")
            field = None
            for s in cfg["steps"]:
                for f in s.get("fields", []):
                    if f["id"] == field_id:
                        field = f
            if field is None:
                check(f"{app_id}/{field_id} 在 wizard.json 里有", False)
                continue
            example = (field.get("label") or "").strip()
            want = example[len("例:"):] if example.startswith("例:") else example

            await page.evaluate("""(id) => {
                const base = 'wdnwebui.wizard.' + id + '.';
                for (const k of Object.keys(localStorage))
                    if (k.startsWith(base)) localStorage.removeItem(k);
            }""", app_id)
            await page.evaluate("(id) => { window.location.hash = '#' + id; }", app_id)
            await page.wait_for_timeout(600)
            await page.evaluate("(id) => { window.enterWizardMode(id); }", app_id)
            try:
                await page.wait_for_selector(f"#{NS}root-{app_id}:not(.hidden)",
                                             timeout=20000)
            except Exception as exc:  # noqa: BLE001
                check(f"{app_id} 向导能打开", False, str(exc)[:80])
                continue
            await page.wait_for_timeout(500)

            # 一步步走到目标字段(按钮可能因 env 步未就绪而禁用 → 用 API 走)
            found = False
            for _ in range(8):
                has = await page.evaluate(
                    "(sel) => !!document.querySelector(sel)",
                    f"#{NS}root-{app_id} [data-field-id='{field_id}']")
                if has:
                    found = True
                    break
                await page.evaluate("() => window.WizardState.stepForward()")
                await page.wait_for_timeout(500)
            if not found:
                check(f"{app_id}/{field_id} 走到该步骤", False)
                continue
            check(f"{app_id}/{field_id} 走到该步骤", True)

            info = await page.evaluate("""(args) => {
                const root = document.querySelector(args.root);
                const el = root.querySelector(`[data-field-id='${args.fid}']`);
                const head = el.closest('.' + args.ns + 'field').querySelector(
                    '.' + args.ns + 'field-head');
                const btn = head && head.querySelector('.' + args.ns + 'fill-example');
                return {
                    hasBtn: !!btn,
                    btnText: btn ? btn.textContent.trim() : '',
                    labelText: head
                        ? head.querySelector('.' + args.ns + 'field-label').textContent.trim()
                        : '',
                    value: el.value,
                };
            }""", {"root": f"#{NS}root-{app_id}", "fid": field_id, "ns": NS})
            check(f"F1 有「一键填入」按钮", info["hasBtn"] and info["btnText"] == "一键填入",
                  f"btn={info['btnText']!r}")
            check("F1 按钮紧跟在例子后面",
                  info["labelText"] == example, f"label={info['labelText']!r}")

            await page.click(f"#{NS}root-{app_id} .{NS}fill-example")
            await page.wait_for_timeout(400)
            after = await page.evaluate("""(args) => {
                const root = document.querySelector(args.root);
                const el = root.querySelector(`[data-field-id='${args.fid}']`);
                const vals = window._wd_wizard_values || {};
                return { value: el.value, captured: vals[args.fid] };
            }""", {"root": f"#{NS}root-{app_id}", "fid": field_id})
            check("F2 输入框被填成示例", after["value"] == want,
                  f"value={after['value']!r}")
            check("F3 值已采集进 state.values", after["captured"] == want,
                  f"captured={after['captured']!r}")

        await browser.close()

    print("\n" + "=" * 60)
    print(f"通过 {PASSED} / {PASSED + len(FAILED)}")
    if FAILED:
        print(f"❌ 失败: {FAILED}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()) or 0)
