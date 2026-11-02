"""端到端探针：连 video-depth 的 WS，统计视频二进制帧与深度事件。

用法（服务已跑起来、深度实例已 running 时）::

    .venv/bin/python scripts/probe_video_depth_ws.py --seconds 12

只读探针：不改任何状态。
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import time
from collections import Counter

import websockets


async def main(url: str, seconds: float) -> int:
    frames = Counter()          # 通道号 → 二进制帧数
    frame_bytes = Counter()
    events = Counter()          # 通道号 → 深度事件数
    statuses = 0
    event_with_img = 0
    last_status = None
    history = 0
    t0 = time.monotonic()
    async with websockets.connect(url, max_size=8 * 1024 * 1024) as ws:
        while time.monotonic() - t0 < seconds:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=3.0)
            except asyncio.TimeoutError:
                break
            if isinstance(raw, (bytes, bytearray)):
                if len(raw) < 2:
                    continue
                idx = raw[0]
                frames[idx] += 1
                frame_bytes[idx] += len(raw) - 1
                continue
            try:
                snap = json.loads(raw)
            except Exception:  # noqa: BLE001
                continue
            t = snap.get("type")
            if t == "status":
                statuses += 1
                last_status = snap
            elif t == "event":
                ch = snap.get("channel")
                events[ch] += 1
                if snap.get("depth_b64"):
                    event_with_img += 1
            elif t == "event_history":
                history = len(snap.get("events") or [])
    print(f"WS {url}")
    print(f"  二进制视频帧: {sum(frames.values())} 张  按路 {dict(sorted(frames.items()))}")
    print(f"  平均帧大小  : "
          f"{sum(frame_bytes.values()) / max(1, sum(frames.values())) / 1024:.1f} KB")
    print(f"  深度事件    : {sum(events.values())} 条  按路 {dict(sorted(events.items()))}")
    print(f"  带热力图的  : {event_with_img} 条")
    print(f"  status 帧   : {statuses} 帧")
    print(f"  历史包      : {history} 条")
    if last_status:
        s = last_status.get("session") or {}
        d = last_status.get("detector") or {}
        print(f"  最新快照    : session={s.get('state')} "
              f"channels={len(s.get('channel_list') or [])} "
              f"detector.enabled={d.get('enabled')} infers={d.get('infers')} "
              f"last_infer_ms={d.get('last_infer_ms')}")
    ok = sum(frames.values()) > 0 and sum(events.values()) > 0
    print("\n✅ WS 帧流 + 深度事件都在" if ok else "\n❌ WS 上没看到东西")
    return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="ws://127.0.0.1:8000/api/video-depth/ws")
    ap.add_argument("--seconds", type=float, default=12.0)
    a = ap.parse_args()
    raise SystemExit(asyncio.run(main(a.url, a.seconds)))
