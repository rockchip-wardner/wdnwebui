#!/usr/bin/env python3
"""回归测试: wizard env_check 步骤只把本地算力(spawned / adopted_local)
算作"已开启",peer adopted 即使 running 也不应显示 ✓。

背景
----
app/static/core/wizard/state.js 的 _probeEnv 拉 /api/compute 后只过滤
status === "running",没过滤 _source === "adopted"。结果:本机没启
swcap_vision_grounding(LocateAnything-3b spawned not_loaded),但 LAN 上另一台
板子的同名模型 running 给 DG peer adopted → wizard UI 显示 ✓ swcap_vision_grounding
并允许跳过"一键开启"步骤,误导用户以为"已就绪"。

对照参考: 同文件 _wizardBringUpModels 已正确跳过 adopted(注释明确说
"peer 实例的 NPU 显存 / worker 进程状态完全由对端机器控制"),_probeEnv
漏掉同样护栏是 bug。

修复: _probeEnv 加 `inst._source !== "adopted"` 过滤;_waitForModelsRunning
对称地加同样过滤。

测试策略
----
对 video-target-localization(manifest.software_capabilities =
["swcap_vision_grounding"],swcap_to_model 唯一条目)做 5 个场景,mock /api/compute
返回不同组合,断言 env-row DOM 的 class 与 text。

| 场景 | 本机 spawned | peer adopted | 期望 |
|---|---|---|---|
| A  | not_loaded    | running      | ✗ swcap_vision_grounding (bug 场景) |
| B  | running       | -            | ✓ swcap_vision_grounding |
| C  | not_loaded    | not_loaded   | ✗ swcap_vision_grounding |
| D  | running       | running      | ✓ swcap_vision_grounding |
| E  | running(多源) | -            | ✓ swcap_vision_grounding (smoke 主路径) |
"""
import json
import sys
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000"
APP_ID = "video-target-localization"
SWCAP = "swcap_vision_grounding"


def mock_payload(scenario_name: str, instances: list) -> dict:
    """构造 mock /api/compute 返回体。"""
    return {
        "instances": instances,
        "cards": [],
        "cards_failed": "",
    }


def make_route_handler(mock_body: str):
    """构造一个闭包 route handler,避免 lambda 默认参数在 loop 中误捕获。"""
    def handler(route):
        route.fulfill(status=200, headers={"Content-Type": "application/json"}, body=mock_body)
    return handler


def instance(model_id, status, source, peer_name=""):
    base = {
        "model_id": model_id,
        "status": status,
        "_source": source,
        "capability_types": [SWCAP],
        "npu_index": 0 if status == "running" else -1,
    }
    if source == "adopted":
        base["_peer_name"] = peer_name
        base["_peer_ip"] = "192.168.99.99"
    return base


SCENARIOS = [
    # (name, mock instances, expects 摸 ok-class present?)
    ("A. peer adopted running only (BUG 场景 — 本机未启)",
     [instance("LocateAnything-3b", "running", "adopted", peer_name="PeerX")],
     False),
    ("B. local spawned running",
     [instance("LocateAnything-3b", "running", "spawned")],
     True),
    ("C. both not_loaded",
     [instance("LocateAnything-3b", "not_loaded", "spawned"),
      instance("LocateAnything-3b", "not_loaded", "adopted", peer_name="PeerX")],
     False),
    ("D. local spawned running + peer adopted running (本机已启,peer 也在跑)",
     [instance("LocateAnything-3b", "running", "spawned"),
      instance("LocateAnything-3b", "running", "adopted", peer_name="PeerX")],
     True),
    ("E. 多个 spawned 本机实例 running(已包含 swcap_vision_grounding)",
     [instance("LocateAnything-3b", "running", "spawned"),
      instance("OtherModel-7b", "running", "spawned")],
     True),
]


def main() -> int:
    ok_count = 0
    failed = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])

        for name, insts, expects_env_ok in SCENARIOS:
            print(f"\n=== {name} ===")
            ctx = browser.new_context()
            page = ctx.new_page()
            page.on("pageerror", lambda e: print(f"  [pageerror] {e}"))

            # mock /api/compute(在 goto 之前装好,确保 wizard mount 时拿到 mock)
            mock_body = json.dumps(mock_payload(name, insts))
            page.route("**/api/compute", make_route_handler(mock_body))

            try:
                page.goto(f"{BASE}/#{APP_ID}", wait_until="domcontentloaded", timeout=15000)
                page.wait_for_selector(".wd-wizard-section", timeout=8000)
                # 等 env-row 渲染
                page.wait_for_selector(".wd-wizard-env-row", timeout=5000)
                page.wait_for_timeout(300)
            except Exception as e:
                print(f"  ❌ mount failed: {e}")
                failed.append(name)
                ctx.close()
                continue

            # 抓所有 env-row,过滤含 swcap_vision_grounding 的那行
            rows = page.evaluate("""() => {
                return Array.from(document.querySelectorAll('.wd-wizard-env-row')).map(r => ({
                    cls: r.className,
                    text: r.textContent.trim(),
                }));
            }""")

            sw_row = None
            for r in rows:
                if SWCAP in r["text"]:
                    sw_row = r
                    break

            if not sw_row:
                print(f"  ❌ 没找到 swcap_vision_grounding 的 env-row")
                failed.append(name)
            else:
                is_ok = "wd-wizard-env-ok" in sw_row["cls"]
                is_fail = "wd-wizard-env-fail" in sw_row["cls"]
                actual = "✓" if is_ok else ("✗" if is_fail else "?")
                print(f"  env-row: cls={sw_row['cls']!r}  text={sw_row['text']!r}")
                if is_ok == expects_env_ok and (is_ok or is_fail):
                    print(f"  ✅ {actual} 符合期望(env_ok={expects_env_ok})")
                    ok_count += 1
                else:
                    print(f"  ❌ 实际 {actual},期望 env_ok={expects_env_ok}")
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