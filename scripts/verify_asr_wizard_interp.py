#!/usr/bin/env python3
"""向导「配音导出 / 同传」那几步的回归。

⚠️ **2026-11-27 重写** —— 原脚本钉的契约已经**不存在**了。

原脚本钉的是向导第 5 步「同声传译（可选）」（开启同传开关 + 朗读语言 +
音色）⇒ 那一步在 **2026-11-26** 就被**移除**了：

* 同传只对**麦克风**两种来源开放（板载麦 / 浏览器麦）；
* 而向导的流程是**文件类**的（它要一个**设备端路径**）⇒ 文件类来源在
  向导里做同传没有意义 ⇒ 那一步换成了「**配音导出（可选）**」（配音音色
  + 导出语言的初始值）。

⇒ 脚本从此一直挂 9 条（2026-11-26 那天没人重跑它 ⇒ 一直没人发现）。

现在钉的是**当前流程**：

  J1 向导能走到第 4 步「配音导出（可选）」
  J2 那一步有「配音音色」下拉
  J3 **同传的三个控件不在向导里**（防回退 —— 别哪天又加回来）
  J4 音色下拉：≥ 3 项 + 值是 ``kind:id``（参考音色 / 内置音色是**两个不同
     的 SDK 参数** ⇒ 混用 ⇒ 音色不生效）+ 默认选中**参考音色**
  J5 向导的「翻译目标语言」下拉：≥ 30 项 + 第一项是「不翻译」
  J6 向导 finish 用的入参形状 ⇒ ``preloadExtraModels`` **不加载 TTS**
     ⇒ 进度条仍是 2 行（识别 / 翻译）
  J7 全程无 JS 错误

用法：
    .venv/bin/python scripts/verify_asr_wizard_interp.py
    .venv/bin/python scripts/verify_asr_wizard_interp.py --headful

依赖：本机服务在跑 + playwright/chromium（本机已装）+ ASR 模型已启用。

⚠️ 回归期间会**真的把翻译模型拉起来**（2.2 GB）—— 那是需求本身的部署形态，
   跑完可以留着。
"""
from __future__ import annotations

import argparse
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

#: 当前的向导步骤（2026-11-26 起）：
#:   环境检查 → 音频源 → 实时翻译（可选）→ **配音导出（可选）** → 完成配置
STEP_TR = "实时翻译（可选）"
STEP_DUB = "配音导出（可选）"

#: ⚠️ **同传那三个字段早就不在向导里了**（2026-11-26 移除）⇒ 这条是
#: **防回退**：别哪天又把「同声传译」加回向导（它只对麦克风来源有意义，
#: 而向导是文件类流程）。
GONE_INTERP_FIELDS = ("asr-interp-enabled", "asr-interp-lang", "asr-interp-voice")

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


def _select_info(page, field_id: str) -> dict:
    """读向导里某个 select 字段的选项信息。"""
    return page.evaluate("""(fid) => {
        const el = document.querySelector(
            '#wd-wizard-root-qwen3-asr [data-field-id="' + fid + '"]');
        if (!el) return {n: -1, first: '', value: '', texts: [], values: []};
        const opts = Array.from(el.options || []);
        return {
            n: opts.length,
            first: (opts[0] && opts[0].textContent || '').trim(),
            value: el.value || '',
            texts: opts.map(o => (o.textContent || '').trim()),
            values: opts.map(o => o.value),
        };
    }""", field_id)


def _has_field(page, field_id: str) -> bool:
    return bool(page.evaluate("""(fid) => !!document.querySelector(
        '#wd-wizard-root-qwen3-asr [data-field-id="' + fid + '"]'
        )""", field_id))


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
            print("向导「配音导出 / 同传」那几步的回归")
            print("=" * 72)

            open_wizard(page)
            # ⚠️ **先填源** —— 「音频源」那一步的源是必填的 ⇒ 不填的话
            #    「下一步」是 disabled ⇒ 走不到第 4 步。
            fill_source(page)

            # ---- J5 第 3 步：翻译目标语言 -------------------------------- #
            print("\n--- 第 3 步「实时翻译（可选）」---")
            ok = goto_step(page, STEP_TR)
            check(f"J0 向导能走到第 3 步「{STEP_TR}」", ok,
                  f"当前步标题 = {step_title(page)!r}")
            tr_info = _select_info(page, "asr-tr-target")
            check("J5a 翻译目标语言下拉有 ≥ 30 项", tr_info["n"] >= 30,
                  f"实际 {tr_info['n']} 项")
            check("J5b 第一项是「不翻译」", tr_info["first"] == "不翻译",
                  f"实际 = {tr_info['first']!r}")
            check("J5c 有一项的 value 是 'off'（哨兵值）",
                  "off" in (tr_info["values"] or []),
                  f"实际 = {tr_info['values'][:3]!r}")

            # ---- J1 第 4 步：配音导出 ------------------------------------ #
            print("\n--- 第 4 步「配音导出（可选）」---")
            ok = goto_step(page, STEP_DUB)
            check(f"J1 向导能走到第 4 步「{STEP_DUB}」", ok,
                  f"当前步标题 = {step_title(page)!r}")
            check("J2 配音导出步有「配音音色」下拉",
                  _has_field(page, "asr-dub-voice"))

            # ---- J3 同传的控件不许回来（防回退）------------------------- #
            gone = [fid for fid in GONE_INTERP_FIELDS if _has_field(page, fid)]
            check("J3 同传的三个控件**不在**向导里（防回退）", not gone,
                  f"仍然存在 = {gone}")

            # ---- J4 音色下拉 ---------------------------------------------- #
            # ⚠️ 选项是**运行时**灌进去的（卡片在 ``wizard:step-rendered`` 里
            #    拉 ``/api/tts/voices``）⇒ 刚渲染完可能还是空的 ⇒ 等一下。
            voice_info = {"n": -1, "first": "", "value": "", "texts": [],
                          "values": []}
            for _ in range(12):
                voice_info = _select_info(page, "asr-dub-voice")
                if voice_info["n"] > 0:
                    break
                page.wait_for_timeout(500)
            check("J4a 音色下拉有 ≥ 3 项", voice_info["n"] >= 3,
                  f"实际 {voice_info['n']} 项")
            check("J4b 音色值是「kind:id」格式",
                  all(":" in v for v in (voice_info["values"] or [])),
                  f"实际 = {voice_info['values']!r}")
            check("J4c 默认选中的是**参考音色**（本板只有这类能出声）",
                  (voice_info["value"] or "").startswith("ref:"),
                  f"实际 = {voice_info['value']!r}")
            check("J4d 音色项都标了类别（参考音色 / 内置音色）",
                  all(t.startswith("参考音色 ") or t.startswith("内置音色 ")
                      for t in (voice_info["texts"] or [])),
                  f"实际 = {voice_info['texts']!r}")

            # ---- J6 finish 的入参形状 ⇒ 不加载 TTS ---------------------- #
            # ⚠️ **向导现在给的就是这个形状**（``wizard.json`` 里没有同传的
            #    字段 ⇒ 框架构造的 opts 里**没有** ``interp``）⇒ 那条
            #    「同传开着就拉 TTS」的分支现在是**死代码** ⇒ TTS **不加载**
            #    ⇒ 进度条仍是 2 行（识别 / 翻译）。
            print("\n--- finish 的模型预加载 ---")
            res = page.evaluate("""async (src) => {
                const r = await window.ImApp_qwen3_asr.preloadExtraModels({
                    source: src, trTarget: 'zh-CN' });
                return {targets: (r && r.targets) || [],
                        soft: (r && r.soft) || []};
            }""", SOURCE)
            ids = [t.get("model_id") for t in res["targets"]]
            check("J6a 向导的入参形状下**不加载** TTS（同传已不在向导里）",
                  "qwen3-tts" not in ids, f"实际 = {ids}")
            check("J6b 目标里有**翻译**模型（进度条仍是 2 行）",
                  any(str(i or "").startswith("hy-mt") for i in ids),
                  f"实际 = {ids}")
            check("J6c 目标都是 npu_index=null（后端按卡位策略挑卡）",
                  all(t.get("npu_index") is None for t in res["targets"]),
                  f"实际 = {res['targets']!r}")

            # ---- J7 无 JS 错误 ------------------------------------------ #
            check("J7 全程没有 JS 错误", not js_errors,
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
