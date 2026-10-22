"""Card manifest discovery + validation (v0.4 plugin refactor).

A *card* is a self-contained application with its own ``manifest.json``
describing its metadata (id, title, capabilities, backend entry points,
frontend assets). The plugin loader produces a list of validated
``CardManifest`` objects that ``main.py`` then uses to wire up FastAPI
routes + static mounts + frontend asset references.

Two discovery channels feed the same validation pipeline:

1. **Built-in** — ``app/apps/<card_id>/manifest.json`` scanned from disk.
   After the v0.25 split this holds only ``text_chat`` + ``tts``.
2. **Installed plugins** — any distribution advertising an entry point in
   the ``wdnwebui.cards`` group. The entry point *name* is the card id and
   its *value* is the importable card module; the card directory is that
   module's parent directory. Because ``_validate()`` derives
   ``static_dir = card_dir / "static"`` and ``main.py`` mounts
   ``/static/apps/<id>`` → ``static_dir``, a plugin package needs no
   special support at all: keep the very same on-disk layout
   (``<pkg>/cards/<card_id>/{manifest.json, api.py, static/}``) inside
   site-packages and every URL, route and validator keeps working.

Both channels therefore share one contract — *a directory containing
``manifest.json`` plus a ``static/`` subdir*, located wherever the package
happens to live. Only ``backend.module`` in the JSON differs: it must name
the plugin's own importable module rather than an ``app.apps.*`` path.

Validation covers:

* required fields present + correctly typed
* ``id`` globally unique across all manifests
* ``software_capabilities`` references existing ``swcap_*`` in MODEL_PRESETS
* ``hardware_capabilities`` references existing ``hwcap_*`` in HARDWARE_CAPABILITIES
* ``model_paths[required=true]`` paths actually exist on disk
* ``requires_core`` modules importable
* ``backend.module`` importable + ``register_api`` / ``register_shutdown`` callables

Non-fatal: cards with errors are reported but kept
and excluded from the app grid (other cards continue to work).
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

LOG = logging.getLogger("core.manifest")

_APP_ROOT = Path(__file__).resolve().parent.parent
_APPS_DIR = _APP_ROOT / "apps"

# Entry-point group used by out-of-tree card distributions. Each point is
# ``<card_id> = <importable card module>``; see module docstring.
_ENTRY_POINT_GROUP = "wdnwebui.cards"


@dataclass
class CardManifest:
    """Validated card metadata, ready to drive registration."""

    # Raw fields (as declared in JSON)
    raw: dict[str, Any]
    # Resolved location
    dir: Path
    static_dir: Optional[Path]

    # ---- Identity ----
    id: str
    title: str

    # ---- Preview ----
    preview_video: str

    # ---- Capabilities ----
    software_capabilities: list[str]
    hardware_capabilities: list[str]

    # ---- Backend ----
    backend_module: str
    backend_register_api: str
    backend_register_shutdown: str

    # ---- Frontend ----
    frontend_js: list[str]
    frontend_css: list[str]

    # ---- DemoGuard ----
    demo_guard_active_state_keys: list[str]
    demo_guard_stop_endpoints: list[dict[str, str]]
    demo_guard_stop_sync_endpoints: list[dict[str, str]]

    # ---- Misc ----
    requires_core: list[str]

    # ---- Status (defaults after non-defaults) ----
    status: str = "ok"  # "ok" | "template_missing" | "fatal"
    errors: list[str] = field(default_factory=list)
    # ---- Visibility ----
    # ``hidden: true`` 让该卡片从首页应用网格消失(代码 / API / 静态资源
    # 全部保留,前端直接 #/<id> 仍可访问,反序列化时想恢复只需置 false)。
    # 用于"先下线 / 等版本升级"的过渡期。比直接 ``remove_card.py`` 安全。
    hidden: bool = False
    # ``display_order: int`` 决定首页应用网格的渲染顺序(升序,小→上
    # 左)。未填或相等时按 manifest.json 字典序兜底;典型用法见
    # ``audio_analysis`` / ``video_detection`` 等 manifest。
    display_order: int = 9999


# --------------------------------------------------------------------------- #
# Required schema fields + types                                               #
# --------------------------------------------------------------------------- #
_REQUIRED_FIELDS: dict[str, type] = {
    "id":       str,
    "title":    str,
    "backend":  dict,
    "frontend": dict,
    "demo_guard": dict,
    "prompts":  dict,
    "requires_core": list,
}

# Fields whose absence triggers a fatal error
_FATAL_FIELDS = {
    "id", "title", "backend.module", "backend.register_api",
    "frontend.template", "uploads_dir",
}


def discover() -> list[CardManifest]:
    """Scan ``app/apps/*/manifest.json`` and return validated manifests.

    Cards with fatal errors are reported (LOG.error) and skipped.
    Cards with non-fatal errors are reported (LOG.warning) but kept
    (caller decides whether to exclude from app grid).
    """
    if not _APPS_DIR.is_dir():
        LOG.warning("apps/ directory not found at %s — no cards loaded", _APPS_DIR)
        return []

    raw_manifests: list[tuple[Path, dict[str, Any]]] = []
    for card_dir in sorted(_APPS_DIR.iterdir()):
        if not card_dir.is_dir():
            continue
        if card_dir.name.startswith("_") or card_dir.name.startswith("."):
            continue
        manifest_path = card_dir / "manifest.json"
        if not manifest_path.is_file():
            LOG.warning("[skip] %s: no manifest.json", card_dir.name)
            continue
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except json.JSONDecodeError as e:
            LOG.error("[skip] %s: manifest.json invalid JSON: %s",
                      card_dir.name, e)
            continue
        raw_manifests.append((card_dir, raw))

    # Installed plugin cards (pip / pip -e installed distributions that
    # advertise the ``wdnwebui.cards`` entry-point group).
    raw_manifests.extend(_discover_from_entry_points())

    # First pass: validate + check id uniqueness.
    seen_ids: set[str] = set()
    validated: list[CardManifest] = []
    known_sw_caps = _known_software_capabilities()
    known_hw_caps = _known_hardware_capabilities()

    for card_dir, raw in raw_manifests:
        m, errors = _validate(
            card_dir, raw, known_sw_caps, known_hw_caps,
        )
        if errors:
            for e in errors:
                LOG.error("[%s] %s", card_dir.name, e)
            if any(f.startswith("FATAL") for f in errors):
                continue
        if m is None:
            continue
        if m.id in seen_ids:
            LOG.error("[FATAL] duplicate card id %r", m.id)
            continue
        seen_ids.add(m.id)
        validated.append(m)

    # Second pass: check requires_core + backend.module importability.
    for m in validated:
        for core_mod in m.requires_core:
            full = f"app.core.{core_mod}" if not core_mod.startswith("app.") else core_mod
            try:
                importlib.import_module(full)
            except Exception as e:  # noqa: BLE001
                m.status = "fatal"
                m.errors.append(
                    f"requires_core module {full!r} not importable: {e}"
                )
        try:
            importlib.import_module(m.backend_module)
        except Exception as e:  # noqa: BLE001
            m.status = "fatal"
            m.errors.append(
                f"backend.module {m.backend_module!r} not importable: {e}"
            )

    return validated


def _discover_from_entry_points() -> list[tuple[Path, dict[str, Any]]]:
    """Collect ``(card_dir, manifest_dict)`` from installed plugin packages.

    Each ``wdnwebui.cards`` entry point names a card module
    (``video_detection = "wdnapps_video_analyzer.cards.video_detection"``).
    We import that module purely to locate its package directory, then read
    ``manifest.json`` from it — so the manifest stays a plain JSON file and
    stays diff-reviewable exactly as it was when the card lived in-tree.

    Failure policy: **never** propagate. A plugin whose optional dependency is
    missing, or whose package is half-removed, must degrade to "card absent"
    rather than take the whole web UI down at boot. Each failure is logged and
    the remaining plugins still load.
    """
    try:
        from importlib.metadata import entry_points
    except Exception as e:  # noqa: BLE001  (py<3.10)
        LOG.warning("entry_points unavailable, plugins not loaded: %s", e)
        return []

    try:
        eps = list(entry_points(group=_ENTRY_POINT_GROUP))
    except Exception as e:  # noqa: BLE001
        LOG.warning("could not enumerate %s entry points: %s",
                    _ENTRY_POINT_GROUP, e)
        return []

    if not eps:
        return []

    out: list[tuple[Path, dict[str, Any]]] = []
    for ep in eps:
        try:
            mod = importlib.import_module(ep.value)
        except Exception as e:  # noqa: BLE001
            LOG.error("[%s] plugin entry point %r not importable: %s",
                      ep.name, ep.value, e)
            continue

        mod_file = getattr(mod, "__file__", None)
        if not mod_file:
            LOG.error("[%s] plugin module %r has no __file__ (namespace pkg?)",
                      ep.name, ep.value)
            continue
        card_dir = Path(mod_file).resolve().parent
        manifest_path = card_dir / "manifest.json"
        if not manifest_path.is_file():
            LOG.error("[%s] no manifest.json in %s", ep.name, card_dir)
            continue
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            LOG.error("[%s] manifest.json unreadable (%s): %s",
                      ep.name, manifest_path, e)
            continue

        # The entry-point name is the card id; a mismatch means the plugin
        # was renamed in pyproject.toml but not in manifest.json (or vice
        # versa), which silently produces two cards or a phantom id.
        declared = str(raw.get("id", "")).strip()
        if declared != ep.name:
            LOG.error("[%s] FATAL: manifest id %r != entry-point name %r",
                      ep.name, declared, ep.name)
            continue
        out.append((card_dir, raw))

    if out:
        LOG.info("loaded %d plugin card(s) from %s: %s",
                 len(out), _ENTRY_POINT_GROUP,
                 ", ".join(str(raw["id"]) for _, raw in out))
    return out


def _validate(
    card_dir: Path,
    raw: dict[str, Any],
    known_sw_caps: set[str],
    known_hw_caps: set[str],
) -> tuple[Optional[CardManifest], list[str]]:
    """Build a CardManifest or return (None, errors)."""
    errors: list[str] = []
    for fname, ftype in _REQUIRED_FIELDS.items():
        if fname not in raw:
            if fname in _FATAL_FIELDS:
                errors.append(f"FATAL: missing required field {fname!r}")
            else:
                errors.append(f"missing recommended field {fname!r}")
                continue
        v = raw[fname]
        if fname in _FATAL_FIELDS and (v is None or v == ""):
            errors.append(f"FATAL: required field {fname!r} is empty")
            continue
        if fname in _FATAL_FIELDS and not isinstance(v, ftype):
            errors.append(
                f"FATAL: field {fname!r} has wrong type, "
                f"expected {ftype.__name__}"
            )
            continue

    if any(e.startswith("FATAL") for e in errors):
        return None, errors

    # Nested fields
    backend = raw["backend"]
    frontend = raw["frontend"]
    demo_guard = raw["demo_guard"]
    prompts = raw["prompts"]
    if "module" not in backend:
        errors.append("FATAL: backend.module missing")
    if "register_api" not in backend:
        errors.append("FATAL: backend.register_api missing")
    if "template" not in frontend:
        errors.append("FATAL: frontend.template missing")

    if any(e.startswith("FATAL") for e in errors):
        return None, errors

    # Capability references
    sw_caps = raw.get("software_capabilities") or []
    if not isinstance(sw_caps, list):
        errors.append("software_capabilities must be a list")
        sw_caps = []
    for cap in sw_caps:
        if cap not in known_sw_caps:
            errors.append(
                f"FATAL: unknown software_capability {cap!r}"
            )
    hw_caps = raw.get("hardware_capabilities") or []
    if not isinstance(hw_caps, list):
        errors.append("hardware_capabilities must be a list")
        hw_caps = []
    for cap in hw_caps:
        if cap not in known_hw_caps:
            errors.append(f"unknown hardware_capability {cap!r}")

    if any(e.startswith("FATAL") for e in errors):
        return None, errors

    static_dir = card_dir / "static"
    has_static = static_dir.is_dir()

    template_path = frontend.get("template", "")
    if has_static and template_path:
        # template_path is something like "/static/apps/<id>/template.html"
        # — confirm the file exists at the static-relative path.
        rel = _static_url_to_rel(template_path)
        if rel is not None:
            file_path = static_dir / rel
            if not file_path.is_file():
                errors.append(
                    f"frontend.template file missing: {file_path}"
                )

    m = CardManifest(
        raw=raw,
        dir=card_dir,
        static_dir=static_dir if has_static else None,
        id=str(raw["id"]).strip(),
        title=str(raw["title"]).strip(),
        preview_video=str(raw.get("preview_video", "")).strip(),
        software_capabilities=list(sw_caps),
        hardware_capabilities=list(hw_caps),
        backend_module=str(backend["module"]).strip(),
        backend_register_api=str(backend["register_api"]).strip(),
        backend_register_shutdown=str(backend.get("register_shutdown", "")).strip(),
        frontend_js=list(frontend.get("js") or []),
        frontend_css=list(frontend.get("css") or []),
        demo_guard_active_state_keys=list(demo_guard.get("active_state_keys") or []),
        demo_guard_stop_endpoints=list(demo_guard.get("stop_endpoints") or []),
        demo_guard_stop_sync_endpoints=list(demo_guard.get("stop_sync_endpoints") or []),
        requires_core=list(raw.get("requires_core") or []),
        hidden=bool(raw.get("hidden", False)),
        display_order=int(raw.get("display_order", 9999)),
    )
    return m, errors


def _static_url_to_rel(url: str) -> Optional[str]:
    """``/static/apps/<id>/foo.html`` → ``foo.html``.

    The on-disk layout is ``app/apps/<id>/static/<rest>``; we strip the
    ``/static/apps/<id>/`` prefix and use ``<rest>`` relative to ``static/``.
    """
    if not url:
        return None
    if url.startswith("/static/apps/"):
        rest = url[len("/static/apps/"):]
        # Strip the leading <id>/ prefix — files live directly under static/
        if "/" in rest:
            return rest.split("/", 1)[1]
        return rest
    return None


def _known_software_capabilities() -> set[str]:
    """Return the set of swcap_* strings declared by any MODEL_PRESETS entry.

    Lazily imported so the manifest loader doesn't drag in the entire
    compute registry (with its wdndds native deps) at import time.
    """
    try:
        from app.core.compute.registry import MODEL_PRESETS
    except Exception as e:  # noqa: BLE001
        LOG.warning("could not import MODEL_PRESETS: %s", e)
        return set()
    caps: set[str] = set()
    for preset in MODEL_PRESETS.values():
        for cap in preset.get("capability_types") or []:
            caps.add(cap)
    return caps


def _known_hardware_capabilities() -> set[str]:
    try:
        from app.core.compute.registry import HARDWARE_CAPABILITIES
    except Exception as e:  # noqa: BLE001
        LOG.warning("could not import HARDWARE_CAPABILITIES: %s", e)
        return set()
    return {e["capability_type"] for e in HARDWARE_CAPABILITIES}


def call_register_api(manifest: CardManifest, fastapi_app: Any) -> None:
    """Call ``manifest.backend_register_api(app)`` with the given FastAPI app.

    Looks up the function by name on the imported backend module.
    Raises ``AttributeError`` if missing, ``Exception`` if call fails.
    """
    mod = importlib.import_module(manifest.backend_module)
    fn = getattr(mod, manifest.backend_register_api, None)
    if fn is None:
        raise AttributeError(
            f"card {manifest.id!r}: module {manifest.backend_module!r} "
            f"has no function {manifest.backend_register_api!r}"
        )
    fn(fastapi_app)


def shutdown_callbacks() -> list[tuple[CardManifest, callable]]:
    """Return [(manifest, callable), ...] for each card that has a
    ``register_shutdown`` function. main.py registers each on app shutdown."""
    out: list[tuple[CardManifest, callable]] = []
    for manifest in discover():
        if not manifest.backend_register_shutdown:
            continue
        try:
            mod = importlib.import_module(manifest.backend_module)
        except Exception:
            continue
        fn = getattr(mod, manifest.backend_register_shutdown, None)
        if fn is None:
            continue
        out.append((manifest, fn))
    return out
