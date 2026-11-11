#!/usr/bin/env python3
# coding=utf-8
"""检查「用 `hidden` **属性**切换显隐，但类的 display 规则把切换吃掉」的元素。

坑
--
HTML 的 `hidden` 属性靠 UA 样式表里的 ``[hidden] { display: none }`` 生效。
只要元素自己的类也写了 ``display``（flex / grid / block …），**作者样式压过
UA 样式**（同特异性时作者来源优先）→ ``el.hidden = true`` 不再有任何效果。

于是「隐藏」的那个元素永远显示。仓库里已经踩过两次：
  * ``.vr-image-preview`` —— 清除图片后预览区仍以空框占位（已修）
  * ``.vr-progress`` —— 检索进度条从进页面起就一直挂着（本次修）

修法统一：给该类补一条 ``.xxx[hidden] { display: none; }``（特异性 (0,2,0)
压过类的 (0,1,0)）。

判据
----
只有**真的用属性切换**的元素才算问题：
  * ``el.hidden = true``      → 属性写法 → 受影响
  * ``classList.add("hidden")`` → 类名写法 → 只要有 ``.xxx.hidden{display:none}``
    就正常，不算问题

跑法::

    .venv/bin/python scripts/check_hidden_attr_trap.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from card_dirs import resolve_cards  # noqa: E402

REPO = Path(__file__).resolve().parent.parent

results: list[bool] = []


def chk(ok: bool, msg: str) -> None:
    print(f"  {'✅' if ok else '❌'} {msg}")
    results.append(bool(ok))


# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------

def parse_css(text: str) -> list[tuple[list[str], dict[str, str]]]:
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


def css_files() -> list[Path]:
    files = list((REPO / "app" / "static" / "core").glob("*.css"))
    files += list((REPO / "app" / "static" / "core" / "wizard").glob("*.css"))
    for c in resolve_cards():
        p = Path(c["dir"]) / "static" / "app.css"
        if p.is_file():
            files.append(p)
    return files


RULES: list[tuple[list[str], dict[str, str]]] = []
for _f in css_files():
    RULES += parse_css(_f.read_text(encoding="utf-8", errors="replace"))


def display_of(cls: str) -> str:
    v = ""
    for sels, d in RULES:
        if f".{cls}" in sels and "display" in d:
            v = d["display"]
    return v


def compensated(cls: str) -> bool:
    """有没有 ``.cls[hidden]`` / ``.cls.hidden`` 的 display:none 补偿规则。"""
    for sels, d in RULES:
        for s in sels:
            if s in (f".{cls}[hidden]", f".{cls}.hidden") and \
                    d.get("display", "").split("!")[0].strip() == "none":
                return True
    return False


# ---------------------------------------------------------------------------
# 模板 → id → 类
# ---------------------------------------------------------------------------

def id_to_classes() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    templates = [REPO / "app" / "static" / "index.html.template"]
    for c in resolve_cards():
        templates.append(Path(c["dir"]) / "static" / "template.html")
    for t in templates:
        if not t.is_file():
            continue
        html = t.read_text(encoding="utf-8")
        for m in re.finditer(r"<(\w+)([^>]*?)>", html):
            a = m.group(2)
            cm = re.search(r'class="([^"]*)"', a)
            im = re.search(r'id="([^"]+)"', a)
            if cm and im:
                out.setdefault(im.group(1), set()).update(cm.group(1).split())
    return out


ID2CLASS = id_to_classes()


# ---------------------------------------------------------------------------
# JS：谁在用属性写法切换 hidden
# ---------------------------------------------------------------------------

def js_files() -> list[Path]:
    files = list((REPO / "app" / "static").rglob("*.js"))
    for c in resolve_cards():
        files += list(Path(c["dir"]).rglob("*.js"))
    return files


def property_toggled() -> dict[str, set[str]]:
    """``{类名: {切换它的变量名, …}}`` —— 只认 ``<var>.hidden = true/false``。"""
    out: dict[str, set[str]] = {}
    for f in js_files():
        js = f.read_text(encoding="utf-8", errors="replace")
        # 变量 ← 元素：<var> = xxx("id") / querySelector(".cls")
        binds: dict[str, set[str]] = {}
        for m in re.finditer(
                r'([A-Za-z_$][\w$]*)\s*=\s*[A-Za-z0-9_.$]*\(?\s*["\']([^"\']+)["\']', js):
            var, target = m.group(1), m.group(2)
            binds.setdefault(var, set()).add(target)
        for m in re.finditer(
                r'([A-Za-z_$][\w$]*)\s*\.\s*hidden\s*=\s*(?:true|false)', js):
            var = m.group(1)
            for target in binds.get(var, set()):
                classes: set[str] = set()
                if target.startswith("."):
                    classes.add(target[1:])
                classes |= ID2CLASS.get(target, set())
                for c in classes:
                    out.setdefault(c, set()).add(var)
    return out


def main() -> int:
    toggled = property_toggled()
    print(f"=== 用 hidden 属性切换的元素（{len(toggled)} 个类）===")
    for cls in sorted(toggled):
        d = display_of(cls)
        if not d:
            continue
        base = d.split("!")[0].strip()
        if base == "none":
            continue
        chk(compensated(cls),
            f".{cls} 有 display:{d} → 需要 [hidden] 补偿规则"
            f"（切换处: {sorted(toggled[cls])[:3]}）")

    # 反向：模板里带 hidden 属性、且类有 display 规则的元素，一并列出，
    # 免得将来 JS 改成属性写法时又踩一次。
    print("\n=== 模板里带 hidden 属性、且类有 display 规则的元素 ===")
    for eid, classes in sorted(ID2CLASS.items()):
        for c in sorted(classes):
            d = display_of(c)
            if not d or d.split("!")[0].strip() == "none":
                continue
            note = "已补偿" if compensated(c) else "未补偿（属性写法会失效）"
            print(f"    #{eid:<26} .{c:<24} display:{d:<12} {note}")

    bad = results.count(False)
    print(f"\n  → {len(results) - bad}/{len(results)} 项通过")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
