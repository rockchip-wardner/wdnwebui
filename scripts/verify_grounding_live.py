"""真机端到端复验：vtl（视频任意目标定位）的事件里**不能有 schema 污染**。

驱动真实的 vtl 卡片（``/api/video-target-localization``）跑一轮检测，
把事件拉下来逐条检查：

  1. 标签里不能出现 JSON Schema 的字段名 / 描述
     （``description`` / ``ROI 左上角 x，0–1000 归一化。`` / ``BoundingBox`` …）
  2. **每帧的目标数必须稳定** —— 这是更严的判据：schema 污染的典型症状
     不是「标签脏」，而是**目标数暴涨**（实测同一批帧带 schema 时
     每帧 [1, 0, 144, 5, 2, 1]，干净时 [1, 0, 1, 1, 1, 1]）。
     只查标签会漏判：模型没跑偏的那些帧看起来是干净的。

用法::

    python -m scripts.verify_grounding_live            # 默认素材 + 默认查询
    python -m scripts.verify_grounding_live --frames 20
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000/api/video-target-localization"

DEFAULT_VIDEO = "/userdata/assets/uploads/rk-0.mp4"
DEFAULT_QUERY = "穿黄色衣服的人"

# JSON Schema 里出现过的字符串（wdnai/grounding.py 的 docstring +
# Field description，以及 schema 的 key 名）
SCHEMA_MARKERS = (
    "description", "ROI", "归一化", "类别 +", "BoundingBox", "LocatorDoc",
    "x1", "y1", "x2", "y2", "boxes", "label", "text", "level",
    "区域", "目标类别名", "事件等级",
)


def _post(path: str, payload=None):
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(
        BASE + path, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return json.loads(e.read() or b"{}")


def _get(path: str):
    with urllib.request.urlopen(BASE + path, timeout=60) as r:
        return json.loads(r.read() or b"{}")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", default=DEFAULT_VIDEO)
    ap.add_argument("--query", default=DEFAULT_QUERY)
    ap.add_argument("--frames", type=int, default=12)
    ap.add_argument("--max-targets", type=int, default=4,
                    help="单帧目标数上限（超过即视为 schema 污染）")
    args = ap.parse_args()

    # Reset first: ``/open`` on a live session re-opens the source, so
    # running this script repeatedly without a ``/close`` in between piles
    # up decoders / detectors and eventually starves the HTTP layer.
    _post("/close")
    _post("/open", {"sources": [args.video], "loop": True})
    await asyncio.sleep(1.0)

    # The detector's event history is **cumulative** — it is not cleared
    # between runs, so a previous session's events would be counted as
    # ours (and with a different query they look like bogus extra
    # targets).  Remember the highest id we start with and only judge the
    # events that are new.
    baseline = max([int(e.get("id") or 0)
                    for e in ((_get("/events") or {}).get("events") or [])],
                   default=0)
    print(f"基线事件 id={baseline}（本轮只判定新增事件）")

    snap: dict = {}
    for _ in range(60):
        snap = _post("/prompt", {"prompts": [args.query], "rr_interval": 0})
        if snap.get("session_ready"):
            break
        print("   ", (snap.get("detail") or snap.get("error")
                      or "等待模型加载…")[:90])
        await asyncio.sleep(3)
    print("session_ready:", snap.get("session_ready"))
    if not snap.get("session_ready"):
        print("❌ 模型没就绪，无法复验")
        return 2

    deadline = time.time() + 300
    events: list = []
    while time.time() < deadline and len(events) < args.frames:
        got = (_get("/events") or {}).get("events") or []
        new = [e for e in got if int(e.get("id") or 0) > baseline]
        if len(new) > len(events):
            events = new
            time.sleep(0.5)
        else:
            await asyncio.sleep(2)

    print(f"\n共 {len(events)} 条事件\n")
    dirty = 0
    over = 0
    counts = []
    for e in events:
        boxes = e.get("boxes") or []
        labels = [b.get("label") for b in boxes]
        counts.append(len(boxes))
        polluted = [lb for lb in labels
                    if lb and any(m in str(lb) for m in SCHEMA_MARKERS)]
        flag = "✅"
        if polluted:
            dirty += 1
            flag = "✗"
        if len(boxes) > args.max_targets:
            over += 1
            flag = "✗"
        print(f"  {flag} id={e.get('id')} targets={len(boxes)} "
              f"labels={labels[:6]}")
        if polluted:
            print(f"      污染标签: {polluted[:6]}")

    print(f"\n每帧目标数 = {counts}")
    print(f"标签污染事件 = {dirty}/{len(events)}")
    print(f"目标数超限事件 = {over}/{len(events)} (上限 {args.max_targets})")

    if not events:
        print("❌ 没采到事件")
        return 2
    if dirty or over:
        print("❌ 仍有 schema 污染")
        return 1
    print("✅ 全部事件干净：标签无 schema 片段，目标数稳定")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
