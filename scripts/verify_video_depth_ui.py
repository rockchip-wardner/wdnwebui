"""video-depth 卡片的浏览器端验证：左右两栏是不是真的各画出了东西。

**脚本自己把链路拉起来**（开路 → 启动深度估计 → 看画面），不依赖外部
先手工开好 —— 因为关掉上一个浏览器标签时 DemoGuard 会 sendBeacon 停掉
后端会话（``beforeunload`` 钩子），外部先开好的状态到这一步早没了。

前置条件：深度实例已 running（见文件头）。

跑法::

    xvfb-run -a .venv/bin/python scripts/verify_video_depth_ui.py
    xvfb-run -a .venv/bin/python scripts/verify_video_depth_ui.py --shot /tmp/vdp.png

只看**画布真的有内容**（不是 DOM 存在就过）：右栏 canvas 必须有非黑像素
（深度热力图），左栏 canvas 必须有非黑像素（视频画面）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8000"
SOURCES = ["/userdata/assets/uploads/rk-0.mp4",
           "/userdata/assets/uploads/rk-12.mp4"]


def _post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def _get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def setup_chain() -> tuple[str, int]:
    """开路 + 启动深度估计。返回 (model_id, npu_index)。"""
    caps = _get("/api/compute/instances-by-capability?capability=swcap_vision_depth")
    insts = caps.get("instances") or []
    if not insts:
        raise SystemExit("深度能力没有可用实例 —— 先在算力配置页启用 "
                         "Depth-Anything-V3")
    inst = insts[0]
    model_id = inst["model_id"]
    npu = inst.get("npu_index")
    _post("/api/video-depth/open", {"sources": SOURCES, "loop": True})
    _post("/api/video-depth/prompt",
          {"rr_interval": 0,
           "models": [{"model_id": model_id, "npu_index": npu}]})
    return model_id, npu


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shot", default="/tmp/video_depth_ui.png")
    ap.add_argument("--wait", type=float, default=25.0)
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("❌ 没装 playwright，跳过")
        return 1

    model_id, npu = setup_chain()
    print(f"链路已就绪：{model_id} @ NPU{npu}")

    fails: list[str] = []
    errors: list[str] = []

    with sync_playwright() as pw:
        # playwright 自带的 chromium 可能没装；系统 chromium（Debian 132）
        # **不支持** ``--headless`` 的 ozone 平台，所以有 DISPLAY 时走
        # 有头模式 + xvfb：``xvfb-run -a python scripts/verify_video_depth_ui.py``。
        launch_kwargs: dict = {"args": ["--no-sandbox", "--disable-gpu"]}
        if os.environ.get("DISPLAY"):
            launch_kwargs["headless"] = False
        if Path("/usr/bin/chromium").exists():
            launch_kwargs["executable_path"] = "/usr/bin/chromium"
        br = pw.chromium.launch(**launch_kwargs)
        page = br.new_page(viewport={"width": 1600, "height": 1000})
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.on("console",
                lambda m: errors.append(f"console.error: {m.text} @ "
                                        f"{getattr(m, 'location', None)}")
                if m.type == "error" else None)

        page.goto(f"{BASE}/#video-depth", wait_until="domcontentloaded",
                  timeout=60000)
        page.wait_for_selector("#page-video-depth", state="attached",
                               timeout=30000)

        # 首次进卡会弹**向导**（向导接管入口页，功能页被藏起来）。
        # 要看功能页得先把向导走完 —— 这里直接点「跳过向导」。
        try:
            page.wait_for_selector(".wd-wizard-skip-btn", timeout=8000)
            page.click(".wd-wizard-skip-btn")
            page.wait_for_timeout(1500)
        except Exception:  # noqa: BLE001 — 没有向导（已完成 / 已跳过）也算正常
            pass

        page.wait_for_function(
            "() => { const p = document.getElementById('page-video-depth');"
            " return p && !p.classList.contains('hidden'); }",
            timeout=30000)

        # 等深度事件真正画到右栏（最多 args.wait 秒）
        deadline = args.wait
        painted = False
        while deadline > 0:
            painted = page.evaluate(
                """() => {
                    const g = document.getElementById('vdp-depth-grid');
                    if (!g) return false;
                    const tiles = g.querySelectorAll('.vd-tile canvas');
                    let any = false;
                    tiles.forEach((c) => {
                        if (!c.width || !c.height) return;
                        const ctx = c.getContext('2d');
                        const d = ctx.getImageData(0, 0, c.width, c.height).data;
                        for (let i = 0; i < d.length; i += 4 * 97) {
                            if (d[i] || d[i + 1] || d[i + 2]) { any = true; break; }
                        }
                    });
                    return any;
                }""")
            if painted:
                break
            page.wait_for_timeout(1000)
            deadline -= 1

        info = page.evaluate(
            """() => {
                const q = (s) => document.querySelectorAll(s);
                const nonBlack = (c) => {
                    if (!c || !c.width || !c.height) return 0;
                    const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
                    let n = 0;
                    for (let i = 0; i < d.length; i += 4 * 31) {
                        if (d[i] || d[i+1] || d[i+2]) n++;
                    }
                    return n;
                };
                return {
                    videoTiles: q('#vdp-grid .vd-tile').length,
                    depthTiles: q('#vdp-depth-grid .vd-tile').length,
                    videoPainted: Array.from(q('#vdp-grid .vd-tile canvas')).map(nonBlack),
                    depthPainted: Array.from(q('#vdp-depth-grid .vd-tile canvas')).map(nonBlack),
                    // canvas 的**像素尺寸**。左右两栏必须覆盖同一个视场
                    // → 宽高比必须一致（视频是显示尺寸，深度图是按送检帧
                    // 宽高比回拉出来的，两者比例都等于源的比例）。
                    videoCanvas: Array.from(q('#vdp-grid .vd-tile canvas')).map(c => [c.width, c.height]),
                    depthCanvas: Array.from(q('#vdp-depth-grid .vd-tile canvas')).map(c => [c.width, c.height]),
                    depthLabels: Array.from(q('#vdp-depth-grid .vd-tile-label')).map(e => e.textContent),
                    videoLabels: Array.from(q('#vdp-grid .vd-tile-label')).map(e => e.textContent),
                    splitVisible: (() => {
                        const s = document.querySelector('.vdp-split');
                        if (!s) return false;
                        const r = s.getBoundingClientRect();
                        return r.width > 200 && r.height > 100;
                    })(),
                    cols: (() => {
                        const s = document.querySelector('.vdp-split');
                        return s ? getComputedStyle(s).gridTemplateColumns : '';
                    })(),
                    meta: (document.getElementById('vdp-meta') || {}).textContent,
                    depthMeta: (document.getElementById('vdp-depth-meta') || {}).textContent,
                    detStatus: (document.getElementById('vdp-detector-status') || {}).textContent,
                    // 页面**不应**再有深度事件历史列表（历史改走日志）
                    hasEventList: !!document.getElementById('vdp-event-list'),
                    hasEventsCount: !!document.getElementById('vdp-events-count'),
                    // 色标图例：条在上、说明在下（说明文字的顶边要低于色标条的底边）
                    legend: (() => {
                        const bar = document.querySelector('.vdp-legend-bar');
                        const txt = document.querySelector('.vdp-legend-text');
                        if (!bar || !txt) return null;
                        const b = bar.getBoundingClientRect();
                        const t = txt.getBoundingClientRect();
                        return { barBottom: Math.round(b.bottom),
                                 textTop: Math.round(t.top),
                                 text: txt.textContent.trim() };
                    })(),
                };
            }""")

        print("=== 页面状态 ===")
        for k, v in info.items():
            print(f"  {k}: {v}")

        if info["videoTiles"] < 1:
            fails.append(f"左栏没有宫格（{info['videoTiles']} 格）")
        if info["depthTiles"] < 1:
            fails.append(f"右栏没有宫格（{info['depthTiles']} 格）")
        if info["videoTiles"] != info["depthTiles"]:
            fails.append(f"左右两栏格数不一致：视频 {info['videoTiles']} vs "
                         f"深度 {info['depthTiles']}")
        if not any(n > 0 for n in info["videoPainted"]):
            fails.append("左栏 canvas 全黑 —— 视频帧没画上去")
        if not painted:
            fails.append("右栏 canvas 全黑 —— 深度热力图没画上去")
        if not info["splitVisible"]:
            fails.append(".vdp-split 布局不可见")
        # 左右两栏的**视场比例**必须一致 —— 深度图是按送检帧宽高比回拉的，
        # 视频是显示尺寸（也按源宽高比），两者比例都该等于源的比例。
        vc = info.get("videoCanvas") or []
        dc = info.get("depthCanvas") or []
        if vc and dc and len(vc) == len(dc):
            for i, ((vw, vh), (dw, dh)) in enumerate(zip(vc, dc)):
                if not (vw and vh and dw and dh):
                    continue
                vr, dr = vw / vh, dw / dh
                if abs(vr - dr) / vr > 0.02:
                    fails.append(
                        f"第 {i + 1} 路左右两栏视场比例不一致："
                        f"视频 {vw}×{vh}（{vr:.4f}）vs 深度 {dw}×{dh}（{dr:.4f}）")
        else:
            fails.append("拿不到左右两栏的 canvas 尺寸")
        # UI 上不再有深度事件历史（历史改走日志）
        if info.get("hasEventList"):
            fails.append("页面上还有「深度事件历史」列表（应已移除）")
        if info.get("hasEventsCount"):
            fails.append("页面上还有事件计数徽标（应已移除）")
        # 色标图例：说明文字必须在色标条**下面**
        lg = info.get("legend")
        if not lg:
            fails.append("找不到色标图例")
        elif lg["textTop"] < lg["barBottom"]:
            fails.append(
                f"色标说明文字不在色标条下面（条底 {lg['barBottom']} / "
                f"文字顶 {lg['textTop']}）")
        for e in errors:
            # favicon 404 是浏览器自己要的，与卡片无关
            if "favicon" in str(e):
                continue
            fails.append(f"前端报错: {e}")

        page.screenshot(path=args.shot, full_page=False)
        print(f"\n截图 → {args.shot}")
        br.close()

    if fails:
        print("\n❌ 失败：")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("\n✅ 左栏视频 + 右栏深度都画出来了")
    return 0


if __name__ == "__main__":
    sys.exit(main())
