#!/usr/bin/env python3
"""回归测试: 向导的 ``type: "image"`` 字段（拖拽上传一张图）。

背景
----
图生图这类卡片**必须有输入图**。向导原本只能填文本/数字/勾选 —— 视频卡的
「源」是另一套机制（值是**路径**，还要 upload_endpoint + thumbnail_endpoint）。
于是用户被卡在「向导里没法给图 ⇒ 必须先跳过向导、去功能页上传」。

2026-11-06 给宿主向导加了 ``"type": "image"``：一个**拖拽上传区**，
值是 **data URL**，字段声明 ``"transient": true`` ⇒ **不落 localStorage**
（一张图的 base64 几百 KB~几 MB；``localStorage.setItem`` 是整体覆盖，
配额爆了会连**其它字段**一起存不进去）。

本脚本断言：

1. 第 2 步有拖拽区，空态文案正确；
2. 选了图 ⇒ 预览出现、值是 data URL、**「下一步」由灰变亮**（required）；
3. **localStorage 里没有 base64**（values 键只有几十字节）；
4. 完成页摘要显示**文件名**，不是整段 base64；
5. 「完成配置」⇒ 卡片拿到图 / 提示词 / 保留强度，并真的发起了生成。

跑法::

    .venv/bin/python scripts/verify_wizard_image_field.py
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000"
CARD = "image-editor"
DEFAULT_SOURCE = "/home/rockchip/assets/media_generator/runs/022a16e93794/img01.png"


def _dump(page) -> dict:
    return page.evaluate(
        """() => {
          const d = document.querySelector('.wd-wizard-image-drop');
          const box = d && d.closest('[data-field-id]');
          const next = [...document.querySelectorAll('.wd-wizard-foot button')]
            .find(b => (b.textContent || '').includes('下一步'));
          const img = d && d.querySelector('.wd-wizard-image-preview-img');
          const ls = {};
          for (let i = 0; i < localStorage.length; i++) {
            const k = localStorage.key(i);
            if (k.includes('image-editor')) ls[k] = (localStorage.getItem(k) || '').length;
          }
          return {
            dropExists: !!d,
            valueLen: box ? String(box.value || '').length : 0,
            valueHead: box ? String(box.value || '').slice(0, 22) : '',
            previewShown: img
              ? getComputedStyle(img.closest('.wd-wizard-image-preview-wrap')).display !== 'none'
              : false,
            name: d ? ((d.querySelector('.wd-wizard-image-name') || {}).textContent || '') : '',
            nextDisabled: next ? next.disabled : null,
            localStorage: ls,
            transient: Object.keys(window._wd_wizard_transient || {}),
          };
        }"""
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=DEFAULT_SOURCE)
    args = ap.parse_args()
    src = Path(args.source)
    if not src.is_file():
        print(f"❌ 输入图不存在: {src}")
        return 2

    failures: list[str] = []
    posts: list[str] = []

    # ⚠️ **先等卡片空闲**。这张卡一次只能跑一个生成（后端 busy 时返回
    # 409 Conflict）⇒ 上一次测试的生成还没跑完的话，本脚本的「完成配置
    # ⇒ 自动开跑」会撞上 409，冒出一个**假失败**。
    import time
    import urllib.request

    for _ in range(90):
        try:
            with urllib.request.urlopen(f"{BASE}/api/{CARD}/", timeout=5) as r:
                snap = json.loads(r.read().decode("utf-8"))
            if snap.get("state") == "idle" and not snap.get("busy"):
                break
        except Exception:  # noqa: BLE001
            pass
        time.sleep(2)
    else:
        print("⚠️  卡片一直忙（前面有生成在跑），继续（可能撞 409）")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
        ctx = browser.new_context(viewport={"width": 1440, "height": 1000})
        # ⚠️ 清掉两处**跨测试残留**，否则本脚本会假失败：
        #  1) 后端记的 active_app —— 开机时 bootRestore 把它还原成
        #     location.hash ⇒ 上一张卡的向导先挂上再被卸载。
        #  2) localStorage 里的向导状态（completed / step / values）——
        #     上一次跑完本脚本后 completed=1 ⇒ 这次进卡**不弹向导**。
        try:
            ctx.request.delete(f"{BASE}/api/compute/active-app")
        except Exception as e:  # noqa: BLE001
            print(f"  [warn] 清 active_app 失败（忽略）: {e}")
        ctx.add_init_script("try { localStorage.clear(); } catch (e) {}")
        page = ctx.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(f"[pageerror] {e}"))
        page.on("console", lambda m: errors.append(f"[console.error] {m.text}")
                if m.type == "error" else None)
        page.on("request", lambda r: posts.append(r.post_data or "")
                if r.method == "POST" and f"/api/{CARD}/generate" in r.url else None)

        page.goto(f"{BASE}/#{CARD}", wait_until="networkidle")
        page.wait_for_timeout(2500)

        # 第 1 步 → 第 2 步
        page.locator("text=下一步").first.click()
        page.wait_for_timeout(1200)

        before = _dump(page)
        print("上传前:", before)
        if not before["dropExists"]:
            failures.append("第 2 步没有 .wd-wizard-image-drop 拖拽区")
        if before["nextDisabled"] is not True:
            failures.append("没选图时「下一步」应当是灰的（required）")

        # 选图
        page.set_input_files(".wd-wizard-image-file", str(src))
        page.wait_for_timeout(1500)
        after = _dump(page)
        print("上传后:", after)

        if not after["previewShown"]:
            failures.append("选图后预览没出现")
        if not after["valueHead"].startswith("data:image/"):
            failures.append(f"字段值不是 data URL: {after['valueHead']!r}")
        if after["nextDisabled"] is not False:
            failures.append("选图后「下一步」仍应是可点的")
        if not after["name"]:
            failures.append("预览区没显示文件名")

        # base64 不许进 localStorage
        ls_vals = after["localStorage"].get("wdnwebui.wizard.image-editor.values", 0)
        if ls_vals > 4096:
            failures.append(f"localStorage 里塞了 {ls_vals} 字节（baseURL 落盘了？）")

        # 填提示词 + 强度 → 下一步 → 完成配置
        page.evaluate(
            """() => {
              const ta = document.querySelector('.wd-wizard-body textarea');
              if (ta) { ta.value = '把背景换成夕阳下的海滩'; ta.dispatchEvent(new Event('input', {bubbles:true})); }
              for (const s of document.querySelectorAll('.wd-wizard-body select')) {
                const vals = [...s.options].map(o => o.value);
                if (vals.includes('0.35')) { s.value = '0.35'; s.dispatchEvent(new Event('change', {bubbles:true})); }
              }
            }"""
        )
        page.wait_for_timeout(400)
        page.locator("text=下一步").first.click()
        page.wait_for_timeout(1000)

        summary = page.evaluate(
            "() => [...document.querySelectorAll('.wd-wizard-summary-row')].map(r => (r.textContent||'').trim())"
        )
        print("完成页摘要:", summary)
        blob = " ".join(summary)
        if "data:image" in blob:
            failures.append("完成页摘要里出现了整段 base64")
        if "img01" not in blob and src.stem not in blob:
            failures.append(f"完成页摘要里没有文件名: {summary}")

        # 完成配置
        btns = page.locator(".wd-wizard-foot button")
        n = btns.count()
        for i in range(n):
            t = btns.nth(i).inner_text()
            if "完成" in t:
                btns.nth(i).click()
                break
        page.wait_for_timeout(4000)

        state = page.evaluate(
            """() => ({
               prompt: document.getElementById('ie-prompt').value,
               strength: document.getElementById('ie-strength').value,
               srcHead: (document.getElementById('ie-source-preview').getAttribute('src') || '').slice(0, 22),
               meta: document.getElementById('ie-meta').textContent,
             })"""
        )
        print("完成后:", state)
        if state["prompt"] != "把背景换成夕阳下的海滩":
            failures.append(f"提示词没回填: {state['prompt']!r}")
        if state["strength"] != "0.35":
            failures.append(f"保留强度没回填: {state['strength']!r}")
        if not state["srcHead"].startswith("data:image/"):
            failures.append(f"卡片页没拿到向导选的图: {state['srcHead']!r}")
        if not posts:
            failures.append("没有发起 POST /api/image-editor/generate —— 向导完成后没自动开跑")
        if errors:
            failures.append(f"前端有 JS 错误: {errors[:3]}")

        browser.close()

    if failures:
        print("\n❌ 失败:")
        for f in failures:
            print("   - " + f)
        return 1
    print("\n✅ 向导 image 字段：拖拽上传 → 不落盘 → 回填 → 自动开跑 全链路正常")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
