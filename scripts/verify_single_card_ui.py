"""单计算卡向导的真实浏览器验证（playwright + chromium）。

用真实页面验证单卡降级在浏览器里成立：
  * vd  → 出现「完整功能需要 2 张计算卡」+ 二选一 radio，切换后只加载
           选中的那个模型，且落在 NPU0
  * vr  → 单 capability 应用：无提示、不阻断，「一键开启」把模型落 NPU0
  * text_chat → 单卡提示 + 模型落 NPU0

用例之间必须**互不污染**：每个用例前把本机模型停干净，并强制 reload
向导（localStorage 里的「上次停留步骤」会让向导跳到第 2 步）。

用法: .venv/bin/python scripts/verify_single_card_ui.py
"""
import json
import re
import sys

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000"
NS = "wd-wizard-"

PASS, FAIL = 0, 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✓ {name}" + (f"  ({detail})" if detail else ""))
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


def reset_local_models(page):
    """把本机（spawned / adopted_local）模型全停掉，让每个用例从干净状态开始。

    远端 peer 的实例**不能**停 —— 那是对端板子的算力，本机无权关。

    DELETE 走 unload + shutdown + SIGTERM，最长要好几秒；停完还要等
    registry 真的把它摘掉。不等的话下一个用例会看到上一轮残留的
    「running」实例，断言忽绿忽红。
    """
    for _ in range(30):
        busy = page.evaluate(
            """async () => {
                const r = await fetch('/api/compute', {cache:'no-store'});
                const d = await r.json();
                const live = (d.instances || []).filter(i =>
                    i._source !== 'adopted'
                    && ['running','loading'].includes(i.status));
                for (const i of live) {
                    try {
                        await fetch('/api/compute/instances', {
                            method: 'DELETE',
                            headers: {'Content-Type':'application/json'},
                            body: JSON.stringify({model: i.model_id}),
                        });
                    } catch (_) {}
                }
                return live.map(i => i.model_id);
            }"""
        )
        if not busy:
            break
        page.wait_for_timeout(1000)
    page.wait_for_timeout(800)


def open_wizard(page, app_id):
    """打开某张卡的向导，**保证停在第 1 步（env_check）**。

    三个坑：
      * hash-only 的 goto **不重载文档** —— 上一个应用的向导容器会留在
        DOM 里，locator 命中多个元素，断言看着像产品坏了。
        → 每个用例前先 about:blank 回到空白页，整页重载。
      * **别在向导挂载完成前就切走**。框架的 home 页会异步跳到默认
        应用（``location.hash = "#" + appId``）并挂它的向导；mount()
        在 ``await _probeEnv()`` 处会挂起，而 ``_active`` 要到 await
        之后才赋值 —— 这个窗口里 unmount() 摘不掉容器，交叠的两次挂载
        会留下两个向导容器（既有的竞态，mount() 里的 _active 赋值
        时机问题）。
        → 等 home 页的向导挂完（容器出现 + 稳定 1s）再导航。
      * home 页的自动跳转**依赖后端记的 active_app**；没记的时候 home
        页根本不跳、也就没有向导容器 —— 这时不能干等。
        → 容器出现就等它落定；不出现就等框架 boot 完直接导航。
    """
    page.goto("about:blank")
    page.goto(f"{BASE}/", wait_until="domcontentloaded")
    # 等 home 页那次自动挂载（默认应用的向导）彻底落定
    try:
        page.wait_for_selector("[id^='wd-wizard-root-']", timeout=8000)
    except Exception:
        pass          # 没有 active_app → home 页不跳应用 → 没有容器可等
    page.wait_for_timeout(1500)
    page.evaluate(
        """() => {
            for (const k of Object.keys(localStorage)) {
                if (k.startsWith('wdnwebui.wizard.')) localStorage.removeItem(k);
            }
        }"""
    )
    page.goto(f"{BASE}/#/{app_id}", wait_until="domcontentloaded")
    page.wait_for_selector(f"#{NS}root-{app_id}", timeout=25000)
    page.wait_for_selector(f"#{NS}body", timeout=10000)
    page.wait_for_timeout(1500)


def wizard_container_count(page):
    return page.eval_on_selector_all(
        "[id^='wd-wizard-root-']", "els => els.length")


def body_text(page):
    return page.inner_text(f"#{NS}body")


def rows(page):
    return page.eval_on_selector_all(
        f".{NS}env-row", "els => els.map(e => e.innerText)")


def main():
    print("=== 真实浏览器验证：单计算卡降级 ===")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = browser.new_context(viewport={"width": 1440, "height": 960})
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        page.goto(f"{BASE}/", wait_until="domcontentloaded")
        page.wait_for_timeout(1000)

        # ---------- 0. 前置：把板子模拟成单卡 ----------
        # 单卡策略只在 card_count==1 时生效。``compute.card_count``（对
        # 探测结果的上限）能热生效、不用重启 —— 本脚本因此在任何硬件上
        # 都能跑，不用把「本机恰好是单卡」当成前提。
        print("\n【0】前置：compute.card_count=1（模拟单卡板）")
        orig_cfg = page.evaluate(
            """async () => {
                const r = await fetch('/api/compute/config', {cache:'no-store'});
                if (!r.ok) return {__http_error: r.status};
                return await r.json();
            }"""
        )
        # **读不到就别写。** PUT /api/compute/config 是**全量替换** compute
        # section —— 拿不到原值还往下写，autostart 会被整个写空，用户板子
        # 上的「开机自启」配置就没了（这个坑真的踩过：某次 GET 拿到 {}，
        # `cfg.autostart || []` 把它写成空数组）。
        if not isinstance(orig_cfg, dict) or "autostart" not in orig_cfg:
            raise SystemExit(
                f"读不到 /api/compute/config（拿到 {orig_cfg!r}）—— "
                "为免把真机配置写空，本次不跑。")
        orig_autostart = orig_cfg.get("autostart") or []
        page.evaluate(
            """async (cfg) => {
                await fetch('/api/compute/config', {
                    method: 'PUT',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        autostart: cfg.autostart,
                        card_count: 1,
                    }),
                });
            }""",
            {"autostart": orig_autostart},
        )
        page.wait_for_timeout(800)

        # ---------- 1. 算力页报告单卡 ----------
        print("\n【1】/api/compute 报告单卡")
        snap = page.evaluate(
            """async () => {
                const r = await fetch('/api/compute', {cache:'no-store'});
                return await r.json();
            }"""
        )
        check("card_count == 1", snap.get("card_count") == 1,
              str(snap.get("card_count")))
        check("model_cards == [0]", snap.get("model_cards") == [0],
              json.dumps(snap.get("model_cards")))
        # 枚举到的设备数随硬件变（USB 形态的 RGA/VPU 也算进去），不能写死
        # —— 只断言「能跑模型的卡比枚举到的少」这个不变量。
        check("枚举到的设备多于能跑模型的卡（USB 形态的不算模型卡）",
              len(snap.get("model_cards") or []) == 1
              and len(snap.get("cards") or []) > 1,
              f"cards={len(snap.get('cards') or [])} "
              f"model_cards={snap.get('model_cards')}")

        # ---------- 2. 监控反查（vr）→ 单 capability，不提示不阻断 ----------
        print("\n【2】监控反查（vr）—— 单 capability 应用，单卡正常")
        reset_local_models(page)
        open_wizard(page, "video-retrieval")
        check("页面上只有一个向导容器",
              wizard_container_count(page) == 1,
              str(wizard_container_count(page)))
        txt = body_text(page)
        check("停在第 1 步（env_check）", "软件能力" in txt,
              " / ".join(txt.split("\n")[:3]))
        check("没有单卡提示块（单 capability 应用不打扰用户）",
              page.locator(f".{NS}single-notice").count() == 0,
              f"count={page.locator(f'.{NS}single-notice').count()}")
        check("只列自己的 capability",
              any("swcap_vision_embedding" in r for r in rows(page)),
              " | ".join(rows(page)))
        check("有「一键开启所需模型」按钮",
              page.locator(f"#{NS}env-bring-btn").count() == 1,
              f"count={page.locator(f'#{NS}env-bring-btn').count()}")
        # 「下一步」的禁用状态只跟 **env 检查**（模型是否就绪）有关，跟单卡
        # 无关 —— 阻断与否看上面两条：无单卡提示块 + 有「一键开启」按钮
        # （阻断时按钮不渲染）。所以这里不断言禁用状态：模型跑没跑是
        # 另一件事，断言它只会让测试依赖模型加载时序。
        check("「下一步」按钮在",
              page.locator(f"#{NS}next-btn").count() == 1,
              f"count={page.locator(f'#{NS}next-btn').count()}")
        check("「跳过向导」仍在（用户能离开）",
              page.locator(f".{NS}skip-btn").count() == 1)

        # ---------- 3. vd → 二选一 ----------
        print("\n【3】音视频描述（vd）—— 二选一")
        reset_local_models(page)
        open_wizard(page, "video-detection")
        check("页面上只有一个向导容器",
              wizard_container_count(page) == 1,
              str(wizard_container_count(page)))
        txt = body_text(page)
        check("出现单卡提示块",
              page.locator(f".{NS}single-notice").count() >= 1,
              f"count={page.locator(f'.{NS}single-notice').count()}")
        check("提示写明「完整功能需要 2 张计算卡」", "2 张计算卡" in txt,
              " / ".join(txt.split("\n")[:3]))
        radios = page.locator(f".{NS}single-pick-item input[type=radio]")
        check("提供 2 个二选一 radio", radios.count() == 2,
              f"count={radios.count()}")
        check("默认选中视频描述",
              radios.nth(0).is_checked()
              and radios.nth(0).get_attribute("value") == "swcap_vision_chat",
              str(radios.nth(0).get_attribute("value")))
        check("未选中的 capability 标为「单卡下未启用」",
              "单卡下未启用" in txt)
        check("只列出选中的那个 capability（+ 中性行）",
              any("swcap_vision_chat" in r and "未启用" not in r
                  for r in rows(page))
              and not any("swcap_audio_chat" in r and "未启用" not in r
                          for r in rows(page)),
              " | ".join(rows(page)))

        # ---------- 4. 切到「音频描述」→ 一键开启只加载 omni @ NPU0 ----------
        print("\n【4】切到「音频描述」→ 一键开启只加载 omni @ NPU0")
        # 先把 VLM 跑起来 —— 单卡下「换模型」的真实场景是：停掉旧的、
        # 加载新的。一键开启的第 1 步必须先停掉 VLM。
        #
        # **必须等到 running 再点一键开启**：模型加载要 ~50s，只等 3s 的话
        # 实例还是 `loading` —— 一键开启对「启动中」的实例**故意不删**
        # （后端对「启动中禁用」是 409 拒绝，硬发只会把整条链带失败，见
        # AGENTS「模型启停的过渡态」）。那样「停掉旧模型」这一步根本不会
        # 发生，断言看到的 DELETE 列表是空的 —— 那不是 bug，是没等到位。
        page.evaluate(
            """async () => {
                await fetch('/api/compute/instances', {
                    method: 'POST',
                    headers: {'Content-Type':'application/json'},
                    body: JSON.stringify({model:'qwen3-vl-4b', npu_index:0}),
                });
            }"""
        )
        vlm_running = False
        for _ in range(60):
            page.wait_for_timeout(2000)
            vlm_running = page.evaluate(
                """async () => {
                    const r = await fetch('/api/compute', {cache:'no-store'});
                    const d = await r.json();
                    return (d.instances || []).some(i =>
                        i.model_id === 'qwen3-vl-4b' && i.status === 'running'
                        && i._source !== 'adopted');
                }"""
            )
            if vlm_running:
                break
        check("VLM 已 running（停旧模型的前提）", vlm_running)
        posts = []
        page.on("request", lambda r: posts.append(
            (r.method, r.post_data))
            if "/api/compute/instances" in r.url else None)
        radios.nth(1).check()
        page.wait_for_timeout(1200)
        txt2 = body_text(page)
        check("切换后 capability 列表只剩音频描述（视频描述降为中性行）",
              any("swcap_audio_chat" in r and "未启用" not in r
                  for r in rows(page))
              and any("swcap_vision_chat" in r and "未启用" in r
                      for r in rows(page)),
              " | ".join(rows(page)))
        bring = page.locator(f"#{NS}env-bring-btn")
        check("「一键开启所需模型」按钮存在", bring.count() == 1,
              f"count={bring.count()}")
        if bring.count():
            posts.clear()
            bring.click()
            # DELETE 非必需模型要走 unload + shutdown + SIGTERM（最长数秒），
            # POST 在它之后 —— 这里给足时间，别把慢当成失败。
            for _ in range(40):
                page.wait_for_timeout(500)
                starts = [pd for (m, pd) in posts
                          if m == "POST" and pd and "npu_index" in pd]
                if starts:
                    break
            check("只 POST 了一个模型（omni）",
                  len(starts) == 1 and "qwen2.5-omni-3b" in starts[0],
                  json.dumps(starts))
            check("npu_index 落在 0",
                  len(starts) == 1
                  and '"npu_index":0' in starts[0].replace(" ", ""),
                  json.dumps(starts))
            dels = [pd for (m, pd) in posts if m == "DELETE"]
            check("先停掉了非必需的 VLM（DELETE qwen3-vl-4b）",
                  len(dels) == 1 and "qwen3-vl-4b" in dels[0],
                  json.dumps(dels))

        # ---------- 5. text_chat → 普通单卡应用（不提示） ----------
        print("\n【5】文本对话（text_chat）—— 单卡正常，**不显示**单卡提示")
        reset_local_models(page)
        open_wizard(page, "text-chat")
        check("页面上只有一个向导容器",
              wizard_container_count(page) == 1,
              str(wizard_container_count(page)))
        txt4 = body_text(page)
        check("没有单卡提示块（单 capability 应用不打扰用户）",
              page.locator(f".{NS}single-notice").count() == 0,
              f"count={page.locator(f'.{NS}single-notice').count()}")
        check("页面里不含「1 张计算卡」字样", "1 张计算卡" not in txt4,
              " / ".join(txt4.split("\n")[:3]))
        check("没有二选一（单 capability 应用）",
              page.locator(f".{NS}single-pick-item").count() == 0)
        posts.clear()
        bring = page.locator(f"#{NS}env-bring-btn")
        if bring.count():
            bring.click()
            for _ in range(40):
                page.wait_for_timeout(500)
                starts = [pd for (m, pd) in posts
                          if m == "POST" and pd and "npu_index" in pd]
                if starts:
                    break
            check("text_chat: 模型 POST 到 NPU0",
                  len(starts) == 1
                  and "qwen3.5-vl-4b" in starts[0]
                  and '"npu_index":0' in starts[0].replace(" ", ""),
                  json.dumps(starts))

        # ---------- 6. vd 单卡：步骤裁剪 + 完成配置后不再调「分析音频」 ----------
        print("\n【6】vd 单卡 —— 步骤裁剪 + 完成配置后不再调「分析音频」")
        reset_local_models(page)
        open_wizard(page, "video-detection")

        def step_labels():
            return page.eval_on_selector_all(
                f".{NS}step-lbl", "els => els.map(e => e.innerText)")

        # 6.1 默认（视频描述）→ 没有「音频理解提示词」这一步
        labels = step_labels()
        check("默认（视频描述）: 4 步，且没有「音频理解提示词」",
              len(labels) == 4 and "音频理解提示词" not in " ".join(labels),
              " | ".join(labels))
        check("默认（视频描述）: 保留「检测提示词 + 检测尺寸」",
              "检测提示词 + 检测尺寸" in " ".join(labels),
              " | ".join(labels))

        # 6.2 切到「音频描述」→ 没有「检测提示词 + 检测尺寸」这一步
        page.locator(f".{NS}single-pick-item input[type=radio]").nth(1).check()
        page.wait_for_timeout(1200)
        labels2 = step_labels()
        check("切到音频描述: 4 步，且没有「检测提示词 + 检测尺寸」",
              len(labels2) == 4
              and "检测提示词 + 检测尺寸" not in " ".join(labels2),
              " | ".join(labels2))
        check("切到音频描述: 保留「音频理解提示词」",
              "音频理解提示词" in " ".join(labels2),
              " | ".join(labels2))

        # 6.3 复现用户报的弹窗
        #
        # 往 localStorage 预埋一条「上一轮双卡时填过的音频理解提示词」——
        # 模拟用户切到单卡后 localStorage 里还留着旧值。
        #   旧行为:audio 步还在 → 旧值进 opts → applyWizardSnapshot 点
        #           「分析音频」→ 后端 502「功能未启用: swcap_audio_chat」
        #           → 弹「启动音频分析失败: 功能未启用: …」
        #   新行为:audio 步被裁掉 → opts 里没有 audioPrompt → 压根不点
        #
        # 视频源用真视频（/userdata/assets/uploads/rk-car.mp4）—— 只有源能
        # 真的打开，applyWizardSnapshot 才会走到「点分析音频」那一步，
        # 否则这个断言就是空的（源打开失败会提前抛错）。
        reset_local_models(page)
        open_wizard(page, "video-detection")
        page.evaluate(
            """() => {
                localStorage.setItem('wdnwebui.wizard.video-detection.values',
                  JSON.stringify({
                    'vd-sources': '/userdata/assets/uploads/rk-car.mp4',
                    'vd-audio-prompt': '转写这段音频',
                  }));
            }"""
        )
        page.reload(wait_until="domcontentloaded")
        page.wait_for_selector(f"#{NS}root-video-detection", timeout=25000)
        page.wait_for_timeout(1500)

        dialogs = []

        def on_dialog(d):
            dialogs.append(d.message)
            try:
                d.accept()
            except Exception:
                pass

        page.on("dialog", on_dialog)
        # 让向导的 _waitForModelsRunning 立刻放行：它默认会等模型 running
        # （最长 90s），本机没起 VLM 的话根本走不到 applyWizardSnapshot
        # —— 那样「分析音频有没有被点」就是个**空断言**。
        page.route(
            re.compile(r"/api/compute(\?|$)"),
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({
                    "card_count": 1, "model_cards": [0], "cards": [],
                    "instances": [{
                        "id": "m0", "model_id": "qwen3-vl-4b", "npu_index": 0,
                        "status": "running",
                        "capability_types": ["swcap_vision_chat"],
                    }],
                })),
        )
        page.evaluate(
            """() => {
                window.__audioClicks = 0;
                const b = document.getElementById('vd-audio-start');
                if (b) b.addEventListener('click', () => { window.__audioClicks++; });
            }"""
        )
        # env → source → detect → finish（单卡下 vd 只有 4 步）
        for _ in range(3):
            page.evaluate("window.WizardState.stepForward()")
            page.wait_for_timeout(400)
        page.wait_for_selector(f"#{NS}finish-btn", timeout=10000)
        page.locator(f"#{NS}finish-btn").click()
        # 等 opts 落定（_finish 先构造 opts 再调 applyWizardSnapshot）
        opts = None
        for _ in range(60):
            page.wait_for_timeout(500)
            opts = page.evaluate("window._wd_wizard_opts || null")
            # applyWizardSnapshot 是异步的：opts 先落定，音频按钮的点击
            # 可能还在后头 —— 等 wizard 卸载（成功路径）或超时。
            if opts and page.evaluate(
                    "document.getElementById('wd-wizard-root-video-detection') === null"):
                break
            if opts and page.evaluate("window.__audioClicks || 0"):
                break
        clicks = page.evaluate("window.__audioClicks || 0")
        check("完成配置后 opts 里**没有** audioPrompt",
              opts is not None and "audioPrompt" not in opts,
              json.dumps(opts, ensure_ascii=False))
        check("完成配置确实跑到了 applyWizardSnapshot（opts 里有 source）",
              opts is not None and "source" in opts,
              json.dumps(opts, ensure_ascii=False))
        check("「分析音频」按钮从未被点击",
              clicks == 0, f"clicks={clicks}")
        check("没有「启动音频分析失败」弹窗",
              not any("启动音频分析失败" in m for m in dialogs),
              " | ".join(dialogs[:3]))
        # 收尾：关掉测试期间真的打开的视频会话
        page.evaluate(
            """async () => {
                try {
                    await fetch('/api/video-detection/close', {method: 'POST'});
                } catch (_) {}
            }"""
        )

        # ---------- 7. 无 JS 报错 ----------
        print("\n【7】全程无 JS 报错")
        check("pageerror 数为 0", len(errors) == 0, " | ".join(errors[:3]))

        # ---------- 8. 收尾：恢复板子原来的配置 ----------
        # card_count=1 是临时设的，autostart 也可能被 set_active_app 追加过
        # 条目 —— 不恢复就污染了真机。
        print("\n【8】恢复真机配置")
        page.evaluate(
            """async (cfg) => {
                await fetch('/api/compute/config', {
                    method: 'PUT',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        autostart: cfg.autostart,
                        card_count: cfg.card_count,
                    }),
                });
            }""",
            {"autostart": orig_autostart,
             "card_count": orig_cfg.get("card_count")},
        )
        page.wait_for_timeout(500)
        restored = page.evaluate(
            """async () => {
                const r = await fetch('/api/compute/config', {cache:'no-store'});
                if (!r.ok) return {__http_error: r.status};
                return await r.json();
            }"""
        )
        check("card_count 已恢复原值",
              restored.get("card_count") == orig_cfg.get("card_count"),
              f"{restored.get('card_count')} vs {orig_cfg.get('card_count')}")
        # autostart 也要断言 —— 它才是用户真正在意的「开机自启」配置，
        # 之前只查 card_count，配置被写空了也照样报绿。
        check("autostart 已恢复原值",
              (restored.get("autostart") or []) == orig_autostart,
              f"{restored.get('autostart')} vs {orig_autostart}")

        browser.close()

    print("\n" + "=" * 50)
    print(f"通过 {PASS} / {PASS + FAIL}")
    if FAIL:
        print(f"失败 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
