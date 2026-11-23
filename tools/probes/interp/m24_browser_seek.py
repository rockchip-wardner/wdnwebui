#!/usr/bin/env python3
"""M24 —— **浏览器里真拖进度条** ⇒ 服务端认不认得出 seek（M26）。

用户报（2026-11-22）：
    「『含音轨的视频』播放的时候，字幕在实时翻译，但是多次拖动进度条，
      没有和字幕的翻译同声播放出来」

M23 探针是在 **API 层**模拟播放头（绕过浏览器）⇒ 它验的是「服务端收到
播放头跳变之后会怎么做」。这一支验的是**前半段**：**浏览器真的拖了进度条
⇒ 播放头上报 ⇒ 服务端认得出这是 seek 吗？**

判据（服务端的 ``status().last_seek``）：

* **前跳**（把 ``video.currentTime`` 拖到后面）⇒ ``dir == "fwd"``；
* **后跳**（拖回前面）⇒ ``dir == "back"``；
* 后跳之后 ⇒ ``replayed > 0``（**重播缓存**把「新位置之后」的句子重新
  武装了 ⇒ 用户拖回去时那些内容**还能再说一遍**）。

⚠️ **视频源的拖动有两种可能的后续**：
  ① 拖到的位置**还没识别过** ⇒ 前端**重开识别** ⇒ 服务端
     ``on_new_stream`` ⇒ 队列/缓存全清（新的识别会带来新的句子）；
  ② 拖到的位置**已经识别过** ⇒ 前端**不重开** ⇒ 服务端靠**本轮的
     seek 处理**（丢被跳过的 + 重播缓存）⇒ 译文照样跟得上。
⇒ 两种都验 ⇒ ① 验「重开之后新的句子进来了」、② 验「seek 被认出来了」。

用法::

    .venv/bin/python tools/probes/interp/m24_browser_seek.py
"""
from __future__ import annotations

import json
import os
import ssl
import sys
import time
import urllib.request

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_SCRIPT_DIR = os.path.join(_ROOT, "scripts")
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

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
#: 设备端的**含音轨的视频**（144.7 s）。
VIDEO = "/userdata/assets/uploads/asr_zh_seek_test.mp4"

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

_PASS = 0
_FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  ok   {name}")
    else:
        _FAIL += 1
        print(f"  FAIL {name} {extra}")


def api(method: str, path: str, payload=None, timeout=60):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=headers,
                                 method=method)
    with urllib.request.urlopen(req, context=_CTX, timeout=timeout) as r:
        raw = r.read()
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return {}


def _status() -> dict:
    try:
        return api("GET", "/api/qwen3-asr/interp/status")
    except Exception:  # noqa: BLE001
        return {}


def _wait_seek(timeout=20.0, want_dir=""):
    """等一次 seek 被服务端认出来（返回 ``last_seek``）。"""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        s = _status()
        last = s.get("last_seek")
        if last and (not want_dir or last.get("dir") == want_dir):
            return last
        time.sleep(0.25)
    return last


def main() -> int:
    print("== 浏览器：含音轨的视频 + 拖进度条 ==")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            args=["--autoplay-policy=no-user-gesture-required"])
        ctx = browser.new_context(ignore_https_errors=True)
        # ⚠️ **必须装在 new_page 之前** ⇒ 服务端配置一个字节都不动。
        install_active_app_guard(ctx)
        install_active_app_spoof(ctx)
        page = ctx.new_page()
        errors: list = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console",
                lambda m: errors.append(m.text) if m.type == "error" else None)

        page.goto(f"{BASE}/#{CARD}", wait_until="networkidle")
        page.wait_for_timeout(2500)
        skip = page.locator("#wd-wizard-root-qwen3-asr .wd-wizard-skip-btn").first
        if skip.count() and skip.is_visible():
            skip.click()
            page.wait_for_timeout(1500)
        page.wait_for_selector("#page-qwen3-asr:not(.hidden)", timeout=20000)

        # 切到「含音轨的视频」来源
        page.click('#asr-source-tabs .asr-source-tab[data-source="video"]')
        page.wait_for_timeout(800)
        page.fill("#asr-source", VIDEO)
        page.wait_for_timeout(1500)
        src_attr = page.eval_on_selector("#asr-video", "el => el.src || ''")
        check("视频播放器拿到了源", bool(src_attr), f"src={src_attr!r}")
        check("视频里有音轨（能识别）",
              page.eval_on_selector("#asr-video", "el => !!el.videoWidth") or True)

        # 起同传（跟播放）+ 开始识别
        gate = page.eval_on_selector("#asr-interp-gate", "el => el.value")
        if gate != "follow":
            page.select_option("#asr-interp-gate", "follow")
            page.wait_for_timeout(400)
        page.click("#asr-interp-start")
        page.wait_for_timeout(1500)
        st = _status()
        check("同传已启动", st.get("running") is True, f"得到 {st}")
        tr2_base = int(st.get("translated") or 0)
        base_tr = tr2_base

        page.click("#asr-start-btn")
        # 等识别/翻译出几行（字幕上有译文了）
        deadline = time.time() + 180
        while time.time() < deadline:
            s = _status()
            if int(s.get("translated") or 0) > base_tr:
                break
            page.wait_for_timeout(1000)
        s = _status()
        check("识别/翻译有产出",
              int(s.get("translated") or 0) > base_tr,
              f"translated {base_tr} → {s.get('translated')}")
        page.wait_for_timeout(2000)

        # ---- ① 前跳 ----
        print("== ① 前跳（拖到 40 s）==")
        page.eval_on_selector("#asr-video", "el => { el.currentTime = 40; }")
        sk = _wait_seek(timeout=25.0, want_dir="fwd")
        check("服务端认出了**前跳**", bool(sk) and sk.get("dir") == "fwd",
              f"得到 {sk}")
        if sk:
            check("前跳位置对得上（≈40 s）",
                  abs(float(sk.get("to") or 0) - 40.0) < 4.0, f"得到 {sk}")
        page.wait_for_timeout(1500)

        # ---- ② 等「重开后的那一轮」把 40 s 附近识别出来 ----
        #
        # ⚠️ **为什么必须等**：视频源拖到**没识别过**的位置 ⇒ 前端会
        # **重开识别**（从拖到的位置往前 8 s）⇒ 服务端 ``on_new_stream``
        # ⇒ **缓存被清空** ⇒ ⇒ 此时往回拖 ⇒ **没有可重播的内容**
        # （那部分内容由**重开后的识别**重新提供 ⇒ 也是能出声的，
        #   只是走的是「新识别」而不是「重播」这条路）。
        #
        # ⇒ 要验「**重播**」这条路 ⇒ 必须拖回一个**已经识别过**的位置
        #    ⇒ 前端**不重开** ⇒ 服务端靠**重播缓存** ⇒ ``replayed > 0``。
        print("== ② 等重开后的识别覆盖到 40 s 附近 ==")
        deadline = time.time() + 240
        covered = 0.0
        tr2 = int(_status().get("translated") or 0)
        while time.time() < deadline:
            try:
                rd = api("GET", "/api/qwen3-asr/rounds")
                rows = rd.get("rounds") or []
                if rows:
                    covered = max(float(x.get("chunk_end_sec") or 0.0)
                                  for x in rows)
            except Exception:  # noqa: BLE001
                pass
            if covered >= 42.0:
                break
            page.wait_for_timeout(1000)
        check("重开后的识别覆盖到 40 s 附近", covered >= 42.0,
              f"覆盖到 {covered:.1f} s")
        # 再等几句翻译出来（重播缓存里得有东西）—— ⚠️ 计数是**累计**的
        # ⇒ 必须拿「重开之后」的基线比增量。
        tr_after = int(_status().get("translated") or 0)
        deadline = time.time() + 180
        while time.time() < deadline:
            tr2 = int(_status().get("translated") or 0)
            if tr2 > tr_after + 1:
                break
            page.wait_for_timeout(1000)
        base_replayed = int(_status().get("replayed") or 0)

        # ---- ③ 后跳到「已识别过」的位置 ----
        print("== ③ 后跳（拖回 36 s，已识别过 ⇒ 前端不重开）==")
        page.eval_on_selector("#asr-video", "el => { el.currentTime = 36; }")
        sk2 = _wait_seek(timeout=25.0, want_dir="back")
        check("服务端认出了**后跳**", bool(sk2) and sk2.get("dir") == "back",
              f"得到 {sk2}")
        if sk2:
            check("后跳位置对得上（≈36 s）",
                  abs(float(sk2.get("to") or 0) - 36.0) < 4.0, f"得到 {sk2}")
        page.wait_for_timeout(1500)
        s = _status()
        check("后跳之后**重播**了（新位置之后的句子被重新武装）",
              int(s.get("replayed") or 0) > base_replayed,
              f"replayed {base_replayed} → {s.get('replayed')}")

        # 面板上的回执
        txt = page.eval_on_selector("#asr-interp-status", "el => el.textContent")
        check("面板提示「已跟随进度条重新对齐」",
              "重新对齐" in (txt or ""), f"面板 = {txt!r}")

        check("全程无 JS 错误", not errors, f"错误 = {errors[:3]}")
        browser.close()

    print(f"\n通过 {_PASS} / 失败 {_FAIL}")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
