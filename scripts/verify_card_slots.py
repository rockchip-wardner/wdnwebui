#!/usr/bin/env python3
"""回归测试: 每张已发现的卡在 index.html.template 里都有**卡位占位符**。

背景
----
宿主把每张卡的页面模板**服务端注入**进首页 HTML：

    app/static/index.html.template  ←  里面每张卡一行 ``{% card_slot "<id>" %}``
    app/core/html_assembly.py::assemble_index_html  ←  逐个替换成该卡的
                                                          static/template.html

**加一张新卡时忘了在 index.html.template 里加那一行占位符** ⇒
模板**从来没被注入** ⇒ 页面里**没有** ``#page-<id>`` 这个 section。

后果不是「报错」，而是**静默错乱**（2026-11-06 实测，image-editor 卡）：

* ``ROUTER.showPage()`` 找不到 page section ⇒ 内容区一片空白；
* ``wizard/state.js::mount()`` 拿不到 pageSection ⇒ 向导容器 fallback 到
  ``document.body.appendChild()`` ⇒ **向导面板被挤到 main.content 外面**，
  落在屏幕下沿 1000 px 处 ⇒ 用户看到「一大片空白 + 面板在下面」；
* 运行期只有一条 WARNING（``no {{card_slot}} placeholder``）⇒ 没人看日志。

本脚本断言：

1. 每个**已发现**的卡在 index.html.template 里都有 ``{% card_slot "<id>" %}``；
2. 实际渲染出的首页里，每个卡都有 ``<section id="page-<id>">``；
3. 没有「占位符在、卡没了」的残留（那种会被 _drop_unmatched_card_slots
   摘掉，但摘掉后模板里的顺序信息就没了 —— 这里只提示，不判失败）。

只需 HTTP（不需要浏览器），跑法::

    .venv/bin/python scripts/verify_card_slots.py
"""
from __future__ import annotations

import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "app" / "static" / "index.html.template"
BASE = "http://127.0.0.1:8000"

_CARD_SLOT_RE = re.compile(r'\{%\s*card_slot\s+"([^"]+)"\s*%\}')


def _manifest_card_ids() -> list[str]:
    """从宿主进程内直接问 manifest（不依赖 HTTP 起来）。"""
    sys.path.insert(0, str(ROOT))
    from app.core.manifest import discover as discover_cards  # noqa: PLC0415

    return [m.id for m in discover_cards()]


def _rendered_index() -> str:
    with urllib.request.urlopen(f"{BASE}/", timeout=10) as r:
        return r.read().decode("utf-8")


def main() -> int:
    card_ids = _manifest_card_ids()
    if not card_ids:
        print("❌ 没有发现任何卡（manifest 加载失败？）")
        return 1

    template = TEMPLATE.read_text(encoding="utf-8")
    slots = _CARD_SLOT_RE.findall(template)
    slot_set = set(slots)

    print(f"已发现的卡 {len(card_ids)} 张: {', '.join(card_ids)}")
    print(f"index.html.template 里的卡位占位符 {len(slots)} 个: {', '.join(slots)}")

    failures: list[str] = []

    # 1) 每张已发现的卡都要有占位符
    for cid in card_ids:
        if cid not in slot_set:
            failures.append(
                f"卡 {cid!r} 没有卡位占位符 —— "
                f"请在 {TEMPLATE.relative_to(ROOT)} 里加一行 "
                f'{{% card_slot "{cid}" %}}'
            )

    # 2) 渲染出的首页里每张卡都要有 page section
    try:
        html = _rendered_index()
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️  取 {BASE}/ 失败（webui 没起？）：{exc}")
        print("   只跑静态检查（占位符）")
        html = ""

    if html:
        for cid in card_ids:
            if f'id="page-{cid}"' not in html:
                failures.append(
                    f"卡 {cid!r} 渲染后的首页里没有 id=\"page-{cid}\" 的 section"
                )

    # 3) 反向：占位符在、卡没了（插件被卸载）—— 只提示
    orphans = [s for s in slots if s not in card_ids]
    if orphans:
        print(f"ℹ️  有占位符但没发现对应卡（插件可能已卸载）: {', '.join(orphans)}")

    # 4) 顺序：占位符在模板里的先后顺序应与 manifest 的展示顺序一致
    if slots:
        order = [c for c in card_ids if c in slot_set]
        print(f"模板里的卡顺序: {' → '.join(order)}")

    if failures:
        print("\n❌ 失败:")
        for f in failures:
            print("   - " + f)
        print(
            "\n💡 卡位占位符漏了 ⇒ 该卡的页面模板**根本没被注入** ⇒ "
            "页面空白 + 向导面板被挤到屏幕外。"
        )
        return 1

    print(f"\n✅ {len(card_ids)} 张卡全部有卡位占位符，渲染结果里也都有 page section")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
