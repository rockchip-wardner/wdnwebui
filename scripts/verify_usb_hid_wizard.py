"""Playwright check: does the USB HID wizard's source step show the
hdmiin live preview grid?

Steps:
  1. Open http://localhost:8000
  2. Inject diagnostic hooks (capture wizard:* events)
  3. Navigate to #usb-hid (boot is done, ROUTER is ready)
  4. Wait for wizard:mounted (tryMount fired) or wizard:skipped
  5. Click 下一步 to leave env_check → arrive at source step
  6. Wait 4s for the auto-click + hdmiin capture to start + first JPEG
  7. Check: is #usb-hid-grid inside the wizard body? Is its
     <img> showing a real JPEG (not the placeholder)?
  8. Print a verdict and dump the relevant DOM snippet
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright


class Verdict(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def find_chromium() -> tuple[str, str] | None:
    """Find a headless-shell first (smallest dep set), fall back to chrome.

    Returns (path, kind) where kind is "headless_shell" or "chromium".
    """
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


def main() -> int:
    print("main: enter", flush=True)
    found = find_chromium()
    print(f"main: found={found}", flush=True)
    if found is None:
        print("ERROR: no usable chromium under ~/.cache/ms-playwright",
              file=sys.stderr, flush=True)
        return 5
    exe, kind = found
    print(f"using {kind}: {exe}", flush=True)
    try:
        run(exe)
        print("main: run() returned", flush=True)
    except Verdict as v:
        print(f"main: Verdict({v.code}, {v})", flush=True)
        return v.code
    except Exception as exc:
        print(f"EXCEPTION: {exc!r}", file=sys.stderr, flush=True)
        import traceback
        traceback.print_exc()
        return 99
    return 0


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

        page.on("console", lambda m: print(f"  [console.{m.type}] {m.text}"))
        page.on("pageerror", lambda e: print(f"  [pageerror] {e}"))

        print("=== step 1: open homepage ===")
        # ⚠️ 先清掉后端记的 active_app —— 开机时 ActivateManager.bootRestore()
        # 会把它还原成 location.hash ⇒ **上一张卡**（比如别人刚用过的
        # image-editor）的向导会先挂上，再在切到 #usb-hid 时被卸载 ⇒
        # 本脚本断言的「第一个向导事件 = wizard:mounted」看到的是
        # `wizard:unmounted(别的卡)` ⇒ **假失败**（2026-11-06 实测）。
        # 测试不该依赖「上一个用过哪张卡」这种全局状态。
        try:
            ctx.request.delete("http://localhost:8000/api/compute/active-app")
        except Exception as e:  # noqa: BLE001
            print(f"  [warn] 清 active_app 失败（忽略）: {e}")
        page.goto("http://localhost:8000", wait_until="networkidle", timeout=15000)

        # Wait for boot to finish
        page.wait_for_function(
            "() => window.__APP_MANIFESTS__ && window.ROUTER"
            "        && window.WizardRegistry && window.WizardState",
            timeout=10000,
        )
        # Inject event capture
        page.evaluate("""() => {
            window.__wizardEvents = [];
            window.__errors = [];
            window.addEventListener('error', (e) => {
                window.__errors.push({kind:'error', msg:String(e.message)});
            });
            for (const ev of ['wizard:mounted', 'wizard:skipped', 'wizard:unmounted']) {
                window.addEventListener(ev, (e) => {
                    window.__wizardEvents.push({kind:ev, detail:e && e.detail, t:Date.now()});
                });
            }
        }""")

        print("=== step 2: navigate to #usb-hid via hash ===")
        page.evaluate("() => { window.location.hash = '#usb-hid'; }")

        # Wait for wizard to actually mount (or skip)
        print("=== step 3: wait for wizard:mounted / wizard:skipped ===")
        page.wait_for_function(
            "() => window.__wizardEvents && window.__wizardEvents.length > 0",
            timeout=10000,
        )
        events = page.evaluate("() => window.__wizardEvents")
        print(f"  wizard events: {events}")
        kind_ev = events[0]["kind"]
        if kind_ev == "wizard:skipped":
            print(f"  ✗ wizard was skipped (reason: {events[0].get('detail', {}).get('reason')})")
            raise Verdict(10, f"wizard skipped: {events[0].get('detail')}")
        if kind_ev != "wizard:mounted":
            raise Verdict(11, f"unexpected first event: {kind_ev}")

        # Wizard should be in DOM. Wait briefly for source step to render.
        # Wizard opens on env_check first. Click 下一步 to go to source.
        print("=== step 4: click 下一步 to leave env_check ===")
        # Wizard renders the buttons. The 下一步 button is in the foot.
        page.wait_for_selector(".wd-wizard-foot button", timeout=5000)
        page.locator(".wd-wizard-foot button").filter(has_text="下一步").first.click()

        # Wait a moment for source step to render + auto-click chain to run
        print("=== step 5: source step — wait for auto-click + capture + grid in wizard ===")
        time.sleep(4.0)

        # Probe the wizard body
        body_html = page.locator(".wd-wizard-body").first.inner_html()
        print(f"  wizard body HTML length: {len(body_html)} chars")
        has_canvas_wrap = "wd-wizard-canvas-wrap" in body_html
        has_open_btn = "wd-wizard-source-action-btn" in body_html
        has_grid_in_wizard = page.locator(".wd-wizard-body #usb-hid-grid").count() > 0
        img_in_wizard = page.locator(".wd-wizard-body #usb-hid-grid img").count()
        active_in_wizard = page.evaluate(
            "() => { const g = document.querySelector('.wd-wizard-body #usb-hid-grid');"
            "        if (!g) return null;"
            "        const img = g.querySelector('img');"
            "        if (!img) return {grid:true, hasImg:false};"
            "        return {grid:true, hasImg:true, src_len:img.src.length, src_starts:img.src.slice(0,30), isActive:img.classList.contains('is-active')}; }"
        )

        print(f"  wizard body contains canvas-wrap: {has_canvas_wrap}")
        print(f"  wizard body contains open button: {has_open_btn}")
        print(f"  wizard body contains #usb-hid-grid: {has_grid_in_wizard}")
        print(f"  wizard body has img inside grid: {img_in_wizard}")
        print(f"  img state: {active_in_wizard}")

        grid_doc = page.evaluate(
            "() => { const g = document.getElementById('usb-hid-grid');"
            "        if (!g) return null;"
            "        const inWizard = g.closest('.wd-wizard-body') ? true : false;"
            "        const inPage = g.closest('#page-usb-hid') ? true : false;"
            "        return { inWizard, inPage,"
            "                 parent: g.parentNode && g.parentNode.tagName + '.' + g.parentNode.className }; }"
        )
        print(f"  grid location: {grid_doc}")

        # Screenshot for visual record
        out = Path("/tmp/usb_hid_wizard.png")
        page.screenshot(path=str(out), full_page=True)
        print(f"  (screenshot: {out})")

        print()
        print("=== verdict ===")
        if active_in_wizard and active_in_wizard.get("isActive"):
            print("✓ grid is inside wizard AND img is active (frame shown)")
            raise Verdict(0, "OK")
        if has_grid_in_wizard and img_in_wizard and not active_in_wizard:
            print("✗ grid + img are in wizard BUT img not yet active — wait longer?")
            raise Verdict(2, "img not active")
        if has_grid_in_wizard and not img_in_wizard:
            print("✗ grid is in wizard but <img> missing — placeholder only")
            raise Verdict(3, "no img")
        if not has_grid_in_wizard:
            print("✗ grid NOT in wizard body — moveCanvasToWizard() didn't fire?")
            print("  body HTML (first 2000 chars):")
            print("  " + body_html[:2000].replace("\n", "\n  "))
            raise Verdict(4, "grid not in wizard")
        raise Verdict(1, "no marker matched — see screenshot")


if __name__ == "__main__":
    sys.exit(main())
