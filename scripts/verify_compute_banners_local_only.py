#!/usr/bin/env python3
"""回归测试: 算力配置页顶部 banner 只对本地算力响(本机有权干预的)。

背景
----
v0.15 之前 banner(stuck / missing_model_dir / 错误桶中的 adopted 子桶)
无差别地覆盖所有 _source,导致远端 peer 的状态触发假阳性告警:

  1. _detectStuck 用 inst.started_at 算 elapsed,但 adopted 的 started_at
     是 adoption 时刻(per-process 全程不变)。elapsed 累积到上千秒,
     "疑似卡住"banner 误报。
  2. "对端 worker 失联"banner:本机无权 bind/重启 peer,告警 = 误导。
  3. missing_model_dir 也会把远端 peer 的目录缺失列出来,本机补不了。

修复策略:三个 banner 入口 + _detectStuck 全部按 isLocalComputeSource
过滤;per-card 卡片保留对 adopted 的可视化(route A, 中性文案)。

测试方法:mock /api/compute 喂不同 _source / status 组合,断言
#cc-banner-stack 内的 banner 标题列表。
"""
import json
import sys
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000"


def mock_compute(instances, services_extra=None, **kwargs):
    """构造 mock /api/compute 返回体。services 用最小合法结构。"""
    snap = {
        "cards": [],
        "cards_failed": "",
        "supported_models": [],
        "services": services_extra or [],
        "instances": instances,
        "running_count": sum(1 for i in instances if i["status"] == "running"),
        "queued_count": sum(1 for i in instances if i["status"] == "not_loaded"),
        "loaded_count": sum(1 for i in instances
                            if i["status"] in {"running", "loading"}),
        "error_count": sum(1 for i in instances if i["status"] == "error"),
        "estimated_init_seconds_per_model": {},
        "default_estimated_init_seconds": 30,
        "worker_alive": True,
        "last_updated": 1.0,
        "capabilities": [],
        "tools": [],
    }
    snap.update(kwargs)
    return snap


def inst(model_id, status, source, peer_name="", started_at=0.0, queued_at=0.0):
    """构造最小实例 dict,_source + status + model_id 三字段必有。"""
    base = {
        "model_id": model_id,
        "status": status,
        "_source": source,
        "npu_index": 0,
        "started_at": started_at,
        "queued_at": queued_at,
        "loaded_at": None,
        "load_time_sec": None,
        "label": f"mock-{model_id}",
        "error": "",
        "memory_estimate_mb": 1000,
    }
    if source == "adopted":
        base["_peer_name"] = peer_name or "PeerX"
        base["_peer_ip"] = "192.168.99.99"
    return base


def make_route_handler(mock_body: str):
    def handler(route):
        route.fulfill(status=200, headers={"Content-Type": "application/json"},
                      body=mock_body)
    return handler


# 场景矩阵:每个 (name, instances, expected_banner_keywords)
#  expected_banner_keywords 是 banner 标题里**应该**出现的子串列表;
#  不在该列表里的 banner 标题不应出现。
SCENARIOS = [
    # ---- 主诉 bug:远端 adopted loading 不应触发 "疑似卡住" banner ----
    ("1. adopted loading 累计 1500s → NO stuck banner (主诉场景)",
     [inst("qwen2.5-omni-3b", "loading", "adopted", started_at=-1500)],
     {"must_have": [], "must_not_have": ["疑似卡住"]}),

    ("2. spawned loading 累计 1500s → 触发 stuck banner (本机应告警)",
     [inst("qwen3-vl-4b", "loading", "spawned", started_at=-1500)],
     {"must_have": ["疑似卡住"], "must_not_have": []}),

    ("3. adopted_local loading 累计 1500s → 触发 stuck banner (本机应告警)",
     [inst("qwen3-vl-4b", "loading", "adopted_local", started_at=-1500)],
     {"must_have": ["疑似卡住"], "must_not_have": []}),

    # ---- 错误 banner 的 adopted 桶已删除 ----
    ("4. adopted error -> 不应再有'对端 worker 失联' banner",
     [inst("qwen3-vl-4b", "error", "adopted", peer_name="PeerX")],
     {"must_have": [], "must_not_have": ["对端 worker 失联"]}),

    ("5. spawned error -> 仍要出现'算力实例启动失败' banner",
     [inst("qwen3-vl-4b", "error", "spawned")],
     {"must_have": ["算力实例启动失败"], "must_not_have": []}),

    ("6. adopted_local error -> 仍要出现'本机孤儿 worker 异常' banner",
     [inst("qwen3-vl-4b", "error", "adopted_local")],
     {"must_have": ["本机孤儿 worker 异常"], "must_not_have": []}),

    # ---- missing_model_dir 也过滤 ----
    ("7. adopted missing_model_dir → NO banner",
     [inst("qwen3-vl-4b", "missing_model_dir", "adopted")],
     {"must_have": [], "must_not_have": ["模型目录缺失"]}),

    ("9. spawned missing_model_dir → 触发 banner",
     [inst("qwen3-vl-4b", "missing_model_dir", "spawned")],
     {"must_have": ["模型目录缺失"], "must_not_have": []}),

    # ---- 复合场景 ----
    ("10. mixed spawned loading + adopted loading + adopted error",
     [inst("qwen3-vl-4b", "loading", "spawned", started_at=-1500),
      inst("qwen2.5-omni-3b", "loading", "adopted", started_at=-1500),
      inst("qwen3-tts", "error", "adopted", peer_name="PeerX")],
     {"must_have": ["疑似卡住"], "must_not_have": ["对端 worker 失联"]}),
]


def read_banners(page):
    return page.evaluate("""() => Array.from(
        document.querySelectorAll('#cc-banner-stack .cc-banner-title'))
        .map(t => t.textContent.trim())""")


def assert_banners(name, expects, titles, ok_count, failed):
    """断言并打印。返回 (ok: bool)。"""
    print(f"  banners: {titles}")
    ok = True
    for kw in expects["must_have"]:
        if not any(kw in t for t in titles):
            print(f"  ❌ expected banner containing {kw!r} not found")
            ok = False
    for kw in expects["must_not_have"]:
        if any(kw in t for t in titles):
            print(f"  ❌ banner containing {kw!r} should NOT appear")
            ok = False
    if ok:
        print("  ✅ matches expectations")
        return True, ok_count + 1, failed
    failed.append(name)
    return False, ok_count, failed


def run_scenario(page, instances):
    """挂一次 scenario,等 banner 渲染稳定后返回 titles。

    简单做法:wait_for_selector + 2s sleep。ComputeConfigPage 1Hz tick 会
    调 load() + render(),banner 内容取决于 mock 数据,2s 内一定走完两
    次 tick 且 mock 数据不变。比 stability loop 更稳定(后者在多场景连
    跑时会被 1Hz tick 触发重新渲染判定而 fail)。"""
    body = json.dumps(mock_compute(instances))
    page.route("**/api/compute", make_route_handler(body))
    page.goto(f"{BASE}/#compute-config",
              wait_until="domcontentloaded", timeout=15000)
    # state="attached" 而非默认 visible —— section 内父 element 带 .hidden
    # 时子元素 visibility 检查会失败。
    page.wait_for_selector("#page-compute-config",
                           state="attached", timeout=8000)
    page.wait_for_selector("#cc-banner-stack",
                           state="attached", timeout=5000)
    page.wait_for_timeout(2000)
    return read_banners(page)


def main() -> int:
    ok_count = 0
    failed = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True,
                                    args=["--no-sandbox", "--disable-gpu"])

        for name, instances, expects in SCENARIOS:
            print(f"\n=== {name} ===")
            ctx = browser.new_context()
            page = ctx.new_page()
            page.on("pageerror", lambda e: print(f"  [pageerror] {e}"))
            try:
                titles = run_scenario(page, instances)
                _, ok_count, failed = assert_banners(
                    name, expects, titles, ok_count, failed)
            except Exception as e:
                print(f"  ❌ mount failed: {e}")
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