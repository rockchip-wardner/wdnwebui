#!/usr/bin/env python3
"""M23 —— **seek（拖动进度条）之后的同步体检**。

用户报（2026-11-22）：
    「『含音轨的视频』播放的时候，字幕在实时翻译，但是多次拖动进度条，
      没有和字幕的翻译同声播放出来」

这一版把「同没同声」变成**一个可测量的数**：

=========================  ==========================================
指标                      口径
=========================  ==========================================
``ph``                    出声那一刻，**源话的播放头**在哪儿（秒）。
``src``                   这句译文对应的**源话位置**（媒体时间轴）。
``err = ph − src``        **同步误差**（秒）。
                          ≈ 0 ⇒ 译文跟源话**同声**；
                          很大 ⇒ 译文在讲**别的位置**的内容（陈旧音频）。
=========================  ==========================================

脚本化的播放头（模拟用户拖进度条）::

    0 ──── 18 s ──── 33 s ──── 48 s ────
    ph=t      ph=60+(t-18)   ph=20+(t-33)   ph=35+(t-48)
              ↑前跳            ↑后跳

用法::

    .venv/bin/python tools/probes/interp/m23_seek_sync.py
    .venv/bin/python tools/probes/interp/m23_seek_sync.py --video <路径> --seconds 60
"""
from __future__ import annotations

import argparse
import json
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request

BASE = "https://127.0.0.1:8000"
DEFAULT_VIDEO = "/userdata/assets/uploads/asr_zh_seek_test.mp4"

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

#: 播放头剧本：(起始墙钟, 起始媒体秒, 播放头速度)
#: ⇒ 相邻两段之间就是**一次拖动进度条**（前跳 / 后跳）。
SCRIPT = (
    (0.0, 0.0, 1.0),     # 正常播放
    (18.0, 60.0, 1.0),   # ← 前跳到 60 s
    (33.0, 20.0, 1.0),   # ← 后跳回 20 s
    (48.0, 35.0, 1.0),   # ← 后跳到 35 s
)


def _req(path: str, payload=None, method=None, timeout=120):
    url = BASE + path
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method=method or ("POST" if data else "GET"))
    with urllib.request.urlopen(req, context=_CTX, timeout=timeout) as r:
        raw = r.read()
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return {"_raw": raw[:200].decode("utf-8", "replace")}


def _multipart(path: str, fields: dict, timeout=900):
    """最小 multipart/form-data（多字段）。"""
    boundary = "----wdnprobe%s" % int(time.time() * 1000)
    body = b""
    for name, value in fields.items():
        body += (("--%s\r\n" % boundary).encode()
                 + ('Content-Disposition: form-data; name="%s"\r\n\r\n' % name).encode()
                 + str(value).encode() + b"\r\n")
    body += ("--%s--\r\n" % boundary).encode()
    req = urllib.request.Request(
        path if path.startswith("http") else BASE + path, data=body,
        headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary})
    with urllib.request.urlopen(req, context=_CTX, timeout=timeout) as r:
        raw = r.read()
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return {"_raw": raw[:300].decode("utf-8", "replace")}


def playhead_at(elapsed: float, no_seek: bool = False) -> float:
    """剧本化的播放头（媒体秒）。

    ⚠️ ``no_seek=True`` ⇒ **不拖动**，播放头从 0 一路 1× 往前走 ——
    用来把「**跟不跟得上字幕节奏**」和「**拖了之后对不对得上**」这两件事
    **分开测**：拖动会把话重播一遍（``replayed``）⇒ 队列里堆着两份内容
    ⇒ 测出来的是**队列积压**，不是「跟播放」的同步误差。
    """
    if no_seek:
        return elapsed
    t0, ph0, speed = SCRIPT[0]
    for t1, ph1, sp1 in SCRIPT[1:]:
        if elapsed < t1:
            return ph0 + (elapsed - t0) * speed
        t0, ph0, speed = t1, ph1, sp1
    return ph0 + (elapsed - t0) * speed


class PlayheadSim(threading.Thread):
    """**模拟浏览器的播放头**（每 250 ms 上报一次）。"""

    def __init__(self, stream_id: str = "", no_seek: bool = False) -> None:
        super().__init__(daemon=True)
        self.stream_id = stream_id
        self.no_seek = bool(no_seek)
        self.samples: list[tuple] = []      # (墙钟, 播放头)
        self._stop = threading.Event()

    def run(self) -> None:
        t0 = time.time()
        while not self._stop.is_set():
            el = time.time() - t0
            ph = playhead_at(el, self.no_seek)
            self.samples.append((time.time(), ph))
            _req("/api/qwen3-asr/interp/playhead",
                 {"stream_id": self.stream_id, "playhead_sec": round(ph, 3),
                  "playing": True, "ended": False, "failed": False})
            time.sleep(0.25)

    def stop(self) -> None:
        self._stop.set()

    def ph_at(self, wall: float) -> float:
        """某个墙钟时刻的播放头（最近一次采样）。"""
        best = 0.0
        for w, p in self.samples:
            if w <= wall:
                best = p
            else:
                break
        return best


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", default=DEFAULT_VIDEO)
    ap.add_argument("--seconds", type=float, default=120.0)
    ap.add_argument("--gate", default="follow", choices=["now", "follow"])
    ap.add_argument("--wait-segments", type=int, default=12,
                    help="等识别/翻译追够多少句才开始拖进度条")
    ap.add_argument("--no-seek", action="store_true",
                    help="**不拖动**（播放头 1× 往前走）—— 单独测「跟字幕节奏」")
    args = ap.parse_args()

    print("== 起同传（gate=%s）==" % args.gate)
    _req("/api/qwen3-asr/interp/stop", {}, method="POST")
    time.sleep(0.5)
    st = _req("/api/qwen3-asr/interp/start",
              {"mode": "steady", "gate": args.gate, "target_lang": "zh-CN",
               "volume": 0.01, "source": "file"})
    print("   interp:", {k: st.get(k) for k in
                         ("running", "mt_ready", "tts_local_ready", "tempo")})

    deadline = time.time() + 120
    while time.time() < deadline:
        s = _req("/api/qwen3-asr/interp/status")
        if s.get("mt_ready") and s.get("tts_local_ready"):
            print("   模型就绪 ✓")
            break
        time.sleep(1.0)
    else:
        print("   ⚠️ 模型没就绪（数字会偏冷启动）")

    _req("/api/tts/queue/clear", {}, method="POST")
    time.sleep(0.5)

    base = int(_req("/api/qwen3-asr/interp/status").get("translated") or 0)
    print("== 识别视频 ==", args.video)
    t0 = time.time()
    res = _multipart("/api/qwen3-asr/transcribe",
                     {"path": args.video, "append": "false",
                      "translate_mode": "line"})
    print("   识别请求已发（%.1f s）⇒ %s" % (time.time() - t0,
                                     {k: res.get(k) for k in ("rid", "state")}))

    # ⚠️ **等识别追上来再拖进度条** —— 真实场景里 ASR 跑在播放头**前面**
    #    （识别比实时快）⇒ 用户拖到的位置**大多已经识别过了** ⇒ 那正是
    #    「字幕有译文、却没同声播出来」的现场。
    print("== 等译够 %d 句再开始拖 ==" % args.wait_segments)
    deadline = time.time() + 420
    while time.time() < deadline:
        s = _req("/api/qwen3-asr/interp/status")
        got = int(s.get("translated") or 0) - base
        if got >= args.wait_segments:
            print("   已译 %d 句 ✓" % got)
            break
        time.sleep(1.0)
    else:
        print("   ⚠️ 没等到（继续测）")

    print("== 模拟播放头（含两次拖动）==")
    sim = PlayheadSim("", no_seek=args.no_seek)
    sim.start()

    print("== 采样 %d s ==" % int(args.seconds))
    seen: dict = {}
    t_sample_start = time.time()
    t_end = time.time() + args.seconds
    while time.time() < t_end:
        q = _req("/api/tts/queue/status")
        for t in (q.get("timings") or []):
            seen[t.get("seq")] = t
        time.sleep(0.25)
    sim.stop()

    # ⚠️⚠️ **只统计「本次运行窗口内」播出的条目** ——
    # 队列的 ``timings`` 是**累积**的（最后一次探针 / 回归留下的历史都在
    # 里面）⇒ 不按时间窗过滤 ⇒ 拿到的 ``t_play`` 是几小时前的 ⇒
    # 用本探针的播放头剧本去外推 ⇒ **同步误差全是假的**。
    rows = [seen[k] for k in sorted(seen, key=lambda x: (x is None, x))]
    before = len(rows)
    rows = [t for t in rows
            if float(t.get("t_play") or 0.0) >= t_sample_start - 2.0]
    print("   （队列 timings 共 %d 条，本次窗口内 %d 条）"
          % (before, len(rows)))
    if not rows:
        print("   ⚠️ 没有采到 timings")
        print(json.dumps(_req("/api/tts/queue/status"), ensure_ascii=False)[:1500])
        return 1

    print("\n%-5s %-9s %-9s %-9s %-8s %-7s %s" % (
        "seq", "t_play", "ph", "src", "err", "a_lag", "text"))
    errs = []
    for t in rows:
        src = float(t.get("src_start_sec") or -1.0)
        ph = sim.ph_at(float(t.get("t_play") or 0.0))
        err = (ph - src) if src >= 0 else None
        if err is not None:
            errs.append(err)
        print("%-5s %-9.2f %-9.2f %-9.2f %-8s %-7s %s" % (
            t.get("seq"), float(t.get("t_play") or 0.0), ph, src,
            ("%.2f" % err) if err is not None else "-",
            t.get("a_lag"), (t.get("text") or "")[:24]))

    if errs:
        big = [e for e in errs if abs(e) > 5.0]
        print("\n同步误差 |err| > 5 s 的句子: %d / %d" % (len(big), len(errs)))
        if big:
            print("   取值:", ", ".join("%.1f" % e for e in big))
    st = _req("/api/qwen3-asr/interp/status")
    print("\ninterp:", {k: st.get(k) for k in
                        ("segments", "translated", "submitted", "pending",
                         "skipped_seek", "replayed", "played")})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
