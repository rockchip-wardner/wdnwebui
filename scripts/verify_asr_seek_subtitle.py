#!/usr/bin/env python3
"""真机浏览器回归：「含音轨的视频」**拖动进度条**之后。

用户报的两个问题
==============

1. **拖动后还留着前面的字幕** —— 拖到一个「还没识别到」的位置，画面上
   挂的还是**拖动前**那一行（旧字幕不更新）。
2. **拖动后识别停滞 / 全是英文输出** —— 切会话后新会话**零上下文** ⇒
   模型重新判语种（中文被判成 English）⇒ 整段英文；文本与音频脱节 ⇒
   看着像「停滞」。

覆盖
====

1. **基线**：没拖动时画面字幕跟着播放头走（有内容）。
2. **旧字幕必须清掉**：拖到「还没识别到」的位置 ⇒ 画面上**不能**还挂着
   拖动前那一行（要么空，要么已经是新内容）。
3. **追上后要恢复**：新识别追上播放头 ⇒ 字幕重新出现，且**不是**旧那行。
4. **语种不翻转**：全程（尤其是切会话之后）语种**不能**变。
5. **识别不停滞**：新一段的轮次**已确认文本在增长**（不是卡死在同一句）。

跑之前要满足：**ASR 模型**在跑（脚本自己会起）。

素材
====

默认用 ``/userdata/assets/uploads/asr_zh_seek_test.mp4``（中文对谈片段，
150 秒）；没有就退回 ``scripts/make_asr_ref_video.py`` 造的英文片段
（``--video`` 可以指定别的）。

跑法::

    .venv/bin/python scripts/verify_asr_seek_subtitle.py
    .venv/bin/python scripts/verify_asr_seek_subtitle.py --video /path/to.mp4
"""
from __future__ import annotations

import argparse
import re
import json
import os
import shutil
import ssl
import subprocess
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

#: 中文对谈片段（150 秒）—— 拖动测试的默认素材。
#: ⚠️ 播放端点（``/api/qwen3-asr/media``）只放行 ``<assets>/uploads`` 与
#: ``<assets>/tts`` ⇒ 素材必须在这个目录里。
ZH_VIDEO = "/userdata/assets/uploads/asr_zh_seek_test.mp4"
#: 英文参考片段（C demo 的 ``asr_en.wav`` → mp4）。
EN_SRC = "/tmp/asr_ref/asr_real_audio.mp4"
EN_VIDEO = "/userdata/assets/uploads/asr_seek_en_test.mp4"

#: 拖动落点相对播放头的偏移（秒）—— 必须落在「还没识别到」的地方。
#:
#: ⚠️ **识别比播放快**（RTF≈0.18）⇒ 拖到**已经识别过**的位置不会触发
#: 重开识别 ⇒ 那样测不到「旧字幕残留」。
SEEK_AHEAD_SEC = 40.0

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


def _ensure_asr_model() -> bool:
    _, snap = api("GET", "/api/compute")
    ok = any(i.get("model_id") == "qwen3-asr"
             and i.get("status") == "running"
             for i in (snap.get("instances") or []))
    if ok:
        return True
    print("  … ASR 模型没在跑，起一个（~30 s）")
    api("POST", "/api/compute/instances",
        {"model": "qwen3-asr", "npu_index": 0}, timeout=240)
    for _ in range(30):
        time.sleep(5)
        _, snap = api("GET", "/api/compute")
        if any(i.get("model_id") == "qwen3-asr"
               and i.get("status") == "running"
               for i in (snap.get("instances") or [])):
            return True
    return False


def _pick_video() -> str:
    """挑素材：优先中文片段，没有就造一个英文的。"""
    if os.path.exists(ZH_VIDEO):
        return ZH_VIDEO
    if os.path.exists(EN_SRC):
        subprocess.run(
            [sys.executable,
             os.path.join(_SCRIPT_DIR, "make_asr_ref_video.py")],
            check=False, capture_output=True)
        if os.path.exists(EN_SRC):
            shutil.copyfile(EN_SRC, EN_VIDEO)
            return EN_VIDEO
    print(f"❌ 没有素材（{ZH_VIDEO} / {EN_SRC} 都没有）")
    return ""


# --------------------------------------------------------------------------- #
# 页面探针
# --------------------------------------------------------------------------- #
PROBE = """
() => {
  const A = window.Qwen3Asr;
  const rs = (A && A.rounds) ? A.rounds : [];
  const tl = (A && A._buildSubTimeline) ? A._buildSubTimeline().lines : [];
  // ⚠️ **会话（epoch）边界** —— 「切会话后新会话有没有带回看上下文」
  // 就是看「新会话的起点」比「上一段的终点」早多少。
  const eps = (A && A._splitEpochs) ? A._splitEpochs(rs) : [];
  const epochSpans = eps.map((ep) => {
    const first = ep[0] || {};
    const last = ep[ep.length - 1] || {};
    return {
      start: (first.chunk_start_sec === undefined
              || first.chunk_start_sec === null) ? null : first.chunk_start_sec,
      end: (last.chunk_end_sec === undefined
            || last.chunk_end_sec === null) ? null : last.chunk_end_sec,
      rounds: ep.length,
    };
  });
  const slots = [...document.querySelectorAll('#asr-video-sub .asr-sub-line')]
    .filter((el) => getComputedStyle(el).visibility !== 'hidden'
                 && Number(el.style.opacity || '0') > 0.01)
    .map((el) => {
      const orig = (el.querySelector('.asr-sub-orig') || {}).textContent || '';
      // ⚠️ 找这一行在时间轴上的位置 —— 「画面上这行是不是播放头附近的那行」
      // 才是「旧字幕有没有残留」的**语义**判据（比「是不是基线那一行」准：
      // 旧字幕可能是上一段的**最后一行**，不一定是采样时看到的那一行）。
      let hit = null;
      for (const ln of tl) {
        const txt = (ln.words || []).map((w) => w.text).join('');
        if (txt === orig) { hit = { start: ln.start, end: ln.end }; break; }
      }
      return { orig: orig, start: hit ? hit.start : null,
               end: hit ? hit.end : null };
    });
  const ranges = (A && A._subRanges) ? A._subRanges : [];
  return {
    t: (document.getElementById('asr-video') || {}).currentTime || 0,
    slots: slots,
    ranges: ranges.map((r) => [r.start, r.end]),
    epochSpans: epochSpans,
    rounds: rs.map((r) => ({
      n: r.round, c: r.committed || '', u: r.unfixed || '',
      s: r.chunk_start_sec, e: r.chunk_end_sec,
      lang: r.language || '',
    })),
    busy: !!(A && A.snapshot && A.snapshot.busy),
    lang: (A && A.snapshot) ? (A.snapshot.language || '') : '',
  };
}
"""


def _probe(page) -> dict:
    return page.evaluate(PROBE)


def _visible(page) -> list[str]:
    return [s["orig"] for s in _probe(page)["slots"]
            if (s["orig"] or "").strip()]


#: 「字幕落后播放头多少秒算**残留**」（秒）。
#:
#: ⚠️ 正常情况下字幕本来就比播放头**慢 4~5 秒** —— SDK 的回退策略
#: （``rollback_tokens = 5``）让「已确认文本」滞后 4~5 轮 ⇒ 字幕行
#: 结束的时刻比播放头晚好几秒之前。
#:
#: ⇒ 残留（拖动前那一行）的特征是**滞后几十秒**（实测 32 秒）⇒ 用
#: 12 秒做门限：正常的 5 秒滞后不算残留，几十秒的才算。
STRAY_LAG_SEC = 12.0


def _stray_lines(page, tol: float = STRAY_LAG_SEC) -> list[dict]:
    """画面上**落后播放头太多**的行（= 拖动前残留的旧字幕）。

    ⚠️ 判据是「这一行**结束**的时刻离播放头有多远」—— 一行最长能跨
    7 秒（``SUB_MAX_LINE_SEC``）⇒ 用「起点」判会误报。
    """
    d = _probe(page)
    out = []
    for s in d["slots"]:
        if not (s["orig"] or "").strip():
            continue
        if s["start"] is None or s["end"] is None:
            continue
        if s["end"] + tol < d["t"]:
            out.append({"orig": s["orig"][:24], "start": s["start"],
                        "end": s["end"], "t": d["t"]})
    return out


def _langs(page) -> list[str]:
    return [r["lang"] for r in _probe(page)["rounds"] if r["lang"]]


def main() -> int:
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--video", default="")
    args = ap.parse_args()
    BASE = args.base

    print("=" * 72)
    print("真机浏览器回归：含音轨的视频 ⇒ 拖动进度条之后")
    print("=" * 72)

    video = args.video or _pick_video()
    if not video:
        return 1
    print(f"素材: {video}")

    if not _ensure_asr_model():
        print("❌ ASR 模型没起来，脚本到此为止")
        return 1

    errors: list[str] = []
    http_fail: list[tuple] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1440, "height": 1100},
                                  ignore_https_errors=True)
        install_active_app_guard(ctx)
        install_active_app_spoof(ctx)
        page = ctx.new_page()
        page.on("pageerror", lambda e: errors.append(f"[pageerror] {e}"))
        # ⚠️ 「Failed to load resource」这类**资源加载**错误由 response 钩子
        # 单独判（它能拿到 URL，能区分「媒体 403」和真正的接口失败）⇒
        # 这里只收**脚本**错误。
        page.on("console", lambda m: errors.append(f"[console.{m.type}] {m.text}")
                if (m.type == "error"
                    and "Failed to load resource" not in m.text) else None)
        page.on("response", lambda r: http_fail.append((r.status, r.url))
                if r.status >= 400 else None)

        page.goto(f"{BASE}/#{CARD}", wait_until="networkidle")
        page.wait_for_timeout(2500)
        skip = page.locator("#wd-wizard-root-qwen3-asr .wd-wizard-skip-btn").first
        if skip.count() and skip.is_visible():
            skip.click()
            page.wait_for_timeout(1500)
        page.wait_for_selector("#page-qwen3-asr:not(.hidden)", timeout=15000)

        page.locator('#asr-source-tabs .asr-source-tab[data-source="video"]').click()
        page.wait_for_timeout(800)
        page.evaluate(
            """(p) => {
              const el = document.getElementById('asr-source');
              if (el) {
                el.value = p;
                el.dispatchEvent(new Event('input', { bubbles: true }));
              }
            }""",
            video,
        )
        page.wait_for_timeout(600)
        page.locator("#asr-start-btn").click()

        # ---- 等第一行字幕出来（基线） ---------------------------------- #
        base_line = ""
        t0 = time.time()
        while time.time() - t0 < 180:
            vis = _visible(page)
            if vis:
                base_line = vis[-1]
                break
            page.wait_for_timeout(400)
        check("基线：没拖动时画面字幕有内容", bool(base_line),
              f"slots={_visible(page)}")
        if not base_line:
            print("❌ 没有字幕，后面的用例跑不了")
            browser.close()
            return 1

        before = _probe(page)
        print(f"  拖动前：播放头={before['t']:.1f}s 字幕={base_line[:30]!r} "
              f"已识别区间={[(round(a,1), round(b,1)) for a,b in before['ranges']]}")

        # ---- 拖到「还没识别到」的位置 ------------------------------------ #
        seek_to = before["t"] + SEEK_AHEAD_SEC
        print(f"  >>> 拖到 {seek_to:.1f}s（肯定还没识别到）")
        page.evaluate(
            """(t) => { const v = document.getElementById('asr-video');
                      if (v) v.currentTime = t; }""",
            seek_to,
        )

        # 拖动后的头几秒：画面上**不能**还有离播放头很远的旧字幕。
        #
        # ⚠️ **必须在新识别追上来之前采样**才有区分度：新识别一旦提交了
        # 第一行，字幕就换成新内容了 ⇒ 「旧字幕还在不在」这个问题就自动
        # 消失了（修复前也会「通过」）。
        #
        # ⇒ 头两次采样落在**识别还没追上来**的窗口里（重开识别要走一次
        # 「停 → 等空闲 → 重开」的 RPC往返，实测 1~3 秒）。
        stray: list[dict] = []
        samples = 0
        for _ in range(2):
            page.wait_for_timeout(250)
            samples += 1
            stray.extend(_stray_lines(page))
        early = _probe(page)
        print(f"  拖动后 {samples * 0.25:.1f}s（识别还没追上）："
              f"字幕={_visible(page)} "
              f"已识别区间={[(round(a,1), round(b,1)) for a,b in early['ranges']]}")
        after_seek = _probe(page)
        print(f"  拖动后：播放头={after_seek['t']:.1f}s "
              f"字幕={_visible(page)} "
              f"已识别区间={[(round(a,1), round(b,1)) for a,b in after_seek['ranges']]}")

        check("拖动后**没有离播放头太远的旧字幕**（旧字幕被清掉）",
              not stray,
              f"{stray[:2]}")

        # ---- 等新识别追上播放头 ------------------------------------------ #
        caught = False
        t0 = time.time()
        while time.time() - t0 < 120:
            d = _probe(page)
            ranges = d["ranges"]
            head = d["t"]
            if ranges and any(r[0] - 0.5 <= head <= r[1] + 1.5 for r in ranges):
                caught = True
                break
            page.wait_for_timeout(500)
        check("新识别追上了播放头（识别区间覆盖播放头）", caught,
              f"ranges={[(round(a,1), round(b,1)) for a,b in after_seek['ranges']]}")

        # ---- 等一次**切会话**（回看的效果只有切会话时才看得见） ---------- #
        #
        # ⚠️ 拖动后的那一段要跑够长才会切会话（会话上限 60 秒）⇒ 素材
        # 不够长的话这条断言就没得测 ⇒ 显式等它发生。
        t0 = time.time()
        eps_n = 0
        while time.time() - t0 < 150:
            eps_n = len(_probe(page)["epochSpans"])
            if eps_n >= 3:
                break
            page.wait_for_timeout(1000)
        check("识别过程中**切过会话**（回看的作用点）", eps_n >= 3,
              f"会话数={eps_n}")

        # 追上之后字幕要回来，而且**必须是在播放头附近**的那一行
        vis = _visible(page)
        check("追上之后字幕恢复", bool(vis), f"slots={vis}")
        stray2 = _stray_lines(page)
        check("追上之后的字幕**在播放头附近**（不是拖动前那一行）",
              bool(vis) and not stray2,
              f"slots={vis} stray={stray2[:2]}")

        # ---- 语种不许翻转（切会话后不退化） ----------------------------- #
        #
        # ⚠️ **单轮的语种标签会瞬时抽风**：模型每轮都重新吐一次
        # ``language <lang>``（SDK ``asr.py:1391`` 保留**最新**的猜测），
        # 而**新会话的头几轮**音频上下文还不够 ⇒ 标签可能是「Russian」
        # 这种瞎猜（实测中文素材上出现过 4 轮）。
        #
        # ⇒ 断言分三层：
        #   1. **每一轮的假设文本**（已确认 + 还在改写的尾巴）里都有中文
        #      —— 这才是「整段变英文」的真判据（出问题时**英文在 unfixed
        #      里**，committed 反而是空的）；
        #   2. **语种标签不许连着错**（≥8 轮连错 = 真的自锁退化了；
        #      修复前那个「同一句英文重复二十多次」的循环就是 20+ 轮）；
        #   3. **最终**语种是中文。
        d = _probe(page)
        cjk = re.compile(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]")
        no_cjk = [r for r in d["rounds"]
                  if (r["c"] + r["u"]).strip() and not cjk.search(r["c"] + r["u"])]
        check("每一轮的假设文本里**都有中文**（没有整段变英文）",
              not no_cjk,
              f"{len(no_cjk)} 轮没有中文，例："
              f"{(no_cjk[0]['c'] + no_cjk[0]['u'])[:60] if no_cjk else ''}")
        langs = [r["lang"] for r in d["rounds"] if r["lang"]]
        worst_run = 0
        run = 0
        for lg in langs:
            run = run + 1 if lg != "Chinese" else 0
            worst_run = max(worst_run, run)
        check("语种没有**连着错 8 轮以上**（没有退化成自锁循环）",
              worst_run < 8,
              f"最长连续非中文 = {worst_run} 轮，语种集合={sorted(set(langs))}")
        check("最终语种是中文", bool(langs) and langs[-1] == "Chinese",
              f"最后语种={langs[-1] if langs else '(空)'}")

        # ---- 切会话必须带回看上下文（**确定性**判据） -------------------- #
        #
        # ⚠️ 「整段变英文」是**概率性**的（取决于那一秒模型抽什么风）⇒
        # 单次跑抓不住 ⇒ 这里断言**结构**：每切一次会话，新会话的**起点**
        # 必须比上一段的**终点**更早（差值 ≈ 回看窗口 10 秒）。
        #
        # ⇒ 没有回看时：新会话的起点 == 上一段的终点（差值 0）⇒ 判据命中 ✓
        # ⇒ 有回看时：新会话带着上一段的尾巴（差值 ≈ 10 s）✓
        spans = d["epochSpans"]
        gaps = []
        for i in range(1, len(spans)):
            prev_end = spans[i - 1]["end"]
            cur_start = spans[i]["start"]
            if prev_end is None or cur_start is None:
                continue
            gap = round(prev_end - cur_start, 1)
            # ⚠️ **只测「切会话」那种边界**：新会话紧跟着上一段（缺口在
            # 几秒量级）。拖动重开识别时新会话是从**更后面**开始的
            # （缺口是几十秒的负数）⇒ 那种边界不算数。
            if gap < -2.0:
                continue
            gaps.append(gap)
        check("切会话时**带回了上下文**（新会话起点比上一段终点更早）",
              bool(gaps) and all(g >= 5.0 for g in gaps),
              f"各段回看缺口={gaps}")

        # ---- 识别没有停滞（已确认文本在增长） --------------------------- #
        d = _probe(page)
        committed_lens = [len(r["c"]) for r in d["rounds"] if r["c"]]
        grew = False
        for i in range(1, len(committed_lens)):
            if committed_lens[i] > committed_lens[i - 1]:
                grew = True
                break
        check("已确认文本**在增长**（没有停滞）", grew,
              f"committed 长度序列尾={committed_lens[-6:]}")
        check("轮次里没有大面积空白（识别没有整段停摆）",
              len(committed_lens) >= 3,
              f"有文本的轮次数={len(committed_lens)} / 总轮次={len(d['rounds'])}")

        check("页面没有 JS 错误", not errors, "; ".join(errors[:3]))
        # ⚠️ **媒体 403 不算错**：页面刷新时会去恢复**上一次**识别的素材，
        # 而那次的素材可能是临时文件（不在 ``<assets>/uploads`` 里）⇒
        # 403 是预期内的。
        real_fail = [u for st, u in http_fail
                     if "/api/qwen3-asr/media" not in u]
        check("没有意外的 HTTP 失败", not real_fail,
              "; ".join(f"{st} {u}" for st, u in real_fail[:3]))
        browser.close()

    print("=" * 72)
    print(f"通过 {_PASS} / 失败 {_FAIL}")
    print("=" * 72)
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
