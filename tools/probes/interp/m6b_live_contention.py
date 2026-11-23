#!/usr/bin/env python3
"""M6b — **实时路径**下卡 0 的 ASR 与 MT 争用（决定「实时源能不能同卡」）。

为什么还要单独测（``m6_same_card_contention.py`` 测的是文件路径）：

* 文件路径是**超实时**跑的（15 s 音频 3 s 跑完）⇒ 轮间隔 ≈ 模型单轮延迟；
* 实时路径的音频是**按 1 s 一块、按真实时间**送进来的 ⇒
  模型的单轮延迟只要 **< 1 s**，识别就永远跟得上；
  一旦 MT 把单轮延迟顶过 1 s ⇒ 识别**开始积压** ⇒ 实时源不可用。

判据用服务端自己给的 ``lag_ms``（块在服务端队列里等了多久才被推理）：

* ``lag_ms`` **有界**（不随时间线性增长）⇒ 识别跟得上 ⇒ 同卡可撑；
* ``lag_ms`` 线性增长 ⇒ 识别在积压 ⇒ 同卡不可撑（要限速/换部署）。

用法::

    python m6b_live_contention.py [--seconds 30] [--out FILE]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import ssl
import statistics
import subprocess
import threading
import time
import wave
from pathlib import Path

import requests
import websockets

BASE = "https://127.0.0.1:8000"
WS_URL = "wss://127.0.0.1:8000/api/qwen3-asr/ws"
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

SRC_WAV = "/userdata/assets/uploads/asr_en.wav"
RAW16 = Path("/userdata/assets/uploads/.m6b_live16.raw")

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


def make_raw() -> bytes:
    """16 kHz / 单声道 / s16le 裸 PCM（实时路径要的格式）。"""
    if not RAW16.exists():
        subprocess.run(["sox", SRC_WAV, "-r", "16000", "-c", "1",
                        "-b", "16", "-e", "signed-integer", str(RAW16)],
                       check=True)
    return RAW16.read_bytes()


def mt_hammer(s: requests.Session, stop: threading.Event, stats: dict) -> None:
    n = 0
    while not stop.is_set():
        try:
            r = s.post(BASE + "/api/qwen3-asr/translate/line",
                       json={"text": MT_TEXT, "lang": "english", "seq": n},
                       timeout=60)
            if r.status_code == 200 and r.json().get("ok"):
                n += 1
                stats["enqueued"] = n
            else:
                stats["last_error"] = r.text[:160]
                time.sleep(0.2)
        except Exception as exc:  # noqa: BLE001
            stats["last_error"] = str(exc)[:160]
            time.sleep(0.3)


async def run_phase(name: str, pcm: bytes, seconds: int, with_mt: bool,
                    s: requests.Session) -> dict:
    stop = threading.Event()
    mt_stats: dict = {}
    if with_mt:
        t = threading.Thread(target=mt_hammer, args=(s, stop, mt_stats),
                             daemon=True)
        t.start()
        time.sleep(1.0)

    r = s.post(BASE + "/api/qwen3-asr/live/start",
               json={"source": "browser", "sample_rate": 16000,
                     "n_channels": 1, "sample_width": 2, "chunk_ms": 1000},
               timeout=60)
    r.raise_for_status()
    stream_id = r.json()["stream_id"]

    lag_ms: list[int] = []
    rounds: list[float] = []
    dropped = 0
    chunk_bytes = 32000                       # 1 s @16 kHz s16le
    n_chunks = seconds
    t0 = time.monotonic()

    async with websockets.connect(WS_URL, ssl=SSL_CTX, open_timeout=15,
                                  ping_interval=None) as ws:
        async def _reader():
            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    return
                try:
                    msg = json.loads(raw)
                except Exception:  # noqa: BLE001
                    continue
                if msg.get("type") == "round":
                    rounds.append(time.monotonic() - t0)

        reader = asyncio.create_task(_reader())
        for i in range(n_chunks):
            target = t0 + i * 1.0                # 真实时间节拍
            wait = target - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            off = (i * chunk_bytes) % max(1, len(pcm) - chunk_bytes)
            body = pcm[off:off + chunk_bytes]
            try:
                resp = s.post(BASE + "/api/qwen3-asr/live/chunk", data=body,
                              headers={"X-Asr-Stream-Id": stream_id,
                                       "X-Asr-Seq": str(i),
                                       "Content-Type": "audio/pcm"},
                              timeout=60)
                if resp.status_code == 200:
                    js = resp.json()
                    if js.get("lag_ms") is not None:
                        lag_ms.append(int(js["lag_ms"]))
                    dropped += int(js.get("dropped_chunks") or 0)
                else:
                    dropped += 1
            except Exception:  # noqa: BLE001
                dropped += 1
        elapsed = time.monotonic() - t0
        reader.cancel()
        try:
            s.post(BASE + "/api/qwen3-asr/live/finish",
                   headers={"X-Asr-Stream-Id": stream_id}, timeout=30)
        except Exception:  # noqa: BLE001
            pass
        try:
            s.delete(BASE + "/api/qwen3-asr/live/stop?stream_id=" + stream_id,
                     timeout=30)
        except Exception:  # noqa: BLE001
            pass

    if with_mt:
        stop.set()

    half = max(1, len(lag_ms) // 2)
    return {
        "phase": name,
        "with_mt": with_mt,
        "chunks": n_chunks,
        "elapsed_sec": round(elapsed, 2),
        "rounds_seen": len(rounds),
        "lag_ms_first_half_median": statistics.median(lag_ms[:half]) if lag_ms else None,
        "lag_ms_second_half_median": (statistics.median(lag_ms[half:])
                                      if lag_ms else None),
        "lag_ms_max": max(lag_ms) if lag_ms else None,
        "lag_ms_all": lag_ms,
        "dropped_chunks": dropped,
        "mt_enqueued": mt_stats.get("enqueued", 0),
        "mt_error": mt_stats.get("last_error", ""),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=25)
    ap.add_argument("--out", default="/userdata/assets/uploads/.m6b_result.json")
    args = ap.parse_args()

    s = _session()
    s.post(BASE + "/api/qwen3-asr/translate/settings",
           json={"enabled": True, "target_lang": "chinese"}, timeout=30)
    pcm = make_raw()
    print(f"素材 {len(pcm) / 32000:.1f} s（循环喂）")

    print("=== 阶段 A：只有 ASR（基线） ===")
    a = asyncio.run(run_phase("baseline", pcm, args.seconds, False, s))
    a.pop("lag_ms_all", None)
    print(json.dumps(a, ensure_ascii=False, indent=2))

    print("=== 阶段 B：ASR + MT 同卡争用 ===")
    b = asyncio.run(run_phase("contended", pcm, args.seconds, True, s))
    b.pop("lag_ms_all", None)
    print(json.dumps(b, ensure_ascii=False, indent=2))

    la, lb = a["lag_ms_second_half_median"], b["lag_ms_second_half_median"]
    grew = (lb is not None and la is not None and lb > max(500, la * 2))
    verdict = ("通过（识别跟得上，同卡可撑）" if not grew else
               "⚠ 不通过（lag 线性增长 ⇒ 识别在积压 ⇒ 需限速）")
    print("=== 结论 ===")
    print(f"lag 中位数 后半段: 基线 {la} ms → 争用 {lb} ms")
    print(f"lag 最大值: 基线 {a['lag_ms_max']} ms → 争用 {b['lag_ms_max']} ms")
    print(f"丢块: 基线 {a['dropped_chunks']} → 争用 {b['dropped_chunks']}")
    print(f"MT 入队请求数: {b['mt_enqueued']}")
    print(f"结论 = {verdict}")
    Path(args.out).write_text(json.dumps(
        {"baseline": a, "contended": b, "verdict": verdict},
        ensure_ascii=False, indent=2))
    print("结果写入", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
