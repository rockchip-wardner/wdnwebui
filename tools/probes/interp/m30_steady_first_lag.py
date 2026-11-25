#!/usr/bin/env python3
"""M30 —— **稳档的首句延迟 + 失败数 + 上行丢块数**（一个探针三个数）。

为什么单独写一个
================
``m12_live_lag.py`` 给的是**逐秒时间线**，但：

* 它的 ``play``（已播数）是**累计**的 ⇒ 看不出「第一句什么时候出声」；
* 它**不报**翻译失败数与上行丢块数（那两个在 ``m9`` 里）⇒ 要跑两个探针
  才能凑齐三个数；
* 逐秒采样 ⇒ 首句延迟的分辨率只有 1 秒。

⇒ 这里按 **100 ms** 采样，并**以增量**为口径，把三个数一次凑齐：

========================  ==========================================
数                       口径
========================  ==========================================
``首声延迟``             第一块音频喂进去 → **用户真的听到第一口**
                        （队列 ``playing`` 变 true ⇒ 音频进了 aplay 管道）
``翻译失败数``           引擎的 ``failed`` 增量
``上行丢块数``           上行的 ``dropped_chunks`` 增量（**不能丢语音**）
========================  ==========================================

另外把首声延迟**拆成五个里程碑**（成句 → 翻译完 → 入队 → 开合成 → 第一口），
这样「慢在哪一段」一眼可见。

⚠️ **「第一口」不能用 ``done`` 当**（v3 修正）
=========================================
队列的 ``done`` 在 ``_finish_job()`` 里自增 ⇒ 那是「**整句**音频全部写进管道」
的时刻。对一个 17 s 的长句，它比用户听到第一口晚 17 s ⇒ 用它当「首句延迟」
会把**合成吞吐**误算成**感知延迟**（实测：快档报的 32.44 s 里，真正的第一口
在 18.2 s）。⇒ 第一口 = ``playing`` 变 true（``_speaking`` 被赋值的那一刻）。
``done`` 另记为「首句写完」，两个数一起看才完整。

⚠️ **喂块必须在后台线程**（v2 修正）
==================================
第一版把「喂块」和「采样」放在同一个循环里 ⇒ 采样被**喂块请求阻塞**：

* ``POST /live/chunk`` 要等服务端把 1 秒 PCM 同步解码完才返回 ⇒ **实测阻塞
  约 0.6 秒**；
* ⇒ 采样实际变成「每 1.05 秒成串来 4~5 个 100 ms 采样」⇒ **两次采样之间有
  ~0.6 秒盲区**；
* ⇒ 「首句延迟」这一栏的**检测粒度就是 ~0.6 s**（实测两跑 9.52 / 8.63 s，
  0.89 s 的离散度正是这个盲区造成的，**不是**系统抖动）。

⇒ v2 把喂块挪进后台线程，采样在主线程**连续跑** ⇒ 盲区消失，粒度真 100 ms。

⚠️ **音量 1%**（按 D2 的自测纪律）—— 走的是「板载喇叭出声」这条，
⇒ 必须把板载混音器也压低，否则 1% 软件音量仍按硬件音量放。

用法::

    .venv/bin/python tools/probes/interp/m30_steady_first_lag.py [steady|fast] [秒数]
"""
from __future__ import annotations

import json
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "https://127.0.0.1:8000"
WAV = "/userdata/assets/uploads/asr_en.wav"

_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode = ssl.CERT_NONE


def api(method: str, path: str, payload=None, headers=None, timeout=120):
    data = None
    hdr = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        hdr["Content-Type"] = "application/json"
    req = urllib.request.Request(
        BASE + path, data=data, headers=hdr, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as r:
            body = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
    try:
        return json.loads(body or "{}")
    except Exception:  # noqa: BLE001
        return {"raw": body[:400]}


def load_pcm() -> bytes:
    """素材统一降成 16 kHz / 单声道 / s16（实时链路的口径）。"""
    out = Path(tempfile.gettempdir()) / ".m30_live16.raw"
    subprocess.run(["sox", WAV, "-r", "16000", "-c", "1", "-b", "16",
                    "-e", "signed-integer", str(out)], check=True)
    return out.read_bytes()


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "steady"
    seconds = int(sys.argv[2]) if len(sys.argv) > 2 else 25
    pcm = load_pcm()

    print(f"=== {mode} 档 · {seconds}s 音频 · 音量 1% ===")
    api("POST", "/api/qwen3-asr/interp/stop", {})
    time.sleep(0.5)
    # ⚠️ 清掉上一轮残留（否则残留作业的 ``playing`` 会在 t=0 就为 true ⇒
    # 「第一口」判据被污染）。
    api("POST", "/api/tts/queue/clear", {})
    time.sleep(0.5)
    api("POST", "/api/qwen3-asr/interp/start",
        {"mode": mode, "gate": "now", "target_lang": "zh-CN",
         "volume": 0.01, "source": "live"})
    time.sleep(0.5)
    base = api("GET", "/api/qwen3-asr/interp/status")
    qbase = api("GET", "/api/tts/queue/status")
    b_seg = int(base.get("segments") or 0)
    b_tr = int(base.get("translated") or 0)
    b_sub = int(base.get("submitted") or 0)
    b_fail = int(base.get("failed") or 0)
    b_drop = int(base.get("dropped_chunks") or 0)
    b_done = int(qbase.get("done") or 0)

    sid = api("POST", "/api/qwen3-asr/live/start",
              {"source": "browser", "sample_rate": 16000, "n_channels": 1,
               "sample_width": 2, "chunk_ms": 1000})["stream_id"]
    chunk = 32000                       # 1 秒
    n = min(seconds, max(1, len(pcm) // chunk))
    print(f"   流 id={sid} · 喂 {n} 块 × 1 s（后台线程，采样连续 100 ms）")

    # 里程碑：第一个出现的时刻（秒）
    ms: dict = {}

    def mark(key: str, t: float) -> None:
        ms.setdefault(key, t)

    t0 = time.monotonic()
    t0_wall = time.time()
    print(f"   t0 墙钟 = {t0_wall:.3f}（用于和队列 timings 的绝对时间对齐）")
    timeline: list[dict] = []
    feed_err: list[str] = []
    feed_done = threading.Event()

    def feed() -> None:
        """后台喂块 + finish。**不参与采样**（v2 的关键改动）。"""
        try:
            for i in range(n):
                target = t0 + i * 1.0
                wait = target - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                off = (i * chunk) % max(1, len(pcm) - chunk)
                req = urllib.request.Request(
                    BASE + "/api/qwen3-asr/live/chunk", data=pcm[off:off + chunk],
                    headers={"X-Asr-Stream-Id": sid, "X-Asr-Seq": str(i),
                             "Content-Type": "audio/pcm"}, method="POST")
                try:
                    with urllib.request.urlopen(req, timeout=60, context=_SSL) as r:
                        r.read()
                except Exception as exc:  # noqa: BLE001
                    feed_err.append(f"块 {i}: {exc}")
            try:
                api("POST", "/api/qwen3-asr/live/finish",
                    headers={"X-Asr-Stream-Id": sid})
            except Exception as exc:  # noqa: BLE001
                feed_err.append(f"finish: {exc}")
        finally:
            feed_done.set()

    th = threading.Thread(target=feed, daemon=True)
    th.start()

    def sample(t: float) -> None:
        st = api("GET", "/api/qwen3-asr/interp/status")
        q = api("GET", "/api/tts/queue/status")
        row = {
            "t": round(t, 2),
            "seg": int(st.get("segments") or 0) - b_seg,
            "tr": int(st.get("translated") or 0) - b_tr,
            "sub": int(st.get("submitted") or 0) - b_sub,
            "play": int(q.get("done") or 0) - b_done,
            "playing": bool(q.get("playing")),
            "lag": st.get("lag_sec"),
        }
        timeline.append(row)
        if row["seg"] > 0:
            mark("成句", t)
        if row["tr"] > 0:
            mark("翻译完", t)
        if row["sub"] > 0:
            mark("入队", t)
        if int(q.get("inflight") or 0) > 0:
            mark("开合成", t)
        # ⚠️⚠️ **队列「忙碌」≠「第一口」** —— 快照里的 ``playing`` 是
        # ``合成中 or 播放中``（``queue.py:922``）⇒ 作业一入队它就为 true。
        # 「第一口」要用队列**自记**的 ``timings[].t_play``（毫秒级，采样无误差）。
        if row["playing"]:
            mark("队列开动", t)
        if row["play"] > 0:
            mark("首句写完", t)

    # 主循环：连续 100 ms 采样，直到「喂完 + 排空」
    deadline = t0 + 300
    drained = False
    grace = 0.0
    while time.monotonic() < deadline:
        t = time.monotonic() - t0
        sample(t)
        if feed_done.is_set():
            st = api("GET", "/api/qwen3-asr/interp/status")
            q = api("GET", "/api/tts/queue/status")
            pend = int(st.get("pending") or 0)
            empty = (pend == 0
                     and int(q.get("depth") or 0) == 0
                     and int(q.get("inflight") or 0) == 0
                     and not q.get("playing"))
            if empty:
                # 再给引擎一点时间把**最后一批**提交掉（攒批是按内容量/时间
                # 触发的 ⇒ 「源读完」的那一刻才会把剩下的全放）。
                if grace <= 0:
                    grace = time.monotonic() + 3.0
                elif time.monotonic() >= grace:
                    drained = True
            else:
                grace = 0.0
        if drained:
            break
        time.sleep(0.1)

    st = api("GET", "/api/qwen3-asr/interp/status")
    q = api("GET", "/api/tts/queue/status")
    api("POST", "/api/qwen3-asr/interp/stop", {})

    fail = int(st.get("failed") or 0) - b_fail
    drop = int(st.get("dropped_chunks") or 0) - b_drop
    seg = int(st.get("segments") or 0) - b_seg
    tr = int(st.get("translated") or 0) - b_tr
    sub = int(st.get("submitted") or 0) - b_sub
    done = int(q.get("done") or 0) - b_done

    # --- 采样密度自检（v2：确认没有盲区）---
    ts = [r["t"] for r in timeline]
    gaps = [round(ts[i + 1] - ts[i], 2) for i in range(len(ts) - 1)]
    max_gap = max(gaps) if gaps else 0.0

    # --- 逐秒表（每个整秒取最后一个采样）---
    per_sec: dict = {}
    for r in timeline:
        per_sec[int(r["t"])] = r

    print("\n--- 逐秒时间线（每个整秒的最后一个采样）---")
    print("      t   切句 翻译 入队 写完 在播   滞后(s)")
    for k in sorted(per_sec):
        r = per_sec[k]
        print(f"   {k:5d}   {r['seg']:3d}  {r['tr']:4d} {r['sub']:4d} "
              f"{r['play']:4d} {'●' if r['playing'] else ' ':4s} {r['lag']}")

    print("\n--- 变化点（只列有变化的）---")
    prev = None
    for r in timeline:
        key = (r["seg"], r["tr"], r["sub"], r["play"], r["playing"], r["lag"])
        if key != prev:
            print("   ", json.dumps(r, ensure_ascii=False))
            prev = key

    print(f"\n--- 采样自检: {len(timeline)} 个采样 · 最大间隔 {max_gap} s ---")
    print(f"--- t0 墙钟 = {t0_wall:.3f} · 结束墙钟 = {time.time():.3f} ---")

    print("\n--- 结果 ---")
    for key in ("成句", "翻译完", "入队", "开合成", "队列开动", "首句写完"):
        v = ms.get(key)
        print(f"  {key:6s}: " + (f"{v:.2f} s" if v is not None else "未发生"))

    # ⚠️ **第一口 = 队列自记的 ``t_play``** —— 毫秒精度，没有采样误差。
    # 快照里的 ``playing`` 只是「忙碌」（含合成中），不能当第一口。
    jobs = [t for t in (q.get("timings") or [])
            if float(t.get("t_submit") or 0) >= t0_wall - 0.5]
    first_sound = None
    if jobs:
        fj = min(jobs, key=lambda t: float(t.get("t_submit") or 0))
        first_sound = float(fj["t_play"]) - t0_wall
        print(f"\n  第一句作业（seq={fj['seq']}）耗时拆解:")
        print(f"    提交      +{float(fj['t_submit']) - t0_wall:6.2f} s")
        print(f"    首帧 PCM  +{float(fj['t_first_pcm']) - t0_wall:6.2f} s"
              f"（合成 {float(fj['t_first_pcm']) - float(fj['t_submit']):.2f} s）")
        print(f"    起播      +{float(fj['t_play']) - t0_wall:6.2f} s"
              f"  ← **用户听到第一口**")
        print(f"    播完      +{float(fj['t_end']) - t0_wall:6.2f} s"
              f"（音频 {fj.get('audio_sec')} s · 滞后 {fj.get('lag')} s）")
        print(f"    文本: {fj.get('text', '')[:60]}")
    print(f"\n  **首声延迟（第一块音频 → 用户听到第一口）**: "
          f"{('%.2f s' % first_sound) if first_sound is not None else '未发生'}")
    print(f"  切句 {seg} · 翻译 {tr} · 入队 {sub} · 播完 {done}")
    print(f"  **翻译失败数**: {fail}")
    print(f"  **上行丢块数**: {drop}")
    print(f"  面板滞后 lag_sec={st.get('lag_sec')} · "
          f"队列 last_a_lag={q.get('last_a_lag')}")
    if feed_err:
        print(f"  ⚠️ 喂块异常: {feed_err}")
    ok = (fail == 0 and drop == 0 and first_sound is not None)
    print(f"\n  判定: {'✅ 出声了、没失败、没丢块' if ok else '⚠️ 有问题'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
