#!/usr/bin/env python3
"""U24 探针（v2）—— 直接复用 ``verify_asr_wizard.py`` 的辅助函数复现视频路径。

用法：
    .venv/bin/python tools/probes/interp/u24_probe.py            # 只跑视频路径
    .venv/bin/python tools/probes/interp/u24_probe.py --audio-first  # 先走一遍音频向导
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

SCRIPT = Path("/userdata/wdnwebui/scripts/verify_asr_wizard.py")
spec = importlib.util.spec_from_file_location("vaw", SCRIPT)
vaw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vaw)

NS = vaw.NS
APP = vaw.APP


def dump(page, tag: str) -> None:
    st = page.evaluate("""(NS) => {
        const root = document.getElementById(NS + 'root-qwen3-asr');
        const nb = root ? root.querySelector('#' + NS + 'next-btn') : null;
        const fb = root ? root.querySelector('#' + NS + 'finish-btn') : null;
        const inp = root ? root.querySelector('[data-field-id="asr-source"]') : null;
        const title = root ? root.querySelector('.' + NS + 'step-title') : null;
        const roots = Array.from(document.querySelectorAll(
            '[id^="' + NS + 'root-"]')).map(
            e => e.id + ':' + (e.classList.contains('hidden') ? 'hidden' : 'shown'));
        return {
            step: title ? title.textContent.trim() : '',
            nextExists: !!nb,
            nextDisabled: nb ? nb.disabled : null,
            nextText: nb ? nb.textContent.trim() : null,
            finishExists: !!fb,
            inputValue: inp ? inp.value : null,
            roots: roots,
            hash: location.hash,
        };
    }""", NS)
    print(f"[{tag}] {st}")


def main() -> int:
    audio_first = "--audio-first" in sys.argv
    with sync_playwright() as p:
        exe = vaw._find_chrome()
        browser = p.chromium.launch(executable_path=exe,
                                    args=["--ignore-certificate-errors"])
        ctx = browser.new_context(ignore_https_errors=True)
        page = ctx.new_page()
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e)))

        if audio_first:
            print("--- 先走一遍音频向导（W1..W6 的那条路）---")
            vaw.open_wizard(page)
            vaw.click_next(page)
            vaw.set_field(page, "asr-source", vaw.WAV)
            try:
                vaw.click_to_finish(page)
            except Exception as exc:
                print("  click_to_finish 失败，完整报错：")
                print(str(exc))
                dump(page, "音频流程失败时")
                print("字段节点数:",
                      page.evaluate(
                          "() => document.querySelectorAll("
                          "'[data-field-id=\"asr-source\"]').length"))
                print("所有 source 节点值:", page.evaluate(
                    "() => Array.from(document.querySelectorAll("
                    "'[data-field-id=\"asr-source\"]')).map(e => e.value)"))
                raise
            page.wait_for_selector(f"#{NS}root-{APP} #{NS}finish-btn", timeout=10000)
            page.click(f"#{NS}root-{APP} #{NS}finish-btn")
            vaw.wait_run_done(0, timeout=180)
            page.wait_for_timeout(1500)

        print("--- 视频路径 ---")
        page.evaluate("(id) => { window.location.hash = ''; }", APP)
        page.wait_for_timeout(500)
        vaw.open_wizard(page)
        dump(page, "向导打开后")
        vaw.click_next(page)
        dump(page, "到 source 步")
        vaw.set_field(page, "asr-source", vaw.MP4)
        page.wait_for_timeout(600)
        dump(page, "填完 mp4")
        # 点一下试试（不做 strict 超时）
        try:
            vaw.click_next(page)
            print("  next 点击成功")
        except Exception as exc:
            print("  next 点击失败:", str(exc).splitlines()[0])
        dump(page, "点完 next")
        print("JS errors:", errs[:5])
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
