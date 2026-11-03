#!/usr/bin/env python3
"""回归：算力配置页在 NPU 遥测链路无响应时，要**说清楚为什么**，而不是
显示一堆 "--"。

背景（2026-11-02）
------------------
本机双 RK1828 的 EP 固件不应答任何 NSMI 遥测命令（chip 数 = 0），
`/api/system-monitor` 返回：

    {"npu": {"available": false,
             "reason": "PCIe 遥测链路无响应（EP 固件不应答 NSMI 命令），已暂停探测，58s 后重试",
             "devices": [...]}}

前端如果只把温度/显存渲染成 "--"，用户看到的是「怎么没有数据」，分不清
是**没数据**还是**链路坏了**。所以 reason 必须落到每张算力卡上。

测试方法：mock /api/compute + /api/system-monitor，断言算力卡上是否出现
reason 文本（正常链路时**不该**出现）。

跑法::

    xvfb-run -a .venv/bin/python scripts/verify_compute_npu_telemetry_note.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

BASE = os.environ.get("WDNAWEBUI_BASE", "http://127.0.0.1:8000")

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


def mock_compute(cards) -> dict:
    return {
        "cards": cards,
        "cards_failed": "",
        "supported_models": [],
        "services": [],
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


def mock_system(npu: dict) -> dict:
    return {
        "cpu": {"percent": 1.0, "per_core": [], "count": 8,
                "load_avg": {"1": 0.1, "5": 0.1, "15": 0.1}},
        "memory": {"total_mb": 8000, "used_mb": 2000,
                   "available_mb": 6000, "percent": 25.0},
        "disk": {"available": True, "path": "/", "total_mb": 100,
                 "used_mb": 50, "free_mb": 50, "percent": 50},
        "workers": {"available": True, "source": "/proc/*/cmdline",
                    "workers": []},
        "npu": npu,
    }


def _handler(body: str):
    def handler(route):
        route.fulfill(status=200, headers={"Content-Type": "application/json"},
                      body=body)
    return handler


def read_notes(page) -> list[str]:
    return page.evaluate("""() => Array.from(
        document.querySelectorAll('#page-compute-config .cc-mem-cell-meta.is-warn'))
        .map(e => e.textContent.trim())""")


def read_cells(page) -> list[str]:
    return page.evaluate("""() => Array.from(
        document.querySelectorAll('#page-compute-config .cc-mem-cell-title'))
        .map(e => e.textContent.trim())""")


def read_cell_rows(page) -> list[dict]:
    """每张算力卡的 {标题, device_id, 温度行}。"""
    return page.evaluate("""() => Array.from(
        document.querySelectorAll('#page-compute-config .cc-mem-cell')).map(c => {
          const metas = Array.from(c.querySelectorAll('.cc-mem-cell-meta'))
              .map(e => e.textContent.trim());
          const t = c.querySelector('.cc-mem-cell-title');
          return {
            title: t ? t.textContent.trim() : '',
            sub: metas.find(x => /^[0-9A-F]{4}:[0-9A-F]{2}:[0-9A-F]{2}\\.0$/.test(x)) || '',
            temp: metas.find(x => x.includes('°C')) || '',
          };
        })""")


def render(page, npu: dict, cards=None) -> list[str]:
    if cards is None:
        cards = [{"npu_index": 0, "device_id": "0003:31:00.0"},
                 {"npu_index": 1, "device_id": "0004:41:00.0"}]
    page.unroute("**/api/compute")
    page.unroute("**/api/system-monitor")
    page.route("**/api/compute", _handler(json.dumps(mock_compute(cards))))
    page.route("**/api/system-monitor",
               _handler(json.dumps(mock_system(npu))))
    # 整页重载：hash-only 的 goto 不重载文档，场景之间会串。
    page.goto("about:blank")
    page.goto(f"{BASE}/#compute-config", wait_until="domcontentloaded",
              timeout=20000)
    page.wait_for_selector("#page-compute-config", state="attached",
                           timeout=10000)
    page.wait_for_timeout(1500)
    return read_notes(page)


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("❌ 没装 playwright，跳过")
        return 1

    reason = "PCIe 遥测链路无响应（EP 固件不应答 NSMI 命令），已暂停探测，58s 后重试"
    ok_dev = {"index": 0, "id": None, "type": "pcie", "bus_id": "0003:31:00.0",
              "status": "Online", "health": None, "power_mw": 4843.0,
              "temp_c": None, "npu_percent": None, "mem_used_mb": None,
              "mem_total_mb": None, "mem_raw": None, "rknn_smi_idx": 0}

    with sync_playwright() as p:
        # playwright 自带的 chromium 没装；系统 chromium（Debian 132）
        # **不支持** ``--headless`` 的 ozone 平台 → 有 DISPLAY 时走有头模式
        # + xvfb（``xvfb-run -a .venv/bin/python scripts/…``）。
        launch_kwargs: dict = {"args": ["--no-sandbox", "--disable-gpu"]}
        if os.environ.get("DISPLAY"):
            launch_kwargs["headless"] = False
        if Path("/usr/bin/chromium").exists():
            launch_kwargs["executable_path"] = "/usr/bin/chromium"
        browser = p.chromium.launch(**launch_kwargs)
        ctx = browser.new_context()
        page = ctx.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        try:
            # ---- 场景 1：遥测链路无响应 → 卡上必须有原因 ------------------
            notes = render(page, {"available": False, "source": "wdnai.RknnSmi",
                                  "reason": reason, "devices": [ok_dev]})
            cells = read_cells(page)
            check("S1a 算力卡渲染出来了", len(cells) >= 1, f"cells={cells}")
            check("S1b 卡上显示了遥测失败原因",
                  any(reason in n for n in notes), f"notes={notes}")
            # 只有 RK182x 协卡带原因（RK3588 主控那格不是协卡）
            card_cells = [c for c in cells if c.startswith("RK182x")]
            check("S1c 每张协卡都带原因",
                  len([n for n in notes if reason in n]) == len(card_cells),
                  f"notes={len(notes)} 协卡={len(card_cells)} cards={cells}")

            # ---- 场景 2：遥测正常 → 不该出现原因行 ----------------------
            notes2 = render(page, {"available": True, "source": "wdnai.RknnSmi",
                                   "devices": [dict(ok_dev, temp_c=43.0,
                                                    npu_percent=12,
                                                    mem_used_mb=1024,
                                                    mem_total_mb=5120)]})
            check("S2a 正常链路时卡上没有原因行", not notes2, f"notes={notes2}")

            # ---- 场景 3：遥测行与算力卡的 index 不是一回事 → 必须按
            #      bus_id 对齐（2026-11-03 修的真 bug）--------------------
            #
            # 本机实测：wdnai 的设备顺序 = [ETH, 0001:11:00.0, 0003:31:00.0,
            # 0004:41:00.0, USB×3]，而注册表的 card.npu_index 来自它自己的
            # 探测 = [0003, 0004, USB, USB]。按 index 配对会把 0001 的
            # 温度画到 0003 那张卡上。
            rows = [
                dict(ok_dev, index=0, bus_id="0001:11:00.0", temp_c=40.0),
                dict(ok_dev, index=1, bus_id="0003:31:00.0", temp_c=41.0),
                dict(ok_dev, index=2, bus_id="0004:41:00.0", temp_c=45.0),
            ]
            render(page, {"available": True, "source": "wdnai.RknnSmi",
                          "devices": rows})
            cell_rows = read_cell_rows(page)
            by_bus = {r["sub"]: r["temp"] for r in cell_rows if r["sub"]}
            check("S3a 0003 那张卡显示的是 0003 自己的温度（41.0°C）",
                  "41.0°C" in by_bus.get("0003:31:00.0", ""),
                  f"by_bus={by_bus}")
            check("S3b 0004 那张卡显示的是 0004 自己的温度（45.0°C）",
                  "45.0°C" in by_bus.get("0004:41:00.0", ""),
                  f"by_bus={by_bus}")
            check("S3c 没有把 0001 的温度（40.0°C）画到别人的卡上",
                  not any("40.0°C" in t for t in by_bus.values()),
                  f"by_bus={by_bus}")
        finally:
            ctx.close()
            browser.close()

    check("S3 前端无 JS 报错", not errors, f"errors={errors[:2]}")
    print(f"\n{'PASS' if FAIL == 0 else 'FAIL'}: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
