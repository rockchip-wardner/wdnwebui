#!/usr/bin/env python3
"""USB HID card — end-to-end endpoint verify.

Exercises every public endpoint of ``/api/usb-hid`` over a running
wdnwebui instance. Designed to be run after the gadget is bound and
``/dev/hidgN`` is writable by the wdnwebui process user.

Usage:
  python3 scripts/verify_usb_hid.py
  python3 scripts/verify_usb_hid.py --host 192.168.1.10
  python3 scripts/verify_usb_hid.py --ready-only    # skip writes

The script **assumes you are running it on the RK3588 board itself**
(the default ``--host 127.0.0.1``). Running it from a remote host
without the gadget reachable will fail every write — that's expected
behaviour, not a bug.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request


# --------------------------------------------------------------------------- #
# HTTP helper (no third-party dep)
# --------------------------------------------------------------------------- #
def _request(method: str, url: str, body: dict | None = None,
             timeout: float = 5.0) -> tuple[int, dict | str]:
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
            try:
                return r.status, json.loads(raw)
            except json.JSONDecodeError:
                return r.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw


def _report(label: str, status: int, body, ok: bool, *, expect: int = 200) -> str:
    """Format a single line of the verification table."""
    mark = "OK" if (ok and status == expect) else "FAIL"
    detail = ""
    if isinstance(body, dict):
        if "detail" in body:
            detail = f" — {body['detail'][:80]}"
        elif "ok" in body:
            detail = f" — ok={body['ok']}"
    return f"  [{mark:4}] {label:32} HTTP {status}{detail}"


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--ready-only", action="store_true",
                   help="Skip write endpoints (snapshot only)")
    args = p.parse_args()
    base = f"http://{args.host}:{args.port}/api/usb-hid"

    print(f"USB HID endpoint verify → {base}")
    print()

    fail = 0

    # 1. snapshot
    status, body = _request("GET", f"{base}/")
    print(_report("GET /", status, body, isinstance(body, dict)
                  and body.get("present") is True))
    if not isinstance(body, dict) or not body.get("present"):
        fail += 1
        print("  ! gadget not present — run scripts/setup_usb_hid_gadget.sh")
        return 1
    if not body.get("ready"):
        print(f"  ! ready=false ({body['devices']}) — verify chardev perms")
        if args.ready_only:
            return 0
        # Continue anyway — write endpoints will return 502 if /dev/hidgN
        # is not writable. That's a valid result to report.

    # 2. keyboard release (always safe)
    status, body = _request("POST", f"{base}/keyboard/release")
    print(_report("POST /keyboard/release", status, body, status == 200))
    if status != 200: fail += 1

    if args.ready_only:
        print()
        print(f"  {fail} failures")
        return fail

    # 3. type "hello"  (US layout, all lowercase)
    status, body = _request("POST", f"{base}/keyboard/type",
                            {"text": "hello", "hold_ms": 8.0})
    ok = status == 200 and isinstance(body, dict) \
        and body.get("chars_typed") == 5 and body.get("bytes_sent", 0) > 0
    print(_report("POST /keyboard/type (hello)", status, body, ok))
    if not ok: fail += 1

    # 4. type uppercase + shifted punct
    status, body = _request("POST", f"{base}/keyboard/type",
                            {"text": "Hi!", "hold_ms": 8.0})
    ok = status == 200 and isinstance(body, dict) \
        and body.get("chars_typed") == 3
    print(_report("POST /keyboard/type (Hi!)", status, body, ok))
    if not ok: fail += 1

    # 5. type non-ASCII → expect 400
    status, body = _request("POST", f"{base}/keyboard/type",
                            {"text": "中文"})
    ok = status == 400 and isinstance(body, dict) and "detail" in body
    print(_report("POST /keyboard/type (中文) → 400", status, body, ok,
                  expect=400))
    if not ok: fail += 1

    # 6. tap ESC key (0x29)
    status, body = _request("POST", f"{base}/keyboard/tap",
                            {"key": "ESC"})
    ok = status == 200 and isinstance(body, dict) and body.get("keycode") == 0x29
    print(_report("POST /keyboard/tap (ESC)", status, body, ok))
    if not ok: fail += 1

    # 7. hotkey Ctrl-C
    status, body = _request("POST", f"{base}/keyboard/hotkey",
                            {"keys": ["c"], "modifier": "CTRL"})
    ok = status == 200 and isinstance(body, dict) and body.get("modifier", 0) & 0x01
    print(_report("POST /keyboard/hotkey (Ctrl-C)", status, body, ok))
    if not ok: fail += 1

    # 8. digitizer move
    status, body = _request("POST", f"{base}/digitizer/move",
                            {"x": 100, "y": 200})
    ok = status == 200 and isinstance(body, dict) and body.get("bytes_sent", 0) > 0
    print(_report("POST /digitizer/move (100,200)", status, body, ok))
    if not ok: fail += 1

    # 9. digitizer click
    status, body = _request("POST", f"{base}/digitizer/click",
                            {"x": 640, "y": 360, "press_ms": 12.0})
    ok = status == 200 and isinstance(body, dict) and body.get("bytes_sent", 0) > 0
    print(_report("POST /digitizer/click (640,360)", status, body, ok))
    if not ok: fail += 1

    # 10. digitizer leave
    status, body = _request("POST", f"{base}/digitizer/leave")
    ok = status == 200 and isinstance(body, dict) and body.get("ok") is True
    print(_report("POST /digitizer/leave", status, body, ok))
    if not ok: fail += 1

    # 11. reset (panic-stop)
    status, body = _request("POST", f"{base}/reset")
    ok = status == 200 and isinstance(body, dict) and body.get("ok") is True
    print(_report("POST /reset", status, body, ok))
    if not ok: fail += 1

    # 12. /desk
    status, body = _request("POST", f"{base}/desk",
                            {"width": 1920, "height": 1080})
    ok = status == 200 and isinstance(body, dict) \
        and body.get("width") == 1920 and body.get("height") == 1080
    print(_report("POST /desk (1920x1080)", status, body, ok))
    if not ok: fail += 1

    print()
    print(f"  {fail} failure(s)" if fail else "  all checks passed ✓")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
