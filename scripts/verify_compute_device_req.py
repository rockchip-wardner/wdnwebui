#!/usr/bin/env python3
"""回归测试: 算力配置页的**设备拓扑要求**块（EP 协卡 + 本机 NPU）。

背景
----
``requires_cards`` 数的是**本机 EP 协卡**，但有些模型还要占住**第三个
NPU 设备** —— 而那个设备**不是 EP 协卡**（本机 RK3588 自带的 NPU），在
算力卡列表里**看不见**。

Qwen-Image-2.1 就是这种模型: TE/DiT 占 2 张 RK1828，**VAE 必须独占**
第三个 NPU（要么本机 3588 的 NPU，要么第三张 1828 协卡）。两种都不
满足时**不允许运行这个模型**。

本脚本 mock ``/api/compute``，断言卡片上：

* 设备要求块**显示**「需要 N 个 NPU 设备」+ 选中的布局；
* 满足 ⇒ 块不带 ``is-blocked``，toggle **可用**；
* 不满足 ⇒ 块带 ``is-blocked`` + 显示缺什么，toggle **禁用**；
* 单卡模型（``total_devices == 1``）⇒ 块**隐藏**（不打扰普通模型）。

用 playwright 跑（和 ``verify_compute_banners_local_only.py`` 同一套
mock/route 手法）。
"""
import json
import sys

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000"

#: 选中的卡片选择器（按 model_id 定位到具体那张卡）。
CARD = '.cc-service-card[data-model="qwenimage21"]'
REQ = CARD + " .cc-device-req"
TOGGLE = CARD + " .cc-toggle input[type=checkbox]"
#: 「运行在」里代表**第三个设备**（本机 NPU）的固定条目。
FIXED = CARD + " .cc-target-fixed"


def _cards():
    """两张 EP 协卡 —— 凑得够 ``requires_cards = 2``。"""
    return [
        {"npu_index": 0, "device_id": "0001:11:00.0", "name": "RK182x #0"},
        {"npu_index": 1, "device_id": "0003:31:00.0", "name": "RK182x #1"},
    ]


def _req_ok():
    """本机满足「2×1828 + 3588」布局。"""
    return {
        "total_devices": 3,
        "ok": True,
        "reason": "",
        "selected": {
            "id": "2x182x+3588",
            "label": "2×RK1828 协卡 + 本机 RK3588 NPU",
            "ep_cards": 2, "local_npu": 1, "ok": True, "reason": "",
            "total_devices": 3,
            "local_label": "本机 RK3588 NPU（VAE 解码）",
        },
        "layouts": [
            {"id": "2x182x+3588", "label": "2×RK1828 协卡 + 本机 RK3588 NPU",
             "ep_cards": 2, "local_npu": 1, "ok": True, "reason": "",
             "total_devices": 3,
             "local_label": "本机 RK3588 NPU（VAE 解码）"},
            {"id": "3x182x", "label": "3×RK1828 协卡",
             "ep_cards": 3, "local_npu": 0, "ok": False,
             "reason": "需要 3 张 EP 协卡，本机只有 2 张",
             "total_devices": 3},
        ],
    }


def _req_bad():
    """本机**两种布局都不满足**（没有第三张设备）。"""
    return {
        "total_devices": 3,
        "ok": False,
        "reason": ("2×RK1828 协卡 + 本机 RK3588 NPU：本机 NPU 不可用"
                   "（没装 rknn2-runtime）；3×RK1828 协卡：需要 3 张 EP 协卡，"
                   "本机只有 2 张"),
        "selected": None,
        "layouts": [
            {"id": "2x182x+3588", "label": "2×RK1828 协卡 + 本机 RK3588 NPU",
             "ep_cards": 2, "local_npu": 1, "ok": False,
             "reason": "本机 NPU 不可用（没装 rknn2-runtime）",
             "total_devices": 3},
            {"id": "3x182x", "label": "3×RK1828 协卡",
             "ep_cards": 3, "local_npu": 0, "ok": False,
             "reason": "需要 3 张 EP 协卡，本机只有 2 张",
             "total_devices": 3},
        ],
    }


def _req_unverified():
    """卡枚举失败（``/dev/rknn3`` 被 worker 占住）⇒ **未验证**，不是「不满足」。"""
    return {
        "total_devices": 3,
        "ok": True,
        "reason": "",
        "unverified": True,
        "note": "NPU 卡枚举失败（RKNN probe timeout），暂时无法确认设备拓扑是否满足",
        "selected": None,
        "layouts": [],
    }


def svc(model_id="qwenimage21", *, req=None, total_devices=3,
        requires_cards=2, available_targets=("182x",), status="not_loaded"):
    """构造最小 service dict。"""
    return {
        "model_id": model_id,
        "status": status,
        "_source": "spawned",
        "npu_index": 0,
        "npu_indices": [],
        "started_at": 0.0,
        "queued_at": 0.0,
        "loaded_at": None,
        "load_time_sec": None,
        "label": "[文生图]Qwen-Image-2.1",
        "error": "",
        "memory_estimate_mb": 10200,
        "capability_types": ["swcap_text_to_image"],
        "description": "mock",
        "requires_cards": requires_cards,
        "requires_devices": total_devices,
        "card_requirements": req,
        "available_targets": list(available_targets),
    }


def mock_compute(services, cards=None):
    cards = _cards() if cards is None else cards
    return {
        "cards": cards,
        "model_cards": [c["npu_index"] for c in cards],
        "cards_failed": "",
        "supported_models": [],
        "services": services,
        "instances": [],
        "running_count": 0,
        "queued_count": 0,
        "loaded_count": 0,
        "error_count": 0,
        "estimated_init_seconds_per_model": {},
        "default_estimated_init_seconds": 30,
        "worker_alive": True,
        "last_updated": 1.0,
        "capabilities": [],
        "tools": [],
    }


def read_state(page):
    """读设备要求块 + toggle 的状态。"""
    return page.evaluate("""([reqSel, tgSel, fixedSel]) => {
        const req = document.querySelector(reqSel);
        const tg  = document.querySelector(tgSel);
        const fx  = Array.from(document.querySelectorAll(fixedSel));
        return {
            reqExists: !!req,
            // 只看 inline display —— offsetParent 在「整页 section 处于
            // 隐藏态」时恒为 null，会把「已渲染」误判成「不可见」。
            reqVisible: !!req && req.style.display !== "none",
            reqText: req ? req.textContent.replace(/\\s+/g, " ").trim() : "",
            reqBlocked: !!req && req.classList.contains("is-blocked"),
            toggleExists: !!tg,
            toggleDisabled: !!tg && tg.disabled,
            toggleTitle: tg ? (tg.closest(".cc-toggle")?.title || "") : "",
            toggleAria: tg ? (tg.getAttribute("aria-label") || "") : "",
            // 「运行在」里的第三个设备（本机 NPU，固定不可勾选）
            fixedCount: fx.length,
            fixedText: fx.map(e => e.textContent.replace(/\\s+/g, " ").trim()).join(" | "),
            epOptCount: document.querySelectorAll(".cc-target-opt").length,
        };
    }""", [REQ, TOGGLE, FIXED])


def run_scenario(page, services):
    """挂 mock → **整页重载** → 等渲染完成 → 读状态。

    必须整页重载（``about:blank`` → 目标 hash）：hash-only 的 goto 不
    重载文档，场景之间只靠 1Hz tick 补渲染 —— 那一拍没赶上就会断言到
    **上一个场景**的状态。

    等待条件用「capability chip 已渲染」而不是固定 sleep：``_updateLocalCardState``
    先填 caps、再填设备要求块 ⇒ chip 出现即代表设备块那一轮已跑完。
    固定 sleep 在**首个场景**（浏览器冷启动、30+ 脚本全量下载）会偶发
    抢跑 —— 实测同一脚本连跑两次，一次绿一次红。
    """
    body = json.dumps(mock_compute(services))
    page.unroute("**/api/compute")
    page.route("**/api/compute", lambda route: route.fulfill(
        status=200, headers={"Content-Type": "application/json"}, body=body))
    page.goto("about:blank")
    page.goto(f"{BASE}/#compute-config", wait_until="domcontentloaded",
              timeout=15000)
    page.wait_for_selector("#page-compute-config", state="attached", timeout=8000)
    try:
        page.wait_for_function(
            """() => {
                const c = document.querySelector('.cc-service-card');
                return !!(c && c.querySelector('.cc-service-cap'));
            }""", timeout=10000)
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] 等待渲染超时（继续断言）: {e}")
    page.wait_for_timeout(300)
    return read_state(page)


SCENARIOS = [
    ("1. 满足：显示「需要 3 个 NPU 设备」+ 选中布局，toggle 可用",
     [svc(req=_req_ok())],
     {"visible": True, "blocked": False, "toggle_disabled": False,
      "must_have": ["需要 3 个 NPU 设备", "2×RK1828 协卡 + 本机 RK3588 NPU"],
      "must_not_have": ["本机 NPU 不可用"],
      "fixed_count": 1, "fixed_must_have": ["本机 RK3588 NPU（VAE 解码）"]}),

    ("2. 不满足：显示缺什么 + toggle 被禁用",
     [svc(req=_req_bad())],
     {"visible": True, "blocked": True, "toggle_disabled": True,
      "must_have": ["需要 3 个 NPU 设备", "本机 NPU 不可用", "3×RK1828 协卡"],
      "must_not_have": [],
      "fixed_count": 0, "fixed_must_have": []}),

    ("3. 单卡模型（total_devices=1）⇒ 不显示设备要求块",
     [svc(model_id="single-card-model", req=None, total_devices=1,
          requires_cards=1)],
     {"visible": False, "blocked": False, "toggle_disabled": False,
      "must_have": [], "must_not_have": ["需要"],
      "fixed_count": 0, "fixed_must_have": []}),

    # ---- 卡枚举失败（/dev/rknn3 被 worker 占住）⇒ **不能**判「硬件
    #      不满足」：一次 driver 争用不该让模型再也起不来。
    ("4. 枚举失败（unverified）⇒ 如实说「无法确认」，但不拦",
     [svc(req=_req_unverified())],
     {"visible": True, "blocked": False, "toggle_disabled": False,
      "must_have": ["需要 3 个 NPU 设备", "NPU 卡枚举失败"],
      "must_not_have": ["本机硬件不满足"],
      "fixed_count": 0, "fixed_must_have": []}),
]


def main() -> int:
    ok_count = 0
    failed = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True,
                                    args=["--no-sandbox", "--disable-gpu"])
        for name, services, expects in SCENARIOS:
            print(f"\n=== {name} ===")
            ctx = browser.new_context()
            page = ctx.new_page()
            page.on("pageerror", lambda e: print(f"  [pageerror] {e}"))
            try:
                st = run_scenario(page, services)
                print(f"  visible={st['reqVisible']} blocked={st['reqBlocked']} "
                      f"toggleDisabled={st['toggleDisabled']}")
                print(f"  text: {st['reqText'][:180]}")
                print(f"  「运行在」EP卡选项={st['epOptCount']} 固定设备={st['fixedCount']}"
                      f" {st['fixedText'][:90]}")
                ok = True
                if st["reqVisible"] != expects["visible"]:
                    print(f"  ❌ visible 期望 {expects['visible']}，实际 {st['reqVisible']}")
                    ok = False
                if st["reqBlocked"] != expects["blocked"]:
                    print(f"  ❌ is-blocked 期望 {expects['blocked']}，实际 {st['reqBlocked']}")
                    ok = False
                if st["toggleDisabled"] != expects["toggle_disabled"]:
                    print(f"  ❌ toggle.disabled 期望 {expects['toggle_disabled']}，"
                          f"实际 {st['toggleDisabled']}")
                    ok = False
                if "fixed_count" in expects and st["fixedCount"] != expects["fixed_count"]:
                    print(f"  ❌ 「运行在」里的第三个设备条目数 期望 "
                          f"{expects['fixed_count']}，实际 {st['fixedCount']}")
                    ok = False
                for kw in expects.get("fixed_must_have", []):
                    if kw not in st["fixedText"]:
                        print(f"  ❌ 「运行在」第三个设备缺少文案 {kw!r}")
                        ok = False
                for kw in expects["must_have"]:
                    if kw not in st["reqText"]:
                        print(f"  ❌ 设备要求块缺少文案 {kw!r}")
                        ok = False
                for kw in expects["must_not_have"]:
                    if kw in st["reqText"]:
                        print(f"  ❌ 设备要求块不该出现 {kw!r}")
                        ok = False
                if expects["toggle_disabled"]:
                    if "设备要求" not in st["toggleTitle"]:
                        print(f"  ❌ toggle title 未说明设备要求：{st['toggleTitle']!r}")
                        ok = False
                    if "NPU 设备" not in st["toggleAria"]:
                        print(f"  ❌ toggle aria-label 未说明设备数：{st['toggleAria']!r}")
                        ok = False
                if ok:
                    print("  ✅ matches expectations")
                    ok_count += 1
                else:
                    failed.append(name)
            except Exception as e:  # noqa: BLE001
                print(f"  ❌ scenario failed: {e}")
                failed.append(name)
            ctx.close()
        browser.close()

    print(f"\n=== 总结: {ok_count}/{len(SCENARIOS)} ===")
    if failed:
        print(f"❌ 失败场景: {failed}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
