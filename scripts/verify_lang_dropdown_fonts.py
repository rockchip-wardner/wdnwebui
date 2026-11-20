#!/usr/bin/env python3
"""真机浏览器回归：右上角「界面语言」下拉**没有豆腐块**。

问题
====

板端字体覆盖有限 —— 泰语（ไทย）/ 天城文（हिन्दी）/ 藏文 / 高棉文 / 缅甸文 /
古吉拉特文 / 泰卢固文 / 泰米尔文 / 孟加拉文 **都没有字体**
（实测：``fc-list :lang=th|hi|bo|km|my|gu|te|ta|bn`` 全空）⇒ 那些本族文字
会渲染成**豆腐块**（□）。

⚠️ 界面语言 = **整页**的文案语言 ⇒ 用户一选「泰语」，**整个界面**都变成
ไทย ⇒ 板端没泰文字体 ⇒ **满屏方框**（比下拉里两个方框严重得多）。

⇒ 修法（``app/static/core/translate.js``）：下拉**只列板端渲染得出来的
语言**，渲染不出来的在面板里**说明一句**（别让用户以为「这板子不支持这些
语言」）；判据用 ``/static/core/font-check.js`` 的**运行时**字体检测。

覆盖
====

1. 下拉里**每一项**的本族名都渲染得出来（逐像素比对 .notdef）。
2. 板端没字体的语言（泰语 / 印地语）**不在**下拉里。
3. 面板里**说清楚**为什么它们不在（「本板没有这些语言的字体，已隐藏：…」）。
4. 按钮上的当前语言名也渲染得出来。
5. 上次选过「渲染不出来」的语言 ⇒ 进页面**不**整屏方框（自动退回中文）。

跑法::

    .venv/bin/python scripts/verify_lang_dropdown_fonts.py
"""
from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.error
import urllib.request

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

os.environ.setdefault(
    "NODE_EXTRA_CA_CERTS",
    "/home/rockchip/.config/wdnwebui/tls/ca.pem")

from playwright.sync_api import sync_playwright  # noqa: E402

from active_app_guard import (  # noqa: E402
    install as install_active_app_guard,
    install_active_app_spoof,
)

BASE = "https://127.0.0.1:8000"

#: 板端**没有**字体的那些语言（本族文字是豆腐块）。
NO_FONT_LANGS = {
    "th": "泰语", "hi": "印地语", "bo": "藏语", "km": "高棉语",
    "my": "缅甸语", "gu": "古吉拉特语", "te": "泰卢固语",
    "ta": "泰米尔文", "bn": "孟加拉文",
}

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


def api(method: str, path: str, payload=None, timeout: int = 60):
    url = BASE + path
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
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
# 页面探针
# --------------------------------------------------------------------------- #
#: **独立**的豆腐块判据 —— 不能用被测代码（``WdnFontCheck``）自己的结论。
#:
#: 把字符画到 canvas 上，跟 U+FFFF（非字符位 ⇒ 一定 .notdef）逐像素比。
#: ⚠️ 不用「量宽度」：豆腐块和正常字符可能同宽。
_PROBE_JS = """
() => {
  const cv = document.createElement('canvas');
  cv.width = 40; cv.height = 40;
  const ctx = cv.getContext('2d');
  const cs = getComputedStyle(document.body);
  const font = (cs.fontSize || '16px') + ' ' + (cs.fontFamily || 'sans-serif');
  const draw = (ch) => {
    ctx.clearRect(0, 0, 40, 40);
    ctx.fillStyle = '#000';
    ctx.textBaseline = 'top';
    ctx.font = font;
    ctx.fillText(ch, 0, 0);
    return ctx.getImageData(0, 0, 40, 40).data;
  };
  const notdef = draw('\\uFFFF');
  const tofu = (text) => {
    const s = String(text || '');
    if (!s.trim()) return false;
    for (const ch of s) {
      if (ch === ' ' || ch === '\\u00a0') continue;
      const px = draw(ch);
      let same = true;
      for (let i = 3; i < px.length; i += 4) {
        if (px[i] !== notdef[i]) { same = false; break; }
      }
      if (same) return true;      // 这个字符是豆腐块
    }
    return false;
  };

  const items = [...document.querySelectorAll('.wdn-lang-item')].map((el) => ({
    code: el.dataset.code || '',
    zh: (el.querySelector('.wdn-lang-item-zh') || {}).textContent || '',
    native: (el.querySelector('.wdn-lang-item-native') || {}).textContent || '',
  }));
  const btn = document.querySelector('.wdn-lang-btn');
  const note = document.querySelector('.wdn-lang-missing');
  return {
    items: items,
    // ⚠️ 每一项的**本族名**都渲染得出来吗？
    tofuItems: items.filter((it) => it.native && tofu(it.native)),
    buttonName: btn ? ((btn.querySelector('.wdn-lang-name') || {}).textContent || '') : '',
    buttonTofu: btn ? tofu((btn.querySelector('.wdn-lang-name') || {}).textContent || '') : false,
    noteText: note && !note.hidden ? (note.textContent || '').trim() : '',
    hasFontCheck: !!(window.WdnFontCheck && window.WdnFontCheck.canRender),
  };
}
"""


def _probe(page) -> dict:
    return page.evaluate(_PROBE_JS)


def main() -> int:
    print("=" * 72)
    print("真机浏览器回归：右上角「界面语言」下拉没有豆腐块")
    print("=" * 72)

    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1440, "height": 1000},
                                  ignore_https_errors=True)
        install_active_app_guard(ctx)
        install_active_app_spoof(ctx)
        page = ctx.new_page()
        page.on("pageerror", lambda e: errors.append(f"[pageerror] {e}"))
        page.on("console", lambda m: errors.append(f"[console.{m.type}] {m.text}")
                if m.type == "error" else None)

        page.goto(f"{BASE}/", wait_until="networkidle")
        page.wait_for_timeout(2500)

        # --- 字体检测器加载了没 ---
        st = _probe(page)
        check("字体检测器加载了（/static/core/font-check.js）", st["hasFontCheck"])

        # --- 打开下拉 ---
        page.locator(".wdn-lang-btn").click()
        page.wait_for_timeout(800)
        st = _probe(page)
        items = st["items"]
        print("  下拉 %d 项：" % len(items))
        for it in items[:6]:
            print("    %-6s %s / %s" % (it["code"], it["zh"], it["native"]))

        check("下拉里有语言（≥15 项）", len(items) >= 15, f"实际 {len(items)}")

        # --- 1. 每一项都渲染得出来 ---
        check("下拉里**每一项**的本族名都渲染得出来（没有豆腐块）",
              not st["tofuItems"],
              "豆腐块项=" + json.dumps(st["tofuItems"], ensure_ascii=False))

        # --- 2. 没字体的语言不摆出来 ---
        codes = {it["code"] for it in items}
        present = [c for c in NO_FONT_LANGS if c in codes]
        check("板端没字体的语言**不在**下拉里（选了整页都是方框）",
              not present, f"还在下拉里={present}")

        # --- 3. 面板里说清楚为什么 ---
        note = st["noteText"]
        check("面板里说明了为什么它们不在",
              "字体" in note and "隐藏" in note, f"说明={note!r}")
        check("说明里点了名（泰语 / 印地语）",
              all(n in note for n in ("泰语", "印地语")), f"说明={note!r}")

        # --- 4. 按钮上的当前语言名 ---
        check("按钮上的语言名渲染得出来（不是方框）",
              not st["buttonTofu"],
              f"按钮={st['buttonName']!r}")

        # --- 5. 上次选过没字体的语言 ⇒ 进页面不整屏方框 ---
        page.evaluate(
            """(code) => {
              try { localStorage.setItem('wdnwebui.translate.lang', code); }
              catch (e) { /* ignore */ }
            }""",
            "th",
        )
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(2500)
        st2 = _probe(page)
        check("上次选过没字体的语言 ⇒ 进页面**不**整屏方框",
              not st2["buttonTofu"], f"按钮={st2['buttonName']!r}")
        saved = page.evaluate(
            """() => { try { return localStorage.getItem('wdnwebui.translate.lang'); }
                       catch (e) { return null; } }""")
        check("那条没字体的语言记录被清掉了（不会一直卡在这个状态）",
              saved in (None, ""), f"localStorage={saved!r}")

        check("没有 JS 错误", not errors, f"errors={errors[:4]}")
        browser.close()

    print("\n" + "=" * 72)
    print(f"通过 {_PASS} / 失败 {_FAIL}")
    print("=" * 72)
    if _FAIL:
        print("❌ 有断言没过")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
