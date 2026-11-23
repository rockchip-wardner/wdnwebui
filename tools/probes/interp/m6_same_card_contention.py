#!/usr/bin/env python3
"""M6 — 卡 0 上 **ASR 与 MT 同卡争用** 实测。

这是「ASR + 翻译放同一张卡」这个部署形态的**关键未知**：
两个都是 llm_mode 8 核上下文 ⇒ 驱动在它们之间**时间片轮转** ⇒
翻译的负载会直接拖慢识别的轮次。

量法（走**真实生产链路**，不绕开服务）：

* 阶段 A（基线）：只跑识别（``POST /api/qwen3-asr/transcribe``），
  从 WS 上记录每条 ``round`` 帧的**到达时刻** ⇒ 轮间隔。
* 阶段 B（争用）：同样跑识别，同时用 ``POST /api/qwen3-asr/translate/line``
  持续打 MT（同一个卡 0 的 hy-mt-1-5 worker）⇒ 再算轮间隔。

判据：轮间隔膨胀 > 30% ⇒ 必须给 MT 限速给 ASR 让路。

用法::

    python m6_same_card_contention.py [--audio PATH] [--out FILE]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import ssl
import statistics
import threading
import time
from pathlib import Path

import requests
import websockets

BASE = "https://127.0.0.1:8000"
WS_URL = "wss://127.0.0.1:8000/api/qwen3-asr/ws"
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

DEFAULT_AUDIO = "/userdata/assets/uploads/asr_en.wav"

MT_TEXT = (
    "The weather is very nice today, and the park near my house is full of "
    "people who came out for a walk after dinner. I have been thinking about "
    "how much has changed in this small town over the past ten years, when the "
    "old factory on the north side was still running and half of the young "
    "people here used to work there. Now the factory has become a small park "
    "with a playground and a coffee shop, and the school has doubled in size. "
    "I do not miss the noise from the factory, but I do miss the way the whole "
    "street used to be busy every morning."
)


def _session() -> requests.Session:
    s = requests.Session()
    s.verify = False
    s.trust_env = False
    return s


def enable_translation(s: requests.Session) -> None:
    """打开字幕翻译开关（否则 translate/line 直接被拒）。"""
    r = s.post(BASE + "/api/qwen3-asr/translate/settings",
               json={"enabled": True, "target_lang": "chinese"}, timeout=30)
    r.raise_for_status()


def mt_hammer(s: requests.Session, stop: threading.Event, stats: dict) -> None:
    """持续打 MT（走真实翻译队列）。"""
    n = 0
    while not stop.is_set():
        try:
            r = s.post(BASE + "/api/qwen3-asr/translate/line",
                       json={"text": MT_TEXT, "lang": "english", "seq": n},
                       timeout=60)
            if r.status_code == 200 and r.json().get("ok"):
                n += 1
                stats["sent"] = n
            else:
                stats["last_error"] = r.text[:200]
                time.sleep(0.2)
        except Exception as exc:  # noqa: BLE001
            stats["last_error"] = str(exc)[:200]
            time.sleep(0.3)


async def run_phase(name: str, audio: str, with_mt: bool,
                    s: requests.Session) -> dict:
    """跑一次识别，收集 round 到达时刻。"""
    rounds: list[float] = []
    done_at: float | None = None
    errors: list[str] = []

    stop = threading.Event()
    mt_stats: dict = {}
    mt_thread = None
    if with_mt:
        mt_thread = threading.Thread(target=mt_hammer, args=(s, stop, mt_stats),
                                     daemon=True)
        mt_thread.start()
        time.sleep(1.0)          # 让 MT 先热起来

    async with websockets.connect(WS_URL, ssl=SSL_CTX,
                                  open_timeout=15, ping_interval=None) as ws:
        t0 = time.monotonic()
        r = s.post(BASE + "/api/qwen3-asr/transcribe",
                   data={"path": audio}, timeout=120)
        r.raise_for_status()

        async def _reader():
            nonlocal done_at
            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=180)
                except asyncio.TimeoutError:
                    errors.append("WS 读超时")
                    return
                now = time.monotonic()
                try:
                    msg = json.loads(raw)
                except Exception:  # noqa: BLE001
                    continue
                kind = msg.get("type")
                if kind == "round":
                    rounds.append(now)
                elif kind == "done":
                    done_at = now
                    return
                elif kind == "error":
                    errors.append(str(msg.get("detail"))[:200])

        await _reader()
        wall = (done_at or time.monotonic()) - t0

    if mt_thread is not None:
        stop.set()
        mt_thread.join(timeout=5)

    intervals = [b - a for a, b in zip(rounds, rounds[1:])]
    return {
        "phase": name,
        "with_mt": with_mt,
        "rounds": len(rounds),
        "wall_sec": round(wall, 3),
        "interval_mean": round(statistics.mean(intervals), 4) if intervals else None,
        "interval_median": round(statistics.median(intervals), 4) if intervals else None,
        "interval_p90": (round(sorted(intervals)[int(len(intervals) * 0.9)], 4)
                         if intervals else None),
        "interval_max": round(max(intervals), 4) if intervals else None,
        "mt_sent": mt_stats.get("sent", 0),
        "mt_error": mt_stats.get("last_error", ""),
        "errors": errors,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", default=DEFAULT_AUDIO)
    ap.add_argument("--out", default="/userdata/assets/uploads/.m6_result.json")
    args = ap.parse_args()

    s = _session()
    enable_translation(s)

    results = []
    print("=== 阶段 A：只有 ASR（基线） ===")
    a = asyncio.run(run_phase("baseline", args.audio, False, s))
    print(json.dumps(a, ensure_ascii=False, indent=2))
    results.append(a)

    print("=== 阶段 B：ASR + MT 同卡争用 ===")
    b = asyncio.run(run_phase("contended", args.audio, True, s))
    print(json.dumps(b, ensure_ascii=False, indent=2))
    results.append(b)

    infl = None
    if a["interval_median"] and b["interval_median"]:
        infl = b["interval_median"] / a["interval_median"]
    verdict = ("通过（同卡可撑）" if infl and infl <= 1.3 else
               "⚠ 不通过（同卡争用过大 ⇒ MT 需限速）")
    print("=== 结论 ===")
    print(f"轮间隔中位数: 基线 {a['interval_median']} s → 争用 {b['interval_median']} s"
          f"（膨胀 {infl:.2f}×）" if infl else "无法计算膨胀比")
    print(f"识别总耗时: 基线 {a['wall_sec']} s → 争用 {b['wall_sec']} s")
    print(f"MT 请求数: {b['mt_sent']}")
    print(f"判据: 膨胀 > 1.30× ⇒ 必须限速。结论 = {verdict}")

    Path(args.out).write_text(json.dumps(
        {"results": results, "inflation": infl, "verdict": verdict},
        ensure_ascii=False, indent=2))
    print("结果写入", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
