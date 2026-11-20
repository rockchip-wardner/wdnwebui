#!/usr/bin/env python3
"""真机浏览器回归：「实时翻译」**关掉再打开** ⇒ 字幕上要有译文。

用户报的缺陷
============

用户对**含音轨的视频**（一集 98 分钟的访谈，≈900 行字幕）：

1. 勾选「实时翻译」⇒ 翻译正常；
2. 取消勾选 ⇒ 没有译文（正常）；
3. **再勾选 ⇒ 字幕上没有实时翻译**（缺陷）。

根因（2026-11-20 实测）
========================

* **批量重发**：行级翻译是**前端驱动**的（每行一次请求）⇒ 「重新勾选」时
  前端把**整条时间轴**的行**一次性**重发 ⇒ 一集 98 分钟 ≈ 900 个请求
  同时打出去。
* **队列爆掉**：服务端翻译队列有界（``QUEUE_MAX``），满了**丢最旧的**
  ⇒ 丢掉的正是**播放头附近的那几行** ⇒ 画面字幕上一直没有译文。
* **勾选框被回退**：``_trSyncUi()`` 会把勾选框按 ``_tr.enabled`` 回写，而
  ``_tr.enabled`` 要等服务端往返才更新 ⇒ 那一瞬间到达的译文帧会把用户
  的点击**悄悄勾回去** ⇒ 「关不掉 / 再打开也没反应」。

修法
====

* 前端：改成「**播放头优先 + 限速**」（``TR_REQ_BATCH`` / ``TR_REQ_TICK_MS``）
  ⇒ 屏幕上那一行**第一批**就要到，队列永远不满。
* 前端：点击时**先本地置位**（``_tr.enabled``）⇒ 勾选框不会被回退。
* 服务端：队列上限 32 → 256（余量）。

跑之前要满足：**ASR 模型**与**翻译模型**在跑。

跑法::

    .venv/bin/python scripts/verify_asr_translate_toggle.py
"""
from __future__ import annotations

import json
import os
import shutil
import ssl
import sys
import time
import urllib.error
import urllib.request

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if str(_SCRIPT_DIR) not in sys.path:
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
#: 580 秒（≈ 10 分钟）的视频 ⇒ ≈ 100 行字幕 ⇒ **超过服务端的队列上限**
#: ⇒ 旧行为（一次性全要）必然爆队列。
SRC_MP4 = "/userdata/assets/uploads/asr_long_toggle_test.mp4"
#: 播放端点（``/api/qwen3-asr/media``）只放行 ``<assets>/uploads`` ⇒ 视频得
#: 在那儿（上面的路径已经是了）。
DST_MP4 = SRC_MP4

#: 目标语言（中文音频 ⇒ 英文才是**真翻译**；中文→中文服务端会「透传」）。
TARGET_LANG = "en"

#: 重新勾选后，**可见的那一行**最多等多久就该有译文。
#: （旧行为：几百个请求一次性打出去 ⇒ 队列爆 ⇒ 可见行排在队尾 ⇒ 拿不到。）
VISIBLE_TR_DEADLINE_SEC = 15.0

#: 重新勾选后，**头 2 秒内**最多允许发出多少个请求（限速生效的证据）。
BURST_LIMIT = 12

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


def api(method: str, path: str, payload=None, timeout: int = 180):
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
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body or "{}")
        except Exception:  # noqa: BLE001
            return e.code, {"raw": body}
    except Exception as e:  # noqa: BLE001
        return -1, {"error": repr(e)}


def _ensure_models() -> tuple[bool, bool]:
    _, snap = api("GET", "/api/compute")
    asr_ok = any(i.get("model_id") == "qwen3-asr"
                 and i.get("status") == "running"
                 for i in (snap.get("instances") or []))
    if not asr_ok:
        print("  … ASR 模型没在跑，起一个（~30 s）")
        api("POST", "/api/compute/instances",
            {"model": "qwen3-asr", "npu_index": 0}, timeout=240)
        for _ in range(30):
            time.sleep(5)
            _, snap = api("GET", "/api/compute")
            if any(i.get("model_id") == "qwen3-asr"
                   and i.get("status") == "running"
                   for i in (snap.get("instances") or [])):
                asr_ok = True
                break
    _, st = api("GET", "/api/qwen3-asr/translate/status")
    mt_ok = bool((st.get("model") or {}).get("running"))
    if not mt_ok:
        print("  … 翻译模型没在跑，起一个（~30 s）")
        api("POST", "/api/qwen3-asr/translate/enable", {}, timeout=260)
        for _ in range(30):
            time.sleep(5)
            _, st = api("GET", "/api/qwen3-asr/translate/status")
            if bool((st.get("model") or {}).get("running")):
                mt_ok = True
                break
    return asr_ok, mt_ok


# --------------------------------------------------------------------------- #
# 页面探针
# --------------------------------------------------------------------------- #
def _state(page) -> dict:
    """页面里的翻译状态 + 画面字幕的可见行。"""
    return page.evaluate(
        """() => {
          const A = window.Qwen3Asr;
          const tl = (A && A._buildSubTimeline) ? A._buildSubTimeline().lines : [];
          const slots = [...document.querySelectorAll('#asr-video-sub .asr-sub-line')];
          const vis = slots.filter((el) => {
            if (getComputedStyle(el).visibility === 'hidden') return false;
            if (Number(el.style.opacity || '0') <= 0.01) return false;
            return true;
          });
          let linesWithTr = 0;
          for (const ln of tl) {
            const slot = String(ln.id || '').split(':').slice(0, 2).join(':');
            const it = (A._trLineTr && A._trLineTr.get(slot)) || null;
            if (it && it.status === 'done' && (it.text || '').trim()) linesWithTr++;
          }
          const cb = document.getElementById('asr-tr-enabled');
          return {
            trEnabled: A && A._tr ? !!A._tr.enabled : null,
            trMode: A ? A._trMode : null,
            checkbox: cb ? !!cb.checked : null,
            lines: tl.length,
            linesWithTr: linesWithTr,
            rounds: (A && A.rounds) ? A.rounds.length : 0,
            busy: A && A.snapshot ? !!A.snapshot.busy : null,
            state: A && A.snapshot ? A.snapshot.state : null,
            videoTime: (document.getElementById('asr-video') || {}).currentTime || 0,
            visible: vis.map((el) => ({
              orig: ((el.querySelector('.asr-sub-orig') || {}).textContent || '').trim(),
              tr: ((el.querySelector('.asr-sub-tr') || {}).textContent || '').trim(),
            })),
          };
        }"""
    )


def _visible_has_tr(page) -> bool:
    """**画面上**的那一行有没有译文。"""
    st = _state(page)
    return any((ln["tr"] or "").strip() for ln in st["visible"])


def _seek(page, sec: float) -> None:
    page.evaluate(
        """(t) => { const v = document.getElementById('asr-video');
                   if (v) { v.currentTime = t; } }""",
        sec)
    page.wait_for_timeout(1200)


def main() -> int:
    print("=" * 72)
    print("真机浏览器回归：「实时翻译」关掉再打开 ⇒ 字幕上要有译文")
    print("=" * 72)

    if not os.path.exists(SRC_MP4):
        print("❌ 缺素材 %s" % SRC_MP4)
        print("   （580 秒的长视频：把 asr_zh_seek_test.mp4 拼 4 份）")
        return 1

    asr_ok, mt_ok = _ensure_models()
    check("ASR 模型在跑", asr_ok)
    check("翻译模型在跑", mt_ok)
    if not (asr_ok and mt_ok):
        print("❌ 模型没起来，脚本到此为止")
        return 1

    api("POST", "/api/qwen3-asr/translate/settings",
        {"enabled": True, "target_lang": TARGET_LANG})
    _, st = api("GET", "/api/qwen3-asr/translate/status")
    check("实时翻译已开启", bool(st.get("enabled")), f"status={st}")
    dropped_before = st.get("dropped")

    errors: list[str] = []
    line_posts: list[float] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1440, "height": 1100},
                                  ignore_https_errors=True)
        install_active_app_guard(ctx)
        install_active_app_spoof(ctx)
        page = ctx.new_page()
        page.on("pageerror", lambda e: errors.append(f"[pageerror] {e}"))
        page.on("console", lambda m: errors.append(f"[console.{m.type}] {m.text}")
                if m.type == "error" else None)
        page.on("request", lambda r: line_posts.append(time.time())
                if "/translate/line" in r.url else None)

        page.goto(f"{BASE}/#{CARD}", wait_until="networkidle")
        page.wait_for_timeout(2500)
        skip = page.locator("#wd-wizard-root-qwen3-asr .wd-wizard-skip-btn").first
        if skip.count() and skip.is_visible():
            skip.click()
            page.wait_for_timeout(1500)
        page.wait_for_selector("#page-qwen3-asr:not(.hidden)", timeout=15000)

        # --- 切到「含音轨的视频」来源 ---
        page.locator('#asr-source-tabs .asr-source-tab[data-source="video"]').click()
        page.wait_for_timeout(800)
        check("页签切到「含音轨的视频」",
              page.evaluate("() => window.Qwen3Asr ? Qwen3Asr._source : ''")
              == "video")

        page.evaluate(
            """(p) => {
              const el = document.getElementById('asr-source');
              if (el) {
                el.value = p;
                el.dispatchEvent(new Event('input', {bubbles: true}));
              }
            }""",
            DST_MP4,
        )
        page.wait_for_timeout(600)
        page.locator("#asr-start-btn").click()

        # --- 等识别结束 ---
        print("  … 识别 580 秒音频（约 2 分钟）")
        t0 = time.time()
        while time.time() - t0 < 600:
            _, snap = api("GET", "/api/qwen3-asr/")
            if snap.get("state") == "done":
                break
            time.sleep(3)
        page.wait_for_timeout(4000)

        stt = _state(page)
        print("  识别结束：行数=%s 有译文的行=%s 轮次=%s"
              % (stt["lines"], stt["linesWithTr"], stt["rounds"]))
        check("识别出了**足够多**的行（> 队列上限 ⇒ 才能测出爆队列）",
              stt["lines"] > 32, f"lines={stt['lines']}")
        check("翻译开着 ⇒ 字幕行已经有译文",
              stt["linesWithTr"] > 0, f"linesWithTr={stt['linesWithTr']}")

        # --- 拖到有字幕的位置（用户场景：视频播了很久） ---
        _seek(page, 120.0)
        page.wait_for_timeout(1500)
        stt = _state(page)
        check("画面上有字幕行", bool(stt["visible"]),
              f"visible={stt['visible']}")

        # --- 关掉 ---
        print("\n  [1] 取消勾选")
        page.locator("#asr-tr-enabled").click()
        page.wait_for_timeout(2500)
        stt = _state(page)
        check("关掉后**勾选框是未勾选**的（没被译文帧回退）",
              stt["checkbox"] is False, f"checkbox={stt['checkbox']}")
        check("关掉后服务端也是关的", stt["trEnabled"] is False,
              f"trEnabled={stt['trEnabled']}")
        check("关掉后画面上的译文收掉了",
              not _visible_has_tr(page), "画面上还有译文")

        # --- 再打开 ---
        print("  [2] 再勾选")
        n_before = len(line_posts)
        t_click = time.time()
        page.locator("#asr-tr-enabled").click()
        page.wait_for_timeout(500)
        stt = _state(page)
        check("再打开后**勾选框是勾选**的（没被译文帧回退）",
              stt["checkbox"] is True, f"checkbox={stt['checkbox']}")
        check("再打开后服务端也是开的", stt["trEnabled"] is True,
              f"trEnabled={stt['trEnabled']}")

        # 头 2 秒内的请求数（限速的证据）
        page.wait_for_timeout(2000)
        burst = len([t for t in line_posts[n_before:] if t - t_click <= 2.0])
        check("重新勾选**不是一次性几百个请求**（限速生效）",
              burst <= BURST_LIMIT, f"2 秒内发了 {burst} 个")

        # --- 可见的那一行多久拿到译文 ---
        print("  … 等可见行拿到译文")
        got_at = None
        t_wait = time.time()
        while time.time() - t_wait < 60:
            if _visible_has_tr(page):
                got_at = time.time() - t_click
                break
            page.wait_for_timeout(400)
        check("**画面上那一行**在 %.0f s 内拿到了译文"
              % VISIBLE_TR_DEADLINE_SEC,
              got_at is not None and got_at <= VISIBLE_TR_DEADLINE_SEC,
              f"等了 {got_at if got_at is not None else '>60'} s")
        if got_at is not None:
            print("     用时 %.1f s" % got_at)

        # --- 服务端没有丢请求 ---
        _, st2 = api("GET", "/api/qwen3-asr/translate/status")
        check("服务端**没有丢**翻译请求（队列没爆）",
              (st2.get("dropped") or 0) <= (dropped_before or 0),
              f"dropped {dropped_before} → {st2.get('dropped')}")

        stt = _state(page)
        print("  最终：行数=%s 有译文的行=%s  累计 line 请求=%s"
              % (stt["lines"], stt["linesWithTr"], len(line_posts)))

        check("没有 JS 错误", not errors, f"errors={errors[:3]}")
        browser.close()

    print("\n" + "=" * 72)
    print(f"通过 {_PASS} / 失败 {_FAIL}")
    print("=" * 72)
    if _FAIL:
        print("❌ 有断言没过")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
