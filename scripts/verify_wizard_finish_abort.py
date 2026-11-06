#!/usr/bin/env python3
"""回归：向导「完成配置」等模型时，遇**终态**必须立即中止，不能空等 90s。

用户实测的死锁（视频目标定位 / vtl）：

  1. 向导 env 步点「一键开启模型」
  2. **另开一个页面**把模型全关
  3. 回到向导点「完成配置」→ 一直卡在「等待模型加载完毕」，
     回不到第 1 步、进不了应用页 —— 该应用永远不可用

根因：``_waitForModelsRunning`` 只在「全部 running」或「90s 超时」时才
收敛。模型被关掉后实例要么从 registry 消失（缺席），要么 status 变
``error``/``not_loaded``（终态）—— 两种都让等待永远等下去。

本脚本守的不变量（全部用 mock ``/api/compute`` 驱动，**不碰真机算力**）：

  A. 实例缺席 → 宽限期（15s）内中止，错误文案含「未运行」，
     且出现「重新开启模型」按钮（用户有出口，不是死路）
  B. 实例 status=error（worker 挂了）→ **立即**中止（不等宽限期）
  C. 等待期间离开向导 → **不许**调 applyWizardSnapshot
     （模型没就绪就把应用拉起来 = 半个死锁）
  D. 「重新开启模型」可恢复：点它 → 重新 POST → 模型 running → 走完 finish
  E. 用户方案：切走 / 「跳过向导」后再进该卡 → 回到向导**第 1 步**
  F. 不破坏既有语义：reload 仍恢复上次停留的步骤

用法::

    .venv/bin/python scripts/verify_wizard_finish_abort.py
"""
import json
import sys
import time
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000"
APP = "video-target-localization"
SWCAP = "swcap_vision_grounding"
MODEL = "LocateAnything-3b"
NS = "ImApp_video_target_localization"

# 缺席宽限期（state.js 的 MISSING_GRACE_MS）+ 轮询余量
MISSING_GRACE_MS = 15_000
ABORT_WAIT_MS = MISSING_GRACE_MS + 12_000


def inst(npu, status, source="spawned", err=""):
    d = {
        "model_id": MODEL,
        "npu_index": npu,
        "status": status,
        "_source": source,
        "capability_types": [SWCAP],
        "started_at": time.time(),
    }
    if err:
        d["error"] = err
    return d


def snapshot(instances):
    return {
        "instances": instances,
        "cards": [],
        "cards_failed": "",
        "card_count": 2,
        "model_cards": [],
        "estimated_init_seconds_per_model": {MODEL: 30},
        "default_estimated_init_seconds": 30,
    }


class Ctl:
    """mock /api/compute 的可变状态。"""

    def __init__(self):
        self.instances = [inst(0, "running"), inst(1, "running")]
        self.posts = 0
        self.deletes = 0

    def body(self):
        return json.dumps(snapshot(self.instances))


def install_routes(page, ctl):
    """mock /api/compute（GET 快照）与 /api/compute/instances（POST/DELETE）。

    POST 命中即把 mock 切回「running」—— 模拟「重新开启模型」真的把模型
    拉起来了，脚本随后能验证 finish 能正常走完。
    """
    def on_compute(route):
        route.fulfill(status=200,
                      headers={"Content-Type": "application/json"},
                      body=ctl.body())

    def on_instances(route):
        req = route.request
        if req.method == "POST":
            ctl.posts += 1
            ctl.instances = [inst(0, "running"), inst(1, "running")]
            route.fulfill(status=200,
                          headers={"Content-Type": "application/json"},
                          body=json.dumps({"ok": True, "instances": [],
                                           "errors": []}))
        else:
            ctl.deletes += 1
            ctl.instances = []
            route.fulfill(status=200,
                          headers={"Content-Type": "application/json"},
                          body=json.dumps({"ok": True}))

    page.route("**/api/compute/instances", on_instances)
    page.route("**/api/compute", on_compute)


def wipe_state(page):
    page.evaluate("""(id) => {
        const base = 'wdnwebui.wizard.' + id + '.';
        for (const k of Object.keys(localStorage)) {
            if (k.startsWith(base)) localStorage.removeItem(k);
        }
    }""", APP)


def step_index(page) -> int:
    # **必须限定在本卡的向导容器内** —— `#wd-wizard-stepper` 是全局 id,
    # 页面里若同时存在两个向导容器(框架自动进的那张卡 + 本卡),用
    # document 级选择器会读到**另一个**向导的 stepper,步骤号恒为 0
    # (症状:「没走到第 2 步」偶发失败,重跑又过)。
    return page.evaluate("""(id) => {
        const root = document.getElementById('wd-wizard-root-' + id);
        if (!root) return -1;
        const a = root.querySelectorAll('.wd-wizard-step-dot');
        for (let i = 0; i < a.length; i++)
            if (a[i].classList.contains('wd-wizard-step-active')) return i;
        return -1;
    }""", APP)


def step_title(page) -> str:
    try:
        return page.eval_on_selector(
            f"#wd-wizard-root-{APP} .wd-wizard-step-title", "e => e.textContent")
    except Exception:
        return "(无标题)"


def enter_card(page) -> None:
    page.evaluate("(id) => { window.location.hash = '#' + id; }", APP)
    page.wait_for_selector(f"#wd-wizard-root-{APP}:not(.hidden)", timeout=15000)
    # mount() 里容器先 remove("hidden")，**之后**才 await _probeEnv 再渲染
    # stepper —— 只等容器会读到空壳，必须等圆点真渲染出来。
    page.wait_for_selector("#wd-wizard-stepper .wd-wizard-step-dot", timeout=15000)
    page.wait_for_timeout(200)


def click_next(page) -> bool:
    btn = page.query_selector(f"#wd-wizard-root-{APP} .wd-wizard-foot .wd-wizard-next")
    if btn is None:
        btn = page.query_selector(f"#wd-wizard-root-{APP} button:has-text('下一步')")
    if btn is None or btn.is_disabled():
        return False
    btn.click()
    return True


def foot_labels(page):
    sel = "#wd-wizard-root-%s button" % APP
    return page.eval_on_selector_all(
        sel, "els => els.map(e => (e.textContent || '').trim())")


def walk_to_finish(page) -> None:
    """env → … → finish（env 步的「下一步」要求模型 running，mock 已给）。"""
    for _ in range(8):
        if "完成配置" in step_title(page):
            return
        if not click_next(page):
            raise AssertionError(
                "点不了下一步，停在 %r（foot=%r）"
                % (step_title(page), foot_labels(page)))
        page.wait_for_timeout(250)
    raise AssertionError("没走到 finish 步，停在 %r" % step_title(page))


def stub_apply(page) -> None:
    """把 applyWizardSnapshot 换成计数桩 —— 真跑会开解码器，没必要。"""
    page.evaluate("""(ns) => {
        window.__applyCalls = 0;
        window[ns] = window[ns] || {};
        const real = window[ns].applyWizardSnapshot;
        window[ns].applyWizardSnapshot = async (opts) => {
            window.__applyCalls++;
            window.__applyOpts = opts;
            if (real) return undefined;   // 不调真的
        };
    }""", NS)


def apply_calls(page) -> int:
    return page.evaluate("() => window.__applyCalls || 0")


def finish_error_text(page) -> str:
    try:
        return page.eval_on_selector(
            f"#wd-wizard-root-{APP} #wd-wizard-finish-error", "e => e.textContent")
    except Exception:
        return ""


def wait_finish_error(page, timeout_ms) -> str:
    """等 finish 错误框出现结论（文案变了就算）。"""
    t0 = time.time()
    while (time.time() - t0) * 1000 < timeout_ms:
        txt = finish_error_text(page)
        if txt and ("未运行" in txt or "启动失败" in txt or "超时" in txt
                    or "离开" in txt):
            return txt
        page.wait_for_timeout(300)
    return finish_error_text(page)


# --------------------------------------------------------------------- #
# 场景
# --------------------------------------------------------------------- #

def scen_a_missing(page, ctl):
    """A. 模型被关掉（实例从 registry 消失）→ 宽限期内中止 + 给出口。"""
    enter_card(page)
    walk_to_finish(page)
    stub_apply(page)
    ctl.instances = []                       # 模型被另一个页面关掉了
    page.click("#wd-wizard-finish-btn")
    txt = wait_finish_error(page, ABORT_WAIT_MS)
    if "未运行" not in txt:
        raise AssertionError(f"缺席应报「未运行」，实际 {txt!r}")
    if page.query_selector("#wd-wizard-finish-retry-btn") is None:
        raise AssertionError("失败后没有「重新开启模型」按钮")
    btn = page.query_selector("#wd-wizard-finish-btn")
    if btn.is_disabled():
        raise AssertionError("中止后「完成配置」按钮应恢复可用")
    return f"缺席 → {int((MISSING_GRACE_MS)/1000)}s 内中止：{txt.strip()[:60]}"


def scen_b_error(page, ctl):
    """B. worker 挂了（status=error）→ 立即中止，不等宽限期。"""
    enter_card(page)
    walk_to_finish(page)
    stub_apply(page)
    ctl.instances = [inst(0, "error", err="compute_worker exited rc=-6"),
                     inst(1, "running")]
    t0 = time.time()
    page.click("#wd-wizard-finish-btn")
    txt = wait_finish_error(page, 10_000)
    dt = (time.time() - t0) * 1000
    if "启动失败" not in txt:
        raise AssertionError(f"error 终态应报「启动失败」，实际 {txt!r}")
    if dt > 8_000:
        raise AssertionError(f"error 终态应立即中止，实际用了 {dt:.0f}ms")
    if page.query_selector("#wd-wizard-finish-retry-btn") is None:
        raise AssertionError("失败后没有「重新开启模型」按钮")
    return f"error 终态 → {dt:.0f}ms 内中止：{txt.strip()[:60]}"


def scen_c_leave(page, ctl):
    """C. 等待期间点「上一步」离开 finish 步 → 不许调 applyWizardSnapshot。"""
    enter_card(page)
    walk_to_finish(page)
    stub_apply(page)
    ctl.instances = [inst(0, "loading"), inst(1, "loading")]
    page.click("#wd-wizard-finish-btn")
    page.wait_for_timeout(1500)              # 进入等待循环
    back = page.query_selector(f"#wd-wizard-root-{APP} button:has-text('上一步')")
    if back is None:
        raise AssertionError("finish 步没有「上一步」按钮")
    back.click()
    page.wait_for_timeout(1500)
    if apply_calls(page) != 0:
        raise AssertionError("离开向导后仍调了 applyWizardSnapshot")
    if "完成配置" in step_title(page):
        raise AssertionError("「上一步」没退掉")
    return f"等待中「上一步」→ applyWizardSnapshot 调用数 = {apply_calls(page)}"


def scen_d_retry(page, ctl):
    """D. 点「重新开启模型」→ 重新 POST → 模型 running → finish 走完。"""
    enter_card(page)
    walk_to_finish(page)
    stub_apply(page)
    ctl.instances = []
    page.click("#wd-wizard-finish-btn")
    txt = wait_finish_error(page, ABORT_WAIT_MS)
    if "未运行" not in txt:
        raise AssertionError(f"前置条件不成立：{txt!r}")
    before = ctl.posts
    page.click("#wd-wizard-finish-retry-btn")
    # POST 命中后 mock 切回 running → 第二次 _finish 应当顺利走完
    for _ in range(40):
        if apply_calls(page) > 0:
            break
        page.wait_for_timeout(300)
    if ctl.posts != before + 1:
        raise AssertionError(f"「重新开启模型」没发 POST（posts={ctl.posts}）")
    if apply_calls(page) != 1:
        raise AssertionError("重试后没走完 finish（applyWizardSnapshot 未调用）")
    if page.query_selector(f"#wd-wizard-root-{APP}") is not None:
        raise AssertionError("finish 成功后向导没卸载")
    return "「重新开启模型」→ POST 1 次 → finish 走完并卸载向导"


def scen_e_navaway(page, ctl):
    """E. 用户方案：切到别的页面后再进该卡 → 回到第 1 步。"""
    enter_card(page)
    if step_index(page) != 0:
        raise AssertionError(f"首次进入应落在第 1 步，实际 {step_index(page)}")
    ok = click_next(page)                   # → 第 2 步
    page.wait_for_timeout(300)
    if step_index(page) != 1:
        btns = page.eval_on_selector_all(
            f"#wd-wizard-root-{APP} button",
            "els => els.map(e => e.id + ':' + e.textContent.trim()"
            " + (e.disabled ? '(disabled)' : ''))")
        raise AssertionError(
            f"没走到第 2 步，实际 {step_index(page)}"
            f"（click_next={ok}）buttons={btns}")
    page.evaluate("() => { window.location.hash = '#compute-config'; }")
    page.wait_for_timeout(1200)
    page.evaluate("(id) => { window.location.hash = '#' + id; }", APP)
    page.wait_for_timeout(1200)
    enter_card(page)
    got = step_index(page)
    if got != 0:
        raise AssertionError(f"切走后再进来应回到第 1 步，实际 {got}")
    return f"切走再进来 → 步骤 = {got}（第 1 步）"


def scen_f_skip(page, ctl):
    """E2. 「跳过向导」后再进该卡 → 回到第 1 步。"""
    enter_card(page)
    click_next(page)
    page.wait_for_timeout(300)
    page.click(f"#wd-wizard-root-{APP} .wd-wizard-foot .wd-wizard-skip-btn")
    page.wait_for_timeout(800)
    # 「跳过向导」后向导已卸载、回到应用页 —— hash 没变不会再触发路由，
    # 要模拟「用户切走再回来」必须先离开本卡。
    page.evaluate("() => { window.location.hash = '#compute-config'; }")
    page.wait_for_timeout(1200)
    enter_card(page)
    got = step_index(page)
    if got != 0:
        raise AssertionError(f"跳过向导后再进来应回到第 1 步，实际 {got}")
    return f"跳过向导再进来 → 步骤 = {got}（第 1 步）"


def scen_g_reload(page, ctl):
    """F. 不破坏既有语义：reload 仍恢复上次停留的步骤。"""
    enter_card(page)
    click_next(page)
    page.wait_for_timeout(300)
    page.reload(wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    enter_card(page)
    got = step_index(page)
    if got != 1:
        raise AssertionError(f"reload 后应恢复到第 2 步，实际 {got}")
    return f"reload → 步骤 = {got}（第 2 步，位置仍恢复）"


SCENARIOS = [
    ("A. 模型被关掉（实例缺席）→ 中止 + 给出口", scen_a_missing),
    ("B. worker 挂了（status=error）→ 立即中止", scen_b_error),
    ("C. 等待期间离开向导 → 不调 applyWizardSnapshot", scen_c_leave),
    ("D. 「重新开启模型」→ 可恢复", scen_d_retry),
    ("E. 切走后再进该卡 → 回到第 1 步", scen_e_navaway),
    ("E2.「跳过向导」后再进该卡 → 回到第 1 步", scen_f_skip),
    ("F. reload 仍恢复上次步骤（既有语义不破）", scen_g_reload),
]


def main() -> int:
    ok = 0
    failed = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True,
                                    args=["--no-sandbox", "--disable-gpu", "--mute-audio"])
        for name, fn in SCENARIOS:
            print(f"\n=== {name} ===", flush=True)
            ctx = browser.new_context(viewport={"width": 1400, "height": 900})
            page = ctx.new_page()
            page.on("pageerror", lambda e: print(f"  [pageerror] {e}"))
            ctl = Ctl()
            try:
                # mock 必须在 goto **之前**装好 —— 框架在页面加载时就会
                # 自动进入「上次打开的卡片」，向导 mount 里的 _probeEnv
                # 会立刻拉 /api/compute。route 装晚了那次请求就打到真
                # 后端了(板子上真在跑的模型会污染 swMissing / swOK)。
                install_routes(page, ctl)
                page.goto(BASE, wait_until="domcontentloaded", timeout=20000)
                page.wait_for_function(
                    "() => window.__APP_MANIFESTS__ && window.ROUTER"
                    " && window.WizardRegistry && window.WizardState",
                    timeout=15000)
                wipe_state(page)
                msg = fn(page, ctl)
                print(f"  ✅ {msg}")
                ok += 1
            except Exception as exc:  # noqa: BLE001
                print(f"  ❌ {exc}")
                failed.append(name)
            finally:
                ctx.close()
        browser.close()

    print(f"\n=== 总结: {ok}/{len(SCENARIOS)} ===")
    if failed:
        print(f"❌ 失败: {failed}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
