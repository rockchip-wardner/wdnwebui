"""Worker plugin discovery — one manifest per model, one shared entry point.

A worker is a JSON file at ``app/core/compute/workers/<id>.json`` plus the
single ``compute_worker.py`` next to it. The manifest is the only
per-model artefact: identity, capabilities, UI metadata, and the name of
the ``wdnai`` spec factory that builds the model.

Why this shape
--------------
Until v0.9 the whole worker was one 2358-line ``worker.py`` that served
every model, with the model→spec mapping living in an ``if/elif`` chain
inside ``_build_session``. Adding a model meant editing that chain.

The first attempt at splitting that up gave every model a *folder*
(``workers/<id>/manifest.json`` + ``workers/<id>/compute_worker.py``).
That did separate declaration from code, but it was heavier than the
problem deserved: all six entry scripts were 45-50 lines and differed
only in a docstring and one line of the form
``mod["SystemLanguageModel"].<factory>()``. Six near-identical files that
have to stay in sync is exactly the failure mode the split was meant to
eliminate.

So the per-model ``build_spec`` function became the manifest field
``spec_factory`` (a method name on ``wdnai.SystemLanguageModel``), and
there is now one entry script. Adding a model is dropping in a JSON file.

Where per-model behaviour belongs
---------------------------------
**Not here.** This package is a transport shell: it brings up the DDS hub,
registers the replier and forwards a prompt. Anything model-specific —
special inference options, a different loading strategy, an extra backend —
belongs in ``wdnai``'s ``LLMLocalSession`` / backend layer, where it can
be shared by every caller instead of forked per worker. The manifest
carries *what* to build and *what to call it*, never *how to behave``.

Compatibility
-------------
``app.core.compute.registry.MODEL_PRESETS`` still exists and is now
*derived* from these manifests, so the ~54 call sites across
``registry.py`` / ``config.py`` / ``compute.py`` / ``manifest.py`` keep
working unchanged.

Zero-coupling rule (same as cards): this package may import ``app.core.*``
only. In particular it must NOT import ``app.apps.*`` or ``app.system.*``
— and it must not import :mod:`app.core.compute.registry` either, since
the registry spawns workers and that would close the cycle.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from wdnai import SoftwareCapability

LOG = logging.getLogger("compute.workers")

WORKERS_DIR = Path(__file__).resolve().parent

#: The one entry script, shared by every model. Its basename stays
#: ``compute_worker.py`` on purpose: the spawn dedup in ``registry.py``
#: and the process scan in ``app/system/api/system_monitor.py`` both match
#: ``compute_worker.py\x00--model\x00<id>\x00`` in ``/proc/<pid>/cmdline``.
#: Keeping the basename *and* the ``--model`` flag means that needle is
#: byte-for-byte unchanged by the flat layout — only the directory part of
#: the path differs.
ENTRY_FILENAME = "compute_worker.py"

#: Suffix of the per-model manifest files (``<id>.json``).
MANIFEST_SUFFIX = ".json"


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
    #: Name of the ``wdnai.SystemLanguageModel`` factory that builds this
    #: model's ``ModelSpec`` (e.g. ``"qwen3_vl_4b"``). This is the only
    #: per-model *code* the worker needs, and it is resolved by
    #: ``getattr`` — a typo here surfaces as an ``error`` instance with
    #: the AttributeError text rather than a silently wrong model.
    #: Model-specific behaviour belongs in wdnai, not here.
    spec_factory: str = ""
    #: Short alias used for the process comm name (``worker:<alias>``).
    #: Linux truncates comm to 15 chars; ``registry._worker_comm_name``
    #: enforces the limit and appends a numeric suffix when it overflows.
    worker_name: str = ""
    #: Directory under ``WDNAI_MODELS_DIR`` holding the weights.
    model_dir_name: str = ""
    #: Default instance name written into a fresh ``config.jsonc``.
    instance_name: str = ""
    #: Number of RK182x cards one worker instance must occupy. ``1`` for
    #: every single-card model; the segmented 9B export needs two.
    requires_cards: int = 1
    memory_estimate_mb: int = 0
    estimated_init_seconds: int = 60
    description: str = ""
    #: Free-form operator notes. The old per-worker docstrings carried
    #: real knowledge (why an option is set to a particular value, what a
    #: hard limit is); that is what this is for.
    notes: str = ""
    available_targets: List[str] = field(default_factory=list)
    #: ``True`` keeps a worker out of the default view while its manifest
    #: stays fully live — the worker-side analogue of a hidden card, and
    #: the safe way to stage a rollout.
    hidden: bool = False
    #: Sort key for the enumeration order (ascending). Defaults to
    #: ``display_order`` semantics used by cards.
    display_order: int = 9999
    #: Extra keys we do not model explicitly, preserved verbatim so a
    #: manifest can carry worker-specific metadata without a code change.
    extra: Dict[str, Any] = field(default_factory=dict)
    #: Absolute path of this worker's manifest file.
    path: str = ""
    status: str = "ok"          # "ok" | "fatal"
    errors: List[str] = field(default_factory=list)

    # ------------------------------------------------------------------ #
    @property
    def entry_path(self) -> str:
        """Absolute path to the shared worker entry script.

        Same file for every model — the manifest decides what gets built,
        not which script runs.
        """
        return str(WORKERS_DIR / ENTRY_FILENAME)

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
            "spec_factory": self.spec_factory,
            "worker_name": self.worker_name,
            "model_dir_name": self.model_dir_name,
            "instance_name": self.instance_name,
            "requires_cards": self.requires_cards,
            "memory_estimate_mb": self.memory_estimate_mb,
            "estimated_init_seconds": self.estimated_init_seconds,
            "description": self.description,
            "notes": self.notes,
            "available_targets": list(self.available_targets),
            "hidden": self.hidden,
            # Sorted ascending by discover(); the compute page uses it as
            # the display order of its 本地算力 card list, so the UI order
            # is declared here instead of in a hand-maintained JS array.
            "display_order": self.display_order,
            "entry_path": self.entry_path,
            "service_name": self.service_name(),
        })
        return out


# --------------------------------------------------------------------------- #
# Scanning
# --------------------------------------------------------------------------- #
def _validate(raw: Any, stem: str) -> tuple[Optional[WorkerManifest], List[str]]:
    errors: List[str] = []
    if not isinstance(raw, dict):
        return None, ["manifest must contain a JSON object"]

    # ``id`` defaults to the filename stem (``qwen3-vl-4b.json`` →
    # ``qwen3-vl-4b``) — same implicit-rule idea as cards. Making it
    # explicit is allowed but must agree: a manifest that renames itself
    # away from its own filename would break ``--model <id>`` ↔
    # ``workers/<id>.json`` lookup and spawn the wrong model.
    worker_id = raw.get("id") or stem
    if not isinstance(worker_id, str) or not worker_id.strip():
        return None, ["'id' 存在但不是非空字符串"]
    if worker_id != stem:
        errors.append(f"id {worker_id!r} 与文件名 {stem!r} 不一致")

    if not isinstance(raw.get("label"), str) or not raw["label"].strip():
        errors.append("'label' 缺失或为空")

    # Required: without it there is no way to build the model, and the
    # failure would otherwise only surface as a boot-time AttributeError.
    factory = raw.get("spec_factory")
    if not isinstance(factory, str) or not factory.strip():
        errors.append("'spec_factory' 缺失（wdnai.SystemLanguageModel 上的方法名）")

    # Capability names are **not** free-form: the vocabulary is closed by
    # ``wdnai.SoftwareCapability``. The old ``startswith("swcap_")`` check
    # accepted any string with the right prefix, which is how ghost
    # capabilities (``swcap_video_query`` / ``swcap_video_analyse`` — never
    # declared by any worker, yet sitting in the legacy rename table) survived
    # unnoticed: they looked valid and only failed much later as a permanent
    # "功能未启用". Enum membership now catches a typo *and* a ghost at load
    # time, on the file that introduced it.
    caps = raw.get("capability_types")
    if caps is None:
        errors.append("'capability_types' 缺失")
    elif not isinstance(caps, list) or not all(
            isinstance(c, str) for c in caps):
        errors.append("'capability_types' 必须是字符串列表")
    elif not caps:
        errors.append("'capability_types' 不能为空 —— worker 至少要提供一项能力")
    else:
        unknown = [c for c in caps if not SoftwareCapability.contains(c)]
        if unknown:
            errors.append(
                "'capability_types' 含非法能力: {}（合法值为: {}）".format(
                    ", ".join(unknown),
                    ", ".join(SoftwareCapability.values()),
                )
            )

    if errors:
        return None, errors

    known = {
        "id", "label", "capability_types", "spec_factory", "worker_name",
        "model_dir_name", "instance_name", "requires_cards",
        "memory_estimate_mb", "estimated_init_seconds", "description", "notes",
        "available_targets", "hidden", "display_order",
    }
    return WorkerManifest(
        id=worker_id,
        label=raw["label"],
        capability_types=list(caps),
        spec_factory=raw["spec_factory"],
        worker_name=raw.get("worker_name") or worker_id[:8],
        model_dir_name=raw.get("model_dir_name", ""),
        instance_name=raw.get("instance_name", ""),
        requires_cards=int(raw.get("requires_cards", 1)),
        memory_estimate_mb=int(raw.get("memory_estimate_mb", 0)),
        estimated_init_seconds=int(raw.get("estimated_init_seconds", 60)),
        description=raw.get("description", ""),
        notes=raw.get("notes", ""),
        available_targets=list(raw.get("available_targets", [])),
        hidden=bool(raw.get("hidden", False)),
        display_order=int(raw.get("display_order", 9999)),
        extra={k: v for k, v in raw.items() if k not in known},
        path="",
    ), errors


def discover() -> List[WorkerManifest]:
    """Scan ``workers/<id>.json`` and return validated manifests.

    A malformed manifest is reported on that worker (``status="fatal"``)
    rather than raised: one bad file must not take down discovery for
    every other model. Files whose stem starts with ``.`` or ``_`` are
    ignored, and manifests marked ``hidden`` are returned with
    ``hidden=True`` so callers can filter them out of the UI while keeping
    them live.
    """
    found: List[WorkerManifest] = []
    if not WORKERS_DIR.is_dir():
        return found

    entry = WORKERS_DIR / ENTRY_FILENAME
    entry_missing = not entry.is_file()

    seen: Dict[str, str] = {}
    for path in sorted(WORKERS_DIR.glob(f"*{MANIFEST_SUFFIX}")):
        stem = path.stem
        if stem.startswith((".", "_")):
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            LOG.error("worker %s: unreadable manifest: %s", stem, exc)
            found.append(WorkerManifest(
                id=stem, label=stem, path=str(path),
                status="fatal", errors=[f"manifest 解析失败: {exc}"],
            ))
            continue

        manifest, errors = _validate(raw, stem)
        if manifest is None:
            LOG.error("worker %s: invalid manifest: %s", stem,
                      "; ".join(errors))
            found.append(WorkerManifest(
                id=stem, label=str(raw.get("label", stem))
                if isinstance(raw, dict) else stem,
                path=str(path), status="fatal", errors=errors,
            ))
            continue
        manifest.path = str(path)

        if manifest.id in seen:
            errors = [f"id {manifest.id!r} 与 {seen[manifest.id]} 重复"]
            manifest.status = "fatal"
            manifest.errors = errors
            LOG.error("worker %s: %s", stem, "; ".join(errors))

        if entry_missing:
            manifest.status = "fatal"
            manifest.errors = manifest.errors + [
                f"入口文件缺失: {ENTRY_FILENAME}"]

        seen[manifest.id] = stem
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


# --------------------------------------------------------------------------- #
# manifest → ModelSpec
# --------------------------------------------------------------------------- #
# The manifest's ``spec_factory`` names a method on ``wdnai.SystemLanguageModel``;
# calling it yields the real ``ModelSpec``.  **One resolver, two callers**:
# the worker builds its ``LLMLocalSession`` from it, and the main process
# builds its ``LLMRPCSession`` from it.
#
# Why it must be the real ModelSpec and NOT the manifest preset dict:
# the preset view carries UI metadata (label / capability_types / notes …)
# but **no ``kind``** — and ``kind`` is what the SDK uses to decide whether
# the JSON-schema hint may go into the prompt.  Handing the dict to
# ``LLMRPCSession`` made ``spec.kind`` resolve to ``None``, the schema
# suppression silently switched itself off, and a 2 KB ``LocatorDoc`` JSON
# Schema rode along into every grounding prompt — the model then grounded
# the schema's own field names ("description", "ROI 左上角 x，0–1000
# 归一化。") instead of the user's target.
#
# Building the spec is cheap (dataclass + path resolution, no RKNN load);
# the actual model load still happens in the worker.
_SPEC_CACHE: Dict[str, Any] = {}


def build_model_spec(model_id: str) -> Any:
    """Return the ``wdnai.ModelSpec`` for ``model_id`` (cached).

    Raises ``RuntimeError`` with an actionable message when the manifest
    has no ``spec_factory`` or the installed wdnai does not expose it —
    the same two failure modes the worker reports.
    """
    cached = _SPEC_CACHE.get(model_id)
    if cached is not None:
        return cached
    manifest = load_one(model_id)
    if manifest is None:
        raise RuntimeError(f"worker {model_id!r}: manifest not found")
    factory_name = (manifest.spec_factory or "").strip()
    if not factory_name:
        raise RuntimeError(
            f"worker {model_id!r}: manifest has no 'spec_factory' "
            f"(expected a wdnai.SystemLanguageModel method name)")
    from wdnai import SystemLanguageModel
    factory = getattr(SystemLanguageModel, factory_name, None)
    if factory is None:
        raise RuntimeError(
            f"worker {model_id!r}: wdnai.SystemLanguageModel has no "
            f"{factory_name!r} — check 'spec_factory' in "
            f"workers/{model_id}.json against the installed wdnai")
    spec = factory()
    _SPEC_CACHE[model_id] = spec
    return spec


def known_capability_types() -> set:
    """Every legal ``swcap_*`` value.

    Used by :func:`app.core.manifest._known_software_capabilities` to
    validate card manifests. The set is now the **closed vocabulary** from
    :class:`wdnai.SoftwareCapability` rather than the union of whatever the
    worker manifests happen to declare.

    Why that matters: a card declaring a capability that *no worker provides*
    is a configuration error, but it is not a *vocabulary* error — and under
    the old union-of-manifests contract it passed card validation silently,
    only to fail much later as a permanent "功能未启用". With the vocabulary
    closed in the SDK, the two concerns separate cleanly: this set answers
    "is this a real capability name", the registry answers "is any model
    currently running to provide it".
    """
    return set(SoftwareCapability.values())


__all__ = [
    "ENTRY_FILENAME",
    "MANIFEST_SUFFIX",
    "WORKERS_DIR",
    "WorkerManifest",
    "discover",
    "known_capability_types",
    "load_one",
    "model_presets",
]