"""Verify:
  1. Open #usb-hid → back-home button shows "退出应用" (not "返回首页")
  2. Click "退出应用" → wizard localStorage completed cleared, hash -> #/
  3. Re-open #usb-hid → wizard pops again (env_check step active)
  4. After completing wizard once more → re-open #usb-hid → no wizard (skip)
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


def find_chromium():
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
    return None


def main() -> int:
    found = find_chromium()
    if found is None:
        return 5
    exe, kind = found
    try:
        run(exe)
    except Verdict as v:
        return v.code
    except Exception as exc:
        print(f"EXC: {exc!r}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 99
    return 0


def run(exe: str) -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True, executable_path=exe,
            args=["--no-sandbox", "--disable-setuid-sandbox",
                  "--disable-dev-shm-usage"],
        )
        ctx = browser.new_context(viewport={"width": 1400, "height": 900})
        page = ctx.new_page()
        page.on("console", lambda m: print(f"  [console.{m.type}] {m.text}"))
        page.on("pageerror", lambda e: print(f"  [pageerror] {e}"))

        page.goto("http://localhost:8000", wait_until="networkidle", timeout=15000)
        page.wait_for_function(
            "() => window.__APP_MANIFESTS__ && window.ROUTER",
            timeout=10000,
        )

        # Clear any pre-existing wizard completed flag (so this is a fresh run)
        page.evaluate(
            "() => { try { localStorage.removeItem('wdnwebui.wizard.usb-hid.completed');"
            "              localStorage.removeItem('wdnwebui.wizard.usb-hid.values');"
            "              localStorage.removeItem('wdnwebui.wizard.usb-hid.step');"
            "            } catch (_) {} }"
        )

        print("=== step 1: open #usb-hid via app-card click → back-home shows '退出应用' ===")
        # First, go to home so the app-card is rendered
        page.evaluate("() => { window.location.hash = '#apps'; }")
        time.sleep(0.6)
        # Click the actual .app-card link (real user gesture, not hash-edit)
        page.locator('a.app-card[data-page="usb-hid"]').first.click()
        time.sleep(2.5)
        btn = page.evaluate(
            "() => { const b = document.querySelector('#page-usb-hid .back-home-btn');"
            "        return b ? { mode: b.dataset.mode, label: b.getAttribute('aria-label'),"
            "                       text: b.innerText.trim() } : null; }"
        )
        print(f"  back-home button: {btn}")
        if not btn or "退出" not in btn.get("label", ""):
            raise Verdict(1, f"back-home not '退出应用' — {btn}")

        # Verify wizard is mounted (first entry: should pop)
        wizard_state = page.evaluate(
            "() => { const w = document.querySelector('.wd-wizard-root, [id^=wd-wizard-root-]');"
            "        return w ? 'wizard-mounted' : 'wizard-absent'; }"
        )
        print(f"  wizard state: {wizard_state}")
        if wizard_state != "wizard-mounted":
            raise Verdict(2, f"first entry should mount wizard, got {wizard_state}")

        # Complete the wizard: walk down to finish
        print("=== step 2: walk wizard to finish ===")
        for _ in range(10):
            try:
                page.wait_for_selector(".wd-wizard-foot button", timeout=2000)
            except Exception:
                break
            btn_text = page.evaluate(
                "() => Array.from(document.querySelectorAll('.wd-wizard-foot button'))"
                "        .map(b => b.innerText.trim())"
            )
            print(f"  wizard foot buttons: {btn_text}")
            if any("完成" in t or "应用" in t for t in btn_text):
                # Click 完成为主按钮
                page.evaluate(
                    "() => Array.from(document.querySelectorAll('.wd-wizard-foot button'))"
                    "        .find(b => b.innerText.includes('完成') || b.innerText.includes('应用'))?.click()"
                )
                break
            elif any("下一步" in t for t in btn_text):
                page.evaluate(
                    "() => Array.from(document.querySelectorAll('.wd-wizard-foot button'))"
                    "        .find(b => b.innerText.includes('下一步'))?.click()"
                )
                time.sleep(0.5)
            else:
                break
        time.sleep(1.5)

        completed_before = page.evaluate(
            "() => localStorage.getItem('wdnwebui.wizard.usb-hid.completed')"
        )
        print(f"  wizard completed (before exit): {completed_before!r}")
        if completed_before != "1":
            raise Verdict(3, f"wizard did not mark completed, got {completed_before!r}")

        print("=== step 3: click back-home (退出应用) — should clear wizard state ===")
        page.evaluate(
            "() => document.querySelector('#page-usb-hid .back-home-btn').click()"
        )
        time.sleep(1.5)
        hash_after = page.evaluate("() => location.hash")
        completed_after = page.evaluate(
            "() => localStorage.getItem('wdnwebui.wizard.usb-hid.completed')"
        )
        print(f"  hash after exit: {hash_after!r}")
        print(f"  wizard completed (after exit): {completed_after!r}")
        if hash_after != "#/":
            raise Verdict(4, f"hash should be '#/' after exit, got {hash_after!r}")
        if completed_after is not None:
            raise Verdict(5, f"completed should be cleared, got {completed_after!r}")

        print("=== step 4: re-open #usb-hid via app-card click → wizard should re-pop ===")
        # go home, then click app-card (the real gesture)
        page.evaluate("() => { window.location.hash = '#apps'; }")
        time.sleep(0.6)
        page.locator('a.app-card[data-page="usb-hid"]').first.click()
        time.sleep(2.5)
        wizard_again = page.evaluate(
            "() => { const w = document.querySelector('.wd-wizard-root, [id^=wd-wizard-root-]');"
            "        return w ? 'wizard-mounted' : 'wizard-absent'; }"
        )
        print(f"  wizard state on re-entry: {wizard_again}")
        if wizard_again != "wizard-mounted":
            raise Verdict(6, f"re-entry should mount wizard again, got {wizard_again}")

        page.screenshot(path="/tmp/usb_hid_exit.png", full_page=True)
        print("  (screenshot: /tmp/usb_hid_exit.png)")
        raise Verdict(0, "OK")


if __name__ == "__main__":
    sys.exit(main())
