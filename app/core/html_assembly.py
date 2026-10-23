"""首页 HTML 拼装 —— 纯函数，**无副作用**。

``/`` 路由每次请求都重新拼一遍 ``index.html``：把
``app/static/index.html.template`` 里的占位符换成各张卡的
``static/template.html`` 片段 + CSS 链接 + manifest JSON。

单独成模块（而不是留在 ``app/main.py`` 里）有两个原因：

1. **可测**：拼装是纯字符串操作，不该为了测它去 import ``app.main`` ——
   那里面的模块级 ``app = create_app()`` 会 boot 算力、拉起
   ``compute_worker`` 子进程去抢 NPU，和正在跑的服务打架。verify 脚本
   要能离线跑（``scripts/verify_card_slot_missing.py``）。
2. **分层**：``app/main.py`` 只管 FastAPI 接线（路由 / 挂载 / 生命周期），
   拼装细节归 core。

占位符协议（``index.html.template`` 里有三种）：

* ``{% card_slot "<card_id>" %}`` → 该卡 ``static/template.html`` 的内容
* ``{{ card_css_links }}``          → 每张卡一个 ``<link rel=stylesheet>``
* ``{{ app_manifests }}``           → ``window.__APP_MANIFESTS__`` 的 JSON

**卡片缺席时占位符必须被摘掉**（见 :func:`_drop_unmatched_card_slots`）——
``{% ... %}`` 在 HTML 里就是普通文本，不摘就会原样显示在页面上。
"""
from __future__ import annotations

import html
import json
import logging
import re
from pathlib import Path

from app.core.manifest import CardManifest

LOG = logging.getLogger("core.html_assembly")

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
INDEX_TEMPLATE_PATH = _STATIC_DIR / "index.html.template"


def assemble_index_html(manifests: list[CardManifest]) -> str:
    """Build the index.html response by injecting card sections + manifests.

    The template has two types of placeholders:
      - ``{% card_slot "<page_id>" %}`` — replaced with that card's
        ``static/template.html`` content.
      - ``{{ card_css_links }}`` — replaced with one ``<link>`` per
        card's CSS file.
      - ``{{ app_manifests }}`` — replaced with the JSON dump of
        ``window.__APP_MANIFESTS__`` (card metadata for the front-end).

    Cards in ``status != "ok"`` are still rendered (their page
    exists) but flagged in ``__APP_MANIFESTS__`` so the UI can show
    a placeholder for the broken bits.

    Placeholders whose card is **not** discovered are dropped from the
    output — see :func:`_drop_unmatched_card_slots` for why that matters.
    """
    template = INDEX_TEMPLATE_PATH.read_text(encoding="utf-8")

    # 1. card_slot injection
    for m in manifests:
        token = '{% card_slot "' + m.id + '" %}'
        if token not in template:
            LOG.warning("[%s] no {{card_slot}} placeholder in index.html.template", m.id)
            continue
        template_path = m.static_dir / "template.html" if m.static_dir else None
        if template_path and template_path.is_file():
            content = template_path.read_text(encoding="utf-8")
            # Strip any leading whitespace so the section is at the right indent
            content = content.lstrip()
            template = template.replace(token, content, 1)
        else:
            # Empty section placeholder if template.html missing
            placeholder = (
                f'<section id="page-{m.id}" class="page hidden" data-page="{m.id}">'
                f'<header class="page-header"><h2>{html.escape(m.title)}</h2></header>'
                f'<div class="placeholder-card"><p>本应用的页面模板未提供。</p></div>'
                f'</section>'
            )
            template = template.replace(token, placeholder, 1)
            LOG.warning("[%s] template.html missing — injected empty placeholder", m.id)

    # 1b. 摘掉**没被替换掉**的 card_slot 占位符。
    #     占位符只对**已发现**的卡生效：插件包被卸载 / 源码被删（删掉
    #     wdnapps_hdmi_processor 之后 usb-hid 卡就没了）时，模板里那一行
    #     占位符没人替换 —— 而 ``{% ... %}`` 在 HTML 里就是**普通文本**，
    #     浏览器会把它原样显示在首页上（症状：页面多出一行
    #     ``{% card_slot "usb-hid" %}``）。模板标记漏给用户看是不能接受的。
    template = _drop_unmatched_card_slots(template)

    # 2. CSS link tags (8-A: all card CSS loaded eagerly at boot)
    css_links: list[str] = []
    for m in manifests:
        for url in m.frontend_css:
            css_links.append(
                f'  <link rel="stylesheet" href="{html.escape(url)}" />'
            )
    template = template.replace(
        "{{ card_css_links }}",
        "\n".join(css_links) if css_links else "  <!-- no card CSS -->",
    )

    # 3. Manifests JSON for window.__APP_MANIFESTS__
    manifests_data = {
        "cards": [_manifest_to_dict(m) for m in manifests],
    }
    manifests_json = json.dumps(manifests_data, ensure_ascii=False)
    template = template.replace("{{ app_manifests }}", manifests_json)

    # 4. Cache-bust static assets (?v=<mtime>) — StaticFiles 默认不发
    # Cache-Control, 浏览器会按 Last-Modified 启发式缓存; 用文件 mtime 作
    # 版本串使 URL 随改动变化, 避免改前端后需手动硬刷新。覆盖 core/ 与
    # system/（算力配置页的 system.js 在后者，早先漏掉它会让改完算力页
    # 前端后浏览器仍跑旧副本，表现为"修复没生效"）。
    for prefix, root in _STAMP_DIRS.items():
        template = _ASSET_RE.sub(_make_stamp(root, prefix), template)

    return template


# 模板里的卡位占位符：``{% card_slot "<card_id>" %}``
_CARD_SLOT_RE = re.compile(r'\{%\s*card_slot\s+"([^"]+)"\s*%\}')


def _drop_unmatched_card_slots(template: str) -> str:
    """把**没被替换掉**的 ``{% card_slot "..." %}`` 占位符从模板里摘掉。

    为什么必须摘：卡位占位符只对**已发现**的卡生效。插件包被卸载、源码被
    删、或 manifest 校验不过（插件 import 失败）时，那一行的占位符没人替换
    —— 而 ``{% ... %}`` 在 HTML 里没有任何特殊含义，浏览器会把它当**普通
    文本**原样显示在首页上。症状是页面上凭空多出一行
    ``{% card_slot "usb-hid" %}``，用户看到的是模板语法泄漏。

    替代成 HTML 注释而不是直接删行：注释在浏览器里不可见（问题消失），
    但源码里还留着一行「这里原本有 usb-hid 的卡位」，将来把插件装回来时
    一眼能看出该往哪儿放。删行则连这点线索都不剩。

    放在 card_slot 注入循环**之后**调用：那一轮已经把所有已发现卡的
    占位符换成了真正的页面片段，所以此处剩下的每一个占位符都属于
    「模板里有、卡却没有」——正是要摘掉的那一类。
    """
    def _sub(match: re.Match) -> str:
        card_id = match.group(1)
        LOG.warning(
            "card_slot 占位符 %r 没有对应的卡片（插件未安装 / manifest 缺失 / "
            "插件 import 失败），已从首页移除", card_id,
        )
        return f'<!-- card_slot "{card_id}": card not installed -->'

    return _CARD_SLOT_RE.sub(_sub, template)


def _make_stamp(root: Path, prefix: str):
    def stamp(match: re.Match) -> str:
        attr, rel = match.group("attr"), match.group("rel")
        try:
            ver = int((root / rel).stat().st_mtime)
        except OSError:
            return match.group(0)
        return f'{attr}="/static/{prefix}/{rel}?v={ver}"'
    return stamp


# 匹配 template 里对 /static/{core,system}/** 的 href/src（尚未带 ?v= 的）。
_ASSET_RE = re.compile(
    r'(?P<attr>href|src)="/static/(?:core|system)/(?P<rel>[^"?]+)"')
_STAMP_DIRS = {
    "core": _STATIC_DIR / "core",
    "system": _STATIC_DIR.parent / "system" / "static",
}


def _manifest_to_dict(m: CardManifest) -> dict:
    """Build the per-card dict that gets serialized to the front-end."""
    return {
        "id": m.id,
        "title": m.title,
        "preview_video": m.preview_video,
        "software_capabilities": m.software_capabilities,
        "hardware_capabilities": m.hardware_capabilities,
        # 能力组：同一组内的能力必须由**同一个**模型同时满足（派生自
        # 该卡的 wizard.json）。前端用它做「一个实例都满足」而不是
        # 「两个实例各满足一半」的就绪判定。
        "capability_groups": m.capability_groups,
        "frontend": {
            "template": m.raw["frontend"]["template"],
            "js": m.frontend_js,
            "css": m.frontend_css,
        },
        "demo_guard": {
            "active_state_keys": m.demo_guard_active_state_keys,
            "stop_endpoints": m.demo_guard_stop_endpoints,
            "stop_sync_endpoints": m.demo_guard_stop_sync_endpoints,
        },
        # .get() 是故意的：卡片包可以被用户自己装进来，manifest 的
        # prompts 段缺失是迟早的事。这里是首页渲染的必经路径，一个 KeyError
        # 就是**整个首页 500** —— 而不是"这张卡的提示词用不上"。
        # manifest._validate 已经把缺 prompts 当可选处理，这里保持一致。
        "prompts": m.raw.get("prompts") or {},
        "status": m.status,
        "errors": m.errors,
        "hidden": m.hidden,
        "display_order": m.display_order,
        # 应用一句话功能简介,缺失则 framework 不渲染 .app-card-desc 标签
        "description": m.raw.get("description", ""),
        # 应用层声明的向导配置文件路径(通常是 "wizard.json",
        # 与 manifest.json 同目录);缺失 → framework tryMount 零开销跳过。
        "wizard_config": str(m.raw.get("wizard_config", "")).strip(),
    }
