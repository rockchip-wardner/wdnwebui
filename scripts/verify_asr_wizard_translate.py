#!/usr/bin/env python3
"""回归：ASR 卡的向导「翻译目标语言」步骤 + 失败降级。

覆盖
====

1. **向导里有这一步** —— 第 3 步是「实时翻译（可选）」，下拉被卡片
   **动态填满**（语言表在后端 provider 里，wizard.json 是静态的写不了，
   走框架的 ``wizard:step-rendered`` 事件）。
2. **默认是「不翻译」** —— ⚠️ 卡片页的 ``targetLang`` 默认值是 ``"en"``，
   拿它当向导的默认值 ⇒ 用户**根本没碰过下拉**也会被当成「选了英语」⇒
   白白加载一个 2.2 GB 的翻译模型。这条守住「默认不加载」。
3. **选了语言 ⇒ 真的调了 ``translate/enable`` + 开了实时翻译开关**。
4. **翻译模型启用失败 ⇒ 识别照常启动** —— 翻译是**旁路**，识别才是主链路。

跑法::

    .venv/bin/python scripts/verify_asr_wizard_translate.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

# ⚠️ Playwright 的两套 TLS 栈不一样：页面走 Chromium（认板子 CA），
#    ctx.request 走 Node（不认）⇒ 必须在 Node 进程起来之前设 CA。
os.environ.setdefault(
    "NODE_EXTRA_CA_CERTS",
    "/home/rockchip/.config/wdnwebui/tls/ca.pem")

from playwright.sync_api import sync_playwright  # noqa: E402

BASE = "https://127.0.0.1:8000"
CARD = "qwen3-asr"
SOURCE = "/userdata/assets/uploads/asr_en.wav"

_PASS = 0
_FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  ok   {name}")
    else:
        _FAIL += 1
        print(f"  FAIL {name} {extra}")


def api(method: str, path: str, payload=None, timeout: int = 30):
    url = BASE + path
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body or "{}")
        except Exception:  # noqa: BLE001
            return e.code, {"raw": body}
    except Exception as e:  # noqa: BLE001
        return -1, {"error": repr(e)}


# --------------------------------------------------------------------------- #
# 向导操作
# --------------------------------------------------------------------------- #
def _next(page) -> None:
    page.locator(".wd-wizard-foot button", has_text="下一步").first.click()
    page.wait_for_timeout(900)


def _fill_source(page, path: str) -> None:
    page.evaluate(
        """(p) => {
          const root = document.getElementById('wd-wizard-root-qwen3-asr');
          const el = (root && root.querySelector('input[data-field-id="asr-source"]'))
                  || (root && root.querySelector('input[type="text"]'));
          if (el) { el.value = p; el.dispatchEvent(new Event('input', {bubbles:true})); }
        }""",
        path,
    )
    page.wait_for_timeout(400)


def _tr_select_dump(page) -> dict:
    return page.evaluate(
        """() => {
          const root = document.getElementById('wd-wizard-root-qwen3-asr');
          const sel = root && root.querySelector('[data-field-id="asr-tr-target"]');
          if (!sel) return {exists: false};
          return {
            exists: true,
            value: sel.value,
            options: [...sel.options].map(o => ({v: o.value, t: o.textContent})),
          };
        }"""
    )


def _summary_rows(page) -> list[str]:
    return page.evaluate(
        "() => [...document.querySelectorAll('.wd-wizard-summary-row')]"
        ".map(r => (r.textContent || '').trim())"
    )


def _wizard_err(page) -> str:
    return page.evaluate(
        """() => {
          const e = document.querySelector('#wd-wizard-root-qwen3-asr .wd-wizard-finish-error');
          return e ? (e.textContent || '').trim() : '';
        }"""
    )


def _card_hint(page) -> str:
    return page.evaluate(
        """() => {
          const h = document.getElementById('asr-hint');
          return h ? (h.textContent || '').trim() : '';
        }"""
    )


def _wait_calls(page, calls: list, needle: str, timeout_s: int = 90) -> bool:
    """等某个请求出现。

    ⚠️ **不能用 time.sleep() 轮询** —— Playwright 的 sync API 只在
    **API 调用之间**派发事件，纯 Python 的 sleep 循环不会 pump 连接 ⇒
    ``page.on("request")`` 收到的事件会一直排队，直到下一次 Playwright
    调用（比如 page.evaluate）才被投递出来 ⇒ 这里会误判成「没调」。
    ⇒ 用 ``page.wait_for_timeout()``（它会 pump 连接）。
    """
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        if any(needle in c for c in calls):
            return True
        page.wait_for_timeout(300)
    return False


def _wait_new_round(page, baseline: int, timeout_s: int = 120) -> tuple[bool, dict]:
    """等识别**新**跑一轮（baseline 是跑之前的 round_count）。

    ⚠️ 用 ``page.wait_for_timeout`` 而不是 time.sleep —— 顺便把
    Playwright 的事件队列 pump 掉（request 事件只在 API 调用之间投递）。
    """
    t0 = time.time()
    snap: dict = {}
    while time.time() - t0 < timeout_s:
        st, snap = api("GET", f"/api/{CARD}/")
        if st == 200 and (snap.get("busy")
                          or (snap.get("round_count") or 0) > baseline):
            return True, snap
        page.wait_for_timeout(500)
    return False, snap


def _new_page(ctx):
    ctx.add_init_script("try { localStorage.clear(); } catch (e) {}")
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(f"[pageerror] {e}"))
    page.on("console",
            lambda m: errors.append(f"[console.error] {m.text}")
            if m.type == "error" else None)
    return page, errors


def _wait_idle(timeout_s: int = 120) -> None:
    """停掉并清空，等卡片回到 idle。

    ⚠️ 顺序要紧：先 stop/reset 再等 idle —— 上一轮跑完的状态是
    ``done``，不是 ``idle``，先等会干等 120 s。
    """
    api("POST", f"/api/{CARD}/stop")
    api("POST", f"/api/{CARD}/reset")
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        st, snap = api("GET", f"/api/{CARD}/")
        if st == 200 and snap.get("state") == "idle" and not snap.get("busy"):
            return
        time.sleep(1)
    print("  [warn] 卡片没等到 idle，继续（可能撞 busy）")


# --------------------------------------------------------------------------- #
def _run_case(browser, *, fail_enable: bool, lang: str) -> tuple[list, list]:
    """跑一遍向导。返回 (requests, console_errors)。"""
    ctx = browser.new_context(viewport={"width": 1440, "height": 1000})
    try:
        ctx.request.delete(f"{BASE}/api/compute/active-app")
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] 清 active_app 失败（忽略）: {e}")
    page, errors = _new_page(ctx)

    if fail_enable:
        page.route("**/api/qwen3-asr/translate/enable",
                   lambda r: r.fulfill(
                       status=500, content_type="application/json",
                       body=json.dumps({"detail": "模拟：翻译模型起不来"})))

    calls: list[str] = []
    page.on("request", lambda r: calls.append(f"{r.method} {r.url}")
            if r.method == "POST" else None)

    page.goto(f"{BASE}/#{CARD}", wait_until="networkidle")
    page.wait_for_selector("#wd-wizard-root-qwen3-asr", timeout=20000)
    page.wait_for_timeout(1500)

    _next(page)                       # env → source
    _fill_source(page, SOURCE)
    _next(page)                       # source → translate

    dump = _tr_select_dump(page)
    print("  下拉:", json.dumps(dump, ensure_ascii=False)[:260])
    check("第 3 步有「翻译目标语言」下拉", dump.get("exists") is True)
    opts = dump.get("options") or []
    check("下拉被卡片动态填上了语言（≥30 项）", len(opts) >= 30, f"实际 {len(opts)}")
    check("第一项是「不翻译」", bool(opts) and opts[0].get("v") == "off",
          f"实际 {opts[0] if opts else None}")
    check("含 zh-CN", any(o.get("v") == "zh-CN" for o in opts))
    check("默认值是「不翻译」（不白加载翻译模型）",
          dump.get("value") == "off", f"实际 {dump.get('value')!r}")

    page.evaluate(
        """(lang) => {
          const sel = document.querySelector('#wd-wizard-root-qwen3-asr [data-field-id="asr-tr-target"]');
          sel.value = lang; sel.dispatchEvent(new Event('change', {bubbles:true}));
        }""",
        lang,
    )
    page.wait_for_timeout(500)
    check("选中的值被记住", _tr_select_dump(page).get("value") == lang,
          f"实际 {_tr_select_dump(page).get('value')!r}")

    _next(page)                       # translate → finish
    rows = _summary_rows(page)
    print("  完成页摘要:", rows)
    check("完成页摘要里有「翻译目标语言」",
          any("翻译目标语言" in r for r in rows), f"实际 {rows}")

    st, before = api("GET", f"/api/{CARD}/")
    baseline = int(before.get("round_count") or 0)

    page.locator(".wd-wizard-foot button", has_text="完成配置").first.click()

    enable_seen = _wait_calls(page, calls, "/translate/enable", 90)
    started, snap = _wait_new_round(page, baseline, 120)
    err = _wizard_err(page)
    hint = _card_hint(page)
    if not started:
        print(f"  [debug] 向导错误框={err!r} 卡片提示={hint!r}")
        print(f"  [debug] 请求={calls[-8:]}")
    check("识别启动了", started,
          f"state={snap.get('state')} busy={snap.get('busy')} "
          f"rounds={snap.get('round_count')} err={err!r}")
    check("向导调了 translate/enable", enable_seen,
          f"实际 {[c for c in calls if 'translate' in c]}")
    js_err = [e for e in errors if "favicon" not in e.lower()
              and "500 (Internal Server Error)" not in e]
    check("无 JS 错误", not js_err, f"实际 {js_err[:3]}")
    ctx.close()
    return calls, errors


def main() -> int:
    if not os.path.isfile(SOURCE):
        print(f"❌ 测试音频不存在: {SOURCE}")
        return 2

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True,
                                    args=["--no-sandbox", "--disable-gpu"])

        # ---------------- 用例 1：向导步骤 + 正常启用 -------------------- #
        print("\n=== 用例 1：向导有「实时翻译」这一步，默认「不翻译」 ===")
        _wait_idle()
        api("POST", f"/api/{CARD}/translate/settings", {"enabled": False})
        time.sleep(1)
        _run_case(browser, fail_enable=False, lang="zh-CN")
        st, ts = api("GET", f"/api/{CARD}/translate/status")
        check("实时翻译开关已打开", bool(ts.get("enabled")),
              f"status={json.dumps(ts, ensure_ascii=False)[:200]}")
        check("目标语言是 zh-CN", ts.get("target_lang") == "zh-CN",
              f"实际 {ts.get('target_lang')!r}")

        # ---------------- 用例 2：翻译模型启用失败 ⇒ 识别照常启动 ---------- #
        print("\n=== 用例 2：翻译模型启用失败 ⇒ 识别照常启动 ===")
        _wait_idle()
        api("POST", f"/api/{CARD}/translate/settings", {"enabled": False})
        time.sleep(1)
        _run_case(browser, fail_enable=True, lang="zh-CN")
        st2, ts2 = api("GET", f"/api/{CARD}/translate/status")
        check("失败时没把实时翻译打开（没模型就别开）",
              not ts2.get("enabled"),
              f"status={json.dumps(ts2, ensure_ascii=False)[:200]}")

        browser.close()

    api("POST", f"/api/{CARD}/stop")
    print(f"\n=== 总结: {_PASS} 通过 / {_FAIL} 失败 ===")
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
