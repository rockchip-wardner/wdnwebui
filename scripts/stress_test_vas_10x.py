#!/usr/bin/env python3
"""Stress test: 10 fresh analyses of /userdata/video_test/rk-0.mp4.

User request: 提示词"厂区画面,重点关注人",每间隔30帧取帧测试,循环测试10次,
看是否还会出现以上问题(echo "scene 户外街道 actors action objects text")。

Approach: 每个 iteration 用稍微不同的 prompt(add iteration 编号) 强制 cache miss,
让模型真正重跑而不是命中磁盘缓存(LanceLan's)。Cache key = (md5, every_n,
prompt_hash),加后缀改变 hash → 必走 model → 真机测试 schema-echo 是否复现。

Run: .venv/bin/python scripts/stress_test_vas_10x.py
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from urllib import request as urlrequest
from urllib.error import HTTPError

BASE = "http://127.0.0.1:8000/api/video-analysis-search/analysis"
SCHEMA_FIELDS = ("scene", "actors", "action", "objects", "text", "note")

VIDEO = "/userdata/video_test/rk-0.mp4"
PROMPT_BASE = "厂区画面,重点关注人"
EVERY_N = 30
N_ITERATIONS = 10
TIMEOUT_SEC = 180  # max wait for completion per iteration


def http(method: str, url: str, body: dict | None = None, timeout: int = 30) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urlrequest.Request(
        url, method=method, data=data,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        return json.loads(e.read().decode("utf-8"))


def count_schema_echo(events: list[dict]) -> tuple[int, list[dict]]:
    """Mirror service.py's _looks_like_schema_echo logic + structural exclusion."""
    bad = []
    for ev in events:
        text = (ev.get("text") or "").lower()
        if not text:
            continue
        if any(c in text for c in (":", "：", "{", "[")):
            continue
        found = sum(
            1 for f in SCHEMA_FIELDS
            if re.search(rf"\b{f}\b", text)
        )
        if found >= 3:
            bad.append({
                "id": ev.get("id"),
                "frame_pos": ev.get("frame_pos"),
                "text": ev.get("text"),
                "structured_scene": (ev.get("structured") or {}).get("scene"),
            })
    return len(bad), bad


def wait_done(state_url: str, timeout_sec: int = TIMEOUT_SEC) -> str:
    """Poll /api/video-analysis-search/analysis/ until terminal state."""
    t0 = time.time()
    last_state = "?"
    while time.time() - t0 < timeout_sec:
        snap = http("GET", state_url)
        last_state = snap.get("state", "?")
        if last_state in ("done", "error", "stopped"):
            return last_state
        time.sleep(2)
    return last_state  # timeout — return whatever last state we saw


def main():
    print(f"=== Stress test: {VIDEO} ===")
    print(f"Base prompt: {PROMPT_BASE!r}")
    print(f"every_n: {EVERY_N}, iterations: {N_ITERATIONS}")
    print(f"Cache-bust: 每 iteration prompt 加 ' [iter-N]' 后缀强制 miss")
    print()

    total_echo = 0
    total_events = 0
    per_iter = []
    summary_text = ""

    for it in range(1, N_ITERATIONS + 1):
        # Force cache miss via prompt suffix that doesn't change semantics
        # (model ignores trailing whitespace + digit tag in practice)
        prompt_it = f"{PROMPT_BASE} [iter-{it}]"

        # Close + clear before each iteration so we're not playing back state
        http("POST", f"{BASE}/close", body={})
        http("POST", f"{BASE}/events/clear", body={})

        t0 = time.time()
        open_resp = http(
            "POST", f"{BASE}/open",
            body={"source": VIDEO, "prompt": prompt_it, "every_n": EVERY_N},
        )
        run_id = open_resp.get("run_id")
        if not run_id:
            print(f"  iter {it}: open failed → {open_resp}")
            continue

        state = wait_done(f"{BASE}/")
        if state != "done":
            print(f"  iter {it}: state={state} (timeout?); skipping")
            continue

        # Read run events
        run = http("GET", f"{BASE}/runs/{run_id}")
        events = run.get("events", [])
        echo_n, echo_bad = count_schema_echo(events)
        total_echo += echo_n
        total_events += len(events)

        elapsed = time.time() - t0
        status = "PASS" if echo_n == 0 else "FAIL"
        print(
            f"  iter {it}: {status}  events={len(events):2}  "
            f"echo={echo_n}  t={elapsed:.1f}s  run={run_id[:8]}"
        )
        if echo_bad:
            for b in echo_bad[:2]:  # show at most 2 per iter
                print(
                    f"         id={b['id']} pos={b['frame_pos']} "
                    f"scene={b['structured_scene']!r} "
                    f"text={b['text'][:50]!r}"
                )
        per_iter.append({
            "iter": it, "run_id": run_id, "events": len(events),
            "echo": echo_n, "elapsed_sec": round(elapsed, 1),
        })
        # capture last summary
        summary_text = (
            run.get("summary", {}).get("summary", "")
            if isinstance(run.get("summary"), dict)
            else run.get("summary", "")
        )

    print()
    print("=== Aggregate ===")
    print(f"  total events: {total_events}")
    print(f"  total schema-echo: {total_echo}")
    print(f"  echo rate: {total_echo}/{total_events} = "
          f"{100 * total_echo / total_events:.1f}%" if total_events else "n/a")
    print()
    print(f"  Last summary: {summary_text[:200]}")
    print()

    if total_echo == 0:
        print("RESULT: ALL 10 ITERATIONS PASSED — schema-echo bug FIXED")
        sys.exit(0)
    else:
        print(f"RESULT: FAIL — {total_echo} echo events in {N_ITERATIONS} runs")
        sys.exit(1)


if __name__ == "__main__":
    main()