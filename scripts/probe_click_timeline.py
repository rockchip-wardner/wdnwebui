#!/usr/bin/env python3
"""实测「点生成图片 → 界面第一次动 / 进度条出现」的完整时间线。

headless chromium：打开首页 → 点「图片生成」应用卡 → 填提示词 → 点生成，
每 100 ms 抓一次 DOM 关键字段；同时记录 WS 帧到达时刻。

用法::

    .venv/bin/python scripts/probe_click_timeline.py [--steps 8] [--wait 25]
"""
import json
import sys
import time

from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:8000/"

# 注入页面的探针：记录 WS 帧到达时刻
PROBE = """
window.__frames = [];
(function () {
  const Native = window.WebSocket;
  const Wrapped = function (...args) {
    const ws = new Native(...args);
    ws.addEventListener('message', (ev) => {
      try {
        const m = JSON.parse(ev.data);
        const p = m.progress || {};
        window.__frames.push({
          t: Math.round(performance.now()),
          state: m.state,
          busy: m.busy,
          stage: p.stage,
          step: p.step,
          total: p.total,
          percent: p.percent,
          detail: p.detail,
        });
      } catch (e) { /* ignore */ }
    });
    return ws;
  };
  Wrapped.prototype = Native.prototype;
  Object.assign(Wrapped, Native);
  window.WebSocket = Wrapped;
})();
"""


def grab(page):
    """抓一次关键 DOM 状态。"""
    return page.evaluate("""() => {
      const $ = (id) => document.getElementById(id);
      const txt = (el) => (el ? (el.textContent || '').trim() : null);
      const box = $('mg-progress');
      const fill = $('mg-progress-fill');
      return {
        btnDisabled: $('mg-generate') ? $('mg-generate').disabled : null,
        meta: txt($('mg-meta')),
        hint: txt($('mg-hint')),
        boxHidden: box ? box.classList.contains('hidden') : null,
        stage: txt($('mg-progress-stage')),
        detail: txt($('mg-progress-detail')),
        pct: txt($('mg-progress-pct')),
        stepTxt: txt($('mg-progress-step')),
        eta: txt($('mg-progress-eta')),
        barWidth: fill ? fill.style.width : null,
        frames: (window.__frames || []).length,
      };
    }""")


def main():
    steps = 8
    wait_s = 25
    if "--steps" in sys.argv:
        steps = int(sys.argv[sys.argv.index("--steps") + 1])
    if "--wait" in sys.argv:
        wait_s = int(sys.argv[sys.argv.index("--wait") + 1])

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True,
                                    args=["--no-sandbox", "--disable-gpu"])
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        page.add_init_script(PROBE)
        errors = []
        page.on("console", lambda m: errors.append(f"[{m.type}] {m.text}"))
        page.on("pageerror", lambda e: errors.append(f"[pageerror] {e}"))

        page.goto(URL, wait_until="networkidle")
        page.wait_for_timeout(800)

        # 首次访问会进向导（wizard.json 存在）⇒ 先跳过
        skip = page.query_selector("text=跳过向导")
        if skip:
            print("=== 检测到向导，先跳过 ===")
            skip.click()
            page.wait_for_timeout(1500)

        # 强制显示卡片页（headless 下网格页不可见，直接切 DOM）
        page.evaluate("""() => {
          document.querySelectorAll('[id^=page-]').forEach(e => e.classList.add('hidden'));
          const p = document.getElementById('page-media-generator');
          if (p) { p.classList.remove('hidden'); p.classList.add('active'); }
          document.querySelectorAll('.wizard-host,.wizard-overlay,[id^=wizard]')
            .forEach(e => e.classList.add('hidden'));
        }""")
        page.wait_for_selector("#mg-prompt", state="visible", timeout=10000)
        page.wait_for_timeout(1200)

        print("=== 激活后（点击前）===")
        print(json.dumps(grab(page), ensure_ascii=False, indent=2))

        page.fill("#mg-prompt", "a cute cat sitting on a wooden chair")
        page.select_option("#mg-steps", str(steps))

        t0 = time.time()
        page.click("#mg-generate")
        print("\n=== 点击生成 t=0.000 ===")

        last = None
        deadline = time.time() + wait_s
        while time.time() < deadline:
            page.wait_for_timeout(100)
            g = grab(page)
            sig = json.dumps({k: v for k, v in g.items() if k != "frames"},
                             ensure_ascii=False)
            if sig != last:
                print(f"[t={time.time()-t0:6.2f}] {sig}  frames={g['frames']}")
                last = sig
            meta = g.get("meta") or ""
            if "失败" in meta:
                break
            # 出完了：按钮恢复可点 + meta 变成「上次生成」
            if g.get("btnDisabled") is False and "上次生成" in meta \
                    and g.get("pct") == "100%":
                break

        print("\n=== WS 帧时间线（相对页面 performance.now()）===")
        for f in page.evaluate("window.__frames || []"):
            print("  " + json.dumps(f, ensure_ascii=False))

        if errors:
            print("\n=== console ===")
            for e in errors[:15]:
                print("  " + e)

        browser.close()


if __name__ == "__main__":
    main()
