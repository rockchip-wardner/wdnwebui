"""真机验证 VAS「帧判据」下沉：分析循环真的按画面变化节流，且分数能推到前端。

走的是真链路：真实视频 + 真实 NPU 推理（qwen3-vl-4b @ npu0）+ 真实 WS。

**一轮只跑一次分析**，靠 ``--mode`` 选。原因是 wdnav 的 VideoCapture 在
「跑完一轮 → 再开一轮」时会踩到 mpp 缓冲的原生断言
（``mpp_buf_slot: Assertion !slot->status.on_used failed``），第二个 run 的
decode 直接死掉、判据计数恒为 0。这是 SDK 侧的老问题、与判据无关，但测试
脚本不能假装没看见 —— 所以**两轮之间要重启服务**（本文件末尾给了命令）。

检查点：
  1. ``POST /judge`` 改判据落库（``gen`` 单调递增）；
  2. 分析跑起来后 WS 收到 ``{"type":"judge"}`` 推送（分数环形缓冲在长）；
  3. 真节流：``evaluated``（看过的帧）**明显大于** ``sampled``（送去推理的帧）；
  4. 手动模式对照：every_n=30 时 sampled ≈ evaluated/30，
     证明默认行为没有被判据改坏（且手动模式不产生分数曲线）。

跑法（两轮之间重启服务 + 重载模型）：
    .venv/bin/python scripts/verify_vas_judge_e2e.py --mode conc
    # 重启服务、重新加载 qwen3-vl-4b 之后：
    .venv/bin/python scripts/verify_vas_judge_e2e.py --mode manual
"""
import argparse
import asyncio
import json
import sys
import urllib.error
import urllib.request
import uuid

import websockets

BASE = "http://localhost:8000"
API = BASE + "/api/video-analysis-search"
WS = "ws://localhost:8000/api/video-analysis-search/analysis/ws"
VIDEO = "/home/rockchip/test/test/carplant_29132868.mp4"
PROMPT = "请用中文简洁描述画面内容，10 个字以内。"

RESULTS: list = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def _req(url: str, body=None, method=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"},
        method=method or ("POST" if data is not None else "GET"))
    with urllib.request.urlopen(r, timeout=20) as resp:
        return json.loads(resp.read().decode())


async def run_one(mode: str, every_n: int, threshold: float, max_s: float) -> dict:
    """跑一轮分析，收集 WS 上的 judge 推送，返回最终 judge 视图。

    **提示词每轮唯一**：分析缓存键含 prompt_hash，同视频同提示词会直接命中
    历史结果走「逐帧来自缓存」路径 —— decode 循环根本不跑，judge 的
    evaluated 恒为 0，看着像判帧坏了，其实是被缓存短路了。
    """
    prompt = f"{PROMPT}（本次标记 {uuid.uuid4().hex[:8]}）"
    _req(API + "/judge", {"mode": mode, "every_n": every_n, "threshold": threshold})
    ws = await websockets.connect(WS, max_size=8 * 1024 * 1024)
    try:
        _req(API + "/analysis/open", {
            "source": VIDEO, "prompt": prompt,
            "every_n": every_n, "sample_mode": mode,
            "change_threshold": threshold,
        })
        latest: dict = {}
        judge_msgs = 0
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max_s
        while loop.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            except asyncio.TimeoutError:
                raw = None
            except websockets.ConnectionClosed:
                break
            if raw is None:
                snap = _req(API + "/analysis")
                latest = snap.get("judge") or latest
                if snap.get("state") in ("done", "stopped", "error"):
                    break
                continue
            msg = json.loads(raw)
            if msg.get("type") == "judge":
                judge_msgs += 1
                latest = msg.get("judge") or latest
            elif msg.get("type") in ("state", "hello"):
                latest = msg.get("judge") or latest
                if msg.get("state") in ("done", "stopped", "error"):
                    break
        return {"judge": latest, "judge_msgs": judge_msgs}
    finally:
        try:
            _req(API + "/analysis/stop", {}, method="POST")
        except urllib.error.HTTPError:
            pass
        await ws.close()


async def run_one(mode: str, every_n: int, threshold: float, max_s: float) -> dict:
    """跑一轮分析，收集 WS 上的 judge 推送，返回最终 judge 视图。

    **提示词每轮唯一**：分析缓存键含 prompt_hash，同视频同提示词会直接命中
    历史结果走「逐帧来自缓存」路径 —— decode 循环根本不跑，判据的
    evaluated 恒为 0，看着像判帧坏了，其实是被缓存短路了。
    """
    prompt = f"{PROMPT}（本次标记 {uuid.uuid4().hex[:8]}）"
    _req(API + "/judge", {"mode": mode, "every_n": every_n, "threshold": threshold})
    ws = await websockets.connect(WS, max_size=8 * 1024 * 1024)
    try:
        _req(API + "/analysis/open", {
            "source": VIDEO, "prompt": prompt,
            "every_n": every_n, "sample_mode": mode,
            "change_threshold": threshold,
        })
        latest: dict = {}
        judge_msgs = 0
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max_s
        while loop.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            except asyncio.TimeoutError:
                raw = None
            except websockets.ConnectionClosed:
                break
            if raw is None:
                snap = _req(API + "/analysis")
                latest = snap.get("judge") or latest
                if snap.get("state") in ("done", "stopped", "error"):
                    break
                continue
            msg = json.loads(raw)
            if msg.get("type") == "judge":
                judge_msgs += 1
                latest = msg.get("judge") or latest
            elif msg.get("type") in ("state", "hello"):
                latest = msg.get("judge") or latest
                if msg.get("state") in ("done", "stopped", "error"):
                    break
        return {"judge": latest, "judge_msgs": judge_msgs}
    finally:
        try:
            _req(API + "/analysis/stop", {}, method="POST")
        except urllib.error.HTTPError:
            pass
        await ws.close()


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="conc", choices=["conc", "manual"])
    ap.add_argument("--max-s", type=float, default=90.0)
    args = ap.parse_args()
    mode = args.mode
    # 阈值跟着判据走 —— 不同度量的刻度不可比（z_mad 灰阶 6.0、conc 比值 15）。
    every_n, threshold = 30, (15.0 if mode == "conc" else 6.0)

    print(f"=== VAS 帧判据端到端（真视频 + 真 NPU，mode={mode}）===")

    before = _req(API + "/judge", {"mode": mode, "every_n": every_n,
                                 "threshold": threshold})
    print(f"  POST /judge → gen={before.get('gen')} mode={before.get('mode')} "
          f"every_n={before.get('every_n')} threshold={before.get('threshold')}")

    run = await run_one(mode, every_n, threshold, max_s=args.max_s)
    j = run["judge"]
    ev, sm = j.get("evaluated", 0), j.get("sampled", 0)
    print(f"  evaluated={ev} sampled={sm} judge推送={run['judge_msgs']} "
          f"recent={len(j.get('recent') or [])}")

    check("判据真在解码热路径上跑过（evaluated > 0）", ev > 0, f"evaluated={ev}")

    if mode == "conc":
        check("WS 收到判据推送", run["judge_msgs"] > 0, f"{run['judge_msgs']} 条")
        check("分数环形缓冲有数据", len(j.get("recent") or []) > 0,
              f"{len(j.get('recent') or [])} 点，末点={((j.get('recent') or [[0,0]])[-1])}")
        check("真节流：看过的帧 > 送去推理的帧", ev > sm > 0, f"{ev} > {sm}")
        ratio = (sm / ev) if ev else 0.0
        check("采样率落在合理区间（1%~90%）", 0 < ratio < 0.9,
              f"{sm}/{ev} = {ratio:.1%}")
    else:
        check("手动模式 ≈ 每 30 帧送一次（默认行为未被改坏）",
              ev > 0 and abs(sm - ev / 30) <= 2, f"{sm} vs {ev}/30={ev/30:.1f}")
        check("手动模式无分数曲线（不比对画面）", not (j.get("recent") or []),
              f"recent={len(j.get('recent') or [])}")

    _req(API + "/judge", {"mode": "manual", "every_n": 30})
    ok = sum(1 for r in RESULTS if r)
    print(f"\n结果: {ok}/{len(RESULTS)}")
    return 0 if ok == len(RESULTS) else 1


sys.exit(asyncio.run(main()))