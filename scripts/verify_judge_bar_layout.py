"""真浏览器验证「视频源 + 判据」两条配置栏的位置与右栏高度。

背景：用户要求把 vd（音视频描述）的视频源栏从右侧 340px 栏搬到视频正下方
横排 —— 原先竖排在窄栏里把右栏拉得比视频还高。这条脚本把"搬没搬"和
"搬完右栏有没有变矮"都量出来，不靠肉眼：

  1. 跳��向导后进功能页；
  2. 断言 #<prefix>-source 在画布**下方**、#<prefix>-judge-config 横排
     （.vj-inline）且两者都在 .vd-stage-col / .vcs-stage-col 里；
  3. 量右侧栏 / 左侧栏高度 —— 右栏内容不该再溢出（scrollHeight 不超
     clientHeight 太多），右栏不该比左栏高；
  4. 视频源栏与画布同起点同宽（看着才像"贴在画面下面"）；
  5. 填源 + 点打开仍然照常（移出 <form> 不许弄坏交互），全程无 pageerror；
  6. 截屏留证。

跑法：.venv/bin/python scripts/verify_judge_bar_layout.py
      ONLY=video-detection,video-analysis-search .venv/bin/python ...
"""
import asyncio
import json
import os
import sys

from playwright.async_api import async_playwright

BASE = "http://localhost:8000"
VIDEO = "/home/rockchip/test/test/carplant_29132868.mp4"
SKIP = "跳过向导"

# card_id -> (前缀, 画布 id, 源输入框 id, 打开按钮 id, 侧栏 sel, 舞台列 sel,
#              是否期望"视频源 + 判据"摊在视频正下方)
CASES = [
    # v0.20：vd（音视频描述）按用户要求把两条栏搬到视频正下方；VAS 保持原样
    # （在左侧控制台里竖排）—— 两者都进脚本，搬走的那张不能靠"另一张没变"
    # 来证明自己变了，反过来也一样。
    ("video-detection",       "vd",  "vd-canvas",  "vd-source",  "vd-open",
     ".vd-side", ".vd-stage-col", True),
    ("video-analysis-search", "vas", "vcs-canvas", "vcs-source", "vcs-open",
     ".vcs-layout > .surv-form", ".vcs-layout > .vd-stage-col", False),
]


async def main() -> int:
    results: list = []

    def chk(ok: bool, msg: str) -> None:
        print(f"  {'✅' if ok else '❌'} {msg}")
        results.append(bool(ok))

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = await browser.new_context(viewport={"width": 1600, "height": 1000})
        page = await ctx.new_page()
        errors: list = []
        page.on("pageerror", lambda e: errors.append("pageerror: " + str(e)))

        only = set(filter(None, (os.environ.get("ONLY") or "").split(",")))
        for app_id, pfx, canvas_id, src_id, open_id, side_sel, stage_sel, moved in CASES:
            if only and app_id not in only:
                continue
            errors.clear()
            print(f"\n=== {app_id} ({pfx}) —— 期望"
                  f"{'摊在视频正下方' if moved else '留在侧栏竖排'} ===")

            await page.goto(f"{BASE}/#{app_id}", wait_until="domcontentloaded")
            await page.wait_for_selector(f"#wd-wizard-root-{app_id}", timeout=20000)
            # 等**跳过按钮**真正渲染出来（wizard root 早就 mount，里面的 footer
            # 是异步追加的，曾等 20s 还找不到按钮 —— 因为只 wait 了 root 而没等
            # 到子节点）。
            await page.wait_for_selector(
                f"#wd-wizard-root-{app_id} .wd-wizard-skip-btn", timeout=20000)
            # 跳过向导 → 功能页（用户报的就是这个状态）
            await page.evaluate("""([root, label]) => {
              const r = document.getElementById(root);
              if (!r) return false;
              const b = Array.from(r.querySelectorAll('button'))
                .find(x => (x.textContent || '').trim() === label);
              if (!b) return false;
              b.click(); return true;
            }""", [f"wd-wizard-root-{app_id}", SKIP])
            # 等功能页**真的**显示出来再量。只 sleep 固定时长会偶发地量到
            # 还盖着向导的隐藏页（表现是后面的 fill 报 element is not
            # visible），那种红是竞态不是回归。
            await page.wait_for_function(
                "id => { const p = document.getElementById('page-' + id);"
                " return p && !p.classList.contains('hidden'); }",
                arg=app_id, timeout=20000)

            m = await page.evaluate("""([appId, cv, src, jc, side, stage]) => {
              const R = (el) => {
                if (!el) return null;
                const r = el.getBoundingClientRect();
                return {x: Math.round(r.x), y: Math.round(r.y),
                        w: Math.round(r.width), h: Math.round(r.height)};
              };
              const q = (s) => document.querySelector(s);
              const sideEl = q(side), stageEl = q(stage);
              const canvasEl = document.getElementById(cv);
              return {
                // 用户眼里的"视频"是**黑框** .vd-stage，不是内层 canvas：
                // canvas 有 max-width:100% 却不强制拉伸，遇到 16:9 素材会在
                // 满宽的黑框里居中显示（实测 960 画布落在 1102 黑框中央）。
                // 拿 canvas 当基准量出来的"没对齐"是量错了对象。
                frame: R(canvasEl ? canvasEl.closest('.vd-stage') : null),
                canvas: R(canvasEl),
                src: R(document.getElementById(src)),
                // **必须按卡片作用域查**：所有卡的 DOM 都同时挂在同一个文档
                // 里（切页靠 .hidden），全局 querySelector 会量到别的卡 —— 那时
                // VAS 会"看到" vd 的 .stage-source-bar，报出一个不存在的红。
                bar: R(q('#page-' + appId + ' .stage-source-bar')),
                judge: R(document.getElementById(jc)),
                judgeInline: !!document.getElementById(jc)?.classList.contains('vj-inline'),
                side: R(sideEl),
                sideScroll: sideEl ? sideEl.scrollHeight : null,
                sideClient: sideEl ? sideEl.clientHeight : null,
                stage: R(stageEl),
                inStage: !!document.getElementById(jc)?.closest(stage),
                inSide: !!document.getElementById(src)?.closest(side),
                sideOverflowPx: sideEl ? sideEl.scrollHeight - sideEl.clientHeight : null,
              };
            }""", [app_id, canvas_id, src_id, f"{pfx}-judge-config",
                    side_sel, stage_sel])

            if not m["canvas"] or not m["src"] or not m["frame"]:
                chk(False, f"布局测量失败：frame={m['frame']} canvas={m['canvas']} "
                           f"src={m['src']}")
                continue

            if moved:
                # 1. 视频源紧贴黑框下沿（同一列、y 更大）
                chk(m["src"]["y"] >= m["frame"]["y"] + m["frame"]["h"] - 2,
                    f"视频源紧贴视频下方：黑框底 y="
                    f"{m['frame']['y'] + m['frame']['h']} 视频源 y={m['src']['y']}")
                # 2. 判据栏也搬过来了，且在舞台列里、不在侧栏里
                chk(m["judge"] is not None and m["inStage"],
                    f"判据栏在舞台列内（DOM 归属正确）inStage={m['inStage']}")
                chk(not m["inSide"], "视频源已不在右侧栏内")
                # 3. 横排
                chk(m["judgeInline"], f"判据栏横排（.vj-inline）={m['judgeInline']}")
                # 4. 两条栏与黑框同起点同宽 —— 贴在画面下面才像一体
                if m["bar"]:
                    same = (abs(m["bar"]["x"] - m["frame"]["x"]) <= 2
                            and abs(m["bar"]["w"] - m["frame"]["w"]) <= 2)
                    chk(same,
                        f"视频源栏与黑框同起点同宽：栏 x={m['bar']['x']} w={m['bar']['w']} / "
                        f"黑框 x={m['frame']['x']} w={m['frame']['w']}")
                if m["judge"]:
                    same_j = (abs(m["judge"]["x"] - m["frame"]["x"]) <= 2
                              and abs(m["judge"]["w"] - m["frame"]["w"]) <= 2)
                    chk(same_j,
                        f"判据栏与黑框同起点同宽：x={m['judge']['x']} w={m['judge']['w']} / "
                        f"黑框 x={m['frame']['x']} w={m['frame']['w']}")
            else:
                # 反向断言：这张卡必须**保持**侧栏竖排（防止共享组件的
                # data-judge-layout 开关写坏，把所有卡都摊开）。
                chk(m["inSide"], "视频源仍在侧栏内（未误搬）")
                chk(not m["inStage"], "判据栏不在舞台列内（未误搬）")
                chk(not m["judgeInline"], f"判据栏仍竖排 .vj-inline={m['judgeInline']}")
                chk(m["bar"] is None, "页面上没有多余的 .stage-source-bar")

            # 5. 右栏不高于左栏，也不溢出（两种布局都得成立）
            if m["side"] and m["stage"]:
                chk(m["side"]["h"] <= m["stage"]["h"] + 2,
                    f"右栏不高于左栏：右={m['side']['h']} 左={m['stage']['h']}")
                chk(m["sideOverflowPx"] is not None and m["sideOverflowPx"] <= 4,
                    f"右栏内容不溢出：scroll-client={m['sideOverflowPx']}px")
            print(f"     尺寸：画布 {m['canvas']['w']}x{m['canvas']['h']} / "
                  f"视频源栏 {m['bar']['h'] if m['bar'] else '?'}px / "
                  f"判据栏 {m['judge']['h'] if m['judge'] else '?'}px / "
                  f"右栏 {m['side']['h'] if m['side'] else '?'}px "
                  f"(scroll {m['sideScroll']})")

            # 6. 交互没坏：填源 + 点打开
            await page.fill(f"#{src_id}", VIDEO)
            await page.click(f"#{open_id}")
            await page.wait_for_timeout(3500)
            opened = await page.evaluate("""([src, cv]) => {
              const el = document.getElementById(src);
              const c = document.getElementById(cv);
              let px = -1;
              try {
                const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
                let s = 0; for (let i = 0; i < d.length; i += 4000) s += d[i];
                px = s;
              } catch (e) { px = -1; }
              const meta = document.getElementById(src.replace('-source', '-meta'));
              return {srcVal: el ? el.value : null,
                      meta: meta ? (meta.textContent || '').trim() : '',
                      canvasNonBlank: px > 0};
            }""", [src_id, canvas_id])
            chk(opened["canvasNonBlank"],
                f"填源 + 点打开仍照常（画布非空）源='{opened['srcVal']}' "
                f"meta='{opened['meta'].strip()}'")
            chk(not errors, f"全程无 pageerror {errors or ''}")
            results[-1] = results[-1] and not errors
            results.append(not errors)

            shot = f"/tmp/judge_layout_{app_id}.png"
            await page.screenshot(path=shot, full_page=True)
            print(f"     截图：{shot}")

        await browser.close()

    ok = sum(1 for r in results if r)
    print(f"\n{'=' * 56}\n通过 {ok}/{len(results)}")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
