#!/usr/bin/env python3
"""真机浏览器回归：「含音轨的视频」⇒ **画面字幕显示双语**。

用户要求：视频来源下打开「实时翻译」⇒ **字幕加上翻译**，且**译文跟随字幕
的显示节奏**（参照 YouTube 的双语字幕：cue 对 cue，译文跟着原文一起出现、
一起淡出）。

覆盖
====

1. **画面字幕是双语的** —— 每个行槽里除了原文还有一行译文
   （``.asr-sub-tr``），译文是**非空**且**不等于原文**。
2. **译文挂在当前行下面** —— 它是**同一行槽**的下半部分 ⇒ 结构上就跟着
   字幕的淡入淡出（不是另起一个独立列表）。
3. **跟着播放头走** —— 播放推进 ⇒ 当前行换行 ⇒ **译文也换**（不是停在第一句）。
4. **转写框同步** —— 「字幕文本」框里同样有译文行，且与画面字幕**同一份
   映射**。
5. **关掉「实时翻译」⇒ 字幕回到纯原文**。

跑之前要满足：**ASR 模型**与**翻译模型**在跑（脚本自己会开实时翻译开关）。

跑法::

    .venv/bin/python scripts/verify_asr_video_dual_subtitle.py
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
SRC_MP4 = "/tmp/asr_ref/asr_real_audio.mp4"
#: 播放端点（``/api/qwen3-asr/media``）只放行 ``<assets>/uploads`` 与
#: ``<assets>/tts`` ⇒ 视频得先拷进去才能在浏览器里播。
DST_MP4 = "/userdata/assets/uploads/asr_dual_sub_test.mp4"

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
        return -1, {"error": repr(e)}


def _ensure_models() -> tuple[bool, bool]:
    _, snap = api("GET", "/api/compute")
    asr_ok = any(i.get("model_id") == "qwen3-asr"
                 and i.get("status") == "running"
                 for i in (snap.get("instances") or []))
    if not asr_ok:
        print("  … ASR 模型没在跑，起一个（~30 s）")
        api("POST", "/api/compute/instances",
            {"model": "qwen3-asr", "npu_index": 0}, timeout=180)
        for _ in range(24):
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
        api("POST", "/api/qwen3-asr/translate/enable", {}, timeout=220)
        _, st = api("GET", "/api/qwen3-asr/translate/status")
        mt_ok = bool((st.get("model") or {}).get("running"))
    return asr_ok, mt_ok


# --------------------------------------------------------------------------- #
# 页面探针
# --------------------------------------------------------------------------- #
def _sub_state(page) -> dict:
    """画面字幕的当前状态：可见行 + 每行的原文/译文 + **该行应有的译文**。

    ``expect`` 是页面自己按映射算出来的「这一行该挂哪句译文」⇒ 拿来和
    DOM 里的实际内容对比 ⇒ 能抓出「换了行但旧译文还留着」这类残留
    （肉眼只看到同一句译文出现在两行上）。
    """
    return page.evaluate(
        """() => {
          const A = window.Qwen3Asr;
          const tl = (A && A._buildSubTimeline) ? A._buildSubTimeline().lines : [];
          const exp = {};
          for (let i = 0; i < tl.length; i++) {
            const its = (A._trItemsForLine ? A._trItemsForLine(tl, i) : []) || [];
            const key = (tl[i].words || []).map((w) => w.text).join('');
            exp[key] = its.map((it) => it.text || it.source || '')
                          .filter((s) => !!s).join(' ');
          }
          const slots = [...document.querySelectorAll('#asr-video-sub .asr-sub-line')];
          const vis = slots.filter((el) => {
            if (getComputedStyle(el).visibility === 'hidden') return false;
            if (Number(el.style.opacity || '0') <= 0.01) return false;
            return true;
          });
          return {
            videoTime: (document.getElementById('asr-video') || {}).currentTime || 0,
            lines: vis.map((el) => {
              const origEl = el.querySelector('.asr-sub-orig');
              const trEl = el.querySelector('.asr-sub-tr');
              const orig = (origEl || {}).textContent || '';
              const tr = (trEl || {}).textContent || '';
              return {
                orig: orig,
                tr: tr,
                trHidden: !!((trEl || {}).classList || []).contains('hidden'),
                // ⚠️ **同一个槽位**（原文和译文在同一个 .asr-sub-line 里）
                sameSlot: !!(origEl && trEl
                             && origEl.closest('.asr-sub-line')
                                === trEl.closest('.asr-sub-line')),
                expect: (exp[orig] === undefined) ? null : exp[orig],
              };
            }),
          };
        }"""
    )


def _norm(text: str) -> str:
    """比文本用：去掉空白和标点。"""
    import re
    return re.sub(r"[\s.,!?;:'\"、，。！？；：]", "", text or "")


def _timeline_rows(page) -> list[dict]:
    """时间轴上**每一行**的原文 / 译文 / 译文的原文。

    ⚠️ 直接读前端的行级译文表（``_trLineTr``）⇒ 不依赖「页面自己算的映射」
    ⇒ 能真的验证「译文挂对了行」（译文条目的 ``source`` 是不是**这一行**
    的原文）。
    """
    return page.evaluate(
        """() => {
          const A = window.Qwen3Asr;
          const tl = (A && A._buildSubTimeline) ? A._buildSubTimeline().lines : [];
          return tl.map((ln) => {
            const slot = String(ln.id || '').split(':').slice(0, 2).join(':');
            const it = (A._trLineTr && A._trLineTr.get(slot)) || null;
            return {
              text: (ln.words || []).map((w) => w.text).join(''),
              tr: it ? (it.text || '') : '',
              src: it ? (it.source || '') : '',
              st: it ? (it.status || '') : '',
            };
          });
        }"""
    )


def _transcript_tr(page) -> list[str]:
    """转写框里的译文行。"""
    return page.evaluate(
        """() => [...document.querySelectorAll(
             '#asr-transcript-body .asr-sub-tr')]
           .filter((el) => !el.classList.contains('hidden'))
           .map((el) => (el.textContent || '').trim())"""
    )


def main() -> int:
    print("=" * 72)
    print("真机浏览器回归：含音轨的视频 ⇒ 画面字幕双语")
    print("=" * 72)

    if not os.path.exists(SRC_MP4):
        print("❌ 缺素材 %s（先跑 scripts/make_asr_ref_video.py）" % SRC_MP4)
        return 1
    shutil.copyfile(SRC_MP4, DST_MP4)

    asr_ok, mt_ok = _ensure_models()
    check("ASR 模型在跑", asr_ok)
    check("翻译模型在跑", mt_ok)
    if not (asr_ok and mt_ok):
        print("❌ 模型没起来，脚本到此为止")
        return 1

    api("POST", "/api/qwen3-asr/translate/settings",
        {"enabled": True, "target_lang": "zh-CN"})
    _, st = api("GET", "/api/qwen3-asr/translate/status")
    check("实时翻译已开启", bool(st.get("enabled")), f"status={st}")

    errors: list[str] = []
    seen_lines: list[str] = []
    seen_tr: list[str] = []
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
              == "video",
              page.evaluate("() => window.Qwen3Asr ? Qwen3Asr._source : ''"))

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

        # --- 等字幕 + 译文 ---
        t0 = time.time()
        got_sub = False
        got_tr = False
        slot_ok = True          # 译文和原文在**同一个行槽**里
        dup_ok = True            # 同一句译文不会同时挂在两行上
        match_ok = True          # 槽里的译文 == 映射算出来的那句（无残留）
        mismatch: list[str] = []
        cur_had_tr = 0           # 「当前行已经有译文」的采样数
        cur_total = 0            # 采样总数（当前行存在的那些）
        while time.time() - t0 < 240:
            snap = _sub_state(page)
            for ln in snap["lines"]:
                if ln["orig"].strip():
                    got_sub = True
                    if ln["orig"] not in seen_lines:
                        seen_lines.append(ln["orig"])
                if ln["tr"].strip():
                    got_tr = True
                    if ln["tr"] not in seen_tr:
                        seen_tr.append(ln["tr"])
                    if not ln.get("sameSlot"):
                        slot_ok = False
            # 同一句译文不能同时出现在两个可见行上（槽位复用没清干净）
            trs_vis = [ln["tr"] for ln in snap["lines"] if (ln["tr"] or "").strip()]
            if len(trs_vis) != len(set(trs_vis)):
                dup_ok = False
            # 槽里的译文必须就是「这一行该挂的那句」
            for ln in snap["lines"]:
                exp = ln.get("expect")
                if exp is None:
                    continue
                if (ln["tr"] or "").strip() != exp.strip():
                    match_ok = False
                    if len(mismatch) < 3:
                        mismatch.append(f"{ln['orig'][:24]!r} dom={ln['tr'][:24]!r} "
                                        f"exp={exp[:24]!r}")
            # ⚠️ **节奏**：**当前行**（最后一条可见行）自己就该有译文 ——
            # 不是等下一行出来才有（那是句子级翻译的毛病：看着像没实时翻译）。
            if snap["lines"]:
                cur_total += 1
                if (snap["lines"][-1]["tr"] or "").strip():
                    cur_had_tr += 1
            if got_sub and got_tr and len(seen_tr) >= 3 and len(seen_lines) >= 3:
                break
            page.wait_for_timeout(500)

        snap = _sub_state(page)
        print("  播放头: %.1f s" % snap["videoTime"])
        for ln in snap["lines"]:
            print("    原文: %s" % (ln["orig"] or "")[:60])
            print("    译文: %s" % (ln["tr"] or "")[:60])

        check("画面字幕出现了（视频在播、字幕跟着走）", got_sub,
              f"lines={seen_lines[:3]}")
        check("画面字幕**带译文**（双语）", got_tr, f"tr={seen_tr[:3]}")
        check("译文与原文在**同一个行槽**里（结构上跟着字幕淡入淡出）",
              slot_ok, "有译文的行不在同一槽")
        check("同一句译文不会同时挂在两行上", dup_ok,
              f"tr={[ln['tr'][:20] for ln in snap['lines']]}")
        check("行里的译文 == 映射算出来的那句（换行不残留旧译文）",
              match_ok, "; ".join(mismatch))
        check("译文**不等于**原文",
              all(t != o for t in seen_tr for o in seen_lines),
              f"tr={seen_tr[:2]}")

        # --- 跟着播放头走：字幕行会换，译文也跟着换 ---
        check("字幕行随播放头推进（不止一句）", len(seen_lines) >= 2,
              f"lines={seen_lines[:4]}")
        check("译文也跟着换（不止一句）", len(seen_tr) >= 2,
              f"tr={seen_tr[:4]}")

        # --- 节奏：译文**逐行**跟着（YouTube 的 cue 对 cue）------------- #
        #
        # ⚠️ 句子级翻译的老毛病：字幕行 ~1.5 s 一行、译文 3~9 s 一句 ⇒ 译文
        # 永远慢半拍，而且长句跨的行**整行没有译文** ⇒ 看着像「不是实时翻译」。
        check("**当前行**自己就有译文（译文跟着这一行走，不等下一行）",
              cur_total > 0 and cur_had_tr >= cur_total * 0.8,
              f"有译文的采样 {cur_had_tr}/{cur_total}")

        # 等识别跑完（最后一行也定稿了 ⇒ 它也该有译文）
        for _ in range(120):
            st = page.evaluate("() => (window.Qwen3Asr.snapshot || {}).state || ''")
            if st == "done":
                break
            page.wait_for_timeout(500)
        # ⚠️ **还要等译文到齐** —— 识别结束 ≠ 译文都回来了（模型还在翻）。
        rows: list[dict] = []
        for _ in range(60):
            rows = _timeline_rows(page)
            if rows and all((r["tr"] or "").strip() for r in rows):
                break
            page.wait_for_timeout(500)
        print("  时间轴 %d 行：" % len(rows))
        for r in rows:
            print("    %-46r -> %s" % (r["text"][:44], (r["tr"] or "")[:36]))
        missing = [r["text"][:30] for r in rows if not (r["tr"] or "").strip()]
        check("**每一行**都有译文（不是只有最后一行）", not missing,
              f"没译文的行={missing[:3]}")
        wrong = [(r["text"][:24], r["src"][:24]) for r in rows
                 if (r["tr"] or "").strip()
                 and _norm(r["src"]) != _norm(r["text"])]
        check("译文挂对了行（译文的原文 == 那一行的原文）", not wrong,
              f"对不上={wrong[:3]}")
        cnt = page.evaluate(
            """() => {
              const A = window.Qwen3Asr;
              return {
                req: (A._trLineReq && A._trLineReq.size) || 0,
                items: (A._tr.items || []).length,
                lines: ((A._buildSubTimeline && A._buildSubTimeline().lines)
                        || []).length,
              };
            }"""
        )
        check("没有重复翻译（请求数 == 行数）",
              cnt["req"] == cnt["lines"], f"{cnt}")
        check("面板里没有过期条目（条目数 == 行数）",
              cnt["items"] == cnt["lines"], f"{cnt}")

        # --- 转写框同步 ---
        trs = _transcript_tr(page)
        check("转写框里也有译文行（同一份映射）", len(trs) >= 1, f"trs={trs[:3]}")

        # --- 关掉「实时翻译」⇒ 字幕回到纯原文 ---
        page.evaluate(
            """() => {
              const cb = document.getElementById('asr-tr-enabled');
              if (cb) { cb.checked = false; cb.dispatchEvent(new Event('change', {bubbles: true})); }
            }"""
        )
        page.wait_for_timeout(1500)
        snap2 = _sub_state(page)
        check("关掉「实时翻译」⇒ 画面字幕上的译文收掉了",
              all(not (ln["tr"] or "").strip() for ln in snap2["lines"]),
              f"lines={snap2['lines']}")

        # --- 刷新页面后译文要回来 ------------------------------------- #
        #
        # ⚠️ 行级翻译是**前端驱动**的（前端才知道行是怎么排的）⇒ 刷新后
        # 前端的行级记账是**空的** ⇒ 这些行得**重新要一遍**译文。
        #
        # ⚠️ 而且**翻译的单位**必须跟着「来源」一起认领 —— 不然刷新后
        # ``_trMode`` 还是默认的 "sentence" ⇒ 行级翻译一次都不发起 ⇒
        # **一句译文都没有**（改行级之前是有的：句子级翻译在后端做，
        # 刷新后新订阅者照样收得到 ⇒ 这是一条本轮引入的回归）。
        api("POST", "/api/qwen3-asr/translate/settings",
            {"enabled": True, "target_lang": "zh-CN"})
        page.wait_for_timeout(6000)     # 等前端的下一次轮询看到「开着」
        n_before = len(_timeline_rows(page))
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(2500)
        skip2 = page.locator("#wd-wizard-root-qwen3-asr .wd-wizard-skip-btn").first
        if skip2.count() and skip2.is_visible():
            skip2.click()
            page.wait_for_timeout(1200)
        page.wait_for_selector("#page-qwen3-asr:not(.hidden)", timeout=15000)
        mode_after = page.evaluate("() => window.Qwen3Asr ? window.Qwen3Asr._trMode : ''")
        check("刷新后认领了「行级」翻译（跟着来源一起认领）",
              mode_after == "line", f"_trMode={mode_after!r}")
        rows2: list[dict] = []
        for _ in range(120):
            rows2 = _timeline_rows(page)
            if rows2 and all((r["tr"] or "").strip() for r in rows2):
                break
            page.wait_for_timeout(500)
        check("刷新后轮次恢复了", len(rows2) >= n_before,
              f"刷新前 {n_before} 行 / 刷新后 {len(rows2)} 行")
        missing2 = [r["text"][:30] for r in rows2 if not (r["tr"] or "").strip()]
        check("刷新后**每一行仍有译文**（行级翻译重新要过了）", not missing2,
              f"没译文的行={missing2[:3]}")
        wrong2 = [(r["text"][:24], r["src"][:24]) for r in rows2
                  if (r["tr"] or "").strip()
                  and _norm(r["src"]) != _norm(r["text"])]
        check("刷新后译文仍挂对了行", not wrong2, f"对不上={wrong2[:3]}")

        check("没有 JS 错误", not errors, f"errors={errors[:4]}")
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
