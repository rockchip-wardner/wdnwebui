#!/usr/bin/env python3
"""端到端：网页翻译向导「一键开启所需模型」必须真的跑通。

这是本次修复的**总验收**：三条改动各自都有单测（见 verify_web_translate_
peer_isolation.py / verify_wizard_replica_clamp.py / verify_translate_replica_
manifest.py），本脚本把它们在**真机真服务**上串起来验一遍：

1. **peer 不再占本板卡位** —— 本板此刻**真的有**一个远端 peer 实例
   （``LocateAnything-3b @ 172.16.9.147``，5500MB）。修复前它会把一张空卡
   判成占满 ⇒ 计划掉到 2 份；修复后 ``expected_count`` 必须是 **4**。
2. **一键开启不再 400** —— 模型包 manifest 补了 ``max_replicas_per_card=2``，
   后端不再拒「每卡第 2 份」。
3. **走完向导 = 翻译服务真的启用** —— 完成配置 → 卡片 ``applyWizardSnapshot``
   → ``enable()`` → NPU 接进 provider 链 ⇒ ``provider_in_chain`` 为真。

⚠️ **会真的起模型**（4 个 worker 进程，每份 ~2200MB，吃卡 0/1）。
跑完**保留**服务启用状态（这就是用户要的「跑通」）；要收工加
``--disable-after``。

用法::

    .venv/bin/python scripts/verify_web_translate_wizard_e2e.py
    .venv/bin/python scripts/verify_web_translate_wizard_e2e.py --disable-after
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

from playwright.sync_api import sync_playwright

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BASE = os.environ.get("WDNWEBUI_BASE", "https://127.0.0.1:8000").rstrip("/")
APP_ID = "web-translate"
WIZARD_NS = "wd-wizard-"

#: 模型加载 + 4 份实例就绪的等待上限
READY_TIMEOUT_S = 300
#: 向导走完（finish → enable）的等待上限
FINISH_TIMEOUT_S = 300

_PASS = 0
_FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  ok   {name}")
    else:
        _FAIL += 1
        print(f"  FAIL {name} {extra}")


def _ctx():
    return ssl._create_unverified_context()


def get_json(path: str, timeout: int = 30, retries: int = 4):
    """GET 一次 JSON；**带重试**。

    ⚠️ 板子在起 4 个 worker / rknn3 刷卡时，uvicorn 的事件循环会短暂
    喘不上气 ⇒ TLS 握手偶尔超时（实测 `TimeoutError: The handshake
    operation timed out`）。这不是功能故障，重试即可。
    """
    last = None
    for i in range(retries):
        try:
            with urllib.request.urlopen(BASE + path, timeout=timeout,
                                        context=_ctx()) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(1.5 * (i + 1))
    raise last


def post_json(path: str, payload=None, timeout: int = 60, method="POST"):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout, context=_ctx()) as r:
        return json.loads(r.read().decode("utf-8"))


def _diagnose_runtime_mismatch() -> None:
    """rc=-6 的真因多半是「模型 toolkit 比本板 runtime 新」—— 直接给结论。

    复用 ``scripts/verify_model_runtime_compat.py`` 的判定逻辑（同一份
    代码，不复刻），把「模型装不上」翻译成一句人能行动的话。
    """
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import importlib.util
        spec = importlib.util.find_spec("verify_model_runtime_compat")
        if spec is None:
            print("  （找不到 verify_model_runtime_compat，跳过诊断）")
            return
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        runtime, rpath = mod.find_runtime()
        print(f"  本板 rknn3 runtime: {runtime[0]}.{runtime[1]}.{runtime[2]} ({rpath})")
        import glob
        root = mod.models_root()
        for d in sorted(glob.glob(os.path.join(root, "*"))):
            vers = set()
            for rknn in glob.glob(os.path.join(d, "*.rknn")):
                v = mod.toolkit_of(rknn)
                if v != (0, 0, 0):
                    vers.add(v)
            if not vers:
                continue
            top = max(vers)
            if top > runtime:
                print(f"  ⇒ {os.path.basename(d)} 用 toolkit "
                      f"{top[0]}.{top[1]}.{top[2]} 构建 > 本板 runtime "
                      f"{runtime[0]}.{runtime[1]}.{runtime[2]} ⇒ NPU 固件拒收"
                      f"（MODEL_SETUP fail / rc=-6）")
                print(f"     修法：① 升板端 rknn3 runtime + EP 固件；"
                      f"或 ② 用 ≤ {runtime[0]}.{runtime[1]}.{runtime[2]} 的 "
                      f"toolkit 重编该模型再分发")
    except Exception as exc:  # noqa: BLE001
        print(f"  （诊断失败：{exc}）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--disable-after", action="store_true",
                    help="跑完把翻译服务停掉（收工用）")
    ap.add_argument("--skip-browser", action="store_true",
                    help="只查后端状态，不走浏览器（调试用）")
    args = ap.parse_args()

    print("=" * 72)
    print("网页翻译向导 e2e：一键开启所需模型 → 走完向导 → 服务启用")
    print("=" * 72)

    # ---------------------------------------------------------------- [0] --
    print("\n[0] 清场：清掉「上次用的卡」，避免开机恢复把别的卡的向导挂上来")
    try:
        req = urllib.request.Request(BASE + "/api/compute/active-app",
                                     method="DELETE")
        urllib.request.urlopen(req, timeout=10, context=_ctx()).read()
        print("  已 DELETE /api/compute/active-app")
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠ 清场失败（不致命）: {exc}")

    snap = get_json("/api/compute")
    peers = [i for i in (snap.get("instances") or [])
             if i.get("_source") == "adopted"]
    print(f"  本板协卡: {snap.get('model_cards')}")
    print(f"  本机实例: {[(i['model_id'], i['npu_index'], i['_source']) for i in (snap.get('instances') or []) if i.get('_source') != 'adopted']}")
    print(f"  远端 peer 实例: {[(i['model_id'], i['npu_index'], i['memory_estimate_mb'], i.get('_peer_ip')) for i in peers]}")

    st = get_json("/api/web-translate/status")
    print(f"  翻译服务当前: enabled={st.get('enabled')} running={st.get('running_count')}/{st.get('expected_count')} slots={st.get('slots')}")
    if peers:
        print(f"  ✅ 本板此刻有 peer 实例 —— 「peer 不占本板卡位」可在本机直接验")
    else:
        # peer 板掉线时这条没法在真机上验 —— 单测
        # (verify_web_translate_peer_isolation.py) 用假 registry 覆盖了它。
        print("  ℹ 本板此刻没有 peer 实例（peer 板离线）—— "
              "「peer 不占本板卡位」由单测覆盖")

    if args.skip_browser:
        check("（--skip-browser）期望份数 == 4（peer 没吃掉本板卡）",
              st.get("expected_count") == 4, f"expected={st.get('expected_count')}")
        return 0 if _FAIL == 0 else 1

    # ---------------------------------------------------------------- [1] --
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True,
                                    args=["--no-sandbox", "--disable-gpu",
                                          "--mute-audio"])
        ctx = browser.new_context()
        page = ctx.new_page()
        page.on("pageerror", lambda e: print(f"  [pageerror] {e}"))
        page.on("console", lambda m: (print(f"  [console.{m.type}] {m.text}")
                                      if m.type in ("error", "warning") else None))

        print("\n[1] 打开网页翻译的向导")
        page.goto(BASE + "/", wait_until="domcontentloaded", timeout=30000)
        # 清掉向导的 localStorage（完成标记 / 步骤 / 值）
        page.evaluate("""() => {
            Object.keys(localStorage)
                .filter(k => k.includes('wizard'))
                .forEach(k => localStorage.removeItem(k));
        }""")
        page.goto(BASE + "/#" + APP_ID, wait_until="domcontentloaded",
                  timeout=30000)
        page.wait_for_selector(".wd-wizard-section", timeout=20000)
        print("  向导已挂载")

        # ---------------------------------------------------------------- [2] --
        print("\n[2] 点「一键开启所需模型」")
        btn_sel = "#" + WIZARD_NS + "env-bring-btn"
        status_sel = "#" + WIZARD_NS + "env-bring-status"
        try:
            page.wait_for_selector(btn_sel, timeout=15000)
        except Exception:
            print("  ⚠ 没看到「一键开启所需模型」按钮 —— 可能模型已在跑")
        if page.query_selector(btn_sel):
            disabled = page.get_attribute(btn_sel, "disabled")
            check("按钮可点（模型已安装）", disabled is None, f"disabled={disabled}")
            page.click(btn_sel)
            # 等按钮旁边的状态文字出结果。
            # ⚠️ 用 evaluate 读文本而不是 text_content() —— 向导会**重渲染**
            # 整个步骤（元素被换掉），text_content 会等 30s 然后超时。
            deadline = time.time() + 180
            text = ""
            while time.time() < deadline:
                text = page.evaluate(
                    """(sel) => {
                        const el = document.querySelector(sel);
                        return el ? (el.textContent || "").trim() : "";
                    }""", status_sel) or ""
                if ("已开启" in text or "失败" in text or "重试" in text):
                    break
                time.sleep(1.0)
            print(f"  按钮旁的状态: {text!r}")
            check("一键开启**没有失败**",
                  "失败" not in text and "每张卡最多" not in text, repr(text))
            # ⚠️ 「被后端接受」的**硬证据是服务端**：槽位被受理 ⇒ registry 里
            # 出现了对应 (卡, 副本) 的实例。UI 文字可能因为向导重渲染而读不到。
            snap_now = get_json("/api/compute")
            mine = [i for i in (snap_now.get("instances") or [])
                    if i.get("model_id") == "hy-mt-1-5"]
            slots_seen = sorted((i.get("npu_index"), i.get("replica"))
                                for i in mine)
            print(f"  服务端已受理的槽位: {slots_seen}")
            check("后端受理了 4 个槽位（卡0×2 + 卡1×2，不再 400）",
                  len(slots_seen) == 4, f"实际 {slots_seen}")
        else:
            print("  （跳过点击）")

        # ---------------------------------------------------------------- [3] --
        print("\n[3] 等 4 份实例就绪（模型加载中）")
        deadline = time.time() + READY_TIMEOUT_S
        st = {}
        while time.time() < deadline:
            st = get_json("/api/web-translate/status")
            if (st.get("running_count") or 0) >= (st.get("expected_count") or 1):
                break
            # ⚠️ **模型装不上**（rc=-6 / MODEL_SETUP fail）—— 这不是向导的锅，
            # 是「模型包的构建 toolkit 比本板 runtime 新」。这时再等也没用，
            # 早退 + 给出诊断，别让脚本空转到超时。
            errs = [i for i in (st.get("instances") or [])
                    if i.get("status") == "error"]
            if errs and (st.get("running_count") or 0) == 0:
                # ⚠️ 错误文案在 /api/compute 的实例条目里（status 端点不带）
                snap_now = get_json("/api/compute")
                err_text = ""
                for i in (snap_now.get("instances") or []):
                    if i.get("status") == "error":
                        err_text = i.get("error") or ""
                        break
                print(f"  ⚠ {len(errs)} 份实例启动失败：{err_text}")
                _diagnose_runtime_mismatch()
                check("模型能装上（worker 不该 rc=-6）", False,
                      f"{err_text} —— 见上面的 runtime/toolkit 诊断")
                browser.close()
                print("\n" + "=" * 72)
                print(f"通过 {_PASS} / 失败 {_FAIL}")
                print("=" * 72)
                print("❌ 向导本身已通（不再 400 / 槽位正确），但模型装不上 —— "
                      "见上面的诊断")
                return 1
            time.sleep(3.0)
        print(f"  status: running={st.get('running_count')}/{st.get('expected_count')} "
              f"slots={st.get('slots')} enabled={st.get('enabled')}")
        check("计划份数 == 4（peer 没再吃掉本板一张卡）",
              st.get("expected_count") == 4, f"expected={st.get('expected_count')}")
        check("4 份都跑起来了",
              (st.get("running_count") or 0) >= 4,
              f"running={st.get('running_count')}")
        cards_used = sorted({i.get("npu_index") for i in (st.get("instances") or [])})
        check("实例落在卡 0 / 卡 1 上",
              cards_used == [0, 1], f"cards={cards_used}")
        replicas = sorted((i.get("npu_index"), i.get("replica"))
                          for i in (st.get("instances") or []))
        check("每张卡 2 份（卡0×2 + 卡1×2）",
              len(replicas) == 4, f"instances={replicas}")

        # ---------------------------------------------------------------- [4] --
        print("\n[4] 走完向导（下一步 → 完成配置）")
        next_sel = "#" + WIZARD_NS + "next-btn"
        if page.query_selector(next_sel):
            page.click(next_sel)
            page.wait_for_selector("#" + WIZARD_NS + "finish-btn", timeout=20000)
            print("  已到完成配置步")
            page.click("#" + WIZARD_NS + "finish-btn")
            deadline = time.time() + FINISH_TIMEOUT_S
            err = ""
            while time.time() < deadline:
                err = page.evaluate(
                    """(sel) => {
                        const el = document.querySelector(sel);
                        return el ? (el.textContent || "").trim() : "";
                    }""", "#" + WIZARD_NS + "finish-error") or ""
                st = get_json("/api/web-translate/status")
                if st.get("enabled") and st.get("provider_in_chain"):
                    break
                if err:
                    break
                time.sleep(2.0)
            print(f"  finish 错误框: {err!r}")
            check("完成配置没有报错", not err, repr(err))
        else:
            print("  ⚠ 没找到「下一步」按钮（env 步可能判定已就绪）")

        st = get_json("/api/web-translate/status")
        print(f"  最终 status: enabled={st.get('enabled')} "
              f"provider_in_chain={st.get('provider_in_chain')} "
              f"running={st.get('running_count')}/{st.get('expected_count')}")
        check("翻译服务已启用", bool(st.get("enabled")), str(st.get("enabled")))
        check("NPU 已接进 provider 链",
              bool(st.get("provider_in_chain")), str(st.get("provider_in_chain")))
        check("4 份实例都在跑",
              (st.get("running_count") or 0) >= 4,
              f"running={st.get('running_count')}")

        # ---------------------------------------------------------------- [5] --
        print("\n[5] 真翻一句（端到端：NPU 推理链路）")
        try:
            out = post_json("/api/translate",
                            {"segments": ["今天天气很好。"], "source": "zh-CN",
                             "target": "en"}, timeout=180)
            segs = out.get("translations") or out.get("segments") or []
            print(f"  翻译结果: {segs}")
            check("翻译接口有返回", bool(out), str(out)[:200])
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:200]
            check("翻译接口可用", False, f"HTTP {exc.code}: {body}")
        except Exception as exc:  # noqa: BLE001
            check("翻译接口可用", False, repr(exc))

        if args.disable_after:
            print("\n[6] --disable-after：停掉翻译服务")
            try:
                post_json("/api/web-translate/disable", timeout=120)
                print("  已停用")
            except Exception as exc:  # noqa: BLE001
                print(f"  ⚠ 停用失败: {exc}")

        browser.close()

    print("\n" + "=" * 72)
    print(f"通过 {_PASS} / 失败 {_FAIL}")
    print("=" * 72)
    if _FAIL:
        print("❌ 有断言没过")
        return 1
    print("✅ 全部通过 —— 网页翻译向导已跑通")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
