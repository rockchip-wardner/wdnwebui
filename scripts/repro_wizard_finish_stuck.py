#!/usr/bin/env python3
"""复现：向导里点了「一键开启模型」→ 另一个页面把模型全关 → 回向导卡死。

⚠️ **会停掉本机全部模型**（前置 + tab B 各一次 ``POST /api/compute/stop-all``）。
   跑完必须 ``sudo systemctl restart wdnwebui.service`` 才能把开机自启的
   模型拉回来。板子上还有别人在用的时候别跑。

用户实测序列（视频目标定位 / vtl）：
  1. 打开 vtl 应用 → 自动进向导模式（env 步）
  2. 点「一键开启所需模型」→ LocateAnything-3b 在 NPU0/NPU1 后台加载
  3. **另开一个网页**（同源另一个 tab）→ 算力配置页 → 「全部停止」
  4. 回到第 1 个 tab → 走到「完成配置」→ 点完成
  → 卡在「完成配置」页等模型加载，回不去也出不来

本脚本按这个序列走一遍，把每一步的 DOM / console / 耗时都记下来，
用来定位**真正的卡点**（是 finish 的等待没超时？还是 wizard 被卸载后
DOM 还在？还是 nav 锁死？）。

**修复后**（v0.28）本脚本的预期输出：finish 不再空等 90s，而是在
模型缺席 15s / 出现 error 终态时**立即中止**并给出「重新开启模型」按钮。
若又看到「+90s 后才报错」，说明修复回退了。

用法：.venv/bin/python scripts/repro_wizard_finish_stuck.py
"""
from __future__ import annotations

import sys
import time

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000"
APP = "video-target-localization"


def log(msg: str) -> None:
    print(msg, flush=True)


def wait_wizard(page, timeout=25000):
    page.wait_for_selector("#wd-wizard-root-" + APP, state="attached", timeout=timeout)
    return True


def step_title(page) -> str:
    try:
        return page.eval_on_selector(
            "#wd-wizard-root-%s .wd-wizard-step-title" % APP, "e => e.textContent")
    except Exception:
        return "(无标题)"


def click_next(page) -> bool:
    btn = page.query_selector("#wd-wizard-root-%s .wd-wizard-foot .wd-wizard-next" % APP)
    if btn is None:
        btn = page.query_selector("#wd-wizard-root-%s button:has-text('下一步')" % APP)
    if btn is None:
        return False
    if btn.is_disabled():
        log("      (下一步 disabled)")
        return False
    btn.click()
    return True


def main() -> int:
    errors_a: list[str] = []
    logs_a: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1600, "height": 1100})

        # ---------- 前置：把本机模型全停掉（env 步只有模型没跑时才显示
        # 「一键开启所需模型」按钮） ----------
        b0 = ctx.new_page()
        b0.goto(f"{BASE}/#compute-config", wait_until="domcontentloaded")
        b0.wait_for_timeout(2000)
        b0.evaluate("""async () => {
            await fetch('/api/compute/stop-all', {method:'POST'});
        }""")
        b0.wait_for_timeout(3000)
        b0.close()
        log("[0] 前置：本机模型已全部停止")

        # ---------- tab A：向导 ----------
        a = ctx.new_page()
        a.on("pageerror", lambda e: errors_a.append("A: " + str(e)))
        a.on("console", lambda m: logs_a.append(f"A[{m.type}] {m.text}"))
        a.goto(f"{BASE}/#{APP}", wait_until="domcontentloaded", timeout=30000)
        wait_wizard(a)
        a.wait_for_timeout(1500)
        log(f"[1] tab A 进向导，当前步骤 = {step_title(a)!r}")

        # ---------- 点一键开启 ----------
        bring = a.query_selector("#wd-wizard-env-bring-btn")
        if bring is None:
            bring = a.query_selector("button:has-text('一键开启')")
        if bring is None:
            log("[2] !! 找不到「一键开启所需模型」按钮")
            return 2
        log("[2] 点「一键开启所需模型」")
        bring.click()
        a.wait_for_timeout(4000)
        log(f"    当前步骤 = {step_title(a)!r}")
        try:
            log(f"    env-bring-status = "
                f"{a.eval_on_selector('#wd-wizard-env-bring-status', 'e=>e.textContent')!r}")
        except Exception:
            log("    env-bring-status = (已随步骤切走而移除)")

        # ---------- tab B：算力配置页，全部停止 ----------
        b = ctx.new_page()
        b.goto(f"{BASE}/#compute-config", wait_until="domcontentloaded", timeout=30000)
        b.wait_for_timeout(2500)
        stop_all = b.query_selector("button:has-text('全部停止')")
        if stop_all is None:
            log("[3] !! 找不到「全部停止」按钮")
        else:
            log("[3] tab B 点「全部停止」")
            stop_all.click()
            b.wait_for_timeout(4000)
        # 算力页把模型停了之后，本机应当 0 个 running
        try:
            snap = b.evaluate("""async () => {
                const r = await fetch('/api/compute', {cache:'no-store'});
                const d = await r.json();
                return (d.instances||[]).filter(i=>i.status==='running')
                          .map(i=>i.model_id+'@'+i.npu_index);
            }""")
            log(f"    停止后本机 running = {snap}")
        except Exception as e:
            log(f"    (读快照失败 {e})")
        b.close()

        # ---------- 回到 tab A，一路走到 finish ----------
        a.bring_to_front()
        a.wait_for_timeout(500)
        log(f"[4] 回到 tab A，当前步骤 = {step_title(a)!r}")
        for i in range(6):
            t = step_title(a)
            if "完成配置" in t:
                log(f"    已到 finish（第 {i} 次点击后）")
                break
            if not click_next(a):
                log(f"    停在 {t!r}，点不了下一步")
                break
            a.wait_for_timeout(1200)
        log(f"    当前步骤 = {step_title(a)!r}")

        # ---------- 点完成配置，观察 ----------
        finish = a.query_selector("#wd-wizard-finish-btn")
        if finish is None:
            log("[5] !! 找不到「完成配置」按钮")
        else:
            log("[5] 点「完成配置」")
            t0 = time.time()
            finish.click()
            # 观察 100s：finish 的等待上限是 90s
            for i in range(20):
                a.wait_for_timeout(5000)
                el = a.query_selector("#wd-wizard-finish-error")
                txt = el.text_content() if el else None
                try:
                    dis = finish.is_disabled()
                except Exception:
                    dis = "(按钮已脱离 DOM)"
                log(f"    +{int(time.time()-t0):>3}s  finish-error = {txt!r}"
                    f"  btn_disabled={dis}")
                if txt and ("超时" in txt or "失败" in txt):
                    log(f"    → {int(time.time()-t0)}s 后有结论")
                    break
            # 此时能不能回退 / 退出？
            names = a.eval_on_selector_all(
                f"#wd-wizard-root-{APP} button",
                "els => els.map(e => (e.textContent||'').trim()"
                " + (e.disabled ? '(disabled)' : ''))")
            log(f"    finish 页可见按钮 = {names}")
            # 走完超时后，能不能离开向导进应用页？
            a.goto(f"{BASE}/#compute-config", wait_until="domcontentloaded")
            a.wait_for_timeout(1500)
            a.goto(f"{BASE}/#{APP}", wait_until="domcontentloaded")
            a.wait_for_timeout(3000)
            try:
                t2 = step_title(a)
                log(f"    离开再回来 → 向导步骤 = {t2!r}")
                names2 = a.eval_on_selector_all(
                    f"#wd-wizard-root-{APP} button",
                    "els => els.map(e => (e.textContent||'').trim()"
                    " + (e.disabled ? '(disabled)' : ''))")
                log(f"    回来后可见按钮 = {names2}")
            except Exception as e:
                log(f"    (再回来失败 {e})")
            log(f"    wizard DOM 还在 = "
                f"{a.query_selector('#wd-wizard-root-' + APP) is not None}")
            log(f"    页面 hash = {a.evaluate('location.hash')!r}")

        log("")
        log(f"tab A pageerror = {errors_a[:5]}")
        interesting = [x for x in logs_a if "wizard" in x.lower() or "error" in x.lower()]
        log(f"tab A console(相关) = {interesting[-12:]}")
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
