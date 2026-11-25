#!/usr/bin/env python3
"""向导「同声传译」那一步的回归（2026-11-25 新增）。

用户拍板 D3：向导里加「**开启同声传译**」开关 + 朗读语言 + 音色，
并且模型加载进度条要**参照 ASR 和翻译**（同一块进度条里多一行）。

本脚本把这条契约钉死：

  I1 向导有第 5 步「同声传译（可选）」，且**能走到**
  I2 那一步有「开启同声传译」开关（checkbox）+「朗读语言」下拉
     +「音色」下拉
  I3 朗读语言**只列能出声的语言**（≥ 8 种）+ 第一项是「跟随翻译目标语言」
  I4 音色下拉的值是 ``kind:id``（参考音色 / 内置音色是**两个不同的
     SDK 参数** ⇒ 混用音色不生效）
  I5 勾上开关 ⇒ ``preloadExtraModels`` 多返回一个 target
     ⇒ 进度条从 2 行（识别 / 翻译）变 3 行（+ 语音合成）
  I6 **不勾**开关 ⇒ 行为与今天完全一致（不加载 TTS）
  I7 全程无 JS 错误

用法：
    .venv/bin/python scripts/verify_asr_wizard_interp.py
    .venv/bin/python scripts/verify_asr_wizard_interp.py --headful

依赖：本机服务在跑 + playwright/chromium（本机已装）+ ASR 模型已启用。
⚠️ 回归期间会**真的把 TTS 模型拉起来**（1.9 GB，独占卡 1）⇒
   这是需求 4 的部署形态本身，跑完可以留着。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT_DIR = Path(__file__).resolve().parent
for _p in (str(_ROOT), str(_SCRIPT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from active_app_guard import (  # noqa: E402
    install as install_active_app_guard,
    install_active_app_spoof,
)

try:
    from playwright.sync_api import sync_playwright
except Exception as exc:  # noqa: BLE001
    print(f"[SKIP] playwright 不可用: {exc}")
    sys.exit(0)

BASE = os.environ.get("WDNWEBUI_BASE", "https://127.0.0.1:8000")
APP = "qwen3-asr"
NS = "wd-wizard-"

#: 一个**短**音频 —— 用来填「音频源」那一步（向导只要有源就能往下走）。
SOURCE = "/userdata/assets/uploads/asr_en.wav"

STEP_TITLE = "同声传译（可选）"

PASSED = 0
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED.append(name)
        print(f"  [FAIL] {name}" + (f" —— {detail}" if detail else ""))


def goto_card(page, app_id: str) -> None:
    page.evaluate("(id) => { window.location.hash = '#' + id; }", app_id)
    page.wait_for_timeout(600)


def open_wizard(page) -> None:
    """打开向导，从第 1 步开始（清掉「已完成 / 上次停留步骤」的痕迹）。"""
    page.goto(BASE, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_function(
        "() => window.WizardState && window.ROUTER && window.WizardRegistry",
        timeout=20000)
    page.evaluate("""(id) => {
        const base = 'wdnwebui.wizard.' + id + '.';
        for (const k of Object.keys(localStorage))
            if (k.startsWith(base)) localStorage.removeItem(k);
    }""", APP)
    # boot-restore 会把 hash 改回「上次进入的卡片」⇒ 等它跑完再切。
    page.wait_for_timeout(2500)
    goto_card(page, APP)
    page.evaluate("(id) => { window.enterWizardMode(id); }", APP)
    page.wait_for_selector(f"#{NS}root-{APP}:not(.hidden)", timeout=20000)
    page.wait_for_timeout(500)


def step_title(page) -> str:
    """当前向导步的标题。"""
    return page.evaluate("""() => {
        const root = document.getElementById('wd-wizard-root-qwen3-asr');
        if (!root) return '';
        const h = root.querySelector('.wd-wizard-step-title')
               || root.querySelector('.wd-wizard-title');
        return (h && h.textContent || '').trim();
    }""")


def fill_source(page) -> None:
    """把「音频源」那一步的**必填**字段填上。

    ⚠️ 不填就走不动 —— 框架的 ``_formStepCanNext`` 对 ``required`` 的
    字段要求 ``el.value`` 非空 ⇒ 否则「下一步」是 **disabled**，点不动
    ⇒ 看起来像「向导卡在第 2 步」。
    """
    page.evaluate("""(src) => {
        const el = document.querySelector(
            '#wd-wizard-root-qwen3-asr [data-field-id="asr-source"]');
        if (!el) return;
        el.value = src;
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
    }""", SOURCE)
    page.wait_for_timeout(500)


def goto_step(page, want_title: str, max_clicks: int = 8) -> bool:
    """一路点「下一步」，直到当前步的标题是 ``want_title``。

    ⚠️ **每点一次之前先补必填字段** —— 「音频源」那一步的源是
    ``required`` ⇒ 不填的话「下一步」是 **disabled** ⇒ 点不动 ⇒
    看起来像「向导卡在第 2 步」。

    ⚠️ 必填字段**只在它那一步渲染出来** ⇒ 必须在**到达那一步之后**
    才填（env 步里根本没有 ``data-field-id="asr-source"`` 这个元素）。
    """
    for _ in range(max_clicks):
        if step_title(page) == want_title:
            return True
        fill_source(page)
        btn = page.query_selector(f"#{NS}next-btn")
        if btn is None:
            return False
        try:
            if btn.is_disabled():
                # 下一步是 disabled ⇒ 必填字段没填上 ⇒ 再填一次然后放弃
                fill_source(page)
                if btn.is_disabled():
                    return False
            btn.click(timeout=8000)
        except Exception:
            return False
        page.wait_for_timeout(700)
    return step_title(page) == want_title


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--headful", action="store_true")
    args = ap.parse_args()

    js_errors: list[str] = []
    console_errors: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=not args.headful,
            args=["--no-sandbox", "--autoplay-policy=no-user-gesture-required"])
        ctx = browser.new_context(viewport={"width": 1440, "height": 960},
                                  ignore_https_errors=True)
        # ⚠️ 必须**在 new_page() 之前**装 —— 拦掉改生产配置的 active-app 写操作，
        #    并让 GET /api/compute/active-app 恒为空（否则 bootRestore 会把
        #    浏览器跳到上一张卡 ⇒ 看到的是别的卡的事件 ⇒ 假失败）。
        install_active_app_guard(ctx)
        install_active_app_spoof(ctx)
        page = ctx.new_page()
        page.on("pageerror", lambda e: js_errors.append(str(e)))
        page.on("console", lambda m: console_errors.append(m.text)
                if m.type == "error" else None)

        try:
            print("=" * 72)
            print("向导「同声传译」那一步的回归")
            print("=" * 72)

            open_wizard(page)
            # ⚠️ **先填源** —— 「音频源」那一步的源是必填的 ⇒ 不填的话
            #    「下一步」是 disabled ⇒ 走不到第 5 步。
            fill_source(page)

            # ---- I1 第 5 步存在且能走到 -------------------------------- #
            ok = goto_step(page, STEP_TITLE)
            check(f"I1 向导能走到第 5 步「{STEP_TITLE}」", ok,
                  f"当前步标题 = {step_title(page)!r}")

            # ---- I2 三个控件都在 ---------------------------------------- #
            has_cb = page.evaluate("""() => !!document.querySelector(
                '#wd-wizard-root-qwen3-asr [data-field-id="asr-interp-enabled"]'
                )""")
            has_lang = page.evaluate("""() => !!document.querySelector(
                '#wd-wizard-root-qwen3-asr [data-field-id="asr-interp-lang"]'
                )""")
            has_voice = page.evaluate("""() => !!document.querySelector(
                '#wd-wizard-root-qwen3-asr [data-field-id="asr-interp-voice"]'
                )""")
            check("I2a 有「开启同声传译」开关", has_cb)
            check("I2b 有「朗读语言」下拉", has_lang)
            check("I2c 有「音色」下拉", has_voice)

            # 开关得是 **checkbox**（值是布尔 ⇒ ``opts.interp === true``）
            cb_type = page.evaluate("""() => {
                const el = document.querySelector(
                  '#wd-wizard-root-qwen3-asr [data-field-id="asr-interp-enabled"]');
                const cb = el && (el.matches?.('input[type=checkbox]')
                    ? el : el.querySelector?.('input[type=checkbox]'));
                return cb ? 'checkbox' : (el ? el.tagName : 'none');
            }""")
            check("I2d 开关是 checkbox（值是布尔）", cb_type == "checkbox",
                  f"实际 = {cb_type}")

            # ---- I3 朗读语言只列能出声的 -------------------------------- #
            lang_info = page.evaluate("""() => {
                const el = document.querySelector(
                  '#wd-wizard-root-qwen3-asr [data-field-id="asr-interp-lang"]');
                if (!el) return {n: -1, first: '', values: []};
                const opts = Array.from(el.options || []);
                return {
                    n: opts.length,
                    first: (opts[0] && opts[0].textContent || '').trim(),
                    values: opts.map(o => o.value),
                };
            }""")
            # 第一项 = 「跟随翻译目标语言」（空值）+ 10 种能出声的语言 ⇒ ≥ 11
            check("I3a 朗读语言下拉有 ≥ 11 项（含「跟随翻译目标语言」）",
                  lang_info["n"] >= 11, f"实际 {lang_info['n']} 项")
            check("I3b 第一项是「跟随翻译目标语言」",
                  lang_info["first"] == "跟随翻译目标语言",
                  f"实际 = {lang_info['first']!r}")
            check("I3c 有空值项（= 跟随翻译目标语言）",
                  "" in (lang_info["values"] or []))

            # ---- I4 音色下拉的值是 kind:id ------------------------------ #
            voice_info = page.evaluate("""() => {
                const el = document.querySelector(
                  '#wd-wizard-root-qwen3-asr [data-field-id="asr-interp-voice"]');
                if (!el) return {n: -1, values: []};
                const opts = Array.from(el.options || []);
                return {n: opts.length, values: opts.map(o => o.value)};
            }""")
            check("I4a 音色下拉有 ≥ 3 项", voice_info["n"] >= 3,
                  f"实际 {voice_info['n']} 项")
            check("I4b 音色值是「kind:id」格式",
                  all(":" in v for v in (voice_info["values"] or [])),
                  f"实际 = {voice_info['values']!r}")

            # ---- I5 / I6 preloadExtraModels 的返回 ---------------------- #
            # ⚠️ 这一步会**真的**把 TTS 模型拉起来（1.9 GB）⇒ 那是需求 4 的
            #    部署形态本身，不是副作用。
            res_on = page.evaluate("""async (src) => {
                const r = await window.ImApp_qwen3_asr.preloadExtraModels({
                    source: src, trTarget: 'zh-CN', interp: true });
                return {targets: (r && r.targets) || [],
                        soft: (r && r.soft) || []};
            }""", SOURCE)
            ids_on = [t.get("model_id") for t in res_on["targets"]]
            check("I5a 勾上同传 ⇒ 目标里有语音合成模型",
                  "qwen3-tts" in ids_on, f"实际 = {ids_on}")
            check("I5b 目标里有翻译模型", "hy-mt-1-5" in ids_on,
                  f"实际 = {ids_on}")
            check("I5c 两个目标都是 npu_index=null（后端按卡位策略挑卡）",
                  all(t.get("npu_index") is None for t in res_on["targets"]),
                  f"实际 = {res_on['targets']!r}")
            check("I5d TTS 是软失败（起不来只标那一行 error）",
                  any("qwen3-tts" in s for s in res_on["soft"]),
                  f"实际 = {res_on['soft']!r}")

            res_off = page.evaluate("""async (src) => {
                const r = await window.ImApp_qwen3_asr.preloadExtraModels({
                    source: src, trTarget: 'zh-CN', interp: false });
                return {targets: (r && r.targets) || []};
            }""", SOURCE)
            ids_off = [t.get("model_id") for t in res_off["targets"]]
            check("I6 不勾同传 ⇒ 不加载 TTS（行为与今天一致）",
                  "qwen3-tts" not in ids_off, f"实际 = {ids_off}")

            # ---- I7 无 JS 错误 ------------------------------------------ #
            check("I7 全程没有 JS 错误", not js_errors,
                  "; ".join(js_errors[:3]))

        finally:
            try:
                page.evaluate("() => { try { window.WizardState.unmount(); }"
                              " catch (_) {} }")
            except Exception:
                pass
            ctx.close()
            browser.close()

    print()
    print("=" * 72)
    print(f"通过 {PASSED} / 失败 {len(FAILED)}")
    if FAILED:
        for f in FAILED:
            print(f"  FAIL  {f}")
    print("=" * 72)
    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
