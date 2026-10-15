"""真浏览器验证帧判据 UI（vas / vd / vtd / vtl 四张视频卡）。

每张卡走一遍真实使用路径：

  1. 进卡 → 框架自动挂向导（功能页此时是隐藏的，向导**盖**在它上面）；
  2. 向导走到「视频源」步，填源 + 点「打开」—— canvas 这时才被搬进向导
     容器，**分数曲线**随之出现（与画布等宽）；
  3. 跳过向导 → 回到功能页 —— **曲线必须消失**（用户明确要求：曲线只在
     向导里出现），配置区与浮框仍在；
  4. 功能页改算法（manual → conc）：阈值解禁，且**真的落库**（回读
     /judge 端点确认）；
  5. 全程无 pageerror。

跑法：.venv/bin/python scripts/verify_frame_judge_ui.py
"""
import asyncio
import json
import os
import sys
import urllib.request

from playwright.async_api import async_playwright

BASE = "http://localhost:8000"
VIDEO = "/home/rockchip/test/test/carplant_29132868.mp4"

# card_id -> (页面 id 前缀, 画布 id, 源输入框 id, 判据 API)
CASES = [
    ("video-analysis-search",     "vas", "vcs-canvas",  "vcs-source",
     "/api/video-analysis-search/judge"),
    ("video-detection",           "vd",  "vd-grid",    "vd-sources", "/api/video-detection/judge"),
    ("video-target-detection",    "vtd", "vtd-grid",   "vtd-sources",
     "/api/video-target-detection/judge"),
    ("video-target-localization", "vtl", "vtl-grid",   "vtl-sources",
     "/api/video-target-localization/judge"),
]

NEXT = "下一步"
SKIP = "跳过向导"
OPEN = "打开"

# 向导第 1 步（env_check）要求本机 running 的实例覆盖 manifest 的
# software_capabilities，否则「下一步」是 disabled 的，走不到视频源步 ——
# 而曲线恰恰要在视频源步（canvas 被搬进向导之后）才验得了。
# 为了验 UI 而不去占第二张 NPU（单卡放不下两个模型，见 AGENTS.md），这里
# 只在**本测试页**里把 /api/compute 的能力清单桩成全绿。属于测试替身，
# 不碰任何产品代码，也不代表真机算力状态。
STUB_CAPS = [
    "swcap_video_describe", "swcap_video_retrieval", "swcap_audio_understand",
    "swcap_grounding", "swcap_text_chat", "swcap_audio_tts",
]
STUB_COMPUTE = {
    "instances": [{"status": "running", "_source": "spawned",
                   "capability_types": STUB_CAPS, "model_id": "stub", "npu_index": 0}],
    "running_count": 1, "capabilities": [],
}


def _get_judge(api: str) -> dict:
    with urllib.request.urlopen(BASE + api, timeout=10) as r:
        return json.loads(r.read().decode())


def _post_judge(api: str, body: dict) -> dict:
    req = urllib.request.Request(
        BASE + api, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def _recommended(api: str) -> str:
    """服务端当前的推荐判帧算法（下拉首位 / ``EXPOSED_METRICS[0]``）。

    测试里**不写死算法名**。v0.21 把推荐项从 conc 换成 gscore 时，下面的
    下拉断言、耗时标注、"切算法"三处曾经全部直接红 —— 它们把"推荐项是谁"
    当成了不变式，而推荐项本来就是会变的。改成从服务端取，顺带还能验证
    **下拉顺序与服务端一致**（见 ``opts[0]`` 那条断言）。
    """
    return _get_judge(api + "/metrics")["metrics"][0]


async def main() -> int:
    results: list = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = await browser.new_context(viewport={"width": 1600, "height": 1000})
        page = await ctx.new_page()
        await page.route("**/api/compute", lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(STUB_COMPUTE)))
        errors: list = []
        page.on("pageerror", lambda e: errors.append("pageerror: " + str(e)))

        only = set(filter(None, (os.environ.get("ONLY") or "").split(",")))
        for app_id, pfx, canvas_id, src_id, api in CASES:
            if only and app_id not in only:
                continue
            errors.clear()
            print(f"\n=== {app_id} ({pfx}) ===")
            # **先把焦点从上一个卡的输入框上摘掉**。`FrameJudge.update()` 在
            # `document.activeElement` 上刻意不回写（不能抹掉用户正在输入的
            # 数字，这是对的）；而 Playwright 的 `select_option` 会把最后操作
            # 的元素留在 focus 态。于是跨卡时那个阈值框拒绝同步、一直留着进入
            # 这轮时的环境值（比如用户自己配的 10），本卡改算法时把陈旧值又发
            # 回去 —— 表现为「切 conc 拿到 10、应为 15」。这不是产品的错，
            # 是测试忘了 blur，整轮会因为**跑之前服务上停在哪**而时绿时红。
            await page.evaluate("() => { try { document.activeElement?.blur(); } catch (_) {} }")
            # 让画面真的在流动，曲线才有分数可画（用 Python 发，别在页面里
            # fetch —— 首次 goto 之前页面还是 about:blank，跨源会被浏览器拒）。
            _rec = _recommended(api)
            _post_judge(api, {"mode": _rec, "every_n": 30})

            await page.goto(f"{BASE}/#{app_id}", wait_until="domcontentloaded")
            await page.wait_for_selector(f"#wd-wizard-root-{app_id}", timeout=20000)

            async def root_btn(label: str, root: str) -> str:
                return await page.evaluate("""
                  ([root, label]) => {
                    const r = document.getElementById(root);
                    if (!r) return 'no-root';
                    const b = Array.from(r.querySelectorAll('button'))
                      .find(x => (x.textContent || '').trim() === label);
                    if (!b) return 'no-btn';
                    if (b.disabled) return 'disabled';
                    b.click(); return 'clicked';
                  }""", [root, label])

            root = f"wd-wizard-root-{app_id}"

            # --- 2. 走到视频源步并打开：canvas 被搬进向导 → 曲线出现 ---
            steps = []
            for _ in range(4):
                await page.wait_for_timeout(500)
                steps.append(await page.evaluate(
                    "id => { const t = document.querySelector('#' + id + ' "
                    ".wd-wizard-step-title'); return t ? t.textContent : ''; }", root))
                if "视频源" in (steps[-1] or ""):
                    break
                await root_btn(NEXT, root)

            # 源必须填**向导自己的**输入框（data-field-id）—— wizard 的
            # 「打开」按钮读的是它，再写回页面 #<prefix>-source。填页面那个
            # 会被 wizard 的空值覆盖，表现为"点了打开没反应、canvas 进了
            # 向导但一帧不播"。
            filled = await page.evaluate("""([root, sid, v]) => {
                const el = document.querySelector(`#${root} [data-field-id="${sid}"]`);
                if (!el) return false;
                el.value = v;
                el.dispatchEvent(new Event('input', {bubbles: true}));
                return true;
              }""", [root, src_id, VIDEO])
            opened = await root_btn(OPEN, root)
            moved = False
            for _ in range(30):
                await page.wait_for_timeout(500)
                moved = await page.evaluate(
                    "cid => !!(document.getElementById(cid)||{})"
                    ".closest?.('.wd-wizard-canvas-wrap')", canvas_id)
                if moved:
                    break
            ok2a = (opened == "clicked" and moved and filled)
            print(f"  {'✅' if ok2a else '❌'} 向导内填源({filled})+点打开({opened}) → canvas 进向导={moved}")
            results.append(ok2a)

            # --- 2b. 向导里改判据 → **立刻**落库（不等「完成配置」）---
            # 曲线就在这一步，改完却要等收尾才生效的话等于没有。
            await page.select_option(
                f"#wd-wizard-root-{app_id} [data-field-id='{pfx}-judge-mode']", "manual")
            await page.wait_for_timeout(1200)
            srv_wiz = _get_judge(api)
            ok2d = srv_wiz.get("mode") == "manual"
            print(f"  {'✅' if ok2d else '❌'} 向导内改算法立刻落库 mode={srv_wiz.get('mode')}")
            results.append(ok2d)
            # 改回推荐算法，让下面的曲线断言仍有分数
            await page.select_option(
                f"#wd-wizard-root-{app_id} [data-field-id='{pfx}-judge-mode']", _rec)
            await page.wait_for_timeout(2500)

            await page.wait_for_timeout(4000)   # 攒几个分数点
            wiz = await page.evaluate(f"""
              () => {{
                const cv = document.getElementById('{canvas_id}');
                const c = document.getElementById('{pfx}-judge-curve-wrap');
                const b = document.getElementById('{pfx}-judge-badge');
                // 曲线 canvas 没有 id（组件只给外层 wrap 加了 id），按 class 取。
                const cc = document.querySelector('#{pfx}-judge-curve-wrap .vj-curve-canvas');
                let painted = null;
                if (cc && cc.width) {{
                  try {{
                    const d = cc.getContext('2d').getImageData(0, 0, cc.width, cc.height).data;
                    let n = 0;
                    for (let i = 3; i < d.length; i += 4) if (d[i] > 8) n++;
                    painted = n;
                  }} catch (e) {{ painted = 'err'; }}
                }}
                const r = cv ? cv.getBoundingClientRect() : null;
                const inst = window.FrameJudge && window.FrameJudge.get('{pfx}');
                return {{
                  curveDisplay: c ? c.style.display : null,
                  curveWidth: c ? c.style.width : null,
                  canvasW: r ? Math.round(r.width) : 0,
                  badgeDisplay: b ? b.style.display : null,
                  badgeTxt: b ? (b.textContent || '').slice(0, 40) : null,
                  recent: inst && inst.judge ? (inst.judge.recent || []).length : null,
                  painted,
                }};
              }}""")
            ok2b = (wiz["curveDisplay"] != "none"
                    and abs(float((wiz["curveWidth"] or "0").replace("px", "") or 0)
                            - wiz["canvasW"]) <= 2)
            print(f"  {'✅' if ok2b else '❌'} 曲线可见且与画布等宽：display="
                  f"{wiz['curveDisplay']} 宽={wiz['curveWidth']} 画布={wiz['canvasW']}")
            results.append(ok2b)
            ok2c = bool(wiz["painted"] and wiz["painted"] != "err")
            print(f"  {'✅' if ok2c else '❌'} 曲线真的画了东西（非空像素={wiz['painted']}）"
                  f" 分数点={wiz['recent']}  浮框='{wiz['badgeTxt']}'")
            results.append(ok2c)

            # --- 2c. 曲线不得压住视频源步里的判据控件 ---
            # 曲线是 fixed 浮层、不占文档流，向导里视频正下方紧跟的就是判据
            # 控件，两者抢同一块屏幕（实测 elementFromPoint 命中的是曲线）。
            # 先滚到判据控件上 —— 用户报的正是"往下滚看配置时曲线压住下拉框"
            await page.evaluate(f"""
              () => {{
                const el = document.querySelector(
                  '#wd-wizard-root-{app_id} [data-field-id$="judge-mode"]');
                if (el) el.scrollIntoView({{block: 'center'}});
              }}""")
            await page.wait_for_timeout(1200)
            clash = await page.evaluate(f"""
              () => {{
                const cc = document.getElementById('{pfx}-judge-curve-wrap');
                const sel = document.querySelector(
                  '#wd-wizard-root-{app_id} [data-field-id$="judge-mode"]');
                if (!cc || !sel) return {{bad: 'missing'}};
                const a = cc.getBoundingClientRect(), b = sel.getBoundingClientRect();
                const hit = !(a.right <= b.left || a.left >= b.right
                          || a.bottom <= b.top || a.top >= b.bottom);
                const at = document.elementFromPoint(
                  b.left + b.width / 2, b.top + b.height / 2);
                return {{hit: hit && cc.style.display !== 'none',
                         topEl: at ? at.tagName + '.' + at.className : '',
                         spacer: !!document.querySelector('.vj-curve-spacer')}};
              }}""")
            ok2e = (clash.get("hit") is False and clash.get("topEl", "").startswith("SELECT")
                    and clash.get("spacer") is True)
            print(f"  {'✅' if ok2e else '❌'} 曲线不压判据控件：压住={clash.get('hit')} "
                  f"命中={clash.get('topEl')} 占位块={clash.get('spacer')}")
            results.append(ok2e)

            # --- 3. 跳过向导 → 功能页 ---
            skipped = await root_btn(SKIP, root)
            await page.wait_for_timeout(1500)
            page_cls = await page.evaluate(
                "id => document.getElementById('page-' + id).className", app_id)
            ok3 = "hidden" not in page_cls and skipped == "clicked"
            print(f"  {'✅' if ok3 else '❌'} 跳过向导({skipped})后功能页可见 class='{page_cls}'")
            results.append(ok3)

            # --- 4. 功能页：曲线隐藏 + 配置区可见 + 改算法落库 ---
            await page.wait_for_selector(f"#{pfx}-judge-mode", state="visible", timeout=10000)
            curve_off = await page.evaluate(
                f"() => document.getElementById('{pfx}-judge-curve-wrap').style.display")
            ok4a = curve_off == "none"
            print(f"  {'✅' if ok4a else '❌'} 功能页曲线隐藏 display='{curve_off}'")
            results.append(ok4a)

            opts = await page.eval_on_selector(
                f"#{pfx}-judge-mode", "el => Array.from(el.options).map(o => o.value)")
            # **契约而不是白名单快照**：下拉 = manual（兜底，零 RGA 开销）
            # + 服务端放行的全部度量，**手动跳帧永远在首位**（是设计：
            # 用户最不踩坑的默认），且不含 SDK 自带的 sad/ncc/z_mad。
            # 放行的具体是哪几个会随选型变，写死 `== ["manual","conc"]` 会在
            # v0.21 引入 gscore 时直接红 —— 而那不是回归，是产品改了。
            srv_metrics = _get_judge(api + "/metrics")
            want = ["manual"] + list(srv_metrics["metrics"])
            ok4b = sorted(opts) == sorted(want) and not (
                set(opts) & {"sad", "ncc", "z_mad"})
            print(f"  {'✅' if ok4b else '❌'} 配置区下拉 = manual（兜底）+ "
                  f"服务端放行项：{opts}")
            results.append(ok4b)
            # **第一个自动度量必须等于服务端推荐项**：``_fillModes`` 把 manual
            # append 在前、然后是 metrics 列表，所以 ``opts[1]`` 才是
            # ``metrics[0]``。default_threshold 的兜底用 ``EXPOSED_METRICS[0]``，
            # 跟下拉第一项自动度量对得上是契约 —— 否则用户选第一个自动度量时
            # 拿到的默认阈值是别的算法的（最坏情况阈值是 0 或一个无效值）。
            rec = srv_metrics["metrics"][0]
            ok4b2 = len(opts) >= 2 and opts[1] == rec
            print(f"  {'✅' if ok4b2 else '❌'} 下拉第一个自动度量 = 服务端 "
                  f"推荐项 {rec}（default_threshold 兜底也是它）")
            results.append(ok4b2)

            # 选项要标上**实测**的单帧耗时，且与后端 /judge/metrics 一致
            # 前端按 frame-judge.js: `ms < 1 ? 2 : 1` 选小数位 —— < 1ms 时
            # 写"0.5"是误导（实际 0.46），所以是设计选择；断言跟着这条规则，
            # 不要写死 `:.1f`。
            labels = await page.eval_on_selector(
                f"#{pfx}-judge-mode",
                "el => Array.from(el.options).map(o => o.textContent)")
            cost_srv = srv_metrics
            cost = (cost_srv.get("cost_ms") or {}).get(_rec)
            zlab = next((x for x in labels if x.startswith(_rec)), "")
            digits = 2 if (cost is not None and cost < 1) else 1
            ok4bb = ("ms/帧" in zlab and cost is not None
                     and f"{cost:.{digits}f}" in zlab)
            print(f"  {'✅' if ok4bb else '❌'} {_rec} 标注实测耗时：{zlab!r} "
                  f"(服务端 cost_ms={cost})")
            results.append(ok4bb)

            await page.select_option(f"#{pfx}-judge-mode", "manual")
            await page.wait_for_timeout(800)
            dis_m = await page.eval_on_selector(
                f"#{pfx}-judge-threshold", "el => el.disabled")
            diag = await page.evaluate(f"""() => {{
                const i = window.FrameJudge.get('{pfx}');
                return {{sel: i.selMode.value, dis: i.inpThr.disabled,
                         await: i._awaitSync, judgeMode: i.judge && i.judge.mode,
                         applied: i.applied}};
            }}""")
            print(f"  {'✅' if dis_m else '❌'} manual 下阈值禁用 {diag}")
            results.append(bool(dis_m))

            # **先把功能页的阈值框显式置成后端默认值**再切算法。四张卡连跑时
            # `page.goto` 只改 hash、**不重载页面**，上一张卡的操作会把
            # `document.activeElement` 留在某个判据输入框上；而
            # `FrameJudge.update()` 刻意不回写聚焦中的输入框（不能抹掉用户
            # 正在输入的数字，这是对的），于是那个框保留着进入本卡时的环境值
            #（比如用户自己配的 10），切算法时又把它发回去。
            # 与其依赖跨卡同步时序，不如让本卡**自己拥有**控件状态。
            want_thr = cost_srv.get("default_threshold", {}).get(_rec)
            if want_thr is not None:
                await page.evaluate("""([sel, v]) => {
                    const el = document.querySelector(sel);
                    if (!el) return;
                    el.value = String(v);
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                }""", [f"#{pfx}-judge-threshold", want_thr])
                await page.wait_for_timeout(800)

            await page.select_option(f"#{pfx}-judge-mode", _rec)
            await page.wait_for_timeout(1000)
            dis_a = await page.eval_on_selector(
                f"#{pfx}-judge-threshold", "el => el.disabled")
            srv = await page.evaluate(
                "async (u) => (await fetch(u, {cache:'no-store'})).json()", api)
            # 阈值输入框这轮被测试显式置成了后端默认值，所以切到推荐算法之后
            # **必须**仍是那个值。只断言 mode 会漏掉"沿用上一个算法的旧阈值"
            # 这类故障 —— 而它正是 AGENTS.md 记的第 1 条静默失效：
            # 老阈值配上新算法，看着"生效了"，实际把节流关掉了。
            ok4c = ((not dis_a) and srv.get("mode") == _rec
                    and want_thr is not None
                    and abs(float(srv.get("threshold", -1)) - float(want_thr)) < 1e-6)
            print(f"  {'✅' if ok4c else '❌'} 切 {_rec}：阈值解禁 + 落库 + 用 "
                  f"{_rec} 默认阈值 mode={srv.get('mode')} "
                  f"every_n={srv.get('every_n')} "
                  f"threshold={srv.get('threshold')}（应为 {want_thr}）")
            results.append(ok4c)

            # --- 4d. 过期配置（下拉已下线的算法）不能在下拉里复活 ---
            # 真实路径：向导 localStorage 里存着上一版的 judgeMode="sad"，
            # applyWizardSnapshot → FrameJudge.fromWizard → applyConfig
            # （setConfig 会临时补同名 option）→ POST → 服务端回落 manual。
            # 幽灵 option 必须被剪掉，否则下拉里挂着一个选不上的死项。
            #
            # **不要在这里传 threshold**。/api/video/judge 是 vd / vtd / vtl
            # 共用一份，任何显式阈值都会把那个数值钉死（服务端只在"没传
            # 阈值"时才按模式重置）。这里曾写死 6（z_mad 时代默认值），后果是
            # 整条链被污染：共享判据停在 6 → 后面几张卡已挂载的控件从 WS 快照
            # 读到 6 → 用户改算法时把陈旧的 6 又发回去 → "切 conc" 拿到 6
            # 而不是 15。这一步要验的是**算法名回落**，与阈值无关，省掉即可。
            await page.evaluate(f"""async () => {{
                await window.FrameJudge.applyConfig('{pfx}', {{
                    mode: 'sad', every_n: 30 }});
            }}""")
            await page.wait_for_timeout(1200)
            opts2 = await page.eval_on_selector(
                f"#{pfx}-judge-mode", "el => Array.from(el.options).map(o => o.value)")
            srv2 = _get_judge(api)
            sel2 = await page.eval_on_selector(
                f"#{pfx}-judge-mode", "el => el.value")
            ok4d = ("sad" not in opts2) and srv2.get("mode") == "manual" \
                and sel2 == "manual"
            print(f"  {'✅' if ok4d else '❌'} 过期/已下线算法不复活：下拉={opts2} "
                  f"选中={sel2} 服务端={srv2.get('mode')}")
            results.append(ok4d)

            # --- 5. pageerror ---
            ok5 = not errors
            print(f"  {'✅' if ok5 else '❌'} 无 pageerror {errors[:3]}")
            results.append(ok5)

            # 复位成手动（/api/video/judge 是三张卡共用一份，留 conc 会
            # 污染下一张卡的初始断言）。
            # **不传 threshold**：传了就等于把一个具体数值钉进共享判据，
            # 后面那张卡从 manual 切 conc 时会**继承**这个值而不是拿模式
            # 默认（服务端只认"没传阈值"才重置）。这里曾写死 6 —— z_mad
            # 时代的默认值 —— 于是整轮跑完共享判据停在 conc@6，而"切 conc"
            # 断言只看 mode 不看阈值，照样判绿：测试自己把回归基线改坏了还
            # 报通过。省略 threshold 让服务端按"模式变了用新默认"处理。
            _post_judge(api, {"mode": "manual", "every_n": 30})

        await browser.close()

    total = sum(1 for r in results if r)
    print(f"\n{'='*46}\n通过 {total} / {len(results)}")
    return 0 if total == len(results) else 1


sys.exit(asyncio.run(main()))