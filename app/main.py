"""FastAPI application factory — plugin-driven (v0.4)."""
from __future__ import annotations

import html
import json
import logging
import os
import re
import subprocess
import threading
from pathlib import Path

# uvicorn's default LOGGING_CONFIG does not configure the root logger, so
# every ``LOG.info(...)`` from app.core.* / app.apps.* / app.system.* is
# silently swallowed without this. Keep this near the top of the module so
# it runs for both ``run.py`` and direct ``uvicorn app.main:app``.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

from fastapi import FastAPI
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.core.config import get_config
from app.core.manifest import (
    CardManifest,
    discover as discover_cards,
)
from app.core.compute.registry import get_registry

LOG = logging.getLogger("app.main")

STATIC_DIR = Path(__file__).parent / "static"
INDEX_TEMPLATE_PATH = STATIC_DIR / "index.html.template"


def _raise_nofile_limit() -> None:
    """把 soft ``RLIMIT_NOFILE`` 提到 hard 上限。

    放在 app 模块而非 ``run.py``：服务有三种入口（``python run.py`` /
    ``uvicorn app.main:app`` / systemd unit），挂在 run.py 上就漏掉后两种，
    而 ``sudo`` 还会把 soft 重置回 1024。1024 装不下多路视频 —— MPP 每路
    VideoCapture 约 17 个 fd，28 路就撞 ``ioctl alloc failed for Too many
    open files``。
    """
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft < hard:
            resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
            LOG.info("RLIMIT_NOFILE soft %d -> %d", soft, hard)
        elif soft < 4096:
            # hard 也抬不动（少数容器 / 老 systemd 配置），点明症状来源，
            # 否则 28 路崩溃时很难联想到 fd。
            LOG.warning(
                "RLIMIT_NOFILE soft=hard=%d，多路视频（约 17 fd/路）"
                "在 30 路左右就会 Too many open files；"
                "请用 LimitNOFILE=524288 的 systemd unit 或 "
                "`ulimit -n 524288` 启动", soft,
            )
    except Exception as exc:  # noqa: BLE001
        LOG.warning("提升 RLIMIT_NOFILE 失败（忽略）: %s", exc)


_raise_nofile_limit()


# --------------------------------------------------------------------------- #
# HTML assembly                                                               #
# --------------------------------------------------------------------------- #
def _assemble_index_html(manifests: list[CardManifest]) -> str:
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
    "core": Path(__file__).resolve().parent / "static" / "core",
    "system": Path(__file__).resolve().parent / "system" / "static",
}


class _NoCacheStaticFiles(StaticFiles):
    """StaticFiles that forces ``Cache-Control: no-cache`` on every response.

    卡片 JS/CSS 没有 URL 版本串，默认的启发式缓存会让前端改动"不生效"
    （用户看到的是上次缓存的老逻辑）。no-cache **不是** no-store —— 浏览器
    仍会缓存，但每次请求都带 ETag 回校验，未变 304、变了立即更新，兼顾
    性能与新鲜度。
    """

    def file_response(self, *args, **kwargs):  # type: ignore[override]
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


def _manifest_to_dict(m: CardManifest) -> dict:
    """Build the per-card dict that gets serialized to the front-end."""
    return {
        "id": m.id,
        "title": m.title,
        "preview_video": m.preview_video,
        "software_capabilities": m.software_capabilities,
        "hardware_capabilities": m.hardware_capabilities,
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
        "prompts": m.raw["prompts"],
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


# --------------------------------------------------------------------------- #
# FastAPI factory                                                             #
# --------------------------------------------------------------------------- #
def create_app() -> FastAPI:
    cfg = get_config()

    # Propagate WDNAI_MODELS_DIR from config so downstream SDKs pick up
    # the configured model root before any LLMSession is built.
    base = cfg.get_section("base", {})
    model_path = (base or {}).get("model_path")
    if isinstance(model_path, str) and model_path.strip():
        os.environ["WDNAI_MODELS_DIR"] = model_path

    # Best-effort: 板载 ES8388 codec Speaker PA 默认 mute,TTS 启动时
    # aplay 写出 PCM 但听不到声音。boot 时 un-mute ES8388 的 Speaker
    # control(失败不影响其它路径)。
    try:
        for cmd in (
            ["amixer", "-c", "1", "sset", "Speaker", "on"],
            ["amixer", "sset", "Speaker", "on"],
        ):
            subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=2.0, check=False,
            )
            break
    except Exception:                   # noqa: BLE001
        pass

    app = FastAPI(
        title="wdnwebui",
        version=__version__,
        description="wdnwebui service (plugin-driven frontend)",
    )

    # 1. Discover cards (manifest scan + validation)
    manifests = discover_cards()
    LOG.info(
        "discovered %d card(s): %s",
        len(manifests),
        ", ".join(m.id for m in manifests),
    )
    for m in manifests:
        if m.errors:
            for e in m.errors:
                LOG.error("[%s] %s", m.id, e)

    # 2. Mount each card's API + register shutdown
    for m in manifests:
        try:
            from app.core.manifest import call_register_api
            call_register_api(m, app)
        except Exception as e:  # noqa: BLE001
            LOG.exception("[%s] register_api failed: %s", m.id, e)
            m.errors.append(f"register_api failed: {e}")
            m.status = "fatal"

    # 3. Mount system pages (manual; not plugin-driven)
    from app.system import register_all as system_register_all
    system_register_all(app)

    # 3b. 帧判据共享 API —— vd / vtd / vtl 共用 VideoSession 单例，
    #     所以判据配置也只有一份。三张卡各自暴露端点会走样（改一处漏
    #     两处），故挂在 core 层由三张卡共用。video-retrieval（监控反查）
    #     只保留检索，没有判据，不在此列。
    from app.core.video.api import register_judge_api
    register_judge_api(app)

    # 4. boot_compute_services — daemon thread (kept from v0.4 architecture)
    threading.Thread(
        target=get_registry().boot_compute_services,
        name="compute-boot",
        daemon=True,
    ).start()

    # 5. Mount per-card static dirs at /static/apps/<id>
    #    卡片资源（app.js / app.css / template.html）不带 ?v= 版本串，
    #    StaticFiles 默认也不发 Cache-Control → 浏览器按 Last-Modified
    #    启发式缓存，改了卡片 JS 后仍用旧副本（症状：点击无反应 / 行为
    #    与源码不符，只能硬刷新）。挂载时显式发 no-cache：浏览器每次
    #    带 ETag 回来校验，未变则 304、变了立即拉新，既保性能又不陈旧。
    for m in manifests:
        if not m.static_dir or not m.static_dir.is_dir():
            continue
        try:
            app.mount(
                f"/static/apps/{m.id}",
                _NoCacheStaticFiles(directory=str(m.static_dir)),
                name=f"static-apps-{m.id}",
            )
        except Exception as e:  # noqa: BLE001
            LOG.warning("[%s] failed to mount static dir: %s", m.id, e)

    # 6. Mount framework-level static at /static/core + /static/system + /static
    # /static/core 同样走 _NoCacheStaticFiles：template 里虽然带了 ?v=<mtime>
    # 版本串，但浏览器在地址栏里手动导航或被 hashchange 切到同一页时，**仍**
    # 拿旧 index.html 跑（hash 变化不重新拉 index），于是 ?v= 不更新、JS 也就
    # 不变。no-cache 让浏览器每次都带 ETag 校验：URL 变了一定拉新，URL 没变
    # 也只是多一次 304。详见 _NoCacheStaticFiles 注释。
    core_static = STATIC_DIR / "core"
    if core_static.is_dir():
        app.mount(
            "/static/core",
            _NoCacheStaticFiles(directory=str(core_static)),
            name="static-core",
        )
    # System pages have their own static dir (not plugin-driven)
    system_static = Path(__file__).parent / "system" / "static"
    if system_static.is_dir():
        app.mount(
            "/static/system",
            StaticFiles(directory=str(system_static)),
            name="static-system",
        )
    app.mount(
        "/static",
        StaticFiles(directory=str(STATIC_DIR)),
        name="static-app",
    )

    @app.get("/")
    async def index() -> Response:
        # Re-assemble on every request to pick up manifest changes during
        # hot-reload. Cheap enough for the ~30 cards case.
        return Response(
            content=_assemble_index_html(discover_cards()),
            media_type="text/html; charset=utf-8",
            headers={"Cache-Control": "no-store, must-revalidate"},
        )

    @app.get("/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "version": __version__,
            "cards": [m.id for m in manifests],
            "version_status": {m.id: m.status for m in manifests},
        }

    @app.on_event("shutdown")
    async def _on_shutdown():
        # Card shutdown handlers
        for m in manifests:
            if not m.backend_register_shutdown:
                continue
            try:
                from app.core.manifest import shutdown_callbacks
                for manifest, fn in shutdown_callbacks():
                    if manifest.id == m.id:
                        try:
                            fn()
                        except Exception as e:  # noqa: BLE001
                            LOG.warning("[%s] shutdown: %s", m.id, e)
            except Exception as e:  # noqa: BLE001
                LOG.warning("[%s] shutdown import: %s", m.id, e)
        # Compute registry cleanup
        try:
            get_registry().close_all()
        except Exception:
            pass

    return app


app = create_app()
