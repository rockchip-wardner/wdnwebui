"""Repro: page reload → does the hdmiin grid become active again,
or does the user see '未连接 HDMI-IN' as before?

Steps:
  1. Open page, navigate to #usb-hid, let wizard mount
  2. Verify initial state OK (grid in wizard, img active)
  3. Reload the page (simulates browser refresh)
  4. After reload, watch grid/placeholder state at t+0.5s / 2s / 5s
  5. Verdict: did the grid get re-activated automatically, or is the
     placeholder still showing?
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

        print("=== open page + nav to #usb-hid ===")
        page.goto("http://localhost:8000", wait_until="networkidle", timeout=15000)
        page.wait_for_function(
            "() => window.__APP_MANIFESTS__ && window.ROUTER",
            timeout=10000,
        )
        page.evaluate("() => { window.location.hash = '#usb-hid'; }")
        # Let initial wizard mount + grid become active
        time.sleep(5)

        before = page.evaluate(
            "() => { const g = document.getElementById('usb-hid-grid');"
            "        const img = g && g.querySelector('img');"
            "        return { gridHasImage: g && g.classList.contains('has-image'),"
            "                 imgActive: img && img.classList.contains('is-active'),"
            "                 imgSrc: img && img.src.slice(0,30) }; }"
        )
        print(f"  before reload: {before}")

        print("=== RELOAD page ===")
        page.reload(wait_until="networkidle", timeout=15000)
        # Re-navigate (the reload resets hash to '' → home; nav back to usb-hid)
        page.wait_for_function(
            "() => window.__APP_MANIFESTS__ && window.ROUTER",
            timeout=10000,
        )
        # Capture *all* /api/usb-hid requests after reload
        reqs = []
        def on_req(req):
            if "/api/usb-hid" in req.url:
                reqs.append({"url": req.url, "method": req.method, "t": time.time()})
        page.on("request", on_req)
        resp_log = []
        def on_resp(resp):
            if "/api/usb-hid" in resp.url:
                resp_log.append({"url": resp.url, "status": resp.status, "t": time.time()})
        page.on("response", on_resp)

        page.evaluate("() => { window.location.hash = '#usb-hid'; }")

        # Diagnostic snapshot
        time.sleep(0.3)
        diag = page.evaluate(
            "() => { return { deviceElExists: !!document.getElementById('usb-hid-hdmiin-device'),"
            "                 pageUsbHidExists: !!document.getElementById('page-usb-hid'),"
            "                 pageHidden: document.getElementById('page-usb-hid')?.classList.contains('hidden'),"
            "                 summary: document.getElementById('usb-hid-hdmiin-summary')?.textContent,"
            "                 appJsLoaded: !!document.querySelector('script[src*=\"usb-hid/app.js\"]'),"
            "                 usbHidInWindow: typeof window.UsbHid,"
            "                 manifestHasUsbHid: window.__APP_MANIFESTS__?.cards?.some(c => c.id === 'usb-hid'),"
            "                 loadedApps: window.DemoGuard && Array.from(window.DemoGuard._loadedApps || []) }; }"
        )
        print(f"  diag after reload+nav: {diag}")

        # Watch the grid at multiple time points
        for t in [0.5, 2.0, 5.0, 8.0]:
            time.sleep(t - (locals().get('_prev', 0)))
            state = page.evaluate(
                "() => { const g = document.getElementById('usb-hid-grid');"
                "        const img = g && g.querySelector('img');"
                "        const ph = g && g.querySelector('.usb-hid-grid-placeholder');"
                "        const phVisible = ph && getComputedStyle(ph).display !== 'none';"
                "        return { gridHasImage: g && g.classList.contains('has-image'),"
                "                 imgActive: img && img.classList.contains('is-active'),"
                "                 imgSrcLen: img && img.src.length,"
                "                 imgSrcStart: img && img.src.slice(0,30),"
                "                 placeholderVisible: phVisible,"
                "                 pageHidden: document.getElementById('page-usb-hid')?.classList.contains('hidden'),"
                "                 summary: document.getElementById('usb-hid-hdmiin-summary')?.textContent }; }"
            )
            print(f"  t={t}s after reload: {state}")
            _prev = t

        print(f"  requests seen: {reqs}")
        print(f"  responses seen: {resp_log}")
        page.screenshot(path="/tmp/usb_hid_refresh.png", full_page=True)
        print(f"  (screenshot: /tmp/usb_hid_refresh.png)")

        # Bonus: switch to home then back, grid should remain active
        print("=== switch to home then back ===")
        page.evaluate("() => { window.location.hash = '#apps'; }")
        time.sleep(0.6)
        page.evaluate("() => { window.location.hash = '#usb-hid'; }")
        time.sleep(1.0)
        switch_state = page.evaluate(
            "() => { const g = document.getElementById('usb-hid-grid');"
            "        const img = g && g.querySelector('img');"
            "        return { gridHasImage: g && g.classList.contains('has-image'),"
            "                 imgActive: img && img.classList.contains('is-active'),"
            "                 imgSrcStart: img && img.src.slice(0,30) }; }"
        )
        print(f"  after switch roundtrip: {switch_state}")
        if not (switch_state["imgActive"]):
            raise Verdict(2, "switch-back broke the grid")

        # Final verdict
        final = page.evaluate(
            "() => { const g = document.getElementById('usb-hid-grid');"
            "        const img = g && g.querySelector('img');"
            "        return { hasImage: g && g.classList.contains('has-image'),"
            "                 imgActive: img && img.classList.contains('is-active') }; }"
        )
        if final["imgActive"]:
            print("✓ grid re-activated after reload")
            raise Verdict(0, "OK")
        print("✗ grid stayed hidden after reload — placeholder still showing")
        raise Verdict(1, "reload-broken")


if __name__ == "__main__":
    sys.exit(main())
