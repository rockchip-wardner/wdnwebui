#!/usr/bin/env python3
"""回归：text-chat 卡作为**插件卡**（wdnapps_audio_chat）仍能正常装载与渲染。

背景
----
text-chat 曾是宿主仓 ``app/apps/text_chat/`` 下的**内置卡**，v0.26 迁到
``wdnapps_audio_chat`` 插件包（**暂存** —— 最终计划拆到独立的
``wdnapps-chat`` 包）。迁移把三样东西从宿主挪走了：

1. 卡片的全部源码（含 ``static/app.css``）；
2. 卡片的 CSS —— 从宿主 ``app/static/core/framework.css`` 搬进卡片自己的
   ``static/app.css``（搬之前那个文件是 1 行空文件，样式寄生在宿主）；
3. 卡片的 ``backend.module`` 命名空间（``app.apps.text_chat`` →
   ``wdnapps_audio_chat.cards.text_chat``）。

本脚本断言迁移后的**浏览器端**表现：

* 卡片能被宿主发现并渲染出 ``#page-text-chat`` section；
* 卡片的 ``app.js`` 被注入且 **IIFE 无 pageerror**（IIFE 里引用的共享模块
  404 时，截图和 DOM 断言都可能「看着像没事」，只有 pageerror 能抓到）；
* 卡片的 ``app.css`` **真的被挂载且非空** —— CSS 漏搬的探针（漏了不报错，
  卡片照常能开、能发消息，只是全是裸样式）；
* 向导能挂上，且 ``window.ImApp_text_chat.applyWizardSnapshot`` 可用；
* 切换离开再切回来，卡片仍然正常（验「卡片 IIFE 只跑一次」的不变式：
  framework 的 ``_loadedApps`` 只增不减，向导重挂不重注脚本）。

跑法（需要服务在跑 + playwright）::

    .venv/bin/python scripts/verify_text_chat_plugin_card.py
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000"
CARD = "text-chat"

_page_errors: list[str] = []
_results: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    _results.append((ok, name))
    mark = "✅" if ok else "❌"
    print(f"  {mark} {name}" + (f" — {detail}" if detail and not ok else ""))
    return ok


def main() -> int:
    # ---- 0. 卡片的静态资源必须真的挂上了（CSS 漏搬的探针）----
    print("\n【0】静态资源")
    for path, must_be_nonempty in (
        ("/static/apps/text-chat/app.js", True),
        ("/static/apps/text-chat/app.css", True),
        ("/static/apps/text-chat/template.html", True),
        ("/static/apps/text-chat/wizard.json", True),
    ):
        try:
            with urllib.request.urlopen(BASE + path, timeout=10) as r:
                body = r.read()
            ok = r.status == 200 and (not must_be_nonempty or len(body) > 0)
            check(f"{path} 200 且非空", ok,
                  f"status={r.status} size={len(body)}")
        except Exception as e:  # noqa: BLE001
            check(f"{path} 200 且非空", False, repr(e))

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        ctx = browser.new_context()
        page = ctx.new_page()
        page.on("pageerror", lambda e: _page_errors.append(str(e)))
        page.on("console", lambda m: _page_errors.append(f"console.error: {m.text}")
                if m.type == "error" else None)

        # ---- 1. 进入卡片：section 存在 + 不是空白 ----
        print("\n【1】卡片渲染")
        page.goto(f"{BASE}/#{CARD}", wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(2500)

        sec_count = page.locator(f"#page-{CARD}").count()
        check(f"#page-{CARD} section 存在", sec_count == 1, f"count={sec_count}")

        # 向导模式下宿主会把页面 section **藏起来**（向导面板接管）——
        # 这是既有行为，不是缺陷。所以「页面可见」要在**跳过向导之后**断言。
        in_wizard = page.evaluate(
            f"""() => {{
              const el = document.getElementById("page-{CARD}");
              if (!el) return {{missing:true}};
              const r = el.getBoundingClientRect();
              return {{hidden: el.classList.contains("hidden"),
                       w: r.width, h: r.height}};
            }}"""
        )
        check("进入卡片时向导接管（页面 section 暂时隐藏）",
              bool(in_wizard.get("hidden")), json.dumps(in_wizard))

        # 卡片的核心 DOM（composer / 发送按钮）必须由 template.html 注入
        for sel, label in (
            ("#text-chat-input", "输入框"),
            ("#text-chat-send-btn", "发送按钮"),
            ("#text-chat-stop-btn", "停止按钮"),
            ("#text-chat-reset-btn", "重置按钮"),
        ):
            check(f"{label} {sel} 存在", page.locator(sel).count() == 1,
                  f"count={page.locator(sel).count()}")

        # ---- 2. 卡片 CSS 真的生效（不是裸样式）----
        print("\n【2】卡片样式")
        css_probe = page.evaluate(
            f"""() => {{
              const el = document.querySelector("#page-{CARD} .imb-card");
              if (!el) return {{ok:false, reason:"no .imb-card"}};
              const cs = getComputedStyle(el);
              return {{ok: cs.minHeight !== "" && cs.minHeight !== "0px",
                       minHeight: cs.minHeight,
                       borderRadius: cs.borderRadius}};
            }}"""
        )
        check(".imb-card 吃到卡片 CSS（min-height 生效）",
              bool(css_probe.get("ok")), json.dumps(css_probe))

        # 思考开关必须是「已禁用」态（应用层总闸关闭）
        thinking = page.evaluate(
            """() => {
              const el = document.getElementById("text-chat-thinking");
              if (!el) return {present:false};
              return {present:true, disabled: el.disabled, checked: el.checked};
            }"""
        )
        check("深度思考开关存在且为 disabled",
              thinking.get("present") and thinking.get("disabled") is True,
              json.dumps(thinking))

        # ---- 3. 向导挂载 + 全局回调 ----
        print("\n【3】向导")
        page.wait_for_selector(".wd-wizard-section", timeout=10000)
        sections = page.locator(".wd-wizard-section").count()
        dots = page.locator(".wd-wizard-step-dot").count()
        check("向导 section 已挂载", sections >= 1, f"sections={sections}")
        check("向导步骤点已渲染", dots >= 2, f"dots={dots}")

        imapp = page.evaluate(
            "typeof window.ImApp_text_chat?.applyWizardSnapshot === 'function'"
        )
        check("window.ImApp_text_chat.applyWizardSnapshot 可用", imapp is True)

        # ---- 4. 跳过向导 → 页面 section 必须露出来 ----
        print("\n【4】跳过向导")
        # 注：跳过按钮只有 class `wd-wizard-skip-btn`，**没有 id**
        #（state.js 的 _ce() 只设 class；只有「上一步」额外设了 id）。
        skip = page.locator(".wd-wizard-skip-btn")
        check("「跳过向导」按钮存在", skip.count() == 1,
              f"count={skip.count()}")
        if skip.count():
            skip.click()
            page.wait_for_timeout(1500)

        visible = page.evaluate(
            f"""() => {{
              const el = document.getElementById("page-{CARD}");
              if (!el) return {{ok:false, reason:"missing"}};
              const r = el.getBoundingClientRect();
              return {{ok: r.width > 0 && r.height > 0,
                       w: r.width, h: r.height,
                       hidden: el.classList.contains("hidden")}};
            }}"""
        )
        check("跳过向导后 section 可见且有尺寸", bool(visible.get("ok")),
              json.dumps(visible))

        # ---- 5. 切走再切回：卡片仍正常（IIFE 不重跑的不变式）----
        print("\n【5】切页往返")
        page.goto(f"{BASE}/#apps", wait_until="domcontentloaded", timeout=15000)
        page.wait_for_timeout(1200)
        page.goto(f"{BASE}/#{CARD}", wait_until="domcontentloaded", timeout=15000)
        page.wait_for_timeout(2000)
        back = page.evaluate(
            f"""() => {{
              const el = document.getElementById("page-{CARD}");
              const inp = document.getElementById("text-chat-input");
              return {{section: !!el, input: !!inp,
                       visible: el ? !el.classList.contains("hidden") : false}};
            }}"""
        )
        check("切回后 section 与输入框仍在", back.get("input") is True,
              json.dumps(back))

        # ---- 6. 无 pageerror ----
        print("\n【6】JS 运行时")
        # console 里可能出现无害的网络告警，只看 pageerror
        hard_errors = [e for e in _page_errors if not e.startswith("console.error:")]
        check("无 pageerror", not hard_errors, "; ".join(hard_errors[:3]))

        browser.close()

    ok = sum(1 for good, _ in _results if good)
    total = len(_results)
    print(f"\n{'=' * 60}")
    print(f"汇总: {ok}/{total} → {'✅ PASS' if ok == total else '❌ FAIL'}")
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
