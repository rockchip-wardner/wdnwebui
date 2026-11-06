#!/usr/bin/env python3
"""回归：向导「一键开启所需模型」必须**先腾出目标卡**，再启新模型。

用户实测 BUG（音视频描述 → 视频目标监控）：vd 卡一键开启后
[VLM]视觉描述v3 在 NPU0、[Omni]多感知描述v2.5 在 NPU1，**都还在启动
中**；马上返回 → vtd 卡一键开启（要 qwen3-vl-4b 同时占 NPU0+NPU1）
→ 最后一页「完成配置」等模型启动完成时失败报错。

根因两条（缺一不可）：

  A. 前端腾卡时**跳过**正在启动中的实例 → omni 继续占着 NPU1 显存 →
     qwen3-vl-4b 启到 NPU1 时显存不足 → worker rc=-6 起不来。
     现在后端改成「排队等它加载完 → 再停」，DELETE 返回时卡已腾空。
  B. 后端 POST **部分失败也返 200**（`ok: not errors`）→ 前端只看 HTTP
     状态码 → 误报「✓ 已开启」，用户走到完成配置页才炸。

本脚本守住五条（全 mock，不碰真机）：

  C1 非必需的 **loading** 实例也要发 DELETE（腾卡，否则显存腾不出来）
  C2 本应用需要的卡位不被 DELETE
  C3 DELETE 必须发生在 POST **之前**（卡腾空了才启新模型）
  C4 POST 返回 200 + ok:false → 一键开启判**失败**（不误报成功）
  C5 POST 返回 200 + ok:true → 判成功

用法：.venv/bin/python scripts/verify_wizard_bringup_loading_card.py
"""
from __future__ import annotations

import json
import sys

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000"

FAILED: list[str] = []
PASSED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"  ✓ {name}" + (f"  ({detail})" if detail else ""))
    else:
        FAILED.append(name)
        print(f"  ✗ {name}" + (f"  ({detail})" if detail else ""))


def _inst(model_id, npu, caps, status="running", source="spawned"):
    return {
        "model_id": model_id, "npu_index": npu, "status": status,
        "_source": source, "capability_types": caps, "transition": "",
    }


# 用户场景：vd 刚启的两个模型都还在 loading 中
#   NPU0: qwen3-vl-4b      → vtd **也要**它（NPU0/NPU1 两张卡）→ 不动
#   NPU1: qwen2.5-omni-3b  → vtd 不要 → 必须腾掉（否则显存不够）
LOADING_SCENE = [
    _inst("qwen3-vl-4b", 0, ["swcap_vision_chat"], status="loading"),
    _inst("qwen2.5-omni-3b", 1, ["swcap_audio_chat"], status="loading"),
]

# vtd 的 swcap_to_model：同一个模型要同时占 NPU0 + NPU1
VTD_CFG = {
    "swcap_to_model": {
        "swcap_vision_chat": {"model_id": "qwen3-vl-4b", "npu_index": [0, 1]},
    },
}


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True,
                                    args=["--no-sandbox", "--disable-gpu", "--mute-audio"])
        ctx = browser.new_context()
        page = ctx.new_page()
        page.on("pageerror", lambda e: print(f"  [pageerror] {e}"))

        # calls: 有序的 (method, body) —— 用来断言 DELETE 在 POST 之前
        calls: list[tuple[str, dict]] = []
        # POST 的返回由这两个变量控制（切场景）
        post_ok = True
        post_errors: list = []

        def on_compute(route):
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"instances": LOADING_SCENE,
                                           "card_count": 2,
                                           "model_cards": [0, 1]}))

        def on_instances(route):
            nonlocal post_ok, post_errors
            req = route.request
            raw = req.post_data or ""
            body = json.loads(raw) if raw else {}
            calls.append((req.method, body))
            if req.method == "DELETE":
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"ok": True}))
            elif req.method == "POST":
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"ok": post_ok,
                                               "errors": post_errors,
                                               "instances": []}))
            else:
                route.fulfill(status=200, body="{}")

        page.route("**/api/compute", on_compute)
        page.route("**/api/compute/instances", on_instances)

        page.goto(BASE, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_function(
            "() => typeof window._wizardBringUpModels === 'function'",
            timeout=15000)

        def bring_up(cfg):
            return page.evaluate(
                "async (cfg) => await window._wizardBringUpModels(cfg)", cfg)

        # ---- 场景 A：卡上有 loading 的非必需实例 → 必须腾掉 ---------------
        print("\n=== A: NPU1 上有一份 loading 的非必需模型 ===")
        calls.clear()
        ok = bring_up(VTD_CFG)
        print(f"  调用序列: {calls}")

        deletes = [(m, b) for m, b in calls if m == "DELETE"]
        posts = [(m, b) for m, b in calls if m == "POST"]
        check("C1 loading 的非必需实例也发了 DELETE",
              any(b.get("model") == "qwen2.5-omni-3b" and b.get("npu_index") == 1
                  for _, b in deletes),
              f"deletes={deletes}")
        check("C2 本应用需要的 qwen3-vl-4b@0 没被 DELETE",
              not any(b.get("model") == "qwen3-vl-4b" for _, b in deletes),
              f"deletes={deletes}")
        check("C3 DELETE 发生在 POST 之前",
              bool(deletes) and bool(posts)
              and calls.index(deletes[0]) < calls.index(posts[0]),
              f"calls={calls}")
        check("A POST 两张卡都启（npu_index=[0,1]）",
              any(b.get("model") == "qwen3-vl-4b"
                  and b.get("npu_index") == [0, 1] for _, b in posts),
              f"posts={posts}")
        check("C5 全部成功判为成功", ok is True, f"ok={ok}")

        # ---- 场景 B：POST 部分失败（HTTP 200 但 ok:false）-----------------
        print("\n=== B: POST 返回 200 但 ok:false（某张卡没起来）===")
        calls.clear()
        post_ok = False
        post_errors = [{"npu_index": 1, "detail": "显存不足"}]
        ok = bring_up(VTD_CFG)
        print(f"  调用序列: {calls}")
        check("C4 部分失败判为失败（不误报「已开启」）",
              ok is False, f"ok={ok}")

        browser.close()

    print("\n" + "=" * 60)
    print(f"通过 {PASSED} / {PASSED + len(FAILED)}")
    if FAILED:
        print(f"❌ 失败: {FAILED}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
