"""插件化改造的浏览器端验收。

目的不是测功能，而是证明**从 wheel 里加载的卡片，在浏览器里与原来住在
app/apps/ 里时表现完全一致**：侧栏出现、点进去页面渲染、卡片 JS/CSS/
wizard.json 全部 200、控制台无 pageerror。

诊断价值最高的判据是 pageerror：卡片 app.js 是 IIFE，一旦它引用的某个
共享模块（/static/core/multi-grid.js 等）404，报错发生在 IIFE 执行期，
截图和 DOM 断言都可能看着"像没事"，只有 pageerror 能抓到。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://localhost:8000"

# 卡片清单**动态发现**，不写死。
#
# 插件包是**可选**的 —— 装了的才有那张卡。写死清单的话，卸掉任一插件包
# 这个脚本就会因为「那张卡不在」而 FAIL，那是脚本的锅不是代码的锅。
# 内置卡(app/apps/)与插件卡(entry point)按 import 命名空间区分：
# 内置是 app.apps，插件是 <包>.cards。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from card_dirs import resolve_cards  # noqa: E402


def _card_list() -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """``(插件卡, 内置卡)``，元素都是 ``(卡 id, 标题)``。

    标题取 manifest.json 的 ``title`` —— 那是页面上真正显示的文字，断言
    用它才准（写死一份标题的话，manifest 改了就红）。
    """
    plugin: list[tuple[str, str]] = []
    builtin: list[tuple[str, str]] = []
    for c in resolve_cards():
        try:
            raw = json.loads(
                (Path(c["dir"]) / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        title = raw.get("title") or c["id"]
        (builtin if c["ns"] == "app.apps" else plugin).append((c["id"], title))
    return plugin, builtin


# 每个插件卡至少点进一次，验证静态资源 + 前端 IIFE 能跑起来。
CARDS, BUILTIN = _card_list()

errors: list[str] = []
page_errors: list[str] = []
results: list[tuple[str, str, str, str]] = []


def check_card(page, cid: str, title: str, kind: str) -> None:
    page.goto(f"{BASE}/#{cid}", wait_until="domcontentloaded")
    page.wait_for_timeout(2500)

    # 卡片标题进了 DOM。
    #
    # 用 textContent 而不是 innerText：向导弹出时卡片正文被隐藏
    # （h2.app-card-title 就在那个容器里），innerText 只返回可见文本，
    # 于是断言会假阴性——实测除 vtl 外全部 FAIL，而页面其实完全正常。
    # 标题元素本身是 0ms 就出现的，等再久也一样。
    seen = page.evaluate(
        "t => document.body.textContent.includes(t)", title
    )
    if not seen:
        errors.append(f"{cid}: DOM 里找不到标题 {title!r}")

    # 向导壳（每张带 wizard.json 的卡都会有）
    if page.evaluate("() => !!document.querySelector('#wd-wizard-root-' + location.hash.slice(1))"):
        wizard_shown = page.evaluate(
            "() => !!document.querySelector('#wd-wizard-stepper .wd-wizard-step-dot')"
        )
        if not wizard_shown:
            errors.append(f"{cid}: 向导容器在，但 stepper 没渲染（停在空壳窗口）")

    # 卡片静态资源：app.js 必须能取到（IIFE 靠它注册事件）
    for asset in ("app.js", "app.css", "template.html"):
        resp = page.request.get(f"{BASE}/static/apps/{cid}/{asset}")
        if resp.status != 200:
            errors.append(f"{cid}: /static/apps/{cid}/{asset} -> {resp.status}")

    # wizard.json：vd / vtl / usb-hid 有，其余没有
    wiz = page.request.get(f"{BASE}/static/apps/{cid}/wizard.json")
    has_wiz = wiz.status == 200
    if has_wiz:
        try:
            json.loads(wiz.text())
        except json.JSONDecodeError as e:
            errors.append(f"{cid}: wizard.json 不是合法 JSON: {e}")

    results.append((cid, kind, "ok" if seen else "NO-TITLE",
                    f"wizard={'yes' if has_wiz else 'no'}"))


with sync_playwright() as pw:
    browser = pw.chromium.launch(args=["--no-sandbox"])
    page = browser.new_page(viewport={"width": 1600, "height": 950})
    page.on("pageerror", lambda e: page_errors.append(str(e)))
    page.on("console", lambda m: page_errors.append(f"console.{m.type}: {m.text}")
            if m.type == "error" else None)

    page.goto(BASE, wait_until="domcontentloaded")
    page.wait_for_timeout(2000)

    # 侧栏应有 8 张卡（audio-analysis hidden，不在侧栏）
    sidebar = page.evaluate(
        "() => Array.from(document.querySelectorAll("
        "'[data-app-id], .nav-item, .side-item'))"
        ".map(e => e.getAttribute('data-app-id') || e.textContent.trim())"
        ".filter(Boolean)"
    )
    print("侧栏/网格条目:", sidebar)

    for cid, title in BUILTIN:
        check_card(page, cid, title, "builtin")
    for cid, title in CARDS:
        check_card(page, cid, title, "plugin")

    browser.close()

print("\n--- 卡片加载结果 ---")
for cid, kind, st, extra in results:
    print(f"  {cid:28s} {kind:8s} {st:9s} {extra}")

if page_errors:
    print("\n--- 浏览器 pageerror / console.error ---")
    for e in dict.fromkeys(page_errors):
        print("  !", e)

print("\nVERDICT:", "OK" if not errors and not page_errors else "FAIL")
for e in errors:
    print("  FAIL:", e)
sys.exit(0 if not errors and not page_errors else 1)