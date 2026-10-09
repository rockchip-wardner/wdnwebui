"""Worker plugin discovery — one folder per model, manifest-declared.

Mirrors the card plugin system (:mod:`app.core.manifest`): a worker is a
directory under ``app/core/compute/workers/<id>/`` holding a
``manifest.json`` (declarative metadata) plus a ``compute_worker.py``
entry point that knows how to build that one model.

Why split this out
------------------
Until v0.9 the whole worker was one 2358-line ``worker.py`` that served
every model, with the model→spec mapping living in an ``if/elif`` chain
inside ``_build_session``. Adding a model meant editing that chain; the
declarative half of the metadata (``MODEL_PRESETS``) and the executable
half (``_build_session``) then had to be kept in sync by hand.

Splitting them makes a worker self-contained: the manifest is the only
place a model's identity is declared, and the worker entry point is the
only place its spec is built.

Compatibility
-------------
``app.core.compute.registry.MODEL_PRESETS`` still exists and is now
*derived* from these manifests, so the ~54 call sites across
``registry.py`` / ``config.py`` / ``compute.py`` / ``manifest.py`` keep
working unchanged.

Zero-coupling rule (same as cards): a worker may import ``app.core.*``
only. In particular it must NOT import ``app.apps.*`` or ``app.system.*``
— and it should not import :mod:`app.core.compute.registry` either, since
the registry spawns workers and that would close the cycle. Everything a
worker needs to know about itself lives in its own manifest.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

LOG = logging.getLogger("compute.workers")

WORKERS_DIR = Path(__file__).resolve().parent

#: Filename inside each worker folder. Kept as ``compute_worker.py`` on
#: purpose: the spawn dedup in ``registry.py`` and the process scan in
#: ``app/system/api/system_monitor.py`` both match this exact string in
#: ``/proc/<pid>/cmdline``. Renaming it means updating both.
ENTRY_FILENAME = "compute_worker.py"
MANIFEST_FILENAME = "manifest.json"


@dataclass
class WorkerManifest:
    """One model a worker can serve.

    ``id`` is the ``model_id`` used everywhere else (``config.jsonc``
    instance bindings, ``--model``, DDS service name), so it is the join
    key for the whole system.
    """

    id: str
    label: str
    capability_types: List[str] = field(default_factory=list)
    #: Short alias used for the process comm name (``worker:<alias>``).
    #: Linux truncates comm to 15 chars; ``registry._worker_comm_name``
    #: enforces the limit and appends a numeric suffix when it overflows.
    worker_name: str = ""
    #: Directory under ``WDNAI_MODELS_DIR`` holding the weights.
    model_dir_name: str = ""
    #: Default instance name written into a fresh ``config.jsonc``.
    instance_name: str = ""
    memory_estimate_mb: int = 0
    estimated_init_seconds: int = 60
    description: str = ""
    available_targets: List[str] = field(default_factory=list)
    #: ``True`` keeps a worker out of the default view while its code and
    #: entry point stay fully live — the worker-side analogue of a hidden
    #: card, and the safe way to stage a rollout.
    hidden: bool = False
    #: Sort key for the enumeration order (ascending). Defaults to
    #: ``display_order`` semantics used by cards.
    display_order: int = 9999
    #: Extra keys we do not model explicitly, preserved verbatim so a
    #: manifest can carry worker-specific metadata without a code change.
    extra: Dict[str, Any] = field(default_factory=dict)
    #: Absolute path of the worker folder.
    path: str = ""
    status: str = "ok"          # "ok" | "fatal"
    errors: List[str] = field(default_factory=list)

    # ------------------------------------------------------------------ #
    @property
    def entry_path(self) -> str:
        """Absolute path to this worker's entry script."""
        return os.path.join(self.path, ENTRY_FILENAME)

    def service_name(self, *, local_only: bool = False, pid: Optional[int] = None) -> str:
        """DDS service name this worker registers.

        ``local_only`` (set by the main process when spawning a
        same-host worker) appends the PID so the worker gets a private
        service instead of sharing the standard topic with LAN peers —
        otherwise FastDDS can route an in-process probe onto a peer.
        """
        base = f"wdndds/rpc/{self.id}"
        if local_only:
            return f"{base}/worker-{pid if pid is not None else os.getpid()}"
        return base

    def preset(self) -> Dict[str, Any]:
        """The ``MODEL_PRESETS``-shaped view of this manifest.

        Keeps ``capability_types`` and friends as lists so callers that
        mutate the result (e.g. ``list(preset["capability_types"])``)
        still get a copy, not the manifest's own list.
        """
        out = dict(self.extra)
        out.update({
            "id": self.id,
            "label": self.label,
            "capability_types": list(self.capability_types),
            "worker_name": self.worker_name,
            "model_dir_name": self.model_dir_name,
            "instance_name": self.instance_name,
            "memory_estimate_mb": self.memory_estimate_mb,
            "estimated_init_seconds": self.estimated_init_seconds,
            "description": self.description,
            "available_targets": list(self.available_targets),
            "entry_path": self.entry_path,
            "service_name": self.service_name(),
        })
        return out


# --------------------------------------------------------------------------- #
# Scanning
# --------------------------------------------------------------------------- #
def _validate(raw: Any, folder: Path) -> tuple[Optional[WorkerManifest], List[str]]:
    errors: List[str] = []
    if not isinstance(raw, dict):
        return None, ["manifest.json must contain a JSON object"]

    # ``id`` defaults to the folder name — same rule as card apps. Keeping
    # it implicit removes the chance of a manifest renaming itself away
    # from the directory the registry spawns out of.
    worker_id = raw.get("id") or folder.name
    if not isinstance(worker_id, str) or not worker_id.strip():
        return None, ["'id' 存在但不是非空字符串"]
    if worker_id != folder.name:
        errors.append(f"id {worker_id!r} 与目录名 {folder.name!r} 不一致")

    if not isinstance(raw.get("label"), str) or not raw["label"].strip():
        errors.append("'label' 缺失或为空")

    caps = raw.get("capability_types")
    if caps is None:
        errors.append("'capability_types' 缺失")
    elif not isinstance(caps, list) or not all(
            isinstance(c, str) and c.startswith("swcap_") for c in caps):
        errors.append("'capability_types' 必须是以 'swcap_' 开头的字符串列表")
    elif not caps:
        errors.append("'capability_types' 不能为空 —— worker 至少要提供一项能力")

    if errors:
        return None, errors

    known = {
        "id", "label", "capability_types", "worker_name", "model_dir_name",
        "instance_name", "memory_estimate_mb", "estimated_init_seconds",
        "description", "available_targets", "hidden", "display_order",
    }
    return WorkerManifest(
        id=worker_id,
        label=raw["label"],
        capability_types=list(caps),
        worker_name=raw.get("worker_name") or worker_id[:8],
        model_dir_name=raw.get("model_dir_name", ""),
        instance_name=raw.get("instance_name", ""),
        memory_estimate_mb=int(raw.get("memory_estimate_mb", 0)),
        estimated_init_seconds=int(raw.get("estimated_init_seconds", 60)),
        description=raw.get("description", ""),
        available_targets=list(raw.get("available_targets", [])),
        hidden=bool(raw.get("hidden", False)),
        display_order=int(raw.get("display_order", 9999)),
        extra={k: v for k, v in raw.items() if k not in known},
        path=str(folder),
    ), errors


def discover() -> List[WorkerManifest]:
    """Scan ``workers/*/manifest.json`` and return validated manifests.

    A malformed manifest is reported on that worker (``status="fatal"``)
    rather than raised: one bad folder must not take down discovery for
    every other model. Folders without a manifest are ignored entirely,
    and folders marked ``hidden`` are returned with ``hidden=True`` so
    callers can filter them out of the UI while keeping the code live.
    """
    found: List[WorkerManifest] = []
    if not WORKERS_DIR.is_dir():
        return found

    seen: Dict[str, str] = {}
    for folder in sorted(WORKERS_DIR.iterdir()):
        if not folder.is_dir() or folder.name.startswith((".", "_")):
            continue
        manifest_path = folder / MANIFEST_FILENAME
        if not manifest_path.is_file():
            continue
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            LOG.error("worker %s: unreadable manifest: %s", folder.name, exc)
            found.append(WorkerManifest(
                id=folder.name, label=folder.name, path=str(folder),
                status="fatal", errors=[f"manifest.json 解析失败: {exc}"],
            ))
            continue

        manifest, errors = _validate(raw, folder)
        if manifest is None:
            LOG.error("worker %s: invalid manifest: %s", folder.name,
                      "; ".join(errors))
            found.append(WorkerManifest(
                id=folder.name, label=str(raw.get("label", folder.name))
                if isinstance(raw, dict) else folder.name,
                path=str(folder), status="fatal", errors=errors,
            ))
            continue

        if manifest.id in seen:
            errors = [f"id {manifest.id!r} 与 {seen[manifest.id]} 重复"]
            manifest.status = "fatal"
            manifest.errors = errors
            LOG.error("worker %s: %s", folder.name, "; ".join(errors))

        entry = Path(manifest.entry_path)
        if not entry.is_file():
            manifest.status = "fatal"
            manifest.errors = manifest.errors + [
                f"入口文件缺失: {ENTRY_FILENAME}"]

        seen[manifest.id] = folder.name
        found.append(manifest)

    # Ascending display_order, then id — matches the card panel's rule.
    found.sort(key=lambda m: (m.display_order, m.id))
    return found


def load_one(worker_id: str) -> Optional[WorkerManifest]:
    """Return the manifest for ``worker_id``, or ``None`` if unknown."""
    for manifest in discover():
        if manifest.id == worker_id:
            return manifest
    return None


def model_presets() -> Dict[str, Dict[str, Any]]:
    """``MODEL_PRESETS``-shaped dict built from the manifests.

    This is what :data:`app.core.compute.registry.MODEL_PRESETS` now is —
    the single source of truth moved from a hand-written literal to the
    scanned manifests, and every existing call site keeps working.
    """
    out: Dict[str, Dict[str, Any]] = {}
    for manifest in discover():
        if manifest.status != "ok" or manifest.hidden:
            continue
        out[manifest.id] = manifest.preset()
    return out


def known_capability_types() -> set:
    """Every ``swcap_*`` any manifest declares.

    Used by :func:`app.core.manifest._known_software_capabilities` to
    validate card manifests — same contract it had when it read
    ``MODEL_PRESETS`` directly.
    """
    caps: set = set()
    for manifest in discover():
        caps.update(manifest.capability_types)
    return caps


__all__ = [
    "ENTRY_FILENAME",
    "MANIFEST_FILENAME",
    "WORKERS_DIR",
    "WorkerManifest",
    "discover",
    "known_capability_types",
    "load_one",
    "model_presets",
]