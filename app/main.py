"""FastAPI application factory — plugin-driven (v0.4)."""
from __future__ import annotations

import html
import json
import logging
import os
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

    return template


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

    # 4. boot_compute_services — daemon thread (kept from v0.4 architecture)
    threading.Thread(
        target=get_registry().boot_compute_services,
        name="compute-boot",
        daemon=True,
    ).start()

    # 5. Mount per-card static dirs at /static/apps/<id>
    for m in manifests:
        if not m.static_dir or not m.static_dir.is_dir():
            continue
        try:
            app.mount(
                f"/static/apps/{m.id}",
                StaticFiles(directory=str(m.static_dir)),
                name=f"static-apps-{m.id}",
            )
        except Exception as e:  # noqa: BLE001
            LOG.warning("[%s] failed to mount static dir: %s", m.id, e)

    # 6. Mount framework-level static at /static/core + /static/system + /static
    core_static = STATIC_DIR / "core"
    if core_static.is_dir():
        app.mount(
            "/static/core",
            StaticFiles(directory=str(core_static)),
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
