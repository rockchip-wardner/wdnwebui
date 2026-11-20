#!/usr/bin/env python3
"""真机浏览器回归：「翻译目标语言」下拉的**显示格式**。

用户要求：选项改成类似右上角「简体中文」下拉框的形式 —— **前面是中文语言
名称，后面是对应语言**；默认「不翻译」这个选项要保留。

覆盖
====

1. **向导第 3 步**的下拉：第一项是「不翻译」且**默认选中**（哨兵值 ``off``）。
2. 每个语言项的文案 = 「中文名 本族名」—— 前面那段**必须是中文**。
3. **本族文字板端渲染不出来 ⇒ 退回英文名**（泰语 / 印地语 那些没字体的
   语言）—— 判据是**豆腐块**（跟右上角那个下拉里的「印地语 / 泰语」一个
   毛病：हिन्दी / ไทย 渲染成一排方框）。
4. **卡片页**的「目标语言」下拉也是同一份格式（两个下拉长得不一样会让人
   以为功能不一样）。
5. 选一项之后 ``value`` 仍是**语言码**（``zh-CN`` / ``ja`` …）—— 显示文案
   变了，**协议不能跟着变**。

跑法::

    .venv/bin/python scripts/verify_asr_translate_lang_labels.py
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
CARD = "qwen3-asr"

_PASS = 0
_FAIL = 0

#: 板端**没有**字体、渲染出来是豆腐块的那些语言（本族文字 ⇒ 必须退回英文名）
_NO_FONT = {"th", "hi", "bo", "km", "my", "gu", "te", "ta", "bn"}


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
        return e.code, {}
    except Exception:  # noqa: BLE001
        return -1, {}


def _wizard_dump(page) -> dict:
    """向导第 3 步的下拉。"""
    return page.evaluate(
        """() => {
          const root = document.getElementById('wd-wizard-root-qwen3-asr');
          const sel = root && root.querySelector('[data-field-id="asr-tr-target"]');
          if (!sel) return {exists: false};
          return {
            exists: true,
            value: sel.value,
            options: [...sel.options].map((o) => ({
              v: o.value, t: o.textContent, sel: o.selected,
            })),
          };
        }"""
    )


def _card_dump(page) -> dict:
    """卡片页的「目标语言」下拉。"""
    return page.evaluate(
        """() => {
          const sel = document.getElementById('asr-tr-target');
          if (!sel) return {exists: false};
          return {
            exists: true,
            value: sel.value,
            options: [...sel.options].map((o) => ({
              v: o.value, t: o.textContent,
            })),
          };
        }"""
    )


def _zh_prefix_ok(text: str) -> bool:
    """文案**前面那段是中文**（后面可以是本族名或英文名）。"""
    head = (text or "").split(" ")[0]
    if not head:
        return False
    return any("\u4e00" <= ch <= "\u9fff" for ch in head)


def main() -> int:
    print("=" * 72)
    print("真机浏览器回归：翻译目标语言下拉的显示格式")
    print("=" * 72)

    errors: list[str] = []

    # ⚠️ **「不翻译」是不是默认项，取决于「实时翻译」当前开没开**（既有规则）：
    #   开着 ⇒ 默认当前的目标语言（用户明明在用翻译，别把他拨回「不翻译」）；
    #   关着 ⇒ 默认「不翻译」（别白加载一个 2.2 GB 的翻译模型）。
    # ⇒ 两种状态都要测，否则断言会随板子状态飘。
    api("POST", "/api/qwen3-asr/translate/settings",
        {"enabled": False, "target_lang": "zh-CN"})

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

        # --- 全新 profile ⇒ 向导一定会弹出来（本地没存「已完成」）---
        page.goto(f"{BASE}/#{CARD}", wait_until="networkidle")
        page.wait_for_timeout(2500)
        page.wait_for_selector("#wd-wizard-root-qwen3-asr", timeout=20000)

        # env → source
        page.locator(".wd-wizard-foot button", has_text="下一步").first.click()
        page.wait_for_timeout(900)
        # 源是**必填**的 ⇒ 不填「下一步」是灰的 ⇒ 先填一条路径
        page.evaluate(
            """() => {
              const root = document.getElementById('wd-wizard-root-qwen3-asr');
              const el = (root && root.querySelector('input[data-field-id="asr-source"]'))
                      || (root && root.querySelector('input[type="text"]'));
              if (el) {
                el.value = '/userdata/assets/uploads/asr_dual_sub_test.mp4';
                el.dispatchEvent(new Event('input', {bubbles: true}));
              }
            }"""
        )
        page.wait_for_timeout(600)
        # source → translate（这一步会触发 ``wizard:step-rendered``）
        page.locator(".wd-wizard-foot button", has_text="下一步").first.click()
        page.wait_for_timeout(1500)

        w = _wizard_dump(page)
        check("向导第 3 步有语言下拉", w.get("exists"), f"dump={w}")
        if not w.get("exists"):
            browser.close()
            return 1

        opts = w["options"]
        check("「不翻译」是**第一项**", opts and opts[0]["v"] == "off",
              f"first={opts[0] if opts else None}")
        check("「实时翻译」关着 ⇒ **默认选中「不翻译」**",
              opts and opts[0]["sel"] and w.get("value") == "off",
              f"value={w.get('value')} first={opts[0] if opts else None}")
        check("「不翻译」的文案没被改", opts and opts[0]["t"] == "不翻译",
              f"t={opts[0]['t'] if opts else None}")
        check("语言项够多（模型词表 36 种，繁体被剔掉）",
              len(opts) >= 30, f"n={len(opts)}")
        check("没有繁体中文（模型分不出简繁）",
              not any(o["v"] == "zh-TW" for o in opts), "")

        # --- 显示格式：前面是中文名 ---
        bad = [o for o in opts if o["v"] != "off" and not _zh_prefix_ok(o["t"])]
        check("每个语言项**前面都是中文名**", not bad,
              f"bad={[(o['v'], o['t']) for o in bad[:4]]}")
        sample = [(o["v"], o["t"]) for o in opts[:6]]
        print("  样例:", sample)

        # --- 豆腐块回退：没字体的语言 ⇒ 用英文名 ---
        bad_font = []
        for o in opts:
            if o["v"] in _NO_FONT:
                # 本族文字不能出现（会是豆腐块）
                if any(ch in o["t"] for ch in "ไทยहिन्दीབོདភាសាမြန်မာગુજરાતીతెలుగుதமிழ்বাংলা"):
                    bad_font.append((o["v"], o["t"]))
                # 必须退回英文名
                if not o["t"].split(" ")[-1].isascii():
                    bad_font.append((o["v"], o["t"]))
        check("没字体的语言**退回英文名**（不显示豆腐块）", not bad_font,
              f"bad={bad_font[:4]}")

        # --- 有字体的语言 ⇒ 用本族名 ---
        good_font = [o for o in opts if o["v"] in ("ja", "ko", "fr", "ru")]
        check("有字体的语言用**本族名**（日本語 / 한국어 / Français …）",
              all(o["t"].split(" ")[-1] == {"ja": "日本語", "ko": "한국어",
                                           "fr": "Français", "ru": "Русский"}[o["v"]]
                  for o in good_font),
              f"got={[(o['v'], o['t']) for o in good_font]}")

        # --- 卡片页的下拉：同一份格式 ---
        skip = page.locator("#wd-wizard-root-qwen3-asr .wd-wizard-skip-btn").first
        if skip.count() and skip.is_visible():
            skip.click()
            page.wait_for_timeout(1500)
        page.wait_for_selector("#page-qwen3-asr:not(.hidden)", timeout=15000)
        page.wait_for_timeout(1500)
        c = _card_dump(page)
        check("卡片页有「目标语言」下拉", c.get("exists"), f"dump={c}")
        if c.get("exists"):
            copts = c["options"]
            check("卡片页的下拉也是「中文名 + 本族名」",
                  copts and all(_zh_prefix_ok(o["t"]) for o in copts),
                  f"bad={[(o['v'], o['t']) for o in copts[:3]]}")
            check("卡片页的下拉**没有**「不翻译」（那边有单独的「实时翻译」开关）",
                  not any(o["v"] == "off" for o in copts), "")

        # --- 选一项：value 仍然是语言码 ---
        page.evaluate(
            """() => {
              const sel = document.getElementById('asr-tr-target');
              if (!sel) return;
              const opt = [...sel.options].find((o) => o.value === 'ja');
              if (opt) { sel.value = 'ja'; sel.dispatchEvent(new Event('change', {bubbles:true})); }
            }"""
        )
        page.wait_for_timeout(1200)
        st = api("GET", "/api/qwen3-asr/translate/status")[1]
        check("选了之后 **value 还是语言码**（协议没跟着显示文案变）",
              st.get("target_lang") == "ja", f"target_lang={st.get('target_lang')}")

        # --- 另一种状态：「实时翻译」**开着** ⇒ 默认当前目标语言（既有行为）---
        api("POST", "/api/qwen3-asr/translate/settings",
            {"enabled": True, "target_lang": "ja"})
        ctx2 = browser.new_context(viewport={"width": 1440, "height": 1000},
                                   ignore_https_errors=True)
        install_active_app_guard(ctx2)
        install_active_app_spoof(ctx2)
        pg2 = ctx2.new_page()
        pg2.goto(f"{BASE}/#{CARD}", wait_until="networkidle")
        pg2.wait_for_timeout(2500)
        pg2.wait_for_selector("#wd-wizard-root-qwen3-asr", timeout=20000)
        pg2.locator(".wd-wizard-foot button", has_text="下一步").first.click()
        pg2.wait_for_timeout(900)
        pg2.evaluate(
            """() => {
              const root = document.getElementById('wd-wizard-root-qwen3-asr');
              const el = (root && root.querySelector('input[data-field-id="asr-source"]'))
                      || (root && root.querySelector('input[type="text"]'));
              if (el) {
                el.value = '/userdata/assets/uploads/asr_dual_sub_test.mp4';
                el.dispatchEvent(new Event('input', {bubbles: true}));
              }
            }"""
        )
        pg2.wait_for_timeout(600)
        pg2.locator(".wd-wizard-foot button", has_text="下一步").first.click()
        pg2.wait_for_timeout(1500)
        w2 = _wizard_dump(pg2)
        check("「实时翻译」开着 ⇒ 默认**当前目标语言**（既有行为没被改）",
              w2.get("value") == "ja", f"value={w2.get('value')}")
        check("开着的时候「不翻译」**仍然是第一项**（只是没选中它）",
              w2.get("options") and w2["options"][0]["v"] == "off",
              f"first={w2.get('options')[0] if w2.get('options') else None}")

        # 还原板子上的翻译设置
        api("POST", "/api/qwen3-asr/translate/settings",
            {"enabled": False, "target_lang": "zh-CN"})

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
