#!/usr/bin/env python3
"""回归测试: 「图片处理」卡（image-editor）的**前端**能不能真的发起生成。

为什么要有这个脚本
------------------
2026-11-06 实测踩到的坑：卡片的 ``app.js`` 里写了 ``state.sourceB64``，
而 ``state`` **在这个文件里从来没声明过**（状态对象叫 ``IE``），宿主也
没有同名全局 ⇒ 点击「生成图片」时抛

    [pageerror] state is not defined

⇒ 按钮点了**什么都不会发生**（连 hint 都不弹）。

为什么之前的验收没抓到：``scripts/verify_image_editor.py`` 走的是
**卡片 HTTP API**（``POST /api/image-editor/generate``），**绕过了前端
JS** ⇒ 后端链路全绿，前端却是坏的。

⇒ 这个脚本用 playwright 走**真实浏览器路径**：

1. 打开卡片页（跳过向导）；
2. 选一张图 ⇒ 断言预览出现；
3. 填提示词 + 选「保留原图」；
4. 点「生成图片」；
5. 断言 **没有 pageerror**，且确实发出了 ``POST /api/image-editor/generate``
   且请求体里带 ``strength``。

跑法::

    .venv/bin/python scripts/verify_image_editor_ui.py
    .venv/bin/python scripts/verify_image_editor_ui.py --source /path/to/img.png
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000"
CARD = "image-editor"
DEFAULT_SOURCE = "/home/rockchip/assets/media_generator/runs/022a16e93794/img01.png"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=DEFAULT_SOURCE, help="用作输入图的本地文件")
    ap.add_argument("--keep-open", action="store_true", help="跑完不关浏览器（调试用）")
    args = ap.parse_args()

    src = Path(args.source)
    if not src.is_file():
        print(f"❌ 输入图不存在: {src}")
        return 2

    errors: list[str] = []
    posts: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
        ctx = browser.new_context(viewport={"width": 1440, "height": 1000})
        page = ctx.new_page()
        page.on("pageerror", lambda e: errors.append(f"[pageerror] {e}"))
        page.on("console", lambda m: errors.append(f"[console.error] {m.text}")
                if m.type == "error" else None)

        def on_request(req):
            if req.method == "POST" and f"/api/{CARD}/generate" in req.url:
                try:
                    body = json.loads(req.post_data or "{}")
                except Exception as exc:  # noqa: BLE001
                    posts.append({"_parse_error": str(exc)})
                    return
                if isinstance(body.get("image"), str) and len(body["image"]) > 100:
                    body["image"] = f"<{len(body['image'])} chars>"
                posts.append(body)

        page.on("request", on_request)

        page.goto(f"{BASE}/#{CARD}", wait_until="networkidle")
        page.wait_for_timeout(2500)

        # 页面段必须在（卡位占位符漏了的话这里就没有）
        if page.locator(f"#page-{CARD}").count() == 0:
            print(f"❌ 页面里没有 #page-{CARD} —— 卡位占位符漏了？"
                  f"（跑 scripts/verify_card_slots.py）")
            browser.close()
            return 1

        # 跳过向导（否则卡片页是隐藏的，点不到按钮）
        skip = page.locator("text=跳过向导").first
        if skip.count():
            skip.click()
            page.wait_for_timeout(1500)

        # 选图
        page.set_input_files("#ie-source-file", str(src))
        page.wait_for_timeout(1000)
        preview_shown = page.evaluate(
            "() => !document.getElementById('ie-source-preview-wrap').classList.contains('hidden')"
        )
        if not preview_shown:
            print("❌ 选图后预览没出现")
            browser.close()
            return 1

        page.fill("#ie-prompt", "把这个苹果变成青苹果，放在热带海滩上")
        page.select_option("#ie-strength", "0.55")

        errors.clear()
        page.click("#ie-generate")
        page.wait_for_timeout(4000)

        print(f"请求体: {posts}")
        print(f"前端错误: {errors}")

        ok = True
        if errors:
            print("❌ 前端有 JS 错误 —— 点「生成图片」是坏的")
            ok = False
        if not posts:
            print("❌ 没有发出 POST /api/image-editor/generate —— 按钮没生效")
            ok = False
        else:
            body = posts[0]
            for key in ("prompt", "image", "strength", "steps"):
                if key not in body:
                    print(f"❌ 请求体缺字段 {key}: {body}")
                    ok = False
            if body.get("strength") != 0.55:
                print(f"❌ strength 没回填/没传对: {body.get('strength')}（期望 0.55）")
                ok = False

        if not args.keep_open:
            browser.close()

    print("✅ 图片处理卡前端流程正常" if ok else "❌ 有问题")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
