#!/usr/bin/env python3
"""回归：**「含音轨的视频」的同传要跟着字幕的节奏**（含拖进度条）。

用户报（2026-11-25）
------------------
「『含音轨的视频』在同传播放的时候请跟随字幕的节奏，也包括进度条前后拖动」

实测到的两个根因
----------------
① **「播放时机」是前端本地状态** —— 它在**没有播放头的源**（板载麦 /
   浏览器麦）上会被强制改成「now」⇒ 用户从麦克风切回视频时，这个「now」
   **留在本地** ⇒ 「开始同传」就把它发给服务端 ⇒ 引擎日志：「播放时钟
   **已关掉（gate=now）**」⇒ 队列的播放时钟被关掉 ⇒ 译文**合成完就播**
   ⇒ 而 ASR 跑在播放头**前面** ⇒ **比字幕位置超前 15~75 秒**（实测）。

② **队列的播放时钟有个「卡住计时器」**（``_clock_stall_at``）—— 它
   **跨会话**保留 ⇒ 上一轮会话结束时媒体停了，计时器就停在那一刻 ⇒
   **下一轮会话一开始它就是陈旧的** ⇒ 8 秒后时钟认定「源不再往前走」⇒
   **对所有句子一律放行** ⇒ 译文又跑到字幕前面去了。
   ⇒ 实测：**第一轮**同步误差 ±0.2 s，**紧接着的第二轮** −13~−24 s。

⇒ 所以这个回归**连跑两轮**（第二轮引擎是热的）—— 只跑一轮抓不到 ②。

覆盖
====
1. **视频来源 ⇒ 播放时机默认「跟播放」**（服务端 gate=follow）；
2. **播放头是活的**（``playhead_missing`` 为假 ⇒ 跟播的前提还在）；
3. **同步误差**：译文出声时，源话的播放头与这句的字幕位置之差（``|err|``）
   —— 除了第一句的**冷启动**起调延迟，其余每句都要贴住字幕；
4. **拖进度条**：前跳 ⇒ 服务端认出 ``dir=fwd``；后跳 ⇒ ``dir=back``
   ⇒ 两边都要**重新武装**（``replayed`` 增长）；
5. **刷新页面**：前端要**接管**仍在跑的同传（面板不该说「同传未开启」）
   ⇒ 源要恢复回视频 ⇒ **模拟「前端停止上报」** ⇒ 服务端要能看出来
   （``playhead_missing``）⇒ 面板要**说清楚** ⇒ 把上报接回来 ⇒
   **心跳**让 age 回落 ⇒ 拖进度条仍然认得出来。

⚠️ **「同传停掉后按钮重新可点」也要断言** —— 前端以前只更新 ``status``
不更新 ``running`` ⇒ 面板永远以为同传在跑 ⇒ 「开始同传」永远 disabled
⇒ **第二轮根本开不起来**（本脚本第一版就栽在这儿）。

跑之前要满足：**ASR / 翻译 / TTS 三个模型都在跑**。

跑法::

    .venv/bin/python scripts/verify_interp_video_sync.py
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

_SCRIPT_DIR = os.path.join(_ROOT, "scripts")
if str(_SCRIPT_DIR) not in sys.path:
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
#: 设备端的**含音轨的视频**（144.7 s，中文，双语字幕）。
VIDEO = "/userdata/assets/uploads/asr_zh_seek_test.mp4"

#: **探针音量 1%**（只量指标，不听声音）—— 与产品默认 5% 是两条纪律。
PROBE_VOLUME_PCT = 1

#: 同步误差的判据（秒）——
#: ⚠️ **第一句不算**：它的起调延迟是**模型 + TTS 的冷启动**（~5~8 s），
#: 不是「跟不跟得上」⇒ 判据只管**第二句往后**。
SYNC_TOL_SEC = 3.0
#: 至少要采到这么多句才下结论（不然就是「没测到」，不是「通过」）。
MIN_LINES = 3
#: **连跑几轮**（第二轮才是「引擎是热的」那一轮 ⇒ 抓「陈旧时钟」）。
ROUNDS = 2

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


def api(method: str, path: str, payload=None, timeout: int = 120):
    url = BASE + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, context=_CTX, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        return e.code, {}
    if not raw:
        return 200, {}
    try:
        return 200, json.loads(raw.decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return 200, {}


def settle(timeout: float = 60.0) -> None:
    """收尾：停同传 + 清队列 + 清识别（别把状态留给下一场）。"""
    api("POST", "/api/qwen3-asr/interp/stop", {})
    api("POST", "/api/tts/queue/clear", {})
    api("POST", "/api/qwen3-asr/stop?reason=internal", {})
    api("POST", "/api/qwen3-asr/reset", {})
    deadline = time.time() + timeout
    while time.time() < deadline:
        _st, q = api("GET", "/api/tts/queue/status")
        if (int(q.get("depth") or 0) == 0 and int(q.get("inflight") or 0) == 0
                and not q.get("playing")):
            return
        time.sleep(0.5)


def _enabled(page, selector: str) -> bool:
    """这个按钮此刻可点吗。"""
    try:
        return bool(page.eval_on_selector(selector, "el => !el.disabled"))
    except Exception:  # noqa: BLE001
        return False


def wait_enabled(page, selector: str, timeout: float = 45.0) -> bool:
    """等一个按钮**变成可点**（面板要跟上服务端的真实状态）。

    ⚠️ **为什么必须等**：同传不是通过界面停的（脚本用 API 停的）⇒ 面板
    要靠**轮询**才知道 ⇒ 轮询是 1 秒一次 ⇒ 按钮不会立刻可点 ⇒ 直接点
    会超时（踩过：``Page.click: element is not enabled``）。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _enabled(page, selector):
            return True
        page.wait_for_timeout(500)
    return False


def _ph_at(samples, wall: float):
    """某个墙钟时刻的播放头（最近一次采样）。"""
    best = None
    for w, p in samples:
        if w <= wall and p >= 0:
            best = p
        else:
            break
    return best


def main() -> int:
    errors: list[str] = []
    settle()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=[
            "--no-sandbox",
            "--autoplay-policy=user-gesture-required",
        ])
        ctx = browser.new_context(ignore_https_errors=True,
                                  viewport={"width": 1440, "height": 1000})
        # ⚠️ **必须 mock 掉 PUT/DELETE active-app**（见 active_app_guard）。
        install_active_app_guard(ctx)
        install_active_app_spoof(ctx)
        page = ctx.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append("console.error: " + m.text)
                if m.type == "error" else None)

        page.goto(f"{BASE}/#qwen3-asr", wait_until="networkidle")
        page.wait_for_timeout(2500)
        skip = page.locator("#wd-wizard-root-qwen3-asr .wd-wizard-skip-btn").first
        if skip.count() and skip.is_visible():
            skip.click()
            page.wait_for_timeout(1500)
        page.wait_for_selector("#page-qwen3-asr:not(.hidden)", timeout=20000)
        page.wait_for_timeout(1200)

        # ---------------- 来源页签 + 路径 ----------------
        page.click('#asr-source-tabs .asr-source-tab[data-source="video"]')
        page.wait_for_timeout(500)
        page.fill("#asr-source", VIDEO)
        page.dispatch_event("#asr-source", "input")
        page.wait_for_timeout(1000)
        src = page.evaluate("() => (window.Qwen3Asr || {})._source")
        check("切到视频来源", src == "video", f"得到 {src!r}")

        for rnd in range(1, ROUNDS + 1):
            print(f"\n===== 第 {rnd} 轮（{'引擎是热的' if rnd > 1 else '引擎刚起来'}）=====")

            # ---------------- 轮次准备：上一轮要**彻底停干净** ----------------
            #
            # ⚠️ 三件事都得做，缺一个这一轮就测不准：
            # * **识别停掉** ⇒ 否则「开始识别」按钮一直 disabled ⇒ 点不动；
            # * **媒体归零** ⇒ 上一轮拖过进度条 ⇒ 播放头与**后端从 0 开始
            #   读的音轨**对不上 ⇒ 同步误差是假的；
            # * **媒体停过** ⇒ 队列播放时钟的「卡住计时器」是**陈旧的**
            #   ⇒ 这正是根因②的场景（**所以这一步不能省**）。
            api("POST", "/api/qwen3-asr/interp/stop", {})
            api("POST", "/api/tts/queue/clear", {})
            api("POST", "/api/qwen3-asr/stop?reason=internal", {})
            api("POST", "/api/qwen3-asr/reset", {})
            page.evaluate("""() => {
                const v = document.getElementById('asr-video');
                if (v) { try { v.pause(); v.currentTime = 0; } catch (e) {} }
            }""")
            page.wait_for_timeout(2000)
            check(f"第 {rnd} 轮：同传停掉后「开始同传」重新可点",
                  wait_enabled(page, "#asr-interp-start"))
            check(f"第 {rnd} 轮：识别停掉后「开始识别」重新可点",
                  wait_enabled(page, "#asr-start-btn"))

            # ---------------- 播放时机 ----------------
            gate_val = page.eval_on_selector("#asr-interp-gate", "el => el.value")
            check(f"第 {rnd} 轮：面板上的「播放时机」是「跟播放」",
                  gate_val == "follow", f"得到 {gate_val!r}")

            page.eval_on_selector(
                "#asr-interp-volume",
                """(el, pct) => {
                    el.value = String(pct);
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                }""", PROBE_VOLUME_PCT)
            page.click("#asr-interp-start")
            page.wait_for_timeout(2000)
            _st, st = api("GET", "/api/qwen3-asr/interp/status")
            check(f"第 {rnd} 轮：服务端 gate = follow", st.get("gate") == "follow",
                  f"得到 {st.get('gate')!r}")
            check(f"第 {rnd} 轮：服务端 source = video",
                  st.get("source") == "video", f"得到 {st.get('source')!r}")
            check(f"第 {rnd} 轮：播放头是活的（playhead_missing 为假）",
                  st.get("playhead_missing") is False,
                  f"得到 age={st.get('playhead_age_sec')}")

            # ---------------- 识别 + 采样 ----------------
            page.click("#asr-start-btn", timeout=90000)
            page.evaluate("""() => {
                window.__ph = [];
                const v = document.getElementById('asr-video');
                const t = setInterval(() => {
                    window.__ph.push([
                        performance.timeOrigin / 1000 + performance.now() / 1000,
                        v ? (v.currentTime || 0) : -1]);
                }, 100);
                window.__phStop = () => clearInterval(t);
            }""")
            deadline = time.time() + 180
            while time.time() < deadline:
                _st, s = api("GET", "/api/qwen3-asr/interp/status")
                if int(s.get("translated") or 0) >= 4:
                    break
                page.wait_for_timeout(2000)
            check(f"第 {rnd} 轮：识别出了句子",
                  int(s.get("translated") or 0) >= 4, f"得到 {s.get('translated')}")

            print(f"   采样 45 s …")
            page.wait_for_timeout(45000)
            samples = page.evaluate("() => window.__ph") or []

            _st, q = api("GET", "/api/tts/queue/status")
            rows = []
            for t in (q.get("timings") or []):
                tp = float(t.get("t_play") or 0)
                src_s = float(t.get("src_start_sec") or -1)
                if src_s < 0 or not tp:
                    continue
                ph = _ph_at(samples, tp)
                if ph is None:
                    continue
                rows.append((t.get("seq"), ph, src_s, ph - src_s,
                             (t.get("text") or "")[:24]))
            for r in rows:
                print("    seq=%s ph=%.2f src=%.2f err=%+.2f %s" % r)
            # ⚠️ **第一句是冷启动**（模型 + TTS 起调）⇒ 不计入同步误差。
            errs = [r[3] for r in rows[1:]]
            check(f"第 {rnd} 轮：采到了够数的句子（{len(rows)} 句）",
                  len(rows) >= MIN_LINES, f"得到 {len(rows)}")
            if errs:
                big = [e for e in errs if abs(e) > SYNC_TOL_SEC]
                check(f"第 {rnd} 轮：译文贴着字幕位置"
                      f"（|err| ≤ {SYNC_TOL_SEC} s，{len(errs) - len(big)}/{len(errs)} 句）",
                      not big, f"超差的: {['%.2f' % e for e in big]}")

            # ---------------- 拖进度条（前跳 / 后跳） ----------------
            print("   拖进度条 …")
            _st, before = api("GET", "/api/qwen3-asr/interp/status")
            replayed0 = int(before.get("replayed") or 0)
            page.evaluate("""() => {
                const v = document.getElementById('asr-video');
                v.currentTime = 60;
            }""")
            page.wait_for_timeout(6000)
            _st, mid = api("GET", "/api/qwen3-asr/interp/status")
            ls = mid.get("last_seek") or {}
            check(f"第 {rnd} 轮：前跳 60 s ⇒ 服务端认出是 seek（fwd）",
                  ls.get("dir") == "fwd", f"得到 {ls}")
            check(f"第 {rnd} 轮：前跳后重新武装了句子（replayed 增长）",
                  int(mid.get("replayed") or 0) > replayed0,
                  f"{replayed0} → {mid.get('replayed')}")

            replayed1 = int(mid.get("replayed") or 0)
            page.evaluate("""() => {
                const v = document.getElementById('asr-video');
                v.currentTime = 20;
            }""")
            page.wait_for_timeout(6000)
            _st, after = api("GET", "/api/qwen3-asr/interp/status")
            ls2 = after.get("last_seek") or {}
            check(f"第 {rnd} 轮：后跳 20 s ⇒ 服务端认出是 seek（back）",
                  ls2.get("dir") == "back", f"得到 {ls2}")
            check(f"第 {rnd} 轮：后跳后重新武装了句子（replayed 增长）",
                  int(after.get("replayed") or 0) > replayed1,
                  f"{replayed1} → {after.get('replayed')}")

            # ---------------- 刷新页面 ⇒ 前端要**接管**仍在跑的同传 ----------------
            if rnd == ROUNDS:
                print("   刷新页面 ⇒ 前端要接管仍在跑的同传 …")
                page.reload(wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(3000)
                skip = page.locator(
                    "#wd-wizard-root-qwen3-asr .wd-wizard-skip-btn").first
                if skip.count() and skip.is_visible():
                    skip.click()
                    page.wait_for_timeout(1200)
                page.wait_for_selector("#page-qwen3-asr:not(.hidden)",
                                       timeout=20000)
                page.wait_for_timeout(2500)

                # ① **接管** —— 面板不该说「同传未开启」（它明明还在跑）。
                panel = (page.text_content("#asr-interp-status") or "").strip()
                check("刷新后前端接管了仍在跑的同传",
                      "未开启" not in panel, f"面板写着 {panel!r}")
                gate_val = page.eval_on_selector("#asr-interp-gate",
                                                 "el => el.value")
                check("刷新后播放时机仍是「跟播放」", gate_val == "follow",
                      f"得到 {gate_val!r}")
                # ⚠️ **源**由 `_restoreFromServer()` 从服务端恢复（路径 +
                #   源一起）⇒ 刷新后前端不该停在默认的「音频文件」。
                src2 = page.evaluate("() => (window.Qwen3Asr || {})._source")
                check("刷新后源恢复回视频", src2 == "video", f"得到 {src2!r}")

                # ② **模拟「前端停止上报」** ⇒ 服务端要能看出来 ⇒ 面板要
                #    **说清楚**（不能让用户对着沉默干瞪眼 —— 这就是
                #    ``playhead_missing`` 存在的理由）。
                #
                # ⚠️ **为什么用「停掉上报」而不是「等它自己断」** —— 媒体
                # 暂停时播放头**不变** ⇒ 前端**本来就不发**（除非有**心跳**）
                # ⇒ 「没上报」有两种完全不同的原因：
                #   * **前端还在，只是媒体暂停着** ⇒ 面板该说「播放头卡住」；
                #   * **前端没了**（页面挂了 / 标签关了）⇒ 面板该说「没上报」。
                # ⇒ 用「主动停掉」把第二种**确定地**复现出来。
                page.evaluate("""() => {
                    const q = window.Qwen3Asr || {};
                    if (q._interpStopPlayhead) q._interpStopPlayhead();
                }""")
                deadline = time.time() + 20
                missing = False
                while time.time() < deadline:
                    _st, st2 = api("GET", "/api/qwen3-asr/interp/status")
                    if st2.get("playhead_missing"):
                        missing = True
                        break
                    page.wait_for_timeout(1000)
                check("前端停止上报 ⇒ 服务端能看出来（playhead_missing）",
                      missing)
                # ⚠️ **面板是前端轮询（1 秒一次）刷的** ⇒ 服务端已经知道了，
                # 面板**可能还没换文案** ⇒ 要**等**它，别读早了。
                panel2 = ""
                deadline = time.time() + 10
                while time.time() < deadline:
                    panel2 = (page.text_content("#asr-interp-status")
                              or "").strip()
                    if "播放头没上报" in panel2:
                        break
                    page.wait_for_timeout(500)
                check("面板明确说了「播放头没上报」",
                      "播放头没上报" in panel2, f"面板写着 {panel2!r}")

                # ③ **把上报接回来** ⇒ **心跳**让 age 回落 ⇒ 「前端还在」
                #    与「媒体在不在动」是**两件事**。
                page.evaluate("""() => {
                    const q = window.Qwen3Asr || {};
                    if (q._interpStartPlayhead) q._interpStartPlayhead();
                }""")
                age = -1.0
                deadline = time.time() + 30
                while time.time() < deadline:
                    _st, st2 = api("GET", "/api/qwen3-asr/interp/status")
                    age = float(st2.get("playhead_age_sec") or -1)
                    if 0 <= age < 5.0:
                        break
                    page.wait_for_timeout(1000)
                check("上报接回来后播放头的 age 回落（< 5 s）", 0 <= age < 5.0,
                      f"得到 age={age}")
                page.evaluate("""() => {
                    const v = document.getElementById('asr-video');
                    if (v) v.currentTime = 45;
                }""")
                page.wait_for_timeout(6000)
                _st, a2 = api("GET", "/api/qwen3-asr/interp/status")
                ls3 = a2.get("last_seek") or {}
                check("刷新后拖进度条仍然认得出来", bool(ls3.get("dir")),
                      f"得到 {ls3}")

            # 收尾这一轮：**只停同传**（不清引擎）⇒ 下一轮引擎是热的 ⇒
            # 才抓得到「陈旧时钟」那个坑。
            #
            # ⚠️ **识别不停** —— 下一轮开头会停它 + 媒体归零 ⇒ 那一停才是
            # 「媒体停过」的场景（根因②的前提）。
            api("POST", "/api/qwen3-asr/interp/stop", {})
            api("POST", "/api/tts/queue/clear", {})
            # ⚠️ **面板要跟上服务端的真实状态** ⇒ 「开始同传」重新可点。
            # （前端以前只更新 status 不更新 running ⇒ 按钮永远 disabled。）
            check(f"第 {rnd} 轮：同传停掉后「开始同传」重新可点",
                  wait_enabled(page, "#asr-interp-start"))

        real_errors = [e for e in errors if "favicon" not in e.lower()]
        check("全程无 JS 错误", not real_errors, f"错误: {real_errors[:3]}")

        browser.close()

    settle()
    print("\n通过 %d / 失败 %d" % (_PASS, _FAIL))
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
