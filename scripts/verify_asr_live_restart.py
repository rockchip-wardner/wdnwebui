#!/usr/bin/env python3
"""回归测试：「流式语音识别」卡**反复中止/重开**之后，不该再报「有音频丢失」。

为什么要有这个脚本
------------------
用户实测（2026-11-18）：选「浏览器麦克风」→ 开始识别 → 反复「中止」→
「开始实时识别」→ 页面出现「⚠ 有音频丢失，识别结果可能有缺口」。

根因（**服务端**，``cards/qwen3_asr/service.py``）：

* ``_LiveAsrSession.stop()`` 取消 worker 协程时，**正在处理的那一块**
  （已出队、还在等推理）**不在队列里** ⇒ 扫队列扫不到它 ⇒ 它的
  ``future`` 永远没人 resolve ⇒ 那个 ``POST /live/chunk`` 请求
  **永久挂死**（实测 15 s 才超时）；
* ⇒ 浏览器的 ``fetch`` 永不返回 ⇒ 客户端的 ``sending`` 标志永远为
  true ⇒ 下一轮「开始实时识别」的 ``_livePump()`` 直接 return
  （**没人上行**）⇒ 上行队列（只容 2 块）溢出 ⇒ 丢最旧 ⇒ 弹「有音频丢失」。

⇒ 修法：``stop()`` 单独把在途那块捞出来 resolve 掉；客户端再加一道
「会话代号」保险 —— 过期的那一轮既不许再碰队列，也不许再中止当前会话。

本脚本走**真实浏览器**（Chromium + 假麦克风设备），断言：

1. 每轮「开始」都有块真的发出去（上行泵没被上一轮卡死）；
2. 客户端**丢块计数恒为 0**；
3. 「有音频丢失」提示**不出现**；
4. 中止之后「开始实时识别」按钮**可用**（不能卡死）；
5. 同一会话内相邻块间隔 < 1.5 s（队列只容 2 块 ⇒ 空洞必然伴随丢块）。

跑法::

    .venv/bin/python scripts/verify_asr_live_restart.py
    .venv/bin/python scripts/verify_asr_live_restart.py --cycles 5
"""
from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import sys
import tempfile
import time
import wave
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "https://127.0.0.1:8000"
CARD = "qwen3-asr"

#: 用真实语音当假麦克风（Chromium 的 ``--use-file-for-fake-audio-capture``）。
#: 没有样本就退回 Chromium 自带的蜂鸣音 —— 那样 ASR 推理太快，撑不出
#: 「推理途中被中止」这个窗口，测出来的意义有限。
SAMPLE_WAV = Path("/userdata/assets/uploads/asr_en.wav")

#: 队列只容 2 块 ⇒ 超过 1 s 必丢块。
MAX_UPLOAD_GAP_SEC = 1.5

#: **确定性复现**用的注入脚本：块请求**刚发出**（服务端还在推理）就点
#: 「中止」—— 这才是踩中 bug 的时刻。
#:
#: 为什么必须这样：服务端那个在途请求要是正常返回（用户恰好在两次请求
#: 之间点中止），就**不会**触发挂死 ⇒ 测试会「假通过」。⇒ 必须把中止
#: 精确地落在「请求已发出、服务端还在推理」的窗口里。
STOP_IN_FLIGHT_JS = """
(() => {
  window.__stopArmed = false;
  window.__stopFired = false;
  window.__inflight = 0;
  const of = window.fetch;
  window.fetch = function (input, init) {
    const url = (typeof input === 'string') ? input
               : ((input && input.url) || '');
    if (url.indexOf('/live/chunk') < 0) {
      return of.apply(this, arguments);
    }
    window.__inflight += 1;
    const p = of.apply(this, arguments);
    p.then(() => { window.__inflight -= 1; },
           () => { window.__inflight -= 1; });
    if (window.__stopArmed && !window.__stopFired) {
      // ⚠️ **等 60 ms 再看**：请求还没回来 ⇒ 服务端正在推理 ⇒ 此刻点
      //    「中止」才是「推理中 stop」（第 1 块只进缓冲、立刻返回，
      //    不是推理中）。
      setTimeout(() => {
        if (!window.__stopFired && window.__inflight > 0) {
          window.__stopFired = true;
          const b = document.getElementById('asr-mic-stop-btn');
          if (b) b.click();
        }
      }, 60);
    }
    return p;
  };
})();
"""


def build_fake_mic(path: str) -> bool:
    """把样本 WAV 降成 16 kHz / 单声道 / s16le（Chromium 假麦克风要的格式）。"""
    if not SAMPLE_WAV.is_file():
        return False
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    try:
        from app.core.audio.capture import resample_s16_linear
    except Exception:  # noqa: BLE001
        return False
    with wave.open(str(SAMPLE_WAV), "rb") as w:
        sr, sw, ch, n = (w.getframerate(), w.getsampwidth(),
                         w.getnchannels(), w.getnframes())
        raw = w.readframes(n)
    if sw == 3:                      # s24le → s16le
        import array
        a = array.array("h")
        for i in range(0, len(raw), 3):
            v = int.from_bytes(raw[i:i + 3], "little", signed=True) >> 8
            a.append(max(-32768, min(32767, v)))
        pcm = a.tobytes()
    elif sw == 1:
        import array
        a = array.array("h", [(b - 128) << 8 for b in raw])
        pcm = a.tobytes()
    else:
        pcm = raw
    if ch > 1:
        import array
        a = array.array("h")
        a.frombytes(pcm)
        pcm = array.array("h", [(a[i] + a[i + 1]) // 2
                                for i in range(0, len(a), 2)]).tobytes()
    out = resample_s16_linear(pcm, sr, 16000)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(out)
    return True


def check_no_hung_request(wav_path: str | None) -> list[str]:
    """服务端层：**中止时在途的那块请求必须拿到回应**（不能挂死）。

    这是根因那一层，而且断言是**确定性**的：

    * 一次发 **30 秒**音频 ⇒ 服务端要连做十几次推理（每块 1 秒）⇒
      这个请求会**在途好几秒**；
    * 0.3 s 后 DELETE 会话 ⇒ 取消 worker 时它**正在推理**；
    * ⇒ 那个在途请求的 ``future`` 没人 resolve ⇒ **请求永久挂死**
      （实测 15 s 超时才断）。

    ⚠️ 浏览器那一层（阶段 1）踩不踩得中取决于时序（中止点必须落在推理
    窗口里）⇒ 根因断言放这里，才不会「时序没对上就假通过」。
    """
    import http.client
    import threading

    problems: list[str] = []
    if not wav_path or not Path(wav_path).is_file():
        return ["没有可用的音频样本，跳过服务端层断言"]
    with wave.open(wav_path, "rb") as w:
        pcm = w.readframes(w.getnframes())
    pcm = pcm * 2                  # ~30 s ⇒ 服务端连做十几次推理

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    def new_conn():
        return http.client.HTTPSConnection("127.0.0.1", 8000, context=ctx,
                                           timeout=15)

    c = new_conn()
    try:
        c.request("POST", "/api/qwen3-asr/live/start",
                  json.dumps({"source": "browser", "sample_rate": 16000,
                              "n_channels": 1, "sample_width": 2,
                              "chunk_ms": 500}).encode(),
                  {"Content-Type": "application/json"})
        sid = json.loads(c.getresponse().read())["stream_id"]
    except Exception as exc:  # noqa: BLE001
        return [f"开不了实时会话：{exc}"]

    def send():
        cc = new_conn()
        try:
            cc.request("POST", "/api/qwen3-asr/live/chunk", pcm,
                       {"Content-Type": "application/octet-stream",
                        "X-Asr-Stream-Id": sid, "X-Asr-Seq": "0"})
            cc.getresponse().read()
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                cc.close()
            except Exception:  # noqa: BLE001
                pass

    th = threading.Thread(target=send)
    th.start()
    time.sleep(0.3)                # 推理进行中 ⇒ 中止
    try:
        c.request("DELETE", "/api/qwen3-asr/live/stop", None,
                  {"X-Asr-Stream-Id": sid})
        c.getresponse().read()
    except Exception as exc:  # noqa: BLE001
        problems.append(f"中止失败：{exc}")
    th.join(timeout=9)
    if th.is_alive():
        problems.append(
            "在途的块请求**挂死**（中止后 9 秒还没回应）—— 服务端没给"
            "在途那块一个回应 ⇒ 浏览器那个 fetch 永不返回 ⇒ 下一轮的"
            "上行泵被卡死 ⇒ 「有音频丢失」")
        th.join(timeout=1)
    try:
        c.close()
    except Exception:  # noqa: BLE001
        pass
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycles", type=int, default=3, help="开始/中止循环几轮")
    ap.add_argument("--run-sec", type=float, default=4.0, help="每轮识别的秒数")
    ap.add_argument("--keep-open", action="store_true")
    args = ap.parse_args()

    errors: list[str] = []
    #: 每个上行块：(发起时刻, 会话 id, 序号, HTTP 状态)
    chunks: list[tuple[float, str, int, int]] = []
    gap_shown = 0
    t0 = time.monotonic()

    tmpdir = tempfile.mkdtemp(prefix="asr-mic-")
    fake_mic = os.path.join(tmpdir, "mic.wav")
    has_speech = build_fake_mic(fake_mic)
    mic_args = ["--use-fake-ui-for-media-stream",
                "--use-fake-device-for-media-stream"]
    if has_speech:
        mic_args.append("--use-file-for-fake-audio-capture=" + fake_mic)
    print(f"假麦克风: {'真实语音 ' + SAMPLE_WAV.name if has_speech else '自带蜂鸣音'}")

    # ---- 阶段 0：服务端层（根因）—— 在途请求不许挂死 ------------------ #
    print("\n[阶段 0] 服务端层：中止时在途的那块请求必须有回应")
    problems0 = check_no_hung_request(fake_mic if has_speech else None)
    for p in problems0:
        print("  ❌ " + p)
    if not problems0:
        print("  ok  在途的块请求在中止后有回应（没挂死）")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-gpu",
                  "--autoplay-policy=no-user-gesture-required"] + mic_args)
        ctx = browser.new_context(viewport={"width": 1440, "height": 1000})
        ctx.grant_permissions(["microphone"], origin=BASE)
        page = ctx.new_page()
        page.add_init_script(STOP_IN_FLIGHT_JS)
        page.on("pageerror", lambda e: errors.append(f"[pageerror] {e}"))
        page.on("console", lambda m: errors.append(f"[console.error] {m.text}")
                if m.type == "error" else None)

        pending: dict[int, float] = {}

        def on_request(req):
            if req.method == "POST" and "/live/chunk" in req.url:
                # ⚠️ 别碰 ``req.post_data`` —— 块是**二进制** PCM，
                #    解成 utf-8 会抛 UnicodeDecodeError。
                pending[id(req)] = time.monotonic() - t0

        def on_response(resp):
            if resp.request.method == "POST" and "/live/chunk" in resp.url:
                started = pending.pop(id(resp.request), None)
                if started is None:
                    started = time.monotonic() - t0
                hdr = resp.request.headers
                chunks.append((started, hdr.get("x-asr-stream-id", ""),
                               int(hdr.get("x-asr-seq") or 0), resp.status))

        page.on("request", on_request)
        page.on("response", on_response)

        page.goto(f"{BASE}/#{CARD}", wait_until="networkidle")
        page.wait_for_timeout(2500)
        if page.locator(f"#page-{CARD}").count() == 0:
            print(f"❌ 页面里没有 #page-{CARD} —— 卡位占位符漏了？")
            browser.close()
            return 1

        skip = page.locator("text=跳过向导").first
        if skip.count():
            skip.click()
            page.wait_for_timeout(1200)
        page.locator('#asr-source-tabs button[data-source="browser"]').click()
        page.wait_for_timeout(600)
        if not page.locator("#asr-mic-btn").is_visible():
            print("❌ 切到「浏览器麦克风」后，「开始实时识别」按钮仍不可见")
            browser.close()
            return 1

        start_btn = page.locator("#asr-mic-btn")
        stop_btn = page.locator("#asr-mic-stop-btn")

        def gap_visible() -> bool:
            return page.evaluate(
                "() => { const el = document.getElementById('asr-live-gap');"
                " return !!(el && !el.classList.contains('hidden')); }"
            )

        def client_dropped() -> int:
            """状态行里的「丢块 N」—— 客户端自己丢的块数。

            状态行格式：``识别中 · 丢块 0 · 延迟 143 ms``。
            """
            txt = page.locator("#asr-live-status").inner_text()
            m = re.search(r"丢块\s*(\d+)", txt)
            return int(m.group(1)) if m else -1

        # -------------------------------------------------------------- #
        # 阶段 1：**推理途中**「中止」（确定性复现 bug 的那一步）
        # -------------------------------------------------------------- #
        #
        # 这一步是整个测试的关键：只有「中止」落在**请求在途**的时刻，
        # 服务端那个在途请求才会挂死 ⇒ 下一轮的上行泵才会被卡死。
        # 随机点的中止有大半概率落在两次请求之间 ⇒ 测不出问题（假通过）。
        page.evaluate("() => { window.__stopArmed = true; window.__stopFired = false; }")
        n_before = len(chunks)
        start_btn.click(timeout=8000)
        page.wait_for_timeout(4000)          # 注入脚本会在第一块发出后自动中止
        page.wait_for_timeout(1500)
        fired = page.evaluate("() => !!window.__stopFired")
        dropped = client_dropped()
        gap = gap_visible()
        print(f"\n[阶段 1] 推理中「中止」已触发={fired} · 客户端丢块 {dropped}"
              f" · 丢块提示={gap}")
        if not fired:
            errors.append("阶段 1：注入的「在途中止」没触发")
        if gap:
            gap_shown += 1
            errors.append("阶段 1：出现「有音频丢失」提示")

        # 关键断言：**再开一轮**，这一轮必须真的在上传。
        #   （bug 在的时候：上一轮的在途请求挂死 ⇒ ``sending`` 永远为
        #     true ⇒ 本轮的 ``_livePump()`` 直接 return ⇒ 一个块都发不出去
        #     ⇒ 队列溢出 ⇒ 丢块 + 「有音频丢失」。）
        n_before = len(chunks)
        start_btn.click(timeout=8000)
        page.wait_for_timeout(int(args.run_sec * 1000))
        dropped2 = client_dropped()
        sent2 = len(chunks) - n_before
        gap2 = gap_visible()
        print(f"[阶段 1] 重开后上行 {sent2} 块 · 客户端丢块 {dropped2}"
              f" · 丢块提示={gap2}")
        if sent2 == 0:
            errors.append("阶段 1：重开后**一个块都没发出去**（上行泵被上一轮"
                          "的在途请求卡死）")
        if dropped2 > 0:
            errors.append(f"阶段 1：重开后客户端丢了 {dropped2} 块")
        if gap2:
            gap_shown += 1
            errors.append("阶段 1：重开后出现「有音频丢失」提示")
        # 收掉这一轮，后面的常规循环从干净状态开始
        try:
            stop_btn.click(timeout=8000)
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(1200)

        # -------------------------------------------------------------- #
        # 阶段 2：常规的「开始 → 中止」反复切换
        # -------------------------------------------------------------- #
        for cycle in range(1, args.cycles + 1):
            n_before = len(chunks)
            try:
                start_btn.click(timeout=8000)
            except Exception as exc:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                errors.append(f"第 {cycle} 轮点「开始实时识别」失败：{exc}")
                break
            page.wait_for_timeout(int(args.run_sec * 1000))
            # ⚠️ **运行中**读「丢块 N」—— 中止之后状态行会变成「已停止」，
            #    那个数就看不到了。
            dropped = client_dropped()
            status_txt = page.locator("#asr-live-status").inner_text().strip()

            try:
                stop_btn.click(timeout=8000)
            except Exception as exc:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                errors.append(f"第 {cycle} 轮点「中止」失败：{exc}")
                break
            page.wait_for_timeout(1500)

            sent = len(chunks) - n_before
            gap = gap_visible()
            print(f"  [第 {cycle} 轮] 上行 {sent} 块 · 客户端丢块 {dropped}"
                  f" · 丢块提示={gap} · 状态「{status_txt}」")
            if sent == 0:
                errors.append(f"第 {cycle} 轮**一个块都没发出去**（上行泵被卡死）")
            if dropped > 0:
                errors.append(f"第 {cycle} 轮客户端丢了 {dropped} 块")
            if gap:
                gap_shown += 1
                errors.append(f"第 {cycle} 轮出现「有音频丢失」提示")

            text = start_btn.inner_text().strip()
            if start_btn.is_disabled() or "结束" in text:
                errors.append(f"第 {cycle} 轮中止后按钮卡在「{text}」"
                              "（disabled）—— 会话状态没复位")

        # 同一会话内相邻两块的间隔
        worst = 0.0
        by_sid: dict[str, list[float]] = {}
        for ts, sid, _seq, _st in chunks:
            by_sid.setdefault(sid, []).append(ts)
        for sid, times in by_sid.items():
            times.sort()
            for a, b in zip(times, times[1:]):
                worst = max(worst, b - a)
        bad_status = sorted({st for _t, _s, _q, st in chunks if st != 200})

        print("\n" + "=" * 72)
        print(f"上行块总数: {len(chunks)}（会话 {len(by_sid)} 个）")
        print(f"同一会话内相邻块最大间隔: {worst:.2f} s"
              f"（阈值 {MAX_UPLOAD_GAP_SEC} s）")
        print(f"非 200 的上行: {bad_status or '无'}")
        print(f"「有音频丢失」出现次数: {gap_shown}")
        print(f"问题: {errors or '无'}")
        print("=" * 72)

        if not args.keep_open:
            browser.close()

    ok = True
    errors.extend(problems0)
    if errors:
        print("❌ 中止/重开流程有问题")
        ok = False
    if worst > MAX_UPLOAD_GAP_SEC:
        print(f"❌ 上行出现 {worst:.2f} s 的空洞（队列只容 2 块 ⇒ 必然丢块）")
        ok = False

    print("✅ 中止/重开流程正常" if ok else "❌ 有问题")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
