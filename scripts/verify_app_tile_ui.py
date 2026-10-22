#!/usr/bin/env python3
"""「添加应用」磁位的浏览器端验证。

覆盖：

1. 磁位出现在应用网格末尾，画的是加号图标 + 提示文字，且两者的 opacity
   确实是半透明（用户明确要求的视觉）；
2. 点磁位不会误触发卡片路由（它不是导航项）；
3. 拖一个**非应用包**进来 → 后端拒绝、venv 不受污染、磁位给出可读原因；
4. 拖一个**应用包**进来 → 装成功、网格里长出新卡片、状态提示正确；
5. 全程无 JS 报错（pageerror / console.error）。

⚠️ 本脚本会真的往 venv 里装包。测完自动卸载恢复现场。

用法::

    .venv/bin/python scripts/verify_app_tile_ui.py
"""
from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://localhost:8000"
TMP = Path("/tmp/wheels")

_FAILED: list[str] = []
_PKG = "wdnapps_demo"


def check(ok: bool, label: str, detail: str = "") -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {label}")
    if not ok:
        if detail:
            print(f"       {detail}")
        _FAILED.append(label)
    return ok


def build_wheels() -> tuple[Path, Path]:
    TMP.mkdir(parents=True, exist_ok=True)
    app = TMP / f"{_PKG}-0.2.0-py3-none-any.whl"
    plain = TMP / "plainlib-1.0.0-py3-none-any.whl"
    if not app.exists():
        make_app_wheel(app)
    if not plain.exists():
        make_plain_wheel(plain)
    return app, plain


def make_app_wheel(path: Path) -> None:
    c = "demo_card"
    M: dict[str, str] = {
        "wdnapps_demo/__init__.py": "",
        f"wdnapps_demo/cards/{c}/__init__.py": (
            '"""演示卡（verify 用）"""\n'
            "def register_api(app):\n"
            "    @app.get('/api/demo-card/ping')\n"
            "    def _p():\n"
            "        return {'ok': True}\n"
            "    return _p\n\n\n"
            "def register_shutdown():\n"
            "    return None\n"
        ),
        f"wdnapps_demo/cards/{c}/manifest.json": (
            '{\n  "id": "demo-card",\n  "title": "演示卡",\n'
            '  "description": "验证自定义应用安装流程",\n'
            '  "software_capabilities": ["swcap_text_chat"],\n'
            '  "hardware_capabilities": [],\n'
            '  "backend": {"module": "wdnapps_demo.cards.demo_card",'
            ' "register_api": "register_api",'
            ' "register_shutdown": "register_shutdown"},\n'
            '  "frontend": {"template": "template.html", "js": ["app.js"], "css": []},\n'
            '  "demo_guard": {"active_state_keys": [], "stop_endpoints": [],'
            ' "stop_sync_endpoints": []},\n'
            '  "prompts": {},\n  "display_order": 90\n}'
        ),
        f"wdnapps_demo/cards/{c}/template.html": "<h1>demo-card</h1>",
        f"wdnapps_demo/cards/{c}/app.js": "window.DEMO_CARD = 1;",
        f"{_PKG}-0.2.0.dist-info/METADATA":
            "Metadata-Version: 2.1\nName: wdnapps-demo\nVersion: 0.2.0\n",
        f"{_PKG}-0.2.0.dist-info/WHEEL":
            "Wheel-Version: 1.0\nGenerator: t\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        f"{_PKG}-0.2.0.dist-info/entry_points.txt":
            "[wdnwebui.cards]\ndemo-card = wdnapps_demo.cards.demo_card\n",
        f"{_PKG}-0.2.0.dist-info/top_level.txt": "wdnapps_demo\n",
    }
    rec = [f"{n},sha256=x,{len(v.encode())}" for n, v in M.items()]
    rec.append(f"{_PKG}-0.2.0.dist-info/RECORD,,")
    M[f"{_PKG}-0.2.0.dist-info/RECORD"] = "\n".join(rec) + "\n"
    with zipfile.ZipFile(path, "w") as z:
        for n, v in M.items():
            z.writestr(n, v)


def make_plain_wheel(path: Path) -> None:
    M = {
        "plainlib/__init__.py": "",
        "plainlib-1.0.0.dist-info/METADATA":
            "Metadata-Version: 2.1\nName: plainlib\nVersion: 1.0.0\n",
        "plainlib-1.0.0.dist-info/WHEEL":
            "Wheel-Version: 1.0\nGenerator: t\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    rec = [f"{n},sha256=x,{len(v.encode())}" for n, v in M.items()]
    rec.append("plainlib-1.0.0.dist-info/RECORD,,")
    M["plainlib-1.0.0.dist-info/RECORD"] = "\n".join(rec) + "\n"
    with zipfile.ZipFile(path, "w") as z:
        for n, v in M.items():
            z.writestr(n, v)


def drop_file(page, whl: Path, tile_sel: str) -> None:
    """把文件拖到磁位上 —— 走真实的 dragenter/dragover/drop 事件链。"""
    payload = path_to_file_payload(whl)
    page.evaluate(
        """([sel, data]) => {
            const tile = document.querySelector(sel);
            const dt = new DataTransfer();
            dt.items.add(new File([new Uint8Array(data.bytes)], data.name,
                                  {type: 'application/octet-stream'}));
            for (const type of ['dragenter', 'dragover', 'drop']) {
                const ev = new DragEvent(type, {bubbles: true, cancelable: true,
                                                dataTransfer: dt});
                tile.dispatchEvent(ev);
            }
        }""",
        [tile_sel, payload],
    )


def path_to_file_payload(whl: Path) -> dict:
    return {"name": whl.name, "bytes": list(whl.read_bytes())}


def main() -> int:
    app_whl, plain_whl = build_wheels()
    errors: list[str] = []

    with sync_playwright() as pw:
        br = pw.chromium.launch()
        page = br.new_page()
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.on("console", lambda m: errors.append(f"console.error: {m.text}")
                if m.type == "error" else None)

        # 不用 wait_until="networkidle"：这个应用有常驻轮询 / WS 心跳（算力状态
        # 1Hz 刷新），网络永远不静默，等 networkidle 必然 30s 超时。改成
        # domcontentloaded + 显式等元素 —— 我们关心的是 DOM 出来了没有。
        page.goto(f"{BASE}/#apps", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_selector(".apps-grid .app-card", timeout=30000)

        print("=== 1. 磁位渲染 ===")
        n_cards = page.locator(".apps-grid .app-card").count()
        n_tiles = page.locator(".apps-grid [data-app-add-tile]").count()
        # 期望值从服务端注入的清单推导，不要写死张数：卡片可以带
        # ``"hidden": true`` 不进网格（audio-analysis 目前就是），写死 8
        # 会把"合法的隐藏"误报成"卡片丢了"。
        expect = page.evaluate(
            """() => (window.__APP_MANIFESTS__ || {cards: []}).cards
                      .filter((c) => c.status === 'ok' && c.hidden !== true).length"""
        )
        check(n_tiles == 1, f"网格里恰好一个添加磁位（实际 {n_tiles}）")
        check(n_cards == expect,
              f"进网格的卡片数与服务端清单一致（{n_cards} / 期望 {expect}）")

        # 磁位必须在网格**末尾**（用户看到的是"列表最后有个空位"）
        order = page.evaluate(
            """() => {
                const g = document.querySelector('#page-apps .apps-grid');
                const kids = Array.from(g.children);
                return kids.map((e) => e.hasAttribute('data-app-add-tile')
                                 ? 'tile' : 'card');
            }"""
        )
        check(order and order[-1] == "tile" and order.count("tile") == 1,
              "磁位在网格末尾", str(order))

        print("\n=== 2. 图标 + 提示文字：半透明 ===")
        vis = page.evaluate(
            """() => {
                const t = document.querySelector('[data-app-add-tile]');
                const icon = t.querySelector('.app-add-icon');
                const hint = t.querySelector('.app-add-hint');
                const sameBox = t.querySelector('.app-add-placeholder');
                if (!icon || !hint) return null;
                const ir = icon.getBoundingClientRect();
                const hr = hint.getBoundingClientRect();
                const sr = sameBox.getBoundingClientRect();
                return {
                    iconOpacity: parseFloat(getComputedStyle(icon).opacity),
                    hintOpacity: parseFloat(getComputedStyle(hint).opacity),
                    hintText: hint.textContent.trim(),
                    iconW: ir.width,
                    // 提示文字是否落在占位块（= 加号所在的那块）里
                    hintInsideBox: hr.top >= sr.top && hr.bottom <= sr.bottom + 2,
                    // 加号是否在提示文字上方
                    iconAboveHint: ir.bottom <= hr.top + 2,
                    iconCentered: Math.abs((ir.left + ir.right) / 2
                                           - (sr.left + sr.right) / 2) < 4,
                };
            }""",
        )
        check(vis is not None, "磁位里能找到加号与提示文字", str(vis))
        if vis:
            check(vis["iconOpacity"] < 0.7,
                  f"加号半透明（opacity={vis['iconOpacity']}）")
            check(vis["hintOpacity"] < 0.7,
                  f"提示文字半透明（opacity={vis['hintOpacity']}）")
            check(vis["iconW"] >= 80,
                  f"加号够大（{vis['iconW']:.0f}px ≥ 80px）")
            check(vis["hintInsideBox"],
                  "提示文字在占位块内（跟加号同一块）", str(vis))
            check(vis["iconAboveHint"], "加号在提示文字上方", str(vis))
            check(vis["iconCentered"], "加号水平居中", str(vis))
            check("wdnapp" in vis["hintText"] and "安装" in vis["hintText"],
                  f"提示文案正确：{vis['hintText']!r}")

        print("\n=== 3. 点磁位不误触发路由 ===")
        before_hash = page.evaluate("() => location.hash")
        page.evaluate(
            """() => {
                const t = document.querySelector('[data-app-add-tile]');
                t.dispatchEvent(new MouseEvent('click', {bubbles: true}));
            }"""
        )
        page.wait_for_timeout(300)
        after_hash = page.evaluate("() => location.hash")
        check(before_hash == after_hash,
              "点磁位不跳路由", f"{before_hash} → {after_hash}")

        print("\n=== 4. 拖非应用包 → 拒绝且不动 venv ===")
        drop_file(page, plain_whl, "[data-app-add-tile]")
        page.wait_for_selector("[data-app-add-tile] .app-add-status.is-error",
                               timeout=60000)
        msg = page.locator("[data-app-add-tile] .app-add-status").inner_text()
        check("不是应用包" in msg, "提示「不是应用包」", msg[:200])
        check("wdnwebui.cards" in msg, "提示缺什么（entry point 组名）", msg[:300])
        sp = ROOT / ".venv/lib/python3.13/site-packages"
        check(not (sp / "plainlib").exists(),
              "venv 里没有 plainlib（非应用包没被装进去）")

        print("\n=== 5. 拖应用包 → 装上并出现在网格 ===")
        drop_file(page, app_whl, "[data-app-add-tile]")
        page.wait_for_selector(
            "[data-app-add-tile] .app-add-status.is-ok, "
            "[data-app-add-tile] .app-add-status.is-error", timeout=120000)
        msg = page.locator("[data-app-add-tile] .app-add-status").inner_text()
        cls = page.locator("[data-app-add-tile] .app-add-status").get_attribute("class")
        check("is-ok" in (cls or ""), f"提示安装成功（{msg[:120]}）")
        check("wdnapps-demo" in msg or "wdnapps_demo" in msg,
              "提示里带包名", msg[:200])

        page.wait_for_timeout(1500)
        ids = page.evaluate(
            """() => Array.from(document.querySelectorAll('#page-apps .app-card'))
                             .map(e => e.dataset.page)"""
        )
        check("demo-card" in ids, "demo-card 出现在应用网格里", str(ids))
        check(ids.count("demo-card") == 1, "没有重复卡片", str(ids))
        n_tiles2 = page.locator(".apps-grid [data-app-add-tile]").count()
        check(n_tiles2 == 1, f"重绘后磁位仍只有一个（{n_tiles2}）")

        print("\n=== 6. 无 JS 报错 ===")
        # 排除**预期内**的失败：第 4 步故意拖了个非应用包，后端返 400，
        # 浏览器照样会把失败的 fetch 记成 console.error。那是我们要的
        # 行为，不是缺陷。
        real = [e for e in errors
                if "favicon" not in e.lower()
                and "status of 400" not in e]
        check(not real, "全程无 pageerror / console.error", "; ".join(real[:3]))

        br.close()

    print("\n=== 清理测试装的包 ===")
    subprocess.run([sys.executable, "-m", "pip", "uninstall", "-y",
                    "--disable-pip-version-check", "wdnapps-demo"],
                   capture_output=True, text=True)

    print()
    if _FAILED:
        print(f"❌ {len(_FAILED)} 项未通过:")
        for f in _FAILED:
            print("   -", f)
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())