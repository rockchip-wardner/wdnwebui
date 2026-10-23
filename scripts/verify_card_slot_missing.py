#!/usr/bin/env python3
"""卡位占位符泄漏的回归验证（离线，不占 NPU、不起浏览器）。

背景（真机复现）：删掉 ``wdnapps_hdmi_processor`` / ``wdnhid`` 两个仓库源码后，
首页上凭空多出一行 ::

    {% card_slot "usb-hid" %}

``{% ... %}`` 在 HTML 里**没有任何特殊含义** —— 浏览器把它当普通文本原样显示。
根因：``index.html.template`` 里的卡位占位符只对**已发现**的卡生效；插件包
被卸载 / 源码被删之后那张卡不在 ``discover()`` 结果里，占位符就没人替换，
于是模板标记漏到了页面上。

这里验四件事：

1. 用**真实**的模板 + **真实**的卡片发现结果拼装，输出里**没有**任何残留的
   ``{% card_slot ... %}`` 原始占位符（核心回归点）。
2. 缺卡的占位符被换成 HTML 注释（浏览器不可见，但源码里留个线索）。
3. **在**的卡照旧注入页面片段 —— 不能为了修漏而把好卡也删了。
4. 纯函数层面：缺卡的占位符被摘掉，别的内容一字不动。

用法::

    .venv/bin/python scripts/verify_card_slot_missing.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.html_assembly import (  # noqa: E402
    _CARD_SLOT_RE,
    _drop_unmatched_card_slots,
    assemble_index_html,
)
from app.core.manifest import discover  # noqa: E402

_FAILED: list[str] = []

# 模板里出现过的占位符长这样：{% card_slot "usb-hid" %}
_ANY_SLOT_RE = re.compile(r"\{%\s*card_slot\b")


def check(ok: bool, label: str, detail: str = "") -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {label}")
    if not ok:
        if detail:
            print(f"       {detail}")
        _FAILED.append(label)
    return ok


def main() -> int:
    manifests = discover()
    ids = {m.id for m in manifests}
    print(f"=== 已发现卡片 ({len(ids)}) ===")
    for m in manifests:
        print(f"  {m.id:28s} status={m.status}")

    print("\n=== 1. 真实拼装：输出里不得残留原始占位符 ===")
    html = assemble_index_html(manifests)
    leftovers = _ANY_SLOT_RE.findall(html)
    check(not leftovers,
          "首页 HTML 里没有残留的 {% card_slot ... %} 占位符",
          f"残留 {len(leftovers)} 处: {leftovers[:5]}")
    # 单独点一下本次事故的那张卡
    check("{% card_slot \"usb-hid\" %}" not in html,
          "事故卡 usb-hid 的占位符没有漏到页面上",
          '输出里仍有 {% card_slot "usb-hid" %}')

    print("\n=== 2. 缺卡的占位符 → HTML 注释（浏览器不可见）===")
    comments = re.findall(r"<!-- card_slot \"[^\"]+\": card not installed -->", html)
    template_text = Path(ROOT / "app" / "static" / "index.html.template").read_text(
        encoding="utf-8")
    template_ids = set(re.findall(r'\{%\s*card_slot\s+"([^"]+)"\s*%\}', template_text))
    missing = sorted(template_ids - ids)
    print(f"  模板里的卡位: {sorted(template_ids)}")
    print(f"  当前缺席的卡: {missing or '（无）'}")
    check(len(comments) == len(missing),
          "缺席卡位数 == 被替换成注释的占位符数",
          f"注释 {len(comments)} 个 vs 缺席 {len(missing)} 张")
    for cid in missing:
        check(f'<!-- card_slot "{cid}": card not installed -->' in html,
              f"缺席卡 {cid} 的占位符已换成注释")

    print("\n=== 3. 在册的卡照旧注入页面片段 ===")
    for m in manifests:
        if m.id not in template_ids:
            continue
        check(f'id="page-{m.id}"' in html,
              f"卡片 {m.id} 的页面片段已注入",
              f'输出里找不到 id="page-{m.id}"')

    print("\n=== 4. 纯函数：只摘占位符，别的内容一字不动 ===")
    sample = (
        '<div class="keep">原样保留</div>\n'
        '      {% card_slot "usb-hid" %}\n'
        '      <p>另一段</p>\n'
    )
    out = _drop_unmatched_card_slots(sample)
    check("{% card_slot" not in out, "缺卡占位符已被摘掉")
    check('<div class="keep">原样保留</div>' in out, "周围内容一字未动")
    check("<p>另一段</p>" in out, "后面的内容也一字未动")
    check("<!-- card_slot \"usb-hid\": card not installed -->" in out,
          "替换成可追溯的 HTML 注释")
    # 幂等：再跑一遍不会二次替换
    check(_drop_unmatched_card_slots(out) == out, "重复调用幂等")
    # 正则本身能认得各种写法
    for token in ('{% card_slot "a" %}', '{%card_slot "b"%}', '{%  card_slot  "c"  %}'):
        m = _CARD_SLOT_RE.search(token)
        check(m is not None, f"正则能识别 {token!r}")

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
