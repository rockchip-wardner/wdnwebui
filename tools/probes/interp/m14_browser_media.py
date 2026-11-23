#!/usr/bin/env python3
"""M14 —— **浏览器里「有翻译但没声音」**的诊断探针。

复现「音频文件源 + 跟播放」的完整链路，把**媒体元素**和**播放头**的
内部状态全 dump 出来：

* ``#asr-audio-player`` 的 ``src`` / ``readyState`` / ``paused`` /
  ``currentTime`` / ``error``；
* 服务端收到的播放头（``/interp/status`` 的 ``playhead_sec``）；
* 句子有没有被放行（``submitted`` / ``pending`` / ``stalled_released``）。

⇒ 分辨三种可能：

1. **元素没有源**（``src`` 空 / 加载失败）⇒ 播放头永远是 0；
2. **元素有源但没播**（``paused=true``）⇒ 自动播放被浏览器策略拦了；
3. **播了但句子没放行** ⇒ 放行器（Gate）的问题。

⚠️ 用**严格自动播放策略**启动 Chromium（``--autoplay-policy=
user-gesture-required``）⇒ 模拟真实桌面浏览器（默认会拦无手势的
自动播放）⇒ headless 默认策略太宽松，测不出问题。

⚠️ **Playwright 的 ``click()`` 自带用户手势** ⇒ 即便开了严格策略，
``play()`` 也会成功 ⇒ 「自动播放被拦」这条路径**测不出来**
⇒ 第二场**直接把 ``play()`` 改成拒绝**（等价于浏览器策略拦截）。

跑法::

    .venv/bin/python tools/probes/interp/m14_browser_media.py
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

#: ⚠️ **模拟「浏览器按自动播放策略拒绝了 play()」** ——
#: Playwright 的 click() 自带用户手势 ⇒ 不这么做就测不出这条路径。
BLOCK_AUTOPLAY_JS = """
(() => {
  HTMLMediaElement.prototype.play = () => Promise.reject(new DOMException(
    'Autoplay is not allowed in this browser.', 'NotAllowedError'));
})();
"""


def api(method: str, path: str, payload=None, timeout: int = 120):
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
        return e.code, {"raw": e.read().decode("utf-8", "replace")}
    except Exception as e:  # noqa: BLE001
        return 0, {"error": str(e)}


def media_state(page) -> dict:
    return page.eval_on_selector(
        "#asr-audio-player",
        """el => ({
             src: el.currentSrc || el.src || '',
             readyState: el.readyState,
             networkState: el.networkState,
             paused: el.paused,
             ended: el.ended,
             currentTime: el.currentTime,
             duration: el.duration,
             error: el.error ? (el.error.code + ':' + el.error.message) : '',
           })""",
    )


def open_card(page, *, block_autoplay: bool = False) -> None:
    """进卡片页 + 跳过向导（返回后页面已就绪）。"""
    page.goto(f"{BASE}/#{CARD}", wait_until="networkidle")
    page.wait_for_timeout(2500)
    skip = page.locator("#wd-wizard-root-qwen3-asr .wd-wizard-skip-btn").first
    if skip.count() and skip.is_visible():
        skip.click()
        page.wait_for_timeout(1500)
    page.wait_for_selector("#page-qwen3-asr:not(.hidden)", timeout=20000)
    page.wait_for_selector("#asr-interp-block", timeout=30000)
    page.wait_for_timeout(1000)


def main() -> int:
    api("POST", "/api/qwen3-asr/interp/stop")
    api("POST", "/api/tts/queue/clear")
    time.sleep(1)

    with sync_playwright() as pw:
        # ⚠️ **严格自动播放策略** —— 真实桌面浏览器的默认行为。
        browser = pw.chromium.launch(args=[
            "--no-sandbox",
            "--autoplay-policy=user-gesture-required",
        ])

        # ============================================================== #
        # 第一场：**有用户手势** ⇒ 自动播放成功 ⇒ 播放头在走
        # ============================================================== #
        print("=== 第一场：有用户手势（点「开始同传」）⇒ 媒体应该播起来 ===")
        ctx = browser.new_context(ignore_https_errors=True,
                                  viewport={"width": 1440, "height": 1000})
        install_active_app_guard(ctx)
        install_active_app_spoof(ctx)
        page = ctx.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        open_card(page)
        print("--- 1. 填文件路径（设备端） ---")
        page.fill("#asr-source", SRC_WAV)
        page.wait_for_timeout(1500)
        st = media_state(page)
        print("  媒体元素:", json.dumps(st, ensure_ascii=False))
        print(f"  src 有值: {bool(st.get('src'))} · readyState={st.get('readyState')}"
              f" · error={st.get('error')!r}")

        print("--- 2. 点「开始同传」（用户手势） ---")
        page.click("#asr-interp-start")
        page.wait_for_timeout(2000)
        st = media_state(page)
        print("  媒体元素:", json.dumps(st, ensure_ascii=False))
        print(f"  paused={st.get('paused')} · currentTime={st.get('currentTime')}")
        print(f"  面板提示: {page.inner_text('#asr-interp-hint')!r}")

        print("--- 3. 等 6 秒，看播放头 / 放行 ---")
        for i in range(6):
            page.wait_for_timeout(1000)
            _s, sv = api("GET", "/api/qwen3-asr/interp/status")
            st = media_state(page)
            print(f"  t={i+1}s currentTime={st.get('currentTime'):.2f} "
                  f"paused={st.get('paused')} | ph={sv.get('playhead_sec')} "
                  f"stalled={sv.get('playhead_stalled')} "
                  f"pending={sv.get('pending')} sub={sv.get('submitted')}")

        print("--- 4. 开始识别（文件源）⇒ 句子应该被放行 ---")
        # ⚠️ **计数是累计的** ⇒ 先取基线（**在点识别之前**），下面看增量。
        _s, base1 = api("GET", "/api/qwen3-asr/interp/status")
        base1_sub = int(base1.get("submitted") or 0)
        page.click("#asr-start-btn")
        deadline = time.time() + 120
        sub = base1_sub
        sv: dict = {}
        while time.time() < deadline:
            _s, sv = api("GET", "/api/qwen3-asr/interp/status")
            sub = int(sv.get("submitted") or 0)
            if sub > base1_sub:
                break
            page.wait_for_timeout(1000)
        print(f"  ⇒ submitted {base1_sub} → {sub} · "
              f"segments={sv.get('segments')} "
              f"translated={sv.get('translated')} "
              f"lag={sv.get('lag_sec')} · {sv.get('tts_status')}")
        page.click("#asr-interp-stop")
        page.wait_for_timeout(1000)
        ctx.close()

        # ============================================================== #
        # 第二场：**自动播放被浏览器拦了** ⇒ 媒体不动 ⇒ 兜底放行
        #
        # ⚠️ 必须**真的跑一次识别**才有句子 ⇒ 否则没有东西可放行
        #    （第一版探针就栽在这儿：只点了开始同传 + 暂停媒体 ⇒
        #      页面根本没跑 /transcribe ⇒ pending 永远是 0）。
        # ============================================================== #
        print("\n=== 第二场：自动播放被拦 ⇒ 兜底放行（译文仍然出声） ===")
        ctx2 = browser.new_context(ignore_https_errors=True,
                                   viewport={"width": 1440, "height": 1000})
        install_active_app_guard(ctx2)
        install_active_app_spoof(ctx2)
        ctx2.add_init_script(BLOCK_AUTOPLAY_JS)
        page2 = ctx2.new_page()
        errors2: list[str] = []
        page2.on("pageerror", lambda e: errors2.append(str(e)))

        open_card(page2)
        page2.fill("#asr-source", SRC_WAV)
        page2.wait_for_timeout(1200)
        _s, base = api("GET", "/api/qwen3-asr/interp/status")
        base_sub = int(base.get("submitted") or 0)
        base_rel = int(base.get("stalled_released") or 0)
        print(f"  基线: submitted={base_sub} stalled_released={base_rel}")

        page2.click("#asr-interp-start")
        page2.wait_for_timeout(1500)
        st = media_state(page2)
        print(f"  媒体: paused={st.get('paused')} "
              f"currentTime={st.get('currentTime'):.2f} "
              f"error={st.get('error')!r}")
        print(f"  面板提示: {page2.inner_text('#asr-interp-hint')!r}")

        print("--- 开始识别 ⇒ 句子会被压住 ⇒ 10 s 后兜底放行 ---")
        page2.click("#asr-start-btn")
        released = False
        for i in range(40):
            page2.wait_for_timeout(1000)
            _s, sv = api("GET", "/api/qwen3-asr/interp/status")
            if i % 3 == 0 or int(sv.get("submitted") or 0) > base_sub:
                st = media_state(page2)
                print(f"  t={i+1}s currentTime={st.get('currentTime'):.2f} "
                      f"paused={st.get('paused')} | ph={sv.get('playhead_sec')} "
                      f"stalled={sv.get('playhead_stalled')} "
                      f"moved={sv.get('playhead_moved')} "
                      f"pending={sv.get('pending')} "
                      f"sub={sv.get('submitted')} "
                      f"兜底={sv.get('stalled_released')}")
            if int(sv.get("submitted") or 0) > base_sub:
                released = True
                print(f"  ⇒ ✅ 兜底放行生效（t={i+1}s，submitted "
                      f"{base_sub} → {sv.get('submitted')}，"
                      f"兜底计数 {base_rel} → {sv.get('stalled_released')}）")
                break
        if not released:
            print("  ⇒ ❌ 兜底没生效（句子还被压着）")
        print(f"  状态行: {page2.inner_text('#asr-interp-status')!r}")

        api("POST", "/api/qwen3-asr/interp/stop")
        api("POST", "/api/tts/queue/clear")
        ctx2.close()
        browser.close()

    print(f"\nJS 错误: {(errors + errors2)[:3]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
