"""Playwright check: finish 步骤不能是死胡同。

用户实测的两个症状:
  1. 进「智能电脑控制」卡直接就是「完成配置」页
  2. 完成配置页没有「上一页」按钮,回不到向导第一页

复现路径(本脚本逐步走一遍;算力快照 mock 掉,理由见下面 on_compute):
  env(0) → task(1) → finish(2) → 点右上角返回(不清 step)
  → 重新进卡 → 断言落在哪一步 + footer 有哪些按钮

覆盖两个层面:
  A. footer 结构:finish 步必须有「上一步」+「跳过向导」
  B. 持久化语义:finish 不该被 _lsSaveStep 记成"停留位置",
     所以重进卡应回到最后一个**有输入**的步骤(task),而不是 finish

用法: .venv/bin/python scripts/verify_wizard_finish_trap.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

APP_ID = "usb-hid"
BASE = "http://localhost:8000"


class Verdict(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def find_chromium() -> tuple[str, str] | None:
    """Find a headless-shell first (smallest dep set), fall back to chrome."""
    import os
    import subprocess

    cache = Path(os.path.expanduser("~/.cache/ms-playwright"))
    if not cache.is_dir():
        return None
    for entry in cache.iterdir():
        if not entry.name.startswith("chromium_headless_shell"):
            continue
        cand = entry / "chrome-headless-shell-linux-arm64" / "chrome-headless-shell"
        if cand.is_file() and os.access(cand, os.X_OK):
            r = subprocess.run(["ldd", str(cand)], capture_output=True, text=True)
            if "not found" not in r.stdout:
                return (str(cand), "headless_shell")
    for entry in cache.iterdir():
        if not entry.name.startswith("chromium-") or entry.name.startswith("chromium_headless"):
            continue
        for rel in ("chrome-linux/chrome", "chrome-linux-arm64/chrome",
                    "chrome-linux64/chrome"):
            cand = entry / rel
            if cand.is_file() and os.access(cand, os.X_OK):
                r = subprocess.run(["ldd", str(cand)], capture_output=True, text=True)
                if "not found" not in r.stdout:
                    return (str(cand), "chromium")
    return None


def foot_labels(page) -> list[str]:
    """footer 上所有按钮的可见文本,按 DOM 顺序。"""
    return page.evaluate(
        "() => Array.from(document.querySelectorAll('#wd-wizard-foot button'))"
        ".map(b => (b.textContent || '').trim()).filter(Boolean)"
    )


def step_index(page) -> int:
    """当前 stepper 里高亮(第 N 个)的序号 → 0 基 idx。"""
    return page.evaluate(
        "() => { const a = document.querySelectorAll('#wd-wizard-stepper .wd-wizard-step-dot');"
        "  for (let i = 0; i < a.length; i++)"
        "    if (a[i].classList.contains('wd-wizard-step-active')) return i;"
        "  return -1; }"
    )


def enter_card(page) -> None:
    page.evaluate("(id) => { window.location.hash = '#' + id; }", APP_ID)
    page.wait_for_selector("#wd-wizard-root-usb-hid:not(.hidden)", timeout=10000)
    # mount() 里容器先 remove("hidden"),**之后**才 await _probeEnv(cfg, app)
    # 再 _renderShell + _gotoStep。所以「容器可见」有一小段窗口时 foot /
    # stepper 还是空的 —— 只等容器会读到空壳,必须等 stepper 真渲染出圆点。
    page.wait_for_selector("#wd-wizard-stepper .wd-wizard-step-dot", timeout=10000)


def run(exe: str) -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            executable_path=exe,
            args=["--no-sandbox", "--disable-setuid-sandbox",
                  "--disable-dev-shm-usage"],
        )
        ctx = browser.new_context(viewport={"width": 1400, "height": 900})
        page = ctx.new_page()
        page.on("pageerror", lambda e: print(f"  [pageerror] {e}"))

        # mock 掉算力快照：脚本要验的是 **footer 结构 + 持久化语义**，
        # 与 env 步的能力判定无关，但它得先从 env 步走到 task 步。
        # 真板子上没人同时提供 usb-hid 要求的整组能力（swcap_vision_chat
        # + swcap_tool_call，且必须**同一个实例**）→ env 步判 fail →
        # 「下一步」被禁用 → 脚本连第一步都走不出去。
        # 快照 mock 掉就跟板子上跑什么模型无关了。
        def on_compute(route):
            route.fulfill(
                status=200, headers={"Content-Type": "application/json"},
                body=json.dumps({
                    "instances": [{
                        "model_id": "mock-vlm", "npu_index": 0, "npu_indices": [0],
                        "status": "running", "_source": "spawned",
                        "capability_types": ["swcap_vision_chat", "swcap_tool_call"],
                    }],
                    "card_count": 1, "model_cards": [0],
                    "cards": [{"npu_index": 0}],
                }))

        def on_active_app(route):
            # 进卡片时框架会 PUT /api/compute/active-app，后端会往
            # compute.autostart 写配置 —— 回归脚本不该改生产配置。
            route.fulfill(status=200, headers={"Content-Type": "application/json"},
                          body=json.dumps({"ok": True}))

        page.route("**/api/compute/active-app", on_active_app)
        page.route("**/api/compute", on_compute)

        print("=== 1. open homepage, wipe any leftover usb-hid wizard state ===")
        # 用 domcontentloaded 而不是 networkidle —— 首页常驻 WS 连接
        # (视频/状态推送),networkidle 永远等不到,必然 20s 超时。
        page.goto(BASE, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_function(
            "() => window.__APP_MANIFESTS__ && window.ROUTER"
            "        && window.WizardRegistry && window.WizardState",
            timeout=10000,
        )
        page.evaluate(
            """(id) => {
                const base = 'wdnwebui.wizard.' + id + '.';
                for (const k of Object.keys(localStorage)) {
                  if (k.startsWith(base)) localStorage.removeItem(k);
                }
            }""",
            APP_ID,
        )

        print("=== 2. enter card → expect step 0 (env) ===")
        enter_card(page)
        got = step_index(page)
        print(f"  step={got} foot={foot_labels(page)}")
        if got != 0:
            raise Verdict(10, f"fresh entry should land on step 0, got {got}")

        print("=== 3. 下一步 → step 1 (task) ===")
        page.locator("#wd-wizard-foot button", has_text="下一步").first.click()
        page.wait_for_timeout(300)
        got = step_index(page)
        print(f"  step={got} foot={foot_labels(page)}")
        if got != 1:
            raise Verdict(11, f"expected step 1, got {got}")

        print("=== 4. 下一步 → step 2 (finish) ===")
        page.locator("#wd-wizard-foot button", has_text="下一步").first.click()
        page.wait_for_timeout(300)
        got = step_index(page)
        labels = foot_labels(page)
        print(f"  step={got} foot={labels}")

        # ---- 断言 A:finish 页必须有回退出口 ----
        if "上一步" not in labels:
            raise Verdict(12, f"finish step has no 上一步 button: {labels}")
        if "完成配置" not in labels:
            raise Verdict(13, f"finish step lost 完成配置 button: {labels}")
        print("  ✓ finish footer = 上一步 + 跳过向导 + 完成配置")

        print("=== 5. 上一步 → must go back to step 1 (task) ===")
        page.locator("#wd-wizard-foot button", has_text="上一步").first.click()
        page.wait_for_timeout(300)
        got = step_index(page)
        print(f"  step={got} foot={foot_labels(page)}")
        if got != 1:
            raise Verdict(14, f"上一步 from finish should reach step 1, got {got}")
        print("  ✓ finish is no longer a dead end")

        # 回到 finish,用**右上角返回**走一遍(那是保留已填字段、
        # 但故意不清 step 的那条路径 —— 也就是用户复现的那条)
        print("=== 6. forward to finish again, then 右上角返回 ===")
        page.locator("#wd-wizard-foot button", has_text="下一步").first.click()
        page.wait_for_timeout(300)
        if step_index(page) != 2:
            raise Verdict(15, "expected to be back on finish")
        # head 返回按钮只有 class,没有 id(_renderShell 里 _ce("button",
        # WIZARD_NS + "head-back-btn", "返回"),没设 .id)
        page.locator(".wd-wizard-head-back-btn").first.click()
        page.wait_for_timeout(600)
        stored = page.evaluate(
            "(id) => localStorage.getItem('wdnwebui.wizard.' + id + '.step')", APP_ID
        )
        print(f"  localStorage .step after leaving from finish = {stored!r}")

        # ---- 断言 B:finish 不该被记成"停留位置" ----
        if stored is not None:
            raise Verdict(16, f"finish step should NOT persist .step, got {stored!r}")
        print("  ✓ finish did not persist a resume position")

        print("=== 7. re-enter card → must NOT land on finish ===")
        enter_card(page)
        got = step_index(page)
        labels = foot_labels(page)
        print(f"  step={got} foot={labels}")
        if got == 2:
            raise Verdict(17, "re-entry landed on finish again — trap still present")
        if got not in (0, 1):
            raise Verdict(18, f"unexpected step after re-entry: {got}")

        # 若回到了 task(1),再按一次「上一步」必须能到 env(0)
        if got == 1:
            print("=== 8. 上一步 from restored step 1 → step 0 ===")
            page.locator("#wd-wizard-foot button", has_text="上一步").first.click()
            page.wait_for_timeout(300)
            got = step_index(page)
            print(f"  step={got} foot={foot_labels(page)}")
            if got != 0:
                raise Verdict(19, f"expected step 0, got {got}")
        print("  ✓ user can reach wizard step 1 again")

        page.screenshot(path="/tmp/wizard_finish_trap.png", full_page=True)
        print("  (screenshot: /tmp/wizard_finish_trap.png)")


def main() -> int:
    found = find_chromium()
    if found is None:
        print("ERROR: no usable chromium under ~/.cache/ms-playwright",
              file=sys.stderr)
        return 5
    exe, kind = found
    print(f"using {kind}: {exe}", flush=True)
    try:
        run(exe)
    except Verdict as v:
        print(f"\nVERDICT FAIL({v.code}): {v}", flush=True)
        return v.code
    except Exception as exc:  # noqa: BLE001
        print(f"\nEXCEPTION: {exc!r}", file=sys.stderr, flush=True)
        import traceback
        traceback.print_exc()
        return 99
    print("\nVERDICT OK", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
