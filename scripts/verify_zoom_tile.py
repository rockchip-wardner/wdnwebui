#!/usr/bin/env python3
"""多路宫格「单击选中 / 二次点击放大」交互回归（vd / vtd / vtl 三张卡通用）。

驱动真实浏览器跑五项交互，判据一律取 DOM 实际状态 + 后端 snapshot，
不依赖截图。fetch 被包了一层记录 /zoom 的调用 —— 「点了以后后端到底
有没有被通知」是这个功能的另一半，光看布局变没变抓不到通知丢失。

三张卡跑的是同一套断言：本轮 v0.24 把放大接线抽进了共享模块
static/core/zoom-frame.js，逐卡复制断言会让三份漂开。

用法: .venv/bin/python scripts/verify_zoom_tile.py [路数] [card ...]
      card 取 video-detection / video-target-detection / video-target-localization
"""
from __future__ import annotations

import subprocess
import sys
import urllib.request

from playwright.sync_api import sync_playwright

BASE = "http://localhost:8000"
SRC = "/home/rockchip/work/wdnwebui/app/apps/video_target_localization/static/preview.mp4"
SHOT_DIR = "/home/rockchip/work/wdnwebui"

CARDS = {
    "video-detection": ("vd-grid", "/api/video-detection", "vd"),
    "video-target-detection": ("vtd-grid", "/api/video-target-detection", "vtd"),
    "video-target-localization": ("vtl-grid", "/api/video-target-localization", "vtl"),
}

ok = True


def chk(label: str, got, want) -> None:
    global ok
    good = got == want
    ok &= good
    print(("  OK   " if good else "  FAIL ") + label)
    if not good:
        print(f"         got={got!r}\n        want={want!r}")


def cols_of(css: str) -> int:
    try:
        return int(css.split("(")[1].split(",")[0].strip())
    except Exception:
        return 1


def run_card(pw, card: str, nch: int) -> None:
    global ok
    grid, api, short = CARDS[card]
    print(f"\n== {card} ({short}) · {nch} 路 ==")

    br = pw.chromium.launch(args=["--no-sandbox"])
    pg = br.new_page(viewport={"width": 1600, "height": 950})

    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append("pageerror: " + str(e)))
    pg.on("console", lambda m: errors.append("console.error: " + m.text)
          if m.type == "error" else None)

    # 记录 /zoom 的实际调用（在卡片脚本跑之前包住 fetch）
    pg.add_init_script("""
      window.__zoomCalls = [];
      const _f = window.fetch;
      window.fetch = function (u, o) {
        const url = typeof u === "string" ? u : (u && u.url) || "";
        if (url.indexOf("/zoom") >= 0) {
          let body = null;
          try { body = JSON.parse((o && o.body) || "null"); } catch (_) {}
          window.__zoomCalls.push(body);
        }
        return _f.apply(this, arguments);
      };
    """)

    pg.goto(BASE, wait_until="domcontentloaded")
    pg.wait_for_function("() => !!window.ROUTER", timeout=20000)
    # boot 收尾还会自己做一次导航（默认落 apps 页）。ROUTER 一出现就调
    # showPage 会被 boot 的导航覆盖掉 —— 等 boot 落定再切，且**确认真的可见**。
    pg.wait_for_timeout(1800)
    for _ in range(6):
        pg.evaluate("p => window.ROUTER.showPage(p)", card)
        pg.wait_for_timeout(700)
        if pg.evaluate("p => !document.getElementById('page-' + p)"
                       ".classList.contains('hidden')", card):
            break
    pg.wait_for_function(
        "g => !!window.MultiGrid && !!window.ZoomFrame && !!document.getElementById(g)",
        arg=grid, timeout=20000)

    vis = pg.evaluate("""(c) => {
      const el = document.getElementById('page-' + c);
      return { hidden: el.classList.contains('hidden'), display: getComputedStyle(el).display };
    }""", card)
    chk("页面可见", vis["hidden"] is False, True)
    chk("ZoomFrame 已加载", pg.evaluate("() => !!window.ZoomFrame"), True)

    opened = pg.evaluate("""async (a) => {
      const srcs = [];
      for (let i = 0; i < a.n; i++) srcs.push(a.src);
      const r = await fetch(a.api + '/open', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sources: srcs, loop: true }) });
      return { ok: r.ok, body: (await r.text()).slice(0, 200) };
    }""", {"api": api, "src": SRC, "n": nch})
    chk(f"打开 {nch} 路", opened["ok"], True)
    if not opened["ok"]:
        print("        " + opened["body"])
        br.close()
        return

    pg.wait_for_function(
        """(a) => document.querySelectorAll('#' + a.g + ' .vd-tile').length === a.n""",
        arg={"g": grid, "n": nch}, timeout=40000)
    pg.wait_for_timeout(2500)   # 等首帧上屏 + WS 快照

    base = pg.evaluate("""(a) => {
      const g = document.getElementById(a);
      const tiles = [...document.querySelectorAll('#' + a + ' .vd-tile')];
      return { n: tiles.length, cols: g.style.gridTemplateColumns,
               zoomed: window.MultiGrid.zoomedIndex(a),
               hidden: tiles.filter(t => t.classList.contains('is-hidden')).length,
               sel: tiles.filter(t => t.classList.contains('selected')).length,
               zoomCalls: window.__zoomCalls.slice() };
    }""", grid)
    print(f"  -- 初始: {base}")
    chk("格子数", base["n"], nch)
    chk("初始非放大", base["zoomed"], None)
    chk("初始无隐藏格", base["hidden"], 0)
    chk("初始无选中", base["sel"], 0)
    chk("初始未调 /zoom", base["zoomCalls"], [])

    def click(i):
        pg.evaluate("""(a) => document.querySelectorAll('#' + a.g + ' .vd-tile')[a.i]
            .dispatchEvent(new MouseEvent('click', {bubbles:true}))""",
            {"g": grid, "i": i})
        pg.wait_for_timeout(1200)   # 等 POST /zoom 往返

    def state():
        return pg.evaluate("""(a) => {
          const g = document.getElementById(a);
          const tiles = [...document.querySelectorAll('#' + a + ' .vd-tile')];
          const zi = window.MultiGrid.zoomedIndex(a);
          return { zoomed: zi, cols: g.style.gridTemplateColumns,
                   isZoomedCls: g.classList.contains('is-zoomed'),
                   hidden: tiles.map(t => t.classList.contains('is-hidden')),
                   sel: tiles.map(t => t.classList.contains('selected')),
                   labelFont: getComputedStyle(
                     tiles[0].querySelector('.vd-tile-label')).fontSize,
                   zoomCalls: window.__zoomCalls.slice() };
        }""", grid)

    # ---- 1. 单击 = 只选中，不放大 ----
    click(0)
    s = state()
    chk("1)单击 ch1 → 选中", s["sel"], [True] + [False] * (nch - 1))
    chk("1)单击 ch1 → 未放大", s["zoomed"], None)
    chk("1)单击 ch1 → 未调 /zoom", s["zoomCalls"], [])

    # ---- 2. 二次点击 = 放大 ----
    click(0)
    s = state()
    print(f"  -- 放大后: {s}")
    chk("2)二次点击 → 放大 ch1", s["zoomed"], 0)
    chk("2)放大 → 其余格隐藏", s["hidden"], [False] + [True] * (nch - 1))
    chk("2)放大 → grid 1 列", s["cols"], "1fr")
    chk("2)放大 → is-zoomed 类", s["isZoomedCls"], True)
    chk("2)放大 → 角标字号放大", s["labelFont"] != "13px", True)
    chk("2)放大 → 通知后端 {ch0:true}", s["zoomCalls"], [{"channel": 0, "zoomed": True}])

    # ---- 3. 再点同一路 = 还原 ----
    click(0)
    s = state()
    chk("3)再点 ch1 → 退出放大", s["zoomed"], None)
    chk("3)退出 → 无隐藏格", s["hidden"], [False] * nch)
    chk("3)退出 → 恢复列数", s["cols"], f"repeat({cols_of(base['cols'])}, 1fr)")
    chk("3)退出 → 通知后端还原", s["zoomCalls"],
        [{"channel": 0, "zoomed": True}, {"channel": 0, "zoomed": False}])

    # ---- 4. ESC 退出 ----
    # 退出放大后 ch1 **仍处于选中态**，再点一次是「放大」而非「缩小」。
    click(0)
    chk("4)单击 ch1(已选中) → 放大", state()["zoomed"], 0)
    n_calls = len(state()["zoomCalls"])
    pg.keyboard.press("Escape")
    pg.wait_for_timeout(1200)
    s = state()
    chk("4)ESC → 退出放大", s["zoomed"], None)
    chk("4)ESC → 无隐藏格", s["hidden"], [False] * nch)
    chk("4)ESC → 保留选中", s["sel"], [True] + [False] * (nch - 1))
    chk("4)ESC → 新增一条通知", len(s["zoomCalls"]), n_calls + 1)
    chk("4)ESC → 通知后端还原", s["zoomCalls"][-1], {"channel": 0, "zoomed": False})

    # ---- 5. 点另一路 = 退出放大并选中那一路 ----
    click(0)
    chk("5)前置已放大 ch1", state()["zoomed"], 0)
    n_calls = len(state()["zoomCalls"])
    click(2)
    s = state()
    chk("5)点 ch3 → 退出放大", s["zoomed"], None)
    exp = [False] * nch
    exp[2] = True
    chk("5)点 ch3 → 选中 ch3", s["sel"], exp)
    chk("5)点 ch3 → 新增一条通知", len(s["zoomCalls"]), n_calls + 1)
    chk("5)点 ch3 → 通知还原的是 ch1(不是选中的 ch3)", s["zoomCalls"][-1],
        {"channel": 0, "zoomed": False})

    # ---- 后端：被放大那一路升档，其余不动 ----
    click(2)          # ch3 此刻已选中，再点一次即放大
    pg.wait_for_timeout(1800)
    chk("截图时处于放大态", state()["zoomed"], 2)
    disp = pg.evaluate("""async (a) => {
      const r = await fetch(a, {cache:'no-store'}).then(x => x.json());
      const s = r.session || r;
      return { zoomed: s.zoomed,
               sizes: (s.channel_list || []).map(c => [c.disp_w, c.disp_h]) };
    }""", api)
    print(f"  -- 放大态后端: {disp}")
    chk("放大态后端记着 ch3(0基2)", disp["zoomed"], 2)
    chk("被放大那一路升档(608x352 源的上限)", disp["sizes"][2], [608, 352])
    chk("其余路仍在原档", disp["sizes"][0], [384, 222])

    # 截图（仅 vtl 留档，其余两张不落盘）
    if short == "vtl":
        pg.locator(f"#{grid}").screenshot(path=f"{SHOT_DIR}/vtl_grid.png")
        pg.wait_for_timeout(400)
        pg.locator(f"#{grid}").screenshot(path=f"{SHOT_DIR}/vtl_zoom.png")
        print(f"  -- 截图 {SHOT_DIR}/vtl_grid.png / vtl_zoom.png")

    pg.evaluate("a => fetch(a + '/close', {method:'POST'})", api)
    # 关闭走的是「状态快照」驱动（MultiGrid.resetIfClosed），不是按钮处理器 ——
    # 等条件而不是死等固定毫秒数，否则 WS tick 慢一拍就假红。
    try:
        pg.wait_for_function(
            "g => window.MultiGrid.zoomedIndex(g) === null"
            " && document.querySelectorAll('#' + g + ' .vd-tile').length === 0",
            arg=grid, timeout=8000)
        cleared = True
    except Exception:
        cleared = False
    chk("close 后宫格与 zoom 态都清空", cleared, True)

    real = [e for e in errors
            if "favicon" not in e and "net::ERR" not in e
            and "WebSocket" not in e and "404" not in e]
    chk("无 JS 运行时错误", real, [])

    br.close()


def main() -> int:
    args = [a for a in sys.argv[1:]]
    nch = 6
    if args and args[0].isdigit():
        nch = int(args[0]); args = args[1:]
    cards = args or list(CARDS)
    print(f"== 多路宫格放大回归 · {nch} 路 · {len(cards)} 张卡 ==")
    print("素材:", SRC)
    with sync_playwright() as pw:
        for card in cards:
            if card not in CARDS:
                print(f"未知卡片: {card}（可选 {list(CARDS)}）")
                continue
            run_card(pw, card, nch)
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        subprocess.run(["pkill", "-f", "[h]eadless_shell"], capture_output=True)
        print("\n" + ("ALL PASS" if ok else "HAS FAILURE"))
