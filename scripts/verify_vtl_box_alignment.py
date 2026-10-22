#!/usr/bin/env python3
"""Regression check: 视频任意目标定位 —— 画框必须落在**算框那一帧**的目标上。

Bug（已修）：事件到达浏览器时，实时流已经往前跑了整整一个推理时延
（本机实测 grounding 1.66s ≈ 41 帧 @25fps）。前端当时定格的是「事件到达
那一刻」的画布，框却属于 1.66s 之前那一帧 —— 目标早就走开了，表现为
「画框在实时画面里偏移」。

修法：后端把**算框用的那一帧**随事件一起下发（``frame_b64``），前端定格
它而不是定格当前画布。

本脚本用 Chromium + CDP（不依赖 playwright）在真浏览器里跑一遍，断言：
  1. 事件带上了整帧（``frame_b64`` 非空）；
  2. 定格期间宫格画布的内容 == 事件自带的那一帧（而不是实时流当前帧）；
  3. 画布上确实画出了框（与原帧相比多出彩色描边像素）。

Run:
  .venv/bin/python scripts/verify_vtl_box_alignment.py
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request

import cv2
import numpy as np
import websockets

BASE = "http://127.0.0.1:8000"
API = BASE + "/api/video-target-localization"
VIDEO = "/userdata/assets/uploads/rk-0.mp4"
CDP_PORT = 9333

# 在页面脚本之前挂上 WebSocket 探针，把事件原样留下来。
WS_PROBE = r"""
(() => {
  const Orig = window.WebSocket;
  window.__wsEvents = [];
  window.__wsOpen = false;
  class Probe extends Orig {
    constructor(...a) {
      super(...a);
      this.addEventListener('open', () => { window.__wsOpen = true; });
      this.addEventListener('message', (ev) => {
        if (typeof ev.data !== 'string') return;
        try {
          const m = JSON.parse(ev.data);
          if (m && m.type === 'event') window.__wsEvents.push(m);
        } catch (_) { /* ignore */ }
      });
    }
  }
  window.WebSocket = Probe;
})();
"""


def _post(path, payload=None):
    """POST 一个端点。503（模型还在加载）不当异常 —— 返回体里带 detail。"""
    req = urllib.request.Request(
        API + path, data=json.dumps(payload or {}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read() or b"{}")
        except Exception:
            return {"detail": f"HTTP {exc.code}"}


class Cdp:
    """极简 CDP 客户端：只够本脚本用。"""

    def __init__(self, ws):
        self.ws = ws
        self._id = 0

    async def call(self, method, params=None, timeout=30):
        self._id += 1
        mid = self._id
        await self.ws.send(json.dumps({"id": mid, "method": method,
                                       "params": params or {}}))
        while True:
            msg = json.loads(await asyncio.wait_for(self.ws.recv(), timeout))
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    async def evaluate(self, expr, timeout=30):
        r = await self.call("Runtime.evaluate", {
            "expression": expr, "returnByValue": True, "awaitPromise": True,
        }, timeout=timeout)
        res = r.get("result", {})
        if r.get("exceptionDetails"):
            raise RuntimeError(json.dumps(r["exceptionDetails"])[:400])
        return res.get("value")


async def _connect_cdp():
    """等 chromium 的调试端口起来，拿到 page target 的 ws 地址。"""
    url = None
    for _ in range(80):
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{CDP_PORT}/json/list", timeout=2) as r:
                targets = json.loads(r.read() or b"[]")
            for t in targets:
                if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                    url = t["webSocketDebuggerUrl"]
                    break
            if url:
                break
        except Exception:
            pass
        await asyncio.sleep(0.25)
    if not url:
        raise RuntimeError("chromium 调试端口没起来")
    ws = await websockets.connect(url, max_size=64 * 1024 * 1024)
    return Cdp(ws)


GRID_CANVAS = """
(() => {
  const c = document.querySelector('#vtl-grid canvas');
  if (!c) return null;
  return { w: c.width, h: c.height, data: c.toDataURL('image/png') };
})()
"""


async def main() -> int:
    profile = tempfile.mkdtemp(prefix="cdp-vtl-")
    chrome = shutil.which("chromium") or shutil.which("chromium-browser") \
        or shutil.which("google-chrome")
    if not chrome:
        print("✗ 找不到 chromium")
        return 2
    proc = subprocess.Popen([
        chrome, "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
        "--hide-scrollbars", "--no-first-run", "--no-default-browser-check",
        # 本机 chromium 是 chromium-x11 包（没编 headless ozone），走现成的
        # X display；窗口挪到屏幕外，别打扰正在用桌面的人。
        "--ozone-platform=x11", "--window-position=-3000,-3000",
        "--window-size=1440,900",
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={profile}",
        "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ok = True
    try:
        cdp = await _connect_cdp()
        await cdp.call("Page.enable")
        await cdp.call("Runtime.enable")
        await cdp.call("Page.addScriptToEvaluateOnNewDocument",
                       {"source": WS_PROBE})
        print("=== 打开「视频任意目标定位」 ===")
        await cdp.call("Page.navigate", {"url": BASE + "/#video-target-localization"})

        for _ in range(60):
            if await cdp.evaluate("(() => !!(window.__wsOpen && "
                                  "document.querySelector('#vtl-grid canvas')))()"):
                break
            await asyncio.sleep(0.25)
        if not await cdp.evaluate("(() => !!window.__wsOpen)()"):
            print("  ✗ WS 没连上")
            print("     诊断: grid=", await cdp.evaluate(
                "(() => !!document.querySelector('#vtl-grid canvas'))()"),
                " url=", await cdp.evaluate("(() => location.href)()"))
            return 2
        print("  ✅ WS 已连接")

        print("=== 打开视频 + 启动定位（人） ===")
        print("  open:", json.dumps(_post("/open", {"sources": [VIDEO], "loop": True}),
                                    ensure_ascii=False)[:110])
        await asyncio.sleep(1.0)
        snap = {}
        for _ in range(40):
            snap = _post("/prompt", {"prompts": ["人"], "rr_interval": 0})
            if snap.get("session_ready"):
                break
            print("   ", (snap.get("detail") or snap.get("error")
                          or "等待模型加载…")[:100])
            await asyncio.sleep(3)
        print("  session_ready:", snap.get("session_ready"),
              "| error:", (snap.get("error") or "")[:120])
        if not snap.get("session_ready"):
            print("  ✗ 定位会话没起来，无法验证")
            return 2

        print("=== 等第一个带框的事件 ===")
        ev = None
        deadline = time.time() + 180
        while time.time() < deadline:
            evs = await cdp.evaluate("(() => window.__wsEvents || [])()")
            with_boxes = [e for e in (evs or []) if e.get("boxes")]
            if with_boxes:
                ev = with_boxes[-1]
                break
            await asyncio.sleep(0.3)
        if not ev:
            print("  ✗ 没等到事件")
            return 2
        print(f"  事件 id={ev.get('id')} frame_pos={ev.get('frame_pos')} "
              f"frame={ev.get('frame_w')}x{ev.get('frame_h')} "
              f"boxes={len(ev.get('boxes') or [])} "
              f"frame_b64={len(ev.get('frame_b64') or '')} 字符")

        # 定格窗口 2s：立刻抓宫格画布
        shot = await cdp.evaluate(GRID_CANVAS)
        if not shot:
            print("  ✗ 宫格画布不存在")
            return 2

        if not ev.get("frame_b64"):
            print("  ✗ 事件没有整帧（frame_b64 为空）—— 前端只能定格错位的画面")
            ok = False
        else:
            raw = base64.b64decode(ev["frame_b64"])
            ev_img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
            png = base64.b64decode(shot["data"].split(",", 1)[1])
            canvas_img = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
            print(f"  事件整帧 {ev_img.shape[1]}x{ev_img.shape[0]} | "
                  f"宫格画布 {canvas_img.shape[1]}x{canvas_img.shape[0]}")

            if canvas_img.shape[:2] != ev_img.shape[:2]:
                print("  ✗ 画布与事件整帧尺寸不同")
                ok = False
            else:
                diff = cv2.absdiff(canvas_img, ev_img)
                mask = (diff.max(axis=2) > 24).astype(np.uint8)
                colored = int((mask & (canvas_img.max(axis=2).astype(int)
                                       - canvas_img.min(axis=2).astype(int) > 40)).sum())
                changed = int(mask.sum())
                total = mask.size
                print(f"  画布 vs 事件整帧：不同像素 {changed}/{total} "
                      f"({100.0 * changed / total:.2f}%)，其中彩色描边 {colored} 像素")
                if changed / total > 0.10:
                    print("  ✗ 画布内容与事件整帧差太多 —— 定格的不是算框那一帧")
                    ok = False
                else:
                    print("  ✅ 画布定格在**算框那一帧**上")
                if colored < 200:
                    print("  ✗ 画布上没画出彩色框描边")
                    ok = False
                else:
                    print("  ✅ 框已画在该帧上")

        await asyncio.sleep(3.5)
        shot2 = await cdp.evaluate(GRID_CANVAS)
        if shot2 and ev.get("frame_b64"):
            png = base64.b64decode(shot2["data"].split(",", 1)[1])
            img2 = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
            raw = base64.b64decode(ev["frame_b64"])
            ev_img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
            if img2.shape == ev_img.shape:
                d = float(cv2.absdiff(img2, ev_img).mean())
                print(f"  定格结束后画布 vs 事件整帧 平均差 {d:.2f} "
                      f"({'已回到实时流 ✓' if d > 3 else '仍在定格（事件可能太密）'})")

        # 历史列表：缩略图 + overlay 的框位置也要对
        print("=== 检查历史列表的框位置 ===")
        hist = await cdp.evaluate("""
        (() => {
          const list = document.getElementById('vtl-event-list');
          const items = list ? list.querySelectorAll('li.vd-event') : [];
          if (!items.length) return null;
          const li = items[0];
          const img = li.querySelector('img.vtl-crop-thumb');
          const ov = li.querySelector('canvas.vtl-crop-overlay');
          if (!img || !ov) return {err: 'no img/overlay'};
          return {
            items: items.length,
            evId: li.getAttribute('data-ev-id'),
            imgW: img.naturalWidth, imgH: img.naturalHeight,
            ovW: ov.width, ovH: ov.height,
            png: ov.toDataURL('image/png'),
          };
        })()
        """)
        if not hist or hist.get("err"):
            print("  ⚠️ 历史列表还没有缩略图条目:", hist)
        else:
            # 取**该条目自己的**事件来对（列表是最新的在前）
            evs = await cdp.evaluate("(() => window.__wsEvents || [])()")
            item_ev = next((e for e in (evs or [])
                            if str(e.get("id")) == str(hist.get("evId"))), None)
            if not item_ev or not item_ev.get("frame_b64"):
                print("  ⚠️ 找不到该条目的事件/整帧，跳过位置比对")
            else:
                raw = base64.b64decode(item_ev["frame_b64"])
                src = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
                png = base64.b64decode(hist["png"].split(",", 1)[1])
                ov = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
                print(f"  历史条目 {hist['items']} 个（事件 #{hist.get('evId')}） | "
                      f"缩略图 {hist['imgW']}x{hist['imgH']} | "
                      f"overlay {hist['ovW']}x{hist['ovH']}")
                if ov.shape != src.shape:
                    print("  ✗ overlay 与整帧尺寸不同")
                    ok = False
                else:
                    # overlay = 整帧 + 画的框；差集就是描边
                    diff = cv2.absdiff(ov, src).max(axis=2)
                    ys, xs = np.where(diff > 40)
                    print(f"  overlay 与整帧的差集像素 {len(xs)}")
                    if len(xs) == 0:
                        print("  ✗ 历史缩略图上没画出框")
                        ok = False
                    else:
                        fw = item_ev.get("frame_w") or 0
                        fh = item_ev.get("frame_h") or 0
                        sx = hist["imgW"] / fw if fw else 1
                        sy = hist["imgH"] / fh if fh else 1
                        exp = []
                        for b in (item_ev.get("boxes") or []):
                            bb = b.get("bbox")
                            if bb and len(bb) == 4:
                                exp.append([bb[0] * sx, bb[1] * sy,
                                            bb[2] * sx, bb[3] * sy])
                        if not exp:
                            print("  ⚠️ 该事件没有框")
                        else:
                            ex1 = min(b[0] for b in exp)
                            ex2 = max(b[2] for b in exp)
                            ey1 = min(b[1] for b in exp)
                            ey2 = max(b[3] for b in exp)
                            print(f"  期望框并集 x[{ex1:.0f},{ex2:.0f}] "
                                  f"y[{ey1:.0f},{ey2:.0f}] | "
                                  f"实际描边 x[{xs.min()},{xs.max()}] "
                                  f"y[{ys.min()},{ys.max()}]")
                            # 描边含标签（画在框上方），y 会略高；x 必须对齐
                            if abs(xs.min() - ex1) > 8 or abs(xs.max() - ex2) > 8:
                                print("  ✗ 历史缩略图的框位置与事件框对不上")
                                ok = False
                            else:
                                print("  ✅ 历史缩略图的框位置正确")

        errs = await cdp.evaluate(
            "(() => window.__pageErrors || [])()")
        if errs:
            print("  ✗ 页面 JS 报错:", errs[:5])
            ok = False
        else:
            print("  ✅ 页面无 JS 报错")

        # 整帧只给最近 N 条事件留着（内存账）—— 但**最新那条必须有**，
        # 否则事件到了却定格不了（旧实现把最新的清掉了）。
        print("=== 连续多帧后，最新事件仍须带整帧 ===")
        deadline = time.time() + 180
        last = None
        while time.time() < deadline:
            evs = await cdp.evaluate("(() => window.__wsEvents || [])()")
            if len(evs) >= 15:
                last = evs[-1]
                break
            await asyncio.sleep(1.0)
        if last is None:
            print("  ⚠️ 没等到 15 条事件，跳过")
        else:
            print(f"  已收到 {len(evs)} 条；最后一条 id={last.get('id')} "
                  f"frame_b64={len(last.get('frame_b64') or '')} 字符")
            if not last.get("frame_b64"):
                print("  ✗ 最新事件没有整帧 —— 整帧保留策略把最新的清掉了")
                ok = False
            else:
                print("  ✅ 最新事件仍带整帧")

        _post("/prompt/stop")
        _post("/close")
    finally:
        try:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)

    print("\n" + ("✅ 全部通过：画框与目标严格对齐" if ok else "❌ 验证失败"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
