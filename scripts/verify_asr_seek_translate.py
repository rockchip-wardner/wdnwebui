#!/usr/bin/env python3
"""真机浏览器回归：**拖动进度条**（前 + 后）+ **开关「实时翻译」** ⇒
字幕每一行都要有译文。

用户报的缺陷
============

对「含音轨的视频」（《圆桌派》EP01 短视），**实时翻译**开着，中途：

1. 往前拖进度条；
2. 又往回拖进度条；
3. 把「实时翻译」取消勾选，再勾选回来；

⇒ **有概率**出现「勾选着实时翻译，但画面字幕上**有几行只有中文、没有
译文**」（用户贴的例：「那个，那个照片，」「别人，别人，」—— 那几句
**只有中文**）。

根因（2026-11-24 实测）
======================

前端四处配合失灵（**服务端没做错什么**）：

1. **开关被轮询撤销** —— ``_trRefresh()``（5 秒一次）用**服务端的旧值**
   覆盖本地的开关状态 ⇒ 正好落在「用户点了」与「服务端处理完」之间时，
   勾选框被**悄悄撤销** ⇒ 服务端其实还**关着** ⇒ 每一次行级请求都被
   **拒收**（``{"ok": false, "skipped": "翻译没开"}``）；
2. **拒收的那几行永远没有译文** —— 前端把「已经要过」记成**永久** ⇒
   再也不重要一次 ⇒ 那一行**永远只有中文**；
3. **重新勾选要重翻整条时间轴** —— 关掉时把译文**删掉** ⇒ 再勾选时
   1500 行全部重来 ⇒ 按每轮 2 个的限速，**40 秒以上**才轮得到播放头
   附近那几行 ⇒ 用户看到的就是「勾着实时翻译，字幕却只有中文」；
4. **请求顺序不看播放头** —— 「从当前行往两边扩」，播放头**不在**已识别
   区间里时（拖动之后必然如此）退化成「从头排」。

修法
====

* 前端：``_trPending`` —— 本地刚改过、还没被服务端确认的开关值**不被
  轮询覆盖**；
* 前端：``TR_REQ_STALE_MS`` —— 「要了但**一直没回来**」的行**过期后
  重要一次** ⇒ 拒收 / 丢队列 / 网络失败都能自愈；
* 前端：关掉开关**只隐藏、不删除**译文 ⇒ 重新勾上**立刻**就有；
* 前端：请求顺序改成「**离播放头最近优先**」。

怎么测（**确定性的，不靠概率**）
==================================

* 「轮询撤销开关」：用 Playwright **把那次 ``POST /translate/settings``
  扣住不放** ⇒ 服务端收不到 ⇒ 服务端的开关保持旧值 ⇒ 这正是「用户刚
  点了、服务端还没处理完」的现场 ⇒ 等过一个轮询周期（5 s）看前端有没有
  被撤销；
* 「拒收 ⇒ 永远没有译文」：把 ``POST /translate/line`` **拦下来**返回
  服务端真实的拒收响应（``{"ok": false, "skipped": "翻译没开"}``）⇒
  看那几行会不会**自己**恢复。

跑之前要满足：**ASR 模型**与**翻译模型**在跑。

跑法::

    .venv/bin/python scripts/verify_asr_seek_translate.py
    .venv/bin/python scripts/verify_asr_seek_translate.py --video /path/to.mp4
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

#: 用户报的那一段（《圆桌派》第八季 EP01 短视）。
VIDEO = ("/userdata/assets/uploads/【圆桌派 第八季】EP01 短视："
         "“五色令人目盲” _ 窦文涛_许子东_马家辉_陈鲁豫 _ "
         "优酷纪实人文 YOUKU DOCUMENTARY.mp4")

#: 中文音频 ⇒ 目标语言选英文才是**真翻译**（中文→中文服务端会「透传」）。
TARGET_LANG = "en"

#: 识别跑多久之后开始测（秒）。
SETTLE_SEC = 60.0

#: 轮询周期是 5 s ⇒ 等这么久就**一定**跨过一次轮询。
POLL_CROSS_SEC = 7.0

#: 重新勾选后，**画面上可见的那几行**最多等多久就该**立刻**有译文。
#:
#: ⚠️ 改动前：关掉时译文被**删掉** ⇒ 再勾选要重翻整条时间轴 ⇒ 40 秒以上。
REOPEN_DEADLINE_SEC = 8.0

#: 「被服务端拒收」的行，最多等多久能自愈（过期重试的间隔是 5 s）。
HEAL_DEADLINE_SEC = 35.0

#: 「关掉」后译文**必须还在**（只是不显示）。
TR_KEEP_MIN = 1

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
_STATE = """
() => {
  const A = window.Qwen3Asr;
  const tl = (A && A._buildSubTimeline) ? A._buildSubTimeline().lines : [];
  const rows = [];
  for (let i = 0; i < tl.length; i++) {
    const ln = tl[i];
    const slot = String(ln.id || '').split(':').slice(0, 2).join(':');
    const it = (A._trLineTr && A._trLineTr.get(slot)) || null;
    rows.push({
      slot: slot,
      text: (ln.words || []).map((w) => w.text).join(''),
      start: ln.start, end: ln.end,
      tr: it ? (it.text || '') : null,
      st: it ? (it.status || '') : null,
      requested: A._trLineReq ? A._trLineReq.has(slot) : false,
    });
  }
  const cb = document.getElementById('asr-tr-enabled');
  const vis = [...document.querySelectorAll('#asr-video-sub .asr-sub-line')]
    .filter((el) => getComputedStyle(el).visibility !== 'hidden'
                 && Number(el.style.opacity || '0') > 0.01)
    .map((el) => ({
      orig: ((el.querySelector('.asr-sub-orig') || {}).textContent || '').trim(),
      tr: ((el.querySelector('.asr-sub-tr') || {}).textContent || '').trim(),
    }));
  return {
    trEnabled: A && A._tr ? !!A._tr.enabled : null,
    trMode: A ? A._trMode : null,
    srcLang: A ? (A._trSrcLang ? A._trSrcLang() : '') : null,
    snapLang: A && A.snapshot ? (A.snapshot.language || '') : null,
    checkbox: cb ? !!cb.checked : null,
    pending: !!(A && A._trPending
                 && Object.prototype.hasOwnProperty.call(A._trPending, 'enabled')),
    trMapSize: A && A._trLineTr ? A._trLineTr.size : 0,
    itemsSize: A && A._tr.items ? A._tr.items.length : 0,
    idBySeq: A && A._trLineIdBySeq ? A._trLineIdBySeq.size : 0,
    slotMap: A && A._trLineSlot ? A._trLineSlot.size : 0,
    reqSize: A && A._trLineReq ? A._trLineReq.size : 0,
    qDepth: A ? A._trQueueDepth : 0,
    frames: (window.__trFrames || []).length,
    framesSample: (window.__trFrames || []).slice(0, 3),
    rows: rows,
    visible: vis,
    rounds: (A && A.rounds) ? A.rounds.length : 0,
    epochs: (A && A._splitEpochs && A.rounds) ? A._splitEpochs(A.rounds).length : 0,
    videoTime: (document.getElementById('asr-video') || {}).currentTime || 0,
  };
}
"""

#: 钩住 WS 的 ``translation`` 帧 —— 「服务端翻了、前端没挂」时用它定位。
_HOOK = """
() => {
  const A = window.Qwen3Asr;
  window.__trFrames = [];
  const orig = A._onFrame.bind(A);
  A._onFrame = (msg) => {
    if (msg && msg.type === 'translation') {
      window.__trFrames.push({
        seq: msg.seq, index: msg.index, status: msg.status,
        stream_id: msg.stream_id, text: (msg.text || '').slice(0, 20),
      });
      if (window.__trFrames.length > 40) window.__trFrames.shift();
    }
    return orig(msg);
  };
  return true;
}
"""


def _state(page) -> dict:
    return page.evaluate(_STATE)


def _near_playhead(page, win: float = 30.0) -> list:
    """**播放头附近**的行（± ``win`` 秒）—— 用户真正看得见的那几行。"""
    st = _state(page)
    t = st["videoTime"]
    return [r for r in st["rows"]
            if (r["start"] is not None and abs(t - r["start"]) <= win)]


def _missing(rows: list) -> list:
    """没有译文的行（**最后一行**还在长 ⇒ 不算）。"""
    miss = [r for r in rows if not (r["tr"] or "").strip()]
    return miss[:-1] if miss else []


def _seek(page, sec: float) -> None:
    page.evaluate(
        """(t) => { const v = document.getElementById('asr-video');
                   if (v) { v.pause(); v.currentTime = t; } }""",
        sec)
    page.wait_for_timeout(1500)


def _report(rows: list, label: str) -> dict:
    miss = _missing(rows)
    print("  [%s] 行数=%d  没译文的行=%d（不含还在长的最后一行）"
          % (label, len(rows), len(miss)))
    for r in miss[:8]:
        print("      没译文: [%s] %r  (requested=%s)"
              % (r["slot"], r["text"][:36], r["requested"]))
    return {"missing": miss}


def _main(video: str) -> int:
    print("=" * 72)
    print("真机浏览器回归：拖动进度条（前 + 后）+ 开关「实时翻译」⇒ 字幕有译文")
    print("=" * 72)

    if not os.path.exists(video):
        print("❌ 缺素材 %s" % video)
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
    dropped_before = st.get("dropped") or 0

    errors: list[str] = []
    line_posts: list[float] = []
    held: list = []
    reject = {"on": False}

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

        # --- 拦下 ``POST /translate/line`` ⇒ 制造服务端的「拒收」 ---
        def _line_route(route):
            if reject["on"]:
                # ⚠️ **服务端真实的拒收响应**（HTTP 200 + ``ok:false``）
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"ok": False,
                                               "skipped": "翻译没开"}))
            else:
                route.continue_()

        page.route("**/api/qwen3-asr/translate/line", _line_route)

        page.goto(f"{BASE}/#{CARD}", wait_until="networkidle")
        page.wait_for_timeout(2500)
        skip = page.locator("#wd-wizard-root-qwen3-asr .wd-wizard-skip-btn").first
        if skip.count() and skip.is_visible():
            skip.click()
            page.wait_for_timeout(1500)
        page.wait_for_selector("#page-qwen3-asr:not(.hidden)", timeout=15000)
        # ⚠️ 钩住 WS 的 ``translation`` 帧（「服务端翻了、前端没挂」时定位用）
        page.evaluate(_HOOK)

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
            video,
        )
        page.wait_for_timeout(600)
        page.locator("#asr-start-btn").click()

        # ------------------------------------------------------------------ #
        # [0] 识别一会儿 ⇒ 有行、有译文
        # ------------------------------------------------------------------ #
        print("  … 识别 %d 秒" % int(SETTLE_SEC))
        # ⚠️ **必须用 ``page.wait_for_timeout``，不能用 ``time.sleep``** ——
        #    ``time.sleep`` 期间 **Playwright 不泵事件** ⇒ **路由拦截器不会
        #    执行** ⇒ 被拦截的请求**全部卡住**（浏览器里 fetch 悬着）⇒
        #    页面上一句译文都没有（假象！）。
        page.wait_for_timeout(int(SETTLE_SEC * 1000))
        stt = _state(page)
        print("  识别中：轮次=%s epoch=%s 行=%s 有译文的行=%s 播放头=%.1f s"
              % (stt["rounds"], stt["epochs"], len(stt["rows"]),
                 len([r for r in stt["rows"] if (r["tr"] or "").strip()]),
                 stt["videoTime"]))
        check("识别出了**足够多**的行（> 8 行才测得了）",
              len(stt["rows"]) > 8, f"lines={len(stt['rows'])}")

        # ⚠️ **限速**：请求速率必须 ≤ 服务端的消化能力（≈3.8 句/秒）⇒
        #    服务端的队列**永远不满** ⇒ **不会丢最旧的**（丢的正是**先要
        #    的那几行** = 播放头附近 ⇒ 画面字幕上永远只有中文 —— 用户报）。
        rate_win = 10.0
        n_rate0 = len(line_posts)
        t_rate0 = time.time()
        page.wait_for_timeout(int(rate_win * 1000))
        rate = (len(line_posts) - n_rate0) / max(0.001, time.time() - t_rate0)
        print("  请求速率：%.2f 个/秒（服务端消化能力 ≈ 2.7~3.8 句/秒）" % rate)
        check("翻译请求**没有洪水**（速率 ≤ 4 个/秒 ⇒ 队列不会爆）",
              rate <= 4.0, f"实测 %.2f 个/秒" % rate)

        near = _near_playhead(page)
        n_tr_near = len([r for r in near if (r["tr"] or "").strip()])
        print("  播放头邻域：%d 行，其中 %d 行有译文" % (len(near), n_tr_near))
        check("**播放头附近**的行已经拿到译文（这正是用户看的那些行）",
              near and n_tr_near == len(near),
              f"{n_tr_near}/{len(near)} 行有译文")

        # ------------------------------------------------------------------ #
        # [1] 核心：轮询**不能**撤销用户刚点的开关
        # ------------------------------------------------------------------ #
        print("\n  [1] 轮询不撤销用户的开关（把设置请求扣住不放）")

        def _hold_settings(route):
            # ⚠️ **不 continue** ⇒ 请求悬着 ⇒ 服务端收不到 ⇒ 服务端的
            #    开关保持**旧值** ⇒ 这正是「用户刚点了、服务端还没处理完」
            #    的现场（真实环境里这个窗口只有几十毫秒，轮询正好撞上
            #    的概率 ≈ 1% ⇒ 用户说「**有概率**」）。
            held.append(route)

        page.route("**/api/qwen3-asr/translate/settings", _hold_settings)

        tr_before = stt["trMapSize"]
        page.locator("#asr-tr-enabled").click()          # ⇒ 关掉
        page.wait_for_timeout(1000)
        stt = _state(page)
        check("点「取消勾选」后前端**立刻**是关的",
              stt["trEnabled"] is False and stt["checkbox"] is False,
              f"trEnabled={stt['trEnabled']} checkbox={stt['checkbox']}")
        check("前端记住了「待服务端确认」",
              stt["pending"] is True, f"pending={stt['pending']}")

        # 等过一个轮询周期（5 s）⇒ 旧行为会在这里把开关**撤销**
        page.wait_for_timeout(int(POLL_CROSS_SEC * 1000))
        stt = _state(page)
        check("跨过一次轮询（%.0f s）后**开关没被撤销**" % POLL_CROSS_SEC,
              stt["trEnabled"] is False and stt["checkbox"] is False,
              f"trEnabled={stt['trEnabled']} checkbox={stt['checkbox']}")

        # 放行 ⇒ 服务端这才是真的关掉
        if held:
            held[0].continue_()
            held.clear()
        page.wait_for_timeout(2500)
        _, st = api("GET", "/api/qwen3-asr/translate/status")
        check("放行后**服务端**确实关掉了", not st.get("enabled"),
              f"enabled={st.get('enabled')}")
        page.unroute("**/api/qwen3-asr/translate/settings", _hold_settings)

        # ------------------------------------------------------------------ #
        # [2] 关掉 ⇒ 只**隐藏**，不**删除**
        # ------------------------------------------------------------------ #
        print("\n  [2] 关掉只隐藏、不删除译文")
        stt = _state(page)
        check("关掉后译文**还留着**（只是不显示）",
              stt["trMapSize"] >= TR_KEEP_MIN and stt["trMapSize"] >= tr_before,
              f"trMapSize {tr_before} → {stt['trMapSize']}")
        vis_tr = [v for v in stt["visible"] if (v["tr"] or "").strip()]
        check("关掉后**画面上的译文收掉了**", not vis_tr,
              f"还有译文：{[v['orig'][:20] for v in vis_tr[:3]]}")

        # ------------------------------------------------------------------ #
        # [3] 再勾选 ⇒ 译文**立刻**回来（不用重翻整条时间轴）
        # ------------------------------------------------------------------ #
        print("\n  [3] 再勾选「实时翻译」")
        n_before = len(line_posts)
        t_click = time.time()
        page.locator("#asr-tr-enabled").click()          # ⇒ 再打开
        page.wait_for_timeout(1200)
        stt = _state(page)
        check("再打开后**勾选框是勾选**的", stt["checkbox"] is True,
              f"checkbox={stt['checkbox']}")
        check("再打开后服务端也是开的", stt["trEnabled"] is True,
              f"trEnabled={stt['trEnabled']}")

        got_at = None
        while time.time() - t_click < REOPEN_DEADLINE_SEC:
            stt = _state(page)
            if stt["visible"] and all((v["tr"] or "").strip()
                                      for v in stt["visible"]):
                got_at = time.time() - t_click
                break
            page.wait_for_timeout(300)
        check("**重新勾选后 %.0f s 内**画面上的行就有译文（不用重翻整条时间轴）"
              % REOPEN_DEADLINE_SEC,
              got_at is not None,
              f"等了 {got_at if got_at is not None else '>%.0f' % REOPEN_DEADLINE_SEC} s")
        if got_at is not None:
            print("     用时 %.1f s" % got_at)

        burst = len([t for t in line_posts[n_before:] if t - t_click <= 2.0])
        check("重新勾选**不是一次性几百个请求**（译文没被删掉）",
              burst <= 12, f"2 秒内发了 {burst} 个")

        # ------------------------------------------------------------------ #
        # [4] 被服务端拒收的行 ⇒ **自己**恢复
        # ------------------------------------------------------------------ #
        print("\n  [4] 服务端拒收 ⇒ 那几行自愈（过期重试）")
        # 挑**离播放头最近**的几行（它们下一轮就会被要）⇒ 清掉译文，
        # 让前端重新要 ⇒ 再让服务端拒收 ⇒ 看它们会不会自己恢复。
        def _pick_and_clear():
            return page.evaluate(
                """() => {
                  const A = window.Qwen3Asr;
                  const tl = (A && A._buildSubTimeline) ? A._buildSubTimeline().lines : [];
                  if (!tl.length) return [];
                  const order = A._trLineOrder(tl);
                  const picked = [];
                  for (const i of order) {
                    if (picked.length >= 6) break;
                    if (i >= tl.length - 1) continue;         // 最后一行还在长
                    const ln = tl[i];
                    if (!ln || !ln.id) continue;
                    picked.push(String(ln.id).split(':').slice(0, 2).join(':'));
                  }
                  for (const s of picked) {
                    if (A._trLineTr) A._trLineTr.delete(s);
                    if (A._trLineReq) A._trLineReq.delete(s);
                  }
                  A._trVer = (A._trVer || 0) + 1;
                  return picked;
                }""")

        slots = _pick_and_clear()
        check("挑到了待测的行", len(slots) >= 3, f"slots={slots}")
        if slots:
            # ⚠️ **顺序很重要**：**先**打开拦截，**再**清译文 ——
            #    否则「清完之后、拦截生效之前」发出去的那条请求会带着
            #    **真译文**回来 ⇒ 断言「拒收期间没有译文」就假失败
            #    （2026-11-24 踩过）。
            reject["on"] = True
            # ⚠️ **清两遍** —— 第一遍清掉已挂上的译文；第二遍清掉
            #    「清的那一刻还在路上」的那几行（否则它们会在拒收窗口里
            #    带着译文回来 ⇒ 断言不成立）。
            page.wait_for_timeout(2000)
            _pick_and_clear()
            n_rej = len(line_posts)
            page.wait_for_timeout(9000)         # ⇒ 这些请求全被拒收
            rej_count = len(line_posts) - n_rej
            check("被拒收的请求**确实发出去了**（复现了拒收路径）",
                  rej_count > 0, f"拒收期间发了 {rej_count} 个")
            still = page.evaluate(
                """(slots) => slots.filter((s) => {
                     const A = window.Qwen3Asr;
                     const it = A._trLineTr ? A._trLineTr.get(s) : null;
                     return !!(it && (it.text || '').trim());
                   })""",
                slots)
            check("拒收期间那几行**没有**译文（正是用户看到的样子）",
                  len(still) == 0, f"居然有 {len(still)} 行拿到了译文")

            # 放行 ⇒ 过期重试应当把它们**要回来**
            reject["on"] = False
            t0 = time.time()
            healed = 0
            while time.time() - t0 < HEAL_DEADLINE_SEC:
                page.wait_for_timeout(1500)
                healed = len(page.evaluate(
                    """(slots) => slots.filter((s) => {
                         const A = window.Qwen3Asr;
                         const it = A._trLineTr ? A._trLineTr.get(s) : null;
                         return !!(it && (it.text || '').trim());
                       })""",
                    slots))
                if healed == len(slots):
                    break
            check("被拒收的行**自己恢复了**（过期后重要一次）",
                  healed == len(slots),
                  f"恢复了 {healed}/{len(slots)} 行（限时 %.0f s）"
                  % HEAL_DEADLINE_SEC)

        # ------------------------------------------------------------------ #
        # [5] 拖动进度条（前 + 后）
        # ------------------------------------------------------------------ #
        print("\n  [5] 拖动进度条（前 + 后）")
        _seek(page, 400.0)
        page.wait_for_timeout(10000)
        stt = _state(page)
        print("  拖到 400 s：轮次=%s epoch=%s 行=%s 播放头=%.1f s"
              % (stt["rounds"], stt["epochs"], len(stt["rows"]), stt["videoTime"]))
        _seek(page, 60.0)
        page.wait_for_timeout(10000)
        stt = _state(page)
        print("  拖回 60 s：行=%s 播放头=%.1f s" % (len(stt["rows"]),
                                                    stt["videoTime"]))

        # ⚠️ **请求顺序**：离播放头最近的先要（白盒断言，不依赖现场）。
        order_ok = page.evaluate(
            """() => {
              const A = window.Qwen3Asr;
              const v = document.getElementById('asr-video');
              const orig = A._audioOriginSec;
              // ⚠️ **不碰 video.currentTime**（改了会触发 seeked ⇒ 重开
              //    识别）⇒ 改 ``_audioOriginSec`` 把「播放头」挪到 30 s。
              A._audioOriginSec = (v ? v.currentTime : 0) - 30;
              const fake = [
                {id: '0:0:a:0:2:aaa',  start: 0,  end: 2},
                {id: '0:1:b:10:12:bbb', start: 10, end: 12},
                {id: '0:2:c:28:32:ccc', start: 28, end: 32},
                {id: '0:3:d:40:42:ddd', start: 40, end: 42},
              ];
              const order = A._trLineOrder(fake);
              A._audioOriginSec = orig;
              const t = 30;
              const dist = (ln) => (t < ln.start) ? (ln.start - t)
                              : ((t > ln.end) ? (t - ln.end) : 0);
              const ds = order.map((i) => dist(fake[i]));
              let mono = true;
              for (let i = 1; i < ds.length; i++) if (ds[i] < ds[i-1]) mono = false;
              return {order: order, dists: ds, mono: mono};
            }""")
        check("请求顺序 = **离播放头最近优先**（距离单调不减）",
              bool(order_ok.get("mono")), f"order={order_ok.get('order')} "
              f"dists={order_ok.get('dists')}")
        check("离播放头最近的那行排第一（不是从头排）",
              (order_ok.get("order") or [None])[0] == 2,
              f"order={order_ok.get('order')}")

        # ------------------------------------------------------------------ #
        # [6] 最终：播放头附近**每一行**都要有译文
        # ------------------------------------------------------------------ #
        print("\n  [6] 等播放头附近铺满（最多 120 s）")
        t0 = time.time()
        near = []
        while time.time() - t0 < 120:
            near = _near_playhead(page)
            if near and not _missing(near):
                break
            page.wait_for_timeout(1500)
        rep = _report(near, "播放头附近")
        check("**播放头附近**没有一行字幕缺译文", len(rep["missing"]) == 0,
              f"缺 {len(rep['missing'])} 行")

        # ⚠️ **整条时间轴**不必全翻完（98 分钟 ≈ 1477 行 ⇒ 按限速要好几
        #    分钟）⇒ 这里只报数，不当断言。
        stt = _state(page)
        all_miss = _missing(stt["rows"])
        print("  整条时间轴：%d 行，还差 %d 行没翻（限速下要几分钟，不当断言）"
              % (len(stt["rows"]), len(all_miss)))

        _, st2 = api("GET", "/api/qwen3-asr/translate/status")
        print("  服务端：submitted=%s translated=%s failed=%s dropped=%s queue=%s"
              % (st2.get("submitted"), st2.get("translated"),
                 st2.get("failed"), st2.get("dropped"), st2.get("queue_depth")))
        check("服务端**没有丢**翻译请求（队列没爆）",
              (st2.get("dropped") or 0) <= dropped_before,
              f"dropped {dropped_before} → {st2.get('dropped')}")
        check("服务端队列**没有积压**（背压生效）",
              (st2.get("queue_depth") or 0) <= 64,
              f"queue_depth={st2.get('queue_depth')}")

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


def main() -> int:
    video = VIDEO
    if "--video" in sys.argv:
        i = sys.argv.index("--video")
        if i + 1 < len(sys.argv):
            video = sys.argv[i + 1]
    return _main(video)


if __name__ == "__main__":
    sys.exit(main() or 0)
