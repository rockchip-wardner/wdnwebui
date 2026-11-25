#!/usr/bin/env python3
"""真机浏览器回归：**同声传译**（ASR 卡里的新模式）。

覆盖
====
1. 面板存在（切句档位 / 播放时机 / 目标语 / 音色 / 音量 / 开始 / 停止）；
2. **目标语言只列「能出声」的**（10 种，MT 37 种 ∩ TTS 11 种）；
3. 板载麦 / 浏览器麦 ⇒ **播放时机置灰**（没有播放头）；
4. 文件源 ⇒ 播放时机可用；开始同传 ⇒ **自动播放**音频播放器；
5. 端到端：音频文件 → 识别 → 翻译 → **送进 TTS 队列**（`submitted > 0`）；
6. 停止同传 ⇒ 引擎停 + TTS 队列清空；
7. 全程**无 JS 错误**。

跑之前要满足：**ASR / 翻译 / TTS 三个模型都在跑**。

跑法::

    .venv/bin/python scripts/verify_interp_browser.py
"""
from __future__ import annotations

import json
import os
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
#: 设备端的音频（英文）—— 文件源用它。
SRC_WAV = "/userdata/assets/uploads/asr_en.wav"

#: ⚠️ **测试播放用 5%**（2026-11-25 用户定案，同日第二次调整）—— 同传的
#: **产品默认**音量也是 **5%**（见插件的 ``interp.py`` / 前端 ``app.js``）；
#: ⇒ 两条纪律是**同一条**。
#:
#: ⇒ 10% 太大声（用户：「测试时10%声音太大了」）⇒ 5% 折中；
#: ⇒ 1% 又太小声（底噪与语音同量级 ⇒ 判断不出杂音/卡顿/断音）。
#:
#: ⚠️ 必须**显式拉回这个值** —— localStorage 里可能存着**旧的** 0.5。
TEST_VOLUME_PCT = 5


def force_test_volume(page) -> None:
    """把同传音量滑块拉到 **5%** 并触发 change ⇒ 后端立刻收到。

    ⚠️ 必须**在点「开始同传」之前**调用 ⇒ 否则第一句就是旧音量放出来的。
    """
    page.eval_on_selector(
        "#asr-interp-volume",
        """(el, pct) => {
            el.value = String(pct);
            el.dispatchEvent(new Event('input', {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
        }""",
        TEST_VOLUME_PCT)
    page.wait_for_timeout(300)


def settle_rounds(timeout: float = 90.0) -> None:
    """**场次之间「排干净」**（2026-11-25，用户报「播放卡顿 + 切音」）。

    ⚠️⚠️ **为什么必须排干净**：四场测试**用的是同一段素材** ⇒ 每一场
    都会把**同一段译文**再灌一遍 TTS 队列 ⇒ 上一场还没放完，下一场已经
    灌进来了 ⇒ 用户在板子旁边听到的是**同一段话被叠着播 3 遍**，中间
    还夹着**重新转写**的空洞（~25 s）⇒ 根本分不清哪些是产品的真实
    表现、哪些是测试方式造成的。

    ⇒ 场次之间：① 停同传（别再送新的）② **等队列排空**（上一场放完）。
    ⚠️ **先等排空、再清**（反过来的话会把这一场**放一半就切掉** ⇒
    制造出我们要消除的那种「切音」）。
    ⇒ 之后每场都是**干净的、可分辨的**一段播放 ⇒ 用户能直接判断
    「杂音 / 卡顿 / 断音」。
    """
    api("POST", "/api/qwen3-asr/interp/stop")
    deadline = time.time() + timeout
    while time.time() < deadline:
        _st, q = api("GET", "/api/tts/queue/status")
        if (int(q.get("depth") or 0) == 0
                and int(q.get("inflight") or 0) == 0
                and not q.get("playing")):
            return
        time.sleep(0.5)
    # ⚠️ 超时了 ⇒ 清掉（下一场别在旧的尾巴上叠）
    api("POST", "/api/tts/queue/clear")


#: ⚠️ **模拟「浏览器按自动播放策略拒绝了 play()」** ——
#: 真实桌面浏览器在「媒体参与度不足 / 没有用户手势」时会拒绝自动播放 ⇒
#: 媒体一直暂停 ⇒ 播放头不动 ⇒ 「跟播放」把句子全压住 ⇒
#: **用户看到译文却一声不响**（用户实测报的就是这个）。
#:
#: ⚠️ Playwright 的 ``click()`` **自带用户手势** ⇒ 即便用
#: ``--autoplay-policy=user-gesture-required``，``play()`` 也会成功
#: ⇒ 这条路径在 headless 里**测不出来** ⇒ 只能**直接让 play() 拒绝**。
BLOCK_AUTOPLAY_JS = """
(() => {
  const err = () => Promise.reject(new DOMException(
    'Autoplay is not allowed in this browser.', 'NotAllowedError'));
  HTMLMediaElement.prototype.play = err;
})();
"""

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
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body or "{}")
        except Exception:  # noqa: BLE001
            return e.code, {"raw": body}
    except Exception as e:  # noqa: BLE001
        return 0, {"error": str(e)}


def main() -> int:
    errors: list[str] = []

    # 干净起点：停同传 + **等上一轮放完**（见 settle_rounds 的说明）
    settle_rounds()

    with sync_playwright() as pw:
        # ⚠️⚠️ **严格自动播放策略**（``user-gesture-required``）——
        # 真实桌面浏览器的默认行为。headless 默认策略太宽松 ⇒
        # 「自动播放被拦 ⇒ 媒体一直暂停 ⇒ 播放头不动 ⇒ 跟播放把所有句子
        # 压住 ⇒ 用户看到译文却一声不响」这条路径**测不出来**（踩过）。
        browser = pw.chromium.launch(args=[
            "--no-sandbox",
            "--autoplay-policy=user-gesture-required",
        ])
        ctx = browser.new_context(ignore_https_errors=True,
                                  viewport={"width": 1440, "height": 1000})
        # ⚠️ 必须**在 new_page() 之前**装 ⇒ 否则脚本会改生产的 active-app
        install_active_app_guard(ctx)
        install_active_app_spoof(ctx)
        page = ctx.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append("console.error: " + m.text)
                if m.type == "error" else None)

        page.goto(f"{BASE}/#{CARD}", wait_until="networkidle")
        page.wait_for_timeout(2500)
        # 向导会挡住卡片页 ⇒ 先跳过（跟其它 ASR 回归同一个套路）
        skip = page.locator("#wd-wizard-root-qwen3-asr .wd-wizard-skip-btn").first
        if skip.count() and skip.is_visible():
            skip.click()
            page.wait_for_timeout(1500)
        page.wait_for_selector("#page-qwen3-asr:not(.hidden)", timeout=20000)
        page.wait_for_selector("#asr-interp-block", timeout=30000)
        page.wait_for_timeout(1500)

        print("== 面板 ==")
        for sel in ("#asr-interp-mode", "#asr-interp-gate", "#asr-interp-target",
                    "#asr-interp-speaker", "#asr-interp-volume",
                    "#asr-interp-start", "#asr-interp-stop",
                    "#asr-interp-status", "#asr-interp-hint"):
            check(f"面板有 {sel}", page.locator(sel).count() == 1)

        # 目标语言：只列能出声的
        opts = page.eval_on_selector_all(
            "#asr-interp-target option",
            "els => els.map(e => e.value)")
        check(f"目标语言列表非空（{len(opts)} 项）", len(opts) >= 8, f"得到 {opts}")
        check("目标语言里没有泰语（不能出声）", "th" not in opts, f"得到 {opts}")
        check("目标语言里有中文", "zh-CN" in opts, f"得到 {opts}")

        # 音色
        voices = page.eval_on_selector_all(
            "#asr-interp-speaker option",
            "els => els.map(e => e.value)")
        check(f"音色列表非空（{len(voices)} 项）", len(voices) >= 3, f"得到 {voices}")

        # 板载麦 / 浏览器麦 ⇒ 播放时机置灰
        print("== 播放时机跟着来源走 ==")
        for src in ("board", "browser"):
            page.click(f'#asr-source-tabs .asr-source-tab[data-source="{src}"]')
            page.wait_for_timeout(400)
            disabled = page.eval_on_selector("#asr-interp-gate",
                                            "el => el.disabled")
            check(f"{src} 源 ⇒ 播放时机置灰", disabled is True)
        # 回到文件源
        page.click('#asr-source-tabs .asr-source-tab[data-source="file"]')
        page.wait_for_timeout(400)
        disabled = page.eval_on_selector("#asr-interp-gate", "el => el.disabled")
        check("file 源 ⇒ 播放时机可用", disabled is False)

        # ================================================================ #
        # **四种来源的结果都显示在页面底部**（2026-11-25）
        #
        # ⚠️ 原来：麦克风来源的「实时翻译」句子流在**页面中部**的「实时识别」
        # 块里，而文件 / 视频来源的在**底部** ⇒ 同一份结果**位置不一致** ⇒
        # 用户得先看来源才知道去哪找译文。
        #
        # ⇒ 现在：四种来源的实时翻译结果都在**底部**的 `#asr-live` 框里
        #    （同一时刻只显示一个），同传结果在它下面的 `#asr-interp-out`。
        # ================================================================ #
        print("== 四种来源的结果统一在底部 ==")
        for src in ("board", "browser"):
            page.click(f'#asr-source-tabs .asr-source-tab[data-source="{src}"]')
            page.wait_for_timeout(500)
            in_bottom = page.eval_on_selector(
                "#asr-rt-sentences", "el => !!el.closest('#asr-live')")
            check(f"{src} 源 ⇒ 实时翻译结果挂在底部的 #asr-live 里", in_bottom is True)
            shown = page.eval_on_selector(
                "#asr-rt-sentences", "el => !el.classList.contains('hidden')")
            check(f"{src} 源 ⇒ 实时翻译结果可见", shown is True)
        page.click('#asr-source-tabs .asr-source-tab[data-source="file"]')
        page.wait_for_timeout(500)
        file_shown = page.eval_on_selector(
            "#asr-sentences", "el => !el.classList.contains('hidden')")
        rt_hidden = page.eval_on_selector(
            "#asr-rt-sentences", "el => el.classList.contains('hidden')")
        check("file 源 ⇒ 实时翻译结果可见（句子流）", file_shown is True)
        check("file 源 ⇒ 麦克风的句子流被藏起来（不重复显示）", rt_hidden is True)
        # 视频来源
        page.click('#asr-source-tabs .asr-source-tab[data-source="video"]')
        page.wait_for_timeout(500)
        tr_in_bottom = page.eval_on_selector(
            "#asr-transcript", "el => !!el.closest('#asr-live')")
        check("video 源 ⇒ 字幕转写框挂在底部的 #asr-live 里", tr_in_bottom is True)
        s_hidden = page.eval_on_selector(
            "#asr-sentences", "el => el.classList.contains('hidden')")
        rt_hidden2 = page.eval_on_selector(
            "#asr-rt-sentences", "el => el.classList.contains('hidden')")
        check("video 源 ⇒ 文件 / 麦克风的句子流被藏起来（不重复显示）",
              s_hidden is True and rt_hidden2 is True)
        page.click('#asr-source-tabs .asr-source-tab[data-source="file"]')
        page.wait_for_timeout(500)

        # 音频播放器（文件源）
        print("== 音频播放器 ==")
        check("有 #asr-audio-player", page.locator("#asr-audio-player").count() == 1)
        hidden = page.eval_on_selector(
            "#asr-audio-wrap", "el => el.classList.contains('hidden')")
        check("file 源 ⇒ 播放器可见", hidden is False)

        # 填路径 + 起同传 + 开始识别
        print("== 端到端 ==")
        page.fill("#asr-source", SRC_WAV)
        page.wait_for_timeout(400)
        src_attr = page.eval_on_selector("#asr-audio-player", "el => el.src || ''")
        check("播放器拿到了源", bool(src_attr), f"src={src_attr!r}")

        # ⚠️ 测试播放用 10%（别用 localStorage 里的旧值）
        force_test_volume(page)
        page.click("#asr-interp-start")
        page.wait_for_timeout(1200)
        st, data = api("GET", "/api/qwen3-asr/interp/status")
        check("同传已启动", data.get("running") is True, f"得到 {data}")

        # ---- **自动播放必须在用户手势里**（2026-11-21）----------------
        #
        # ⚠️ 之前 ``_interpAutoPlay()`` 写在 ``await fetch(...)`` **之后**
        # ⇒ 用户手势（transient activation）在多数浏览器里已经失效 ⇒
        # ``el.play()`` 被自动播放策略**拒绝** ⇒ 媒体一直暂停 ⇒ 播放头
        # 不动 ⇒ 跟播放把**所有**句子永久压住 ⇒
        # **用户看到译文却一声不响**（用户实测报「有翻译，但是没有播放」）。
        #
        # ⇒ 「开始同传」这个**点击手势**里必须同步调 ``play()``。
        media = page.eval_on_selector(
            "#asr-audio-player",
            "el => ({paused: el.paused, t: el.currentTime, "
            "ready: el.readyState, err: el.error ? el.error.code : 0})")
        check("**开始同传 ⇒ 媒体真的开始播了**（自动播放没被拦）",
              media.get("paused") is False and float(media.get("t") or 0) > 0.0,
              f"得到 {media}")
        page.wait_for_timeout(1500)
        media2 = page.eval_on_selector(
            "#asr-audio-player", "el => ({paused: el.paused, t: el.currentTime})")
        check("播放头在走（currentTime 在涨）",
              float(media2.get("t") or 0) > float(media.get("t") or 0) + 0.3,
              f"{media} → {media2}")

        # ⚠️ **计数是累计的**（跨引擎生命周期不清零）⇒ 必须取**基线**比
        # **增量** —— 否则上一轮留下的 ``submitted>0`` 会让这条断言
        # **假通过**（哪怕这一轮一句都没送进去）。
        _st, base1 = api("GET", "/api/qwen3-asr/interp/status")
        base1_sub = int(base1.get("submitted") or 0)
        base1_seg = int(base1.get("segments") or 0)
        base1_tr = int(base1.get("translated") or 0)
        page.click("#asr-start-btn")
        # 等「送进 TTS 队列」的句数 > 0（TTS 首帧 ~3 s ⇒ 给足时间）
        deadline = time.time() + 150
        submitted = base1_sub
        while time.time() < deadline:
            _st, s = api("GET", "/api/qwen3-asr/interp/status")
            submitted = int(s.get("submitted") or 0)
            if submitted > base1_sub:
                break
            page.wait_for_timeout(1000)
        check(f"有句子送进 TTS 队列（submitted {base1_sub} → {submitted}）",
              submitted > base1_sub, f"status={s}")

        _st, s = api("GET", "/api/qwen3-asr/interp/status")
        check(f"切句数有增量（{base1_seg} → {s.get('segments')}）",
              int(s.get("segments") or 0) > base1_seg, f"得到 {s}")
        check(f"翻译数有增量（{base1_tr} → {s.get('translated')}）",
              int(s.get("translated") or 0) > base1_tr, f"得到 {s}")

        # ---- **同传结果列表（页面底部）**（2026-11-25 新增）--------
        #
        # ⚠️ 原来同传**没有任何结果列表** —— 页面上只有一句状态行 ⇒ 用户
        # 看不到「说了什么」⇒ 现在后端补了 ``interp_translated`` 帧（原文 +
        # 完整译文）⇒ 底部一句一句列出来。
        # ================================================================ #
        print("== 同传结果列表（页面底部）==")
        deadline = time.time() + 60
        rows = []
        while time.time() < deadline:
            rows = page.eval_on_selector_all(
                "#asr-interp-list .asr-interp-sent",
                """els => els.map(e => ({
                    src: (e.querySelector('.asr-sent-src') || {}).textContent || '',
                    dst: (e.querySelector('.asr-sent-dst') || {}).textContent || '',
                    tag: (e.querySelector('.asr-interp-tag') || {}).textContent || '',
                }))""")
            if any(r["src"] and r["dst"] and r["dst"] != "翻译中"
                   and "（" not in r["dst"] for r in rows):
                break
            page.wait_for_timeout(1000)
        check(f"底部有同传结果行（{len(rows)} 行）", len(rows) > 0, f"得到 {rows[:2]}")
        check("同传结果行有原文",
              any(r["src"].strip() for r in rows), f"得到 {rows[:2]}")
        check("同传结果行有**完整译文**（不是「翻译中」占位）",
              any(r["dst"].strip() and r["dst"].strip() != "翻译中"
                  and not r["dst"].startswith("（") for r in rows),
              f"得到 {rows[:2]}")
        # 位置：**在「实时结果」框的下面** ⇒ 两类结果都在页面底部。
        geom = page.evaluate("""() => {
            const live = document.getElementById('asr-live');
            const out = document.getElementById('asr-interp-out');
            const ctrl = document.getElementById('asr-interp-block');
            if (!live || !out || !ctrl) return null;
            const y = (el) => el.getBoundingClientRect().top + window.scrollY;
            return {live: y(live), out: y(out), ctrl: y(ctrl),
                    outHidden: out.classList.contains('hidden')};
        }""")
        check("同传结果区可见", geom and geom.get("outHidden") is False, f"得到 {geom}")
        check("同传结果区在「实时结果」框**下面**",
              geom and float(geom.get("out") or 0) > float(geom.get("live") or 0),
              f"得到 {geom}")
        check("同传结果区在同传控制条**下面**（页面底部）",
              geom and float(geom.get("out") or 0) > float(geom.get("ctrl") or 0),
              f"得到 {geom}")

        # 面板上的状态行
        text = page.inner_text("#asr-interp-status")
        check("状态行显示播报中", "播报" in text or "滞后" in text, f"得到 {text!r}")

        # 停止
        page.click("#asr-interp-stop")
        page.wait_for_timeout(1200)
        _st, s = api("GET", "/api/qwen3-asr/interp/status")
        check("停止后 running = false", s.get("running") is False, f"得到 {s}")
        _st, q = api("GET", "/api/tts/queue/status")
        check("停止后 TTS 队列清空", int(q.get("depth") or 0) == 0, f"得到 {q}")

        # JS 错误
        real_errors = [e for e in errors if "favicon" not in e.lower()]
        check("全程无 JS 错误", not real_errors, f"错误: {real_errors[:3]}")

        # ================================================================ #
        # **第二场：自动播放被浏览器拦了**（用户报「有翻译，但是没有播放」）
        #
        # ⇒ 媒体一直暂停 ⇒ 播放头不动 ⇒ 「跟播放」压住所有句子 ⇒
        #    **有译文、一声不响**。
        #
        # ⇒ 期望：① 面板**说清楚**（提示 + 状态行）；② **兜底放行** ⇒
        #    句子照样进 TTS 队列 ⇒ 用户**听得见**译文。
        # ================================================================ #
        print("== 第二场：自动播放被拦 ⇒ 兜底放行 ⇒ 译文仍然出声 ==")
        ctx2 = browser.new_context(ignore_https_errors=True,
                                   viewport={"width": 1440, "height": 1000})
        install_active_app_guard(ctx2)
        install_active_app_spoof(ctx2)
        # ⚠️ 必须**在 new_page() 之前**装 ⇒ 否则脚本会改生产的 active-app
        ctx2.add_init_script(BLOCK_AUTOPLAY_JS)
        page2 = ctx2.new_page()
        errors2: list[str] = []
        page2.on("pageerror", lambda e: errors2.append(str(e)))

        page2.goto(f"{BASE}/#{CARD}", wait_until="networkidle")
        page2.wait_for_timeout(2500)
        skip2 = page2.locator("#wd-wizard-root-qwen3-asr .wd-wizard-skip-btn").first
        if skip2.count() and skip2.is_visible():
            skip2.click()
            page2.wait_for_timeout(1500)
        page2.wait_for_selector("#page-qwen3-asr:not(.hidden)", timeout=20000)
        page2.wait_for_selector("#asr-interp-block", timeout=30000)
        page2.wait_for_timeout(1000)

        page2.fill("#asr-source", SRC_WAV)
        page2.wait_for_timeout(800)

        # 基线（计数是**累计**的 ⇒ 看增量）
        _st, base = api("GET", "/api/qwen3-asr/interp/status")
        base_sub = int(base.get("submitted") or 0)
        base_rel = int(base.get("stalled_released") or 0)
        base_done = int((base.get("tts_status") or {}).get("done") or 0)

        # ⚠️ 测试播放一律 1%（别用 localStorage 里的旧值）
        force_test_volume(page2)
        page2.click("#asr-interp-start")
        page2.wait_for_timeout(1500)
        media = page2.eval_on_selector(
            "#asr-audio-player",
            "el => ({paused: el.paused, t: el.currentTime})")
        check("**自动播放被拦 ⇒ 媒体确实没在播**",
              media.get("paused") is True and float(media.get("t") or 0) == 0.0,
              f"得到 {media}")
        hint2 = page2.inner_text("#asr-interp-hint")
        check("提示里**说清楚是自动播放被拦**", "拦" in hint2, f"得到 {hint2!r}")

        # 开始识别 ⇒ 产生句子
        # ⚠️ 第一场的识别可能**还在跑** ⇒ 按钮是 disabled ⇒ Playwright 会
        # 自动等到可点（这里放宽超时，别让它 30 s 就报错）。
        page2.click("#asr-start-btn", timeout=90000)

        # 等兜底放行（FOLLOW_STALL_SEC = 10 s ⇒ 给足时间）
        deadline = time.time() + 180
        sub = base_sub
        rel = base_rel
        done = base_done
        stalled_seen = False
        pending_seen = 0
        status = {}
        while time.time() < deadline:
            _st, status = api("GET", "/api/qwen3-asr/interp/status")
            sub = int(status.get("submitted") or 0)
            rel = int(status.get("stalled_released") or 0)
            done = int((status.get("tts_status") or {}).get("done") or 0)
            pending_seen = max(pending_seen, int(status.get("pending") or 0))
            if status.get("playhead_stalled"):
                stalled_seen = True
            # ⚠️ **两个条件都要满足**才停：
            # ① 句子进了 TTS 队列（``submitted``）；
            # ② **音频真的播出来了**（队列的 ``done``）⇒ 这才是用户
            #    真正关心的（「听得见译文」）。
            if sub > base_sub and done > base_done:
                break
            page2.wait_for_timeout(1000)

        check("面板提示过「播放头卡住」（playhead_stalled）", stalled_seen,
              f"status={status}")
        check("句子确实被压住过（pending > 0）", pending_seen > 0,
              f"pending 峰值={pending_seen}")
        # ⚠️⚠️ **这里断言的是「播出来了」，不是放行器的兜底计数** ——
        # **文件源**的放行器**根本不查播放头**（``interp.py``
        # ``_batch_ready``：文件源按**内容量**攒批 ⇒ 直接放行）⇒
        # 放行器那个 ``stalled_released`` 计数**永远不涨**（它是**实时流**
        # 的兜底：麦 → 一边说一边播 ⇒ 播放头不动 ⇒ 10 s 后强行放行）。
        # ⇒ **文件源**的「媒体暂停 ⇒ 别把译文永久压住」由**队列的播放
        # 时钟**兜底（``queue.py::_clock_ready`` 的 ``_CLOCK_STALL_SEC``
        # = 8 s：源不再往前走 ⇒ 放行）⇒ 断言落在**用户可见的结果**上。
        check("**译文没有被永久压住（真的播出来了）**（队列 done 增量 > 0）",
              done > base_done, f"{base_done} → {done} · status={status}")
        check("**兜底后译文仍进了 TTS 队列**（submitted 增量 > 0）",
              sub > base_sub, f"{base_sub} → {sub} · status={status}")
        check("播放头从没走过（自动播放被拦的指纹）",
              status.get("playhead_moved") is False, f"得到 {status}")

        _st, q = api("GET", "/api/tts/queue/status")
        check("TTS 队列确实收到了句子（depth/done > 0）",
              int(q.get("depth") or 0) > 0 or int(q.get("done") or 0) > 0,
              f"得到 {q}")

        # 状态行也要说清楚
        #
        # ⚠️⚠️ **必须等，不能只读一次**（2026-11-25 实测踩过）——
        # 「播放头卡住」这个提示是**引擎 3 秒后**才判出来的
        # （``FOLLOW_STALL_WARN_SEC`` = 3 s）⇒ 读早了会读到「正在播报」
        # 那句 ⇒ **假失败**。
        #
        # ⚠️ **为什么这次会读早**：上面的循环一旦 ``sub``/``done`` 都涨了
        # 就退出 ⇒ **上一场遗留的 TTS 队列**还在放的时候，``done`` 会很快
        # 涨 ⇒ 采样点落在那 3 秒窗口里 ⇒ 状态行还没来得及说「卡住」。
        # ⇒ 这里改成**轮询到说清楚为止**（有上限）。
        status_text = ""
        deadline_txt = time.time() + 25
        while time.time() < deadline_txt:
            status_text = page2.inner_text("#asr-interp-status")
            if ("没在放" in status_text or "兜底" in status_text
                    or "卡住" in status_text):
                break
            page2.wait_for_timeout(1000)
        check("状态行提到媒体没在放 / 已兜底放行",
              "没在放" in status_text or "兜底" in status_text
              or "卡住" in status_text,
              f"得到 {status_text!r}")

        real_errors2 = [e for e in errors2 if "favicon" not in e.lower()]
        check("第二场全程无 JS 错误", not real_errors2, f"错误: {real_errors2[:3]}")

        page2.click("#asr-interp-stop")
        page2.wait_for_timeout(800)
        # ⚠️ **场次之间排干净**（见 settle_rounds）—— 不排的话下一场
        # 会把**同一段译文**再灌一遍 ⇒ 用户听到的是叠着播的重复内容。
        settle_rounds()
        ctx2.close()

        # ================================================================ #
        # **第三场：媒体先播完，再开始识别**（用户报「只播放了第一句」）
        #
        # ⚠️ **根因**：媒体播完 ⇒ ``paused=true, ended=true`` ⇒ 前端上报
        # ``playing=false`` ⇒ 放行器「播放头到达就放行」那条判据失效；
        # 而兜底放行又要求「播放头**从没走过**」⇒ 媒体播过 ⇒ 兜底也不触发
        # ⇒ **媒体播完之后才识别出来的句子被永久压住** ⇒ 只播了第一句。
        #
        # ⇒ 这一场**故意**让媒体先播完（文件 15 s）再开始识别 ⇒
        #    识别出来的句子**必须全部**被放行。
        # ================================================================ #
        print("== 第三场：媒体先播完 ⇒ 剩下的译文照样全播 ==")
        ctx3 = browser.new_context(ignore_https_errors=True,
                                   viewport={"width": 1440, "height": 1000})
        install_active_app_guard(ctx3)
        install_active_app_spoof(ctx3)
        page3 = ctx3.new_page()
        errors3: list[str] = []
        page3.on("pageerror", lambda e: errors3.append(str(e)))

        page3.goto(f"{BASE}/#{CARD}", wait_until="networkidle")
        page3.wait_for_timeout(2500)
        skip3 = page3.locator("#wd-wizard-root-qwen3-asr .wd-wizard-skip-btn").first
        if skip3.count() and skip3.is_visible():
            skip3.click()
            page3.wait_for_timeout(1500)
        page3.wait_for_selector("#page-qwen3-asr:not(.hidden)", timeout=20000)
        page3.wait_for_selector("#asr-interp-block", timeout=30000)
        page3.fill("#asr-source", SRC_WAV)
        page3.wait_for_timeout(800)

        _st, base3 = api("GET", "/api/qwen3-asr/interp/status")
        base3_seg = int(base3.get("segments") or 0)
        base3_sub = int(base3.get("submitted") or 0)

        # ⚠️ 测试播放一律 1%（别用 localStorage 里的旧值）
        force_test_volume(page3)
        page3.click("#asr-interp-start")
        # ⚠️ **等媒体播完**（15 s 的文件 + 余量）
        deadline = time.time() + 60
        ended = False
        while time.time() < deadline:
            page3.wait_for_timeout(500)
            m = page3.eval_on_selector(
                "#asr-audio-player",
                "el => ({ended: el.ended, t: el.currentTime})")
            if m.get("ended") is True:
                ended = True
                break
        check("媒体确实播完了", ended is True, "等不到 ended")

        # 源已经结束 ⇒ 这时候开始识别
        page3.click("#asr-start-btn", timeout=90000)

        deadline = time.time() + 180
        st3: dict = {}
        while time.time() < deadline:
            _st, st3 = api("GET", "/api/qwen3-asr/interp/status")
            seg3 = int(st3.get("segments") or 0) - base3_seg
            sub3 = int(st3.get("submitted") or 0) - base3_sub
            if seg3 > 0 and sub3 >= seg3:
                break
            page3.wait_for_timeout(1000)
        seg3 = int(st3.get("segments") or 0) - base3_seg
        sub3 = int(st3.get("submitted") or 0) - base3_sub
        check(f"源结束后识别出了句子（segments 增量 {seg3}）", seg3 > 0,
              f"status={st3}")
        check(f"**源播完后，句子全部被放行**（submitted {sub3} >= segments {seg3}）",
              sub3 >= seg3 and sub3 > 0, f"status={st3}")
        check("没有句子被永久压住（pending = 0）",
              int(st3.get("pending") or 0) == 0, f"status={st3}")
        check("状态里标了「源已结束」（source_done）",
              st3.get("source_done") is True, f"status={st3}")

        real_errors3 = [e for e in errors3 if "favicon" not in e.lower()]
        check("第三场全程无 JS 错误", not real_errors3, f"错误: {real_errors3[:3]}")

        page3.click("#asr-interp-stop")
        page3.wait_for_timeout(800)
        # ⚠️ **场次之间排干净**（见 settle_rounds）—— 不排的话下一场
        # 会把**同一段译文**再灌一遍 ⇒ 用户听到的是叠着播的重复内容。
        settle_rounds()
        ctx3.close()

        # ================================================================ #
        # **第四场：媒体声称在播、播放头却一动不动**（用户报「只播放了
        # 第一句」的**现场**：日志里浏览器只上报过 1 次播放头 (0, true)）
        #
        # ⇒ 媒体卡在缓冲 / 解码不动 ⇒ ``currentTime`` 不走 ⇒ 播放头冻住
        # ⇒ 「播放头到达就放行」永远不成立 ⇒ 句子全被压住
        # ⇒ ⇒ **兜底放行**（10 s 后）⇒ 译文仍然出声。
        # ================================================================ #
        print("== 第四场：媒体卡住（声称在播但播放头不动）⇒ 兜底放行 ==")
        ctx4 = browser.new_context(ignore_https_errors=True,
                                   viewport={"width": 1440, "height": 1000})
        install_active_app_guard(ctx4)
        install_active_app_spoof(ctx4)
        # ⚠️ 让 ``currentTime`` **永远返回 0**（媒体在放、时间不走）
        ctx4.add_init_script("""
(() => {
  const proto = HTMLMediaElement.prototype;
  Object.defineProperty(proto, "currentTime", {
    get() { return 0; },
    set(v) { /* 冻住 */ },
    configurable: true,
  });
})();
""")
        page4 = ctx4.new_page()
        errors4: list[str] = []
        page4.on("pageerror", lambda e: errors4.append(str(e)))

        page4.goto(f"{BASE}/#{CARD}", wait_until="networkidle")
        page4.wait_for_timeout(2500)
        skip4 = page4.locator("#wd-wizard-root-qwen3-asr .wd-wizard-skip-btn").first
        if skip4.count() and skip4.is_visible():
            skip4.click()
            page4.wait_for_timeout(1500)
        page4.wait_for_selector("#page-qwen3-asr:not(.hidden)", timeout=20000)
        page4.wait_for_selector("#asr-interp-block", timeout=30000)
        page4.fill("#asr-source", SRC_WAV)
        page4.wait_for_timeout(800)

        _st, base4 = api("GET", "/api/qwen3-asr/interp/status")
        base4_sub = int(base4.get("submitted") or 0)
        base4_rel = int(base4.get("stalled_released") or 0)
        base4_seg = int(base4.get("segments") or 0)

        # ⚠️ 测试播放一律 1%（别用 localStorage 里的旧值）
        force_test_volume(page4)
        page4.click("#asr-interp-start")
        page4.wait_for_timeout(1500)
        media4 = page4.eval_on_selector(
            "#asr-audio-player",
            "el => ({paused: el.paused, t: el.currentTime})")
        check("**媒体声称在播、但播放头不动**（复现用户现场）",
              media4.get("paused") is False
              and float(media4.get("t") or 0) == 0.0,
              f"得到 {media4}")

        page4.click("#asr-start-btn", timeout=90000)

        deadline = time.time() + 180
        st4: dict = {}
        sub4 = base4_sub
        rel4 = base4_rel
        seg4 = 0
        while time.time() < deadline:
            _st, st4 = api("GET", "/api/qwen3-asr/interp/status")
            sub4 = int(st4.get("submitted") or 0) - base4_sub
            rel4 = int(st4.get("stalled_released") or 0) - base4_rel
            seg4 = int(st4.get("segments") or 0) - base4_seg
            if seg4 > 0 and sub4 >= seg4:
                break
            page4.wait_for_timeout(1000)
        # ⚠️ **断言「结局」而不是「走的哪条通道」** —— 放行有三条路
        # （兜底 / 源已结束 / 播放头到了），用户只关心**一句都不能丢**。
        check(f"**一句都没丢**（submitted {sub4} ≥ segments {seg4}）",
              seg4 > 0 and sub4 >= seg4, f"status={st4}")
        check("没有句子被永久压住（pending = 0）",
              int(st4.get("pending") or 0) == 0, f"status={st4}")
        check("译文确实进了 TTS 队列（submitted 增量 > 0）",
              sub4 > 0, f"增量={sub4} · status={st4}")

        real_errors4 = [e for e in errors4 if "favicon" not in e.lower()]
        check("第四场全程无 JS 错误", not real_errors4, f"错误: {real_errors4[:3]}")

        page4.click("#asr-interp-stop")
        page4.wait_for_timeout(800)
        # ⚠️ **场次之间排干净**（见 settle_rounds）—— 不排的话下一场
        # 会把**同一段译文**再灌一遍 ⇒ 用户听到的是叠着播的重复内容。
        settle_rounds()
        ctx4.close()

        browser.close()

    print(f"\n通过 {_PASS} / 失败 {_FAIL}")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
