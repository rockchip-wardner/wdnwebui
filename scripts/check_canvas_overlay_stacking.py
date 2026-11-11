#!/usr/bin/env python3
# coding=utf-8
"""检查画布上的浮层（提示层 / 检测事件浮框）不会被宫格画面盖住。

背景
----
宫格那一版（``.vd-grid``）把画面从「非定位的 ``<canvas>``」换成了
「``position: relative`` 的 ``.vd-tile``」，而 ``.vd-tile`` 是
``MultiGrid.paint()`` **追加**到宫格 div 上的 —— 排在提示层之后。

CSS 2.1 附录 E 第 8 步：定位元素且 ``z-index: auto`` 的，按 **DOM 顺序**
绘制。``.vd-tile`` / ``.vd-overlay`` / ``.vd-event-badge`` 三者都是定位
元素、都没写 z-index → 同一层叠组 → 后来的 ``.vd-tile`` 赢。``.vd-tile``
还有不透明的 ``background: #000`` → **整个浮框被画面盖死**。

用户看到的现象：音视频描述页播放器右上角那个「显示检测内容的浮框」不见了。
（``.vd-overlay`` 同理 —— EOF / 帧渲染失败 / WS 错误 这些提示也跟着看不见。）

为什么是 z-index 而不是改 DOM
----------------------------
tile 是运行期 append 的，静态 DOM 里没法排在它后面；而 ``.vtd-crop-popup``
（同类的浮窗）早就是用 ``z-index: 6`` 解决的 —— 这里沿用同一套层级。

本脚本不依赖浏览器：直接解析 CSS，算出「谁压在谁上面」并断言契约。
跑法::

    .venv/bin/python scripts/check_canvas_overlay_stacking.py

服务在跑的话会顺带核对**实际发出去的** CSS（防止改了文件但没重新加载）。
"""
from __future__ import annotations

import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from card_dirs import card_dir  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "app" / "static" / "core"
BASE = "http://127.0.0.1:8000"

#: 浮层 → 必须压在画面之上的选择器
OVERLAYS = (".vd-overlay", ".vd-event-badge")

results: list[bool] = []


def chk(ok: bool, msg: str) -> None:
    print(f"  {'✅' if ok else '❌'} {msg}")
    results.append(bool(ok))


# ---------------------------------------------------------------------------
# 极简 CSS 解析：只要 selector → declarations
# ---------------------------------------------------------------------------

def parse_css(text: str) -> list[tuple[list[str], dict[str, str]]]:
    """``[(选择器列表, 声明表), …]`` —— 注释先剥掉。"""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    out = []
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", text):
        sels = [s.strip() for s in m.group(1).split(",") if s.strip()]
        decls = {}
        for d in m.group(2).split(";"):
            if ":" not in d:
                continue
            k, v = d.split(":", 1)
            decls[k.strip().lower()] = v.strip().lower()
        if sels:
            out.append((sels, decls))
    return out


def rule_of(rules, selector: str) -> dict[str, str]:
    """某选择器的**最后一条**规则（同等特异性下后写的赢）。"""
    found: dict[str, str] = {}
    for sels, decls in rules:
        if selector in sels:
            found = decls
    return found


def z_index(rules, selector: str) -> int | None:
    """z-index 的数值；``auto`` / 没写 → None（等价于 0）。"""
    v = rule_of(rules, selector).get("z-index")
    if not v or v == "auto":
        return None
    try:
        return int(v)
    except ValueError:
        return None


def position(rules, selector: str) -> str:
    return rule_of(rules, selector).get("position", "static")


# ---------------------------------------------------------------------------
# 契约断言
# ---------------------------------------------------------------------------

def check_css(css_text: str, label: str) -> None:
    print(f"\n=== {label} ===")
    rules = parse_css(css_text)

    tile_pos = position(rules, ".vd-tile")
    # 画面是定位元素 → 浮层必须显式给 z-index 才压得住（tree order 兜不住）
    chk(tile_pos != "static",
        f".vd-tile 是定位元素（position: {tile_pos}）"
        + (" → 浮层必须靠 z-index 压过它" if tile_pos != "static" else ""))

    zs = {sel: z_index(rules, sel) for sel in OVERLAYS}
    for sel, z in zs.items():
        chk(z is not None and z > 0,
            f"{sel} 的 z-index > 0（实际 {z if z is not None else 'auto'}）")

    badge_z, ovl_z = zs.get(".vd-event-badge"), zs.get(".vd-overlay")
    if badge_z is not None and ovl_z is not None:
        chk(badge_z > ovl_z,
            f"浮框({badge_z}) 压在提示层({ovl_z}) 之上（两个框可能重叠）")

    chk("display" in rule_of(rules, ".vd-event-badge.hidden")
        and rule_of(rules, ".vd-event-badge.hidden").get("display") == "none",
        ".vd-event-badge.hidden 仍是 display:none（事件没来时不占位）")


def check_cards() -> None:
    """每张宫格卡：浮层元素必须在宫格 div 里，且用的是受 z-index 管辖的类名。"""
    print("\n=== 卡片模板 ===")
    for card_id in ("video-detection", "video-target-detection",
                    "video-target-localization", "video-depth"):
        try:
            d = card_dir(card_id)
        except KeyError:
            print(f"  ⚠️  找不到卡片 {card_id}，跳过")
            continue
        tpl = d / "static" / "template.html"
        if not tpl.is_file():
            print(f"  ⚠️  {card_id}: 没有 template.html，跳过")
            continue
        html = tpl.read_text(encoding="utf-8")

        # 只认**精确**的类名 token —— `vd-event-badge-text` 是浮框里的
        # <span>，不是一层浮框，不能算进来。
        tokens: set[str] = set()
        for m in re.finditer(r'class="([^"]*)"', html):
            tokens.update(m.group(1).split())

        has_grid = "vd-grid" in html
        chk(not has_grid or "vd-overlay" in tokens,
            f"{card_id}: 宫格里有提示层（.vd-overlay）")
        if "vd-event-badge-text" in tokens:
            chk("vd-event-badge" in tokens,
                f"{card_id}: 有检测浮框（.vd-event-badge）")
            # 文字 span 必须**嵌在**浮框里面，不能自成一层（自成一层就没有 z-index 管）
            badge_at = html.find('class="vd-event-badge')
            text_at = html.find('vd-event-badge-text')
            chk(badge_at != -1 and badge_at < text_at
                and "</div>" in html[text_at:text_at + 200],
                f"{card_id}: 文字 span 嵌在浮框内部，不是独立一层")


def main() -> int:
    css = (CORE / "framework.css").read_text(encoding="utf-8") + "\n" + \
          (CORE / "multi-grid.css").read_text(encoding="utf-8")
    check_css(css, "仓库里的 CSS")
    check_cards()

    # 服务在跑的话，核对实际发出去的 CSS（改了文件没重载也要能看出来）。
    # 两份都要取 —— .vd-tile 在 multi-grid.css，浮层在 framework.css。
    served = ""
    for path in ("/static/core/framework.css", "/static/core/multi-grid.css"):
        try:
            with urllib.request.urlopen(f"{BASE}{path}", timeout=5) as r:
                served += r.read().decode("utf-8", "replace") + "\n"
        except Exception as e:  # noqa: BLE001
            print(f"\n=== 服务未启动或取不到 {path}（{e}），跳过线上核对 ===")
            served = ""
            break
    if served:
        check_css(served, "服务上实际发出去的 CSS")

    bad = results.count(False)
    print(f"\n  → {len(results) - bad}/{len(results)} 项通过")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
