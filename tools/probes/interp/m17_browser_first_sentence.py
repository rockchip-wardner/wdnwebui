#!/usr/bin/env python3
"""M17 —— **浏览器里「只播第一句」的逐秒复现**。

用户实测：浏览器里跑音频文件源，**只听到第一句**。

本探针**完全按用户的操作序列**走：

1. 打开 ASR 卡 → 音频文件源 → 填设备端路径；
2. 点「开始同传」（⇒ 自动播放媒体）；
3. 点「开始识别」。

然后**逐秒**记录：

* 媒体元素（``paused`` / ``currentTime`` / ``ended``）；
* 服务端播放头（``playhead_sec`` / ``playhead_stalled``）；
* 切句 / 翻译 / **送进队列** / **待放行**（``pending``）；
* TTS 队列深度 / 已播数 / 电平。

⇒ 抓「句子卡在哪一环」：切句？翻译？**放行**？播放？

用法::

    .venv/bin/python tools/probes/interp/m17_browser_first_sentence.py
"""
from __future__ import annotations

import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_SCRIPT_DIR = os.path.join(_ROOT, "scripts")
if _SCRIPT_DIR not in sys.path:
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
SRC_WAV = "/userdata/assets/uploads/asr_en.wav"


def api(method: str, path: str, payload=None, timeout: int = 120):
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        hdrs = {"Content-Type": "application/json"}
    else:
        hdrs = {}
    req = urllib.request.Request(BASE + path, data=data, headers=hdrs,
                                 method=method)
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            body = r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
    try:
        return json.loads(body or "{}")
    except Exception:  # noqa: BLE001
        return {"raw": body}


def main() -> int:
    api("POST", "/api/qwen3-asr/interp/stop")
    api("POST", "/api/tts/queue/clear")
    time.sleep(1.0)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=[
            "--no-sandbox",
            "--autoplay-policy=user-gesture-required",
        ])
        ctx = browser.new_context(ignore_https_errors=True,
                                  viewport={"width": 1440, "height": 1000})
        install_active_app_guard(ctx)
        install_active_app_spoof(ctx)
        page = ctx.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        page.goto(f"{BASE}/#{CARD}", wait_until="networkidle")
        page.wait_for_timeout(2500)
        skip = page.locator("#wd-wizard-root-qwen3-asr .wd-wizard-skip-btn").first
        if skip.count() and skip.is_visible():
            skip.click()
            page.wait_for_timeout(1500)
        page.wait_for_selector("#page-qwen3-asr:not(.hidden)", timeout=20000)
        page.wait_for_selector("#asr-interp-block", timeout=30000)
        page.wait_for_timeout(800)

        print("=== 用户操作序列：填路径 → 开始同传 → 开始识别 ===")
        page.fill("#asr-source", SRC_WAV)
        page.wait_for_timeout(1000)
        page.click("#asr-interp-start")
        page.wait_for_timeout(1000)
        page.click("#asr-start-btn")

        base = api("GET", "/api/qwen3-asr/interp/status")
        b_seg = int(base.get("segments") or 0)
        b_tr = int(base.get("translated") or 0)
        b_sub = int(base.get("submitted") or 0)
        print(f"基线: seg={b_seg} tr={b_tr} sub={b_sub}\n")

        prev = None
        for i in range(75):
            time.sleep(1.0)
            st = api("GET", "/api/qwen3-asr/interp/status")
            q = api("GET", "/api/tts/queue/status")
            media = page.eval_on_selector(
                "#asr-audio-player",
                "el => ({paused: el.paused, ended: el.ended, "
                "t: el.currentTime})")
            row = {
                "t": i + 1,
                "media": f"{media['t']:.1f}{'⏸' if media['paused'] else ''}"
                         f"{'⏹' if media['ended'] else ''}",
                "ph": st.get("playhead_sec"),
                "stalled": st.get("playhead_stalled"),
                "seg": int(st.get("segments") or 0) - b_seg,
                "tr": int(st.get("translated") or 0) - b_tr,
                "sub": int(st.get("submitted") or 0) - b_sub,
                "pend": st.get("pending"),
                "qd": q.get("depth"),
                "qdone": int(q.get("done") or 0),
                "peak": q.get("last_peak"),
                "err": (st.get("last_error") or "")[:40],
            }
            if row != prev:
                print("  ", json.dumps(row, ensure_ascii=False))
                prev = row

        st = api("GET", "/api/qwen3-asr/interp/status")
        print("\n最终:", json.dumps({
            "segments": int(st.get("segments") or 0) - b_seg,
            "translated": int(st.get("translated") or 0) - b_tr,
            "submitted": int(st.get("submitted") or 0) - b_sub,
            "pending": st.get("pending"),
            "stalled_released": st.get("stalled_released"),
            "playhead_moved": st.get("playhead_moved"),
            "playhead_sec": st.get("playhead_sec"),
        }, ensure_ascii=False))
        print("状态行:", page.inner_text("#asr-interp-status"))
        print("提示:", page.inner_text("#asr-interp-hint"))
        print("JS 错误:", errors[:3])
        api("POST", "/api/qwen3-asr/interp/stop")
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
