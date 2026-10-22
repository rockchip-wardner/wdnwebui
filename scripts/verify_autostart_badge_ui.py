#!/usr/bin/env python3
"""UI 回归：算力配置页的「开机自启」徽章必须和 ``config.compute.autostart`` 一致。

真机症状：跑完「音视频描述」应用后，config.jsonc 里 autostart 有两条
（``qwen3-vl-4b@npu0`` + ``qwen2.5-omni-3b@npu1``），但算力配置页上只有
[VLM]视觉描述v3 带「开机自启」徽章，[Omni]多感知描述v2.5 没有。

纯逻辑那层在 ``scripts/verify_autostart_badge.py``；这个脚本查**页面真的
画对了没有**（徽章是 DOM 里的 ``.cc-autostart-badge``，按可见性判定）。

跑法：``.venv/bin/python scripts/verify_autostart_badge_ui.py``
（只读页面，不点任何按钮）
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

from playwright.sync_api import sync_playwright

BASE = os.environ.get("WDNWEBUI_BASE", "http://127.0.0.1:8000")
PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✅ {name}" + (f"  ({detail})" if detail else ""))
    else:
        FAIL += 1
        print(f"  ❌ {name}" + (f"  ({detail})" if detail else ""))


def _api(path: str) -> dict:
    with urllib.request.urlopen(f"{BASE}{path}", timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


cfg = _api("/api/compute/config")
autostart = {e["model"]: e["npu_index"] for e in (cfg.get("autostart") or [])}
print(f"config.compute.autostart = {autostart}")

with sync_playwright() as p:
    browser = p.chromium.launch(args=["--no-sandbox"])
    page = browser.new_page(viewport={"width": 1600, "height": 1100})
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        page.goto(f"{BASE}/#compute-config", wait_until="domcontentloaded",
                  timeout=20000)
        # 页面 1Hz 轮询 /api/compute 后才画卡片。刚重启服务时第一拍可能
        # 拿不到数据 → 卡片一直不出现。显式等 API 有响应再等卡片。
        page.wait_for_function(
            """async () => {
                try {
                    const r = await fetch('/api/compute', {cache: 'no-store'});
                    if (!r.ok) return false;
                    const d = await r.json();
                    return Array.isArray(d.services);
                } catch (e) { return false; }
            }""",
            timeout=20000)
        page.wait_for_selector("#cc-local-cards .cc-service-card",
                               timeout=30000)
        page.wait_for_timeout(2500)

        cards = page.eval_on_selector_all(
            "#cc-local-cards .cc-service-card",
            """els => els.map(el => {
                const badge = el.querySelector('.cc-autostart-badge');
                return {
                    model: el.dataset.model || '',
                    source: el.dataset.source || '',
                    badge: badge ? badge.textContent.trim() : '',
                    visible: badge ? badge.offsetParent !== null : false,
                };
            })""")
    finally:
        browser.close()

    print(f"\n本机算力栏共 {len(cards)} 张卡")
    for c in cards:
        mark = "有" if c["visible"] else "无"
        print(f"  {c['model']:22} src={c['source']:12} 徽章={mark}"
              f"{'（' + c['badge'] + '）' if c['badge'] else ''}")

    for c in cards:
        want = c["model"] in autostart
        # peer 卡在「网络算力栏」，本机栏里不该出现；这里只看本机栏
        check(f"{c['model']} 徽章{'应显示' if want else '不应显示'}",
              c["visible"] == want,
              f"visible={c['visible']} config有={want}")

    check("全程无 JS 报错", len(errors) == 0, " | ".join(errors[:3]))

print("\n" + "=" * 50)
print(f"通过 {PASS} / {PASS + FAIL}")
if FAIL:
    print("❌ 有失败项")
    sys.exit(1)
print("✅ 全部通过")
