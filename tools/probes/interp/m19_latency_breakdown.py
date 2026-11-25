#!/usr/bin/env python3
"""M19 —— **同传全链路的分段耗时明细**（用户报「滞后 15 秒」）。

把「一句话从**源话说完**到**译文播完**」之间的每一段拆开：

=========================  ==================================================
段                        口径
=========================  ==================================================
**ASR 出句**              切句器切出这句的时刻（``t_seg``）
**翻译队列排队**          ``t_deq − t_seg``（前面排了几句）
**等 MT 就绪**            ``wait_mt``（冷启动是大头；稳态 ≈ 0）
**纯翻译推理**            ``infer``（MT 真跑的时间）
**放行等待**              ``t_submit − t_tr``（放行器压住的那段）
**队列排队**              ``t_synth_start − t_submit``
**合成首窗**              ``t_first_pcm − t_synth_start``（这段之前一声不响）
**出声启动**              ``t_play − t_first_pcm``（aplay 起来 + 写第一块）
**播完**                  ``t_end − t_play`
**端到端滞后**            ``t_end − 源话说完时刻``
=========================  ==================================================

⚠️ **必须先热身**（``--warm``，默认开）：MT / TTS 冷启动要 20~30 s，
不热身测到的是「冷启动」不是「链路」。热身 = 先起同传、等
``mt_ready`` / ``tts_local_ready`` 都为真，再跑正式测量。

用法::

    .venv/bin/python tools/probes/interp/m19_latency_breakdown.py [follow|now] [--cold]
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


def api(method: str, path: str, payload=None, timeout: int = 180):
    data = None
    hdrs = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=hdrs,
                                 method=method)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            body = r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
    try:
        return json.loads(body or "{}")
    except Exception:  # noqa: BLE001
        return {"raw": body}


def fmt(v) -> str:
    try:
        return f"{float(v):6.2f}"
    except Exception:  # noqa: BLE001
        return "   -  "


def warm_up() -> dict:
    """把 MT / TTS 拉到**热态**（等模型加载完）。

    ⇒ 不热身的话，测出来的是「冷启动 24 s」，不是「链路 3 s」。
    """
    print("[热身] 起同传（触发 MT / TTS 的 ensure）…")
    api("POST", "/api/qwen3-asr/interp/stop")
    api("POST", "/api/tts/queue/clear")
    api("POST", "/api/qwen3-asr/interp/start", {
        "mode": "steady", "gate": "follow", "target_lang": "zh-CN",
        "volume": 0.01, "source": "file",
    })
    t0 = time.time()
    last = {}
    while time.time() - t0 < 180:
        time.sleep(2.0)
        last = api("GET", "/api/qwen3-asr/interp/status")
        mt = bool(last.get("mt_ready"))
        tts = bool(last.get("tts_local_ready"))
        print(f"  [{time.time() - t0:5.1f}s] mt_ready={mt} "
              f"tts_local_ready={tts} tts_waiting={last.get('tts_waiting')}")
        if mt and tts:
            print(f"[热身] 完成，用时 {time.time() - t0:.1f}s")
            return last
    print("[热身] 超时（继续测，但数据含冷启动）")
    return last


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    gate = args[0] if args else "follow"
    cold = "--cold" in sys.argv

    if not cold:
        warm_up()

    print(f"=== 同传链路分段耗时（文件源 · gate={gate} · "
          f"{'冷态' if cold else '热态'}）===")
    api("POST", "/api/qwen3-asr/interp/stop")
    api("POST", "/api/tts/queue/clear")
    time.sleep(1.0)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=[
            "--no-sandbox", "--autoplay-policy=user-gesture-required"])
        ctx = browser.new_context(ignore_https_errors=True,
                                  viewport={"width": 1440, "height": 1000})
        install_active_app_guard(ctx)
        install_active_app_spoof(ctx)
        page = ctx.new_page()
        page.goto(f"{BASE}/#{CARD}", wait_until="networkidle")
        page.wait_for_timeout(2500)
        skip = page.locator("#wd-wizard-root-qwen3-asr .wd-wizard-skip-btn").first
        if skip.count() and skip.is_visible():
            skip.click()
            page.wait_for_timeout(1500)
        page.wait_for_selector("#page-qwen3-asr:not(.hidden)", timeout=20000)
        page.wait_for_selector("#asr-interp-block", timeout=30000)
        page.fill("#asr-source", SRC_WAV)
        page.wait_for_timeout(800)

        t0 = time.time()
        page.click("#asr-interp-start")
        page.wait_for_timeout(800)
        # 播放时机：默认「跟播放」；要「不等播放」就切一下
        if gate == "now":
            page.select_option("#asr-interp-gate", "now")
            page.wait_for_timeout(600)
        page.click("#asr-start-btn", timeout=90000)

        # 等到「播完」或超时
        deadline = time.time() + 240
        st: dict = {}
        q: dict = {}
        # ⚠️ **面板读数序列** —— 用户看到的那个「滞后」就是 ``lag_sec``，
        # 它在跟播模式下 = 「源话比**最后一句已播译文**前进了多少秒」。
        # ⇒ 逐秒采样，最后给 max / avg（结束时它会归 0，所以**必须采样**）。
        panel_lag: list[float] = []
        queue_lag: list[float] = []
        while time.time() < deadline:
            page.wait_for_timeout(1000)
            st = api("GET", "/api/qwen3-asr/interp/status")
            q = api("GET", "/api/tts/queue/status")
            panel_lag.append(float(st.get("lag_sec") or 0.0))
            queue_lag.append(float(q.get("last_lag_sec") or 0.0))
            tms = q.get("timings") or []
            if len(tms) >= 2 and not q.get("playing") and int(q.get("depth") or 0) == 0:
                break
        page.wait_for_timeout(2000)
        st = api("GET", "/api/qwen3-asr/interp/status")
        q = api("GET", "/api/tts/queue/status")

        itm = st.get("timings") or []
        qtm = q.get("timings") or []

        print("\n--- 同传侧（ASR 出句 → 翻译队列 → 等 MT → 推理 → 放行 → 入队）---")
        print(f"{'#':>3} {'翻译排队':>8} {'等MT':>7} {'推理':>7} {'放行等待':>8} "
              f"{'源起-源止':>10}  源文")
        rows = []
        for t in itm:
            seg = t.get("t_seg") or 0
            deq = t.get("t_deq") or seg
            tr = t.get("t_tr") or 0
            sub = t.get("t_submit") or 0
            d_q = (deq - seg) if seg else 0
            d_gate = (sub - tr) if sub and tr else 0
            rows.append((t, d_q, d_gate))
            print(f"{t.get('index'):>3} {fmt(d_q)} {fmt(t.get('wait_mt')):>7} "
                  f"{fmt(t.get('infer')):>7} {fmt(d_gate):>8} "
                  f"{fmt((t.get('src_end') or 0) - (t.get('src_start') or 0)):>10}  "
                  f"{str(t.get('text'))[:26]!r}")

        print("\n--- TTS 侧（入队 → 合成 → 首窗 → 出声 → 播完）---")
        print(f"{'seq':>4} {'排队':>6} {'合成首窗':>8} {'出声启动':>8} "
              f"{'播放时长':>8} {'变速':>5} {'端到端滞后':>10}  译文")
        for t in qtm:
            q_wait = (t.get("t_synth_start") or 0) - (t.get("t_submit") or 0)
            win = (t.get("t_first_pcm") or 0) - (t.get("t_synth_start") or 0)
            pstart = (t.get("t_play") or 0) - (t.get("t_first_pcm") or 0)
            plen = (t.get("t_end") or 0) - (t.get("t_play") or 0)
            print(f"{t.get('seq'):>4} {fmt(q_wait)} {fmt(win)} {fmt(pstart)} "
                  f"{fmt(plen)} {fmt(t.get('tempo')):>5} {fmt(t.get('lag')):>10}  "
                  f"{str(t.get('text'))[:24]!r}")

        def m(vals):
            if isinstance(vals, (int, float)):
                return float(vals)
            vals = [float(v) for v in vals if isinstance(v, (int, float))]
            return (sum(vals) / len(vals)) if vals else 0.0

        d_q = [r[1] for r in rows]
        d_gate = [r[2] for r in rows]
        waits = [t.get("wait_mt") or 0 for t in itm]
        infers = [t.get("infer") or 0 for t in itm]
        wins = [(t.get("t_first_pcm") or 0) - (t.get("t_synth_start") or 0)
                for t in qtm]
        pstart = [(t.get("t_play") or 0) - (t.get("t_first_pcm") or 0)
                  for t in qtm]
        qwait = [(t.get("t_synth_start") or 0) - (t.get("t_submit") or 0)
                 for t in qtm]
        lags = [t.get("lag") or 0 for t in qtm]
        tempos = [t.get("tempo") or 1 for t in qtm]
        # 源话总长（文件源：音频时间轴）
        src_span = max([(t.get("src_end") or 0) for t in itm] or [0]) - \
            min([(t.get("src_start") or 0) for t in itm] or [0])
        out_span = sum([(t.get("t_end") or 0) - (t.get("t_play") or 0)
                        for t in qtm] or [0])

        print("\n--- 汇总 ---")
        print(f"  翻译排队（出句 → 取出）      平均 {m(d_q):5.2f} s")
        print(f"  等 MT 就绪                  平均 {m(waits):5.2f} s")
        print(f"  纯翻译推理                  平均 {m(infers):5.2f} s")
        print(f"  放行等待（译文 → 入队）     平均 {m(d_gate):5.2f} s")
        print(f"  队列排队（入队 → 开始合成） 平均 {m(qwait):5.2f} s")
        print(f"  合成首窗（开始合成 → 首 PCM）平均 {m(wins):5.2f} s  ← 出声前的空白")
        print(f"  出声启动（首 PCM → 写 aplay） 平均 {m(pstart):5.2f} s")
        print(f"  播完（出声 → 播完）         平均 {m(out_span / max(1, len(qtm)) if len(qtm) else 0.0):5.2f} s")
        print(f"  端到端滞后（源话说完 → 播完）平均 {m(lags):5.2f} s")
        print(f"\n  产能：源话 {src_span:.2f} s vs 译文 {out_span:.2f} s "
              f"=> {out_span / max(0.01, src_span):.3f}× "
              f"（>1 ⇒ 越播越落后）")
        print(f"  实际变速：{', '.join(str(t) for t in tempos) or '-'}")
        print(f"\n  面板显示的滞后: lag_sec={st.get('lag_sec')} "
              f"lag_avg_sec={st.get('lag_avg_sec')} "
              f"队列 last_lag_sec={(q.get('last_lag_sec'))} "
              f"avg_lag_sec={(q.get('avg_lag_sec'))}")
        print(f"  切句 {st.get('segments')} · 翻译 {st.get('translated')} · "
              f"入队 {st.get('submitted')} · 已播 {(q.get('done'))} · "
              f"pending {st.get('pending')}")

        # ---- 面板读数序列（用户实际看到的那个数） ------------------- #
        print("\n--- 面板读数序列（每秒采样）---")
        print("  面板 lag_sec（跟播：源话比最后一句已播译文前进了多少）: "
              + " ".join(f"{v:.1f}" for v in panel_lag))
        print("  队列 last_lag_sec（端到端：播完 − 源话说完）: "
              + " ".join(f"{v:.1f}" for v in queue_lag))
        if panel_lag:
            print(f"  面板滞后: max {max(panel_lag):.1f} s · "
                  f"avg {sum(panel_lag) / len(panel_lag):.1f} s")
        if queue_lag:
            print(f"  队列滞后: max {max(queue_lag):.1f} s · "
                  f"avg {sum(queue_lag) / len(queue_lag):.1f} s")

        api("POST", "/api/qwen3-asr/interp/stop")
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
