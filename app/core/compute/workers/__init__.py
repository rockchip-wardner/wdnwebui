"""Worker plugin discovery — one manifest per model, shipped **with the model**.

A worker is a JSON manifest living **inside the model folder**, next to the
weights it describes::

    /userdata/models/Qwen3-VL-4B/
    ├── qwen3-vl-4b.json          ← this manifest
    ├── llm_Qwen3-VL-4B.1828.rknn
    └── ...

That makes the folder self-describing and the distribution unit one folder:
a HuggingFace repo holds the weights *and* the manifest, so ``git clone`` is
the whole installation. It also makes the board's inventory honest — a model
that is not installed has no folder, hence no manifest, hence no card in the
compute page.

File naming: ``<model_id>.json`` (canonical, the file name *is* the id) or
``worker.json`` (id read from the body). Other ``.json`` files in the folder
are ignored unless they carry a ``spec_factory``.

Where per-model behaviour belongs
---------------------------------
**Not here.** This package is a transport shell: it brings up the DDS hub,
registers the replier and forwards a prompt. Anything model-specific —
special inference options, a different loading strategy, an extra backend —
belongs in ``wdnai``'s ``LLMLocalSession`` / backend layer, where it can be
shared by every caller instead of forked per worker. The manifest carries
*what* to build and *what to call it*, never *how to behave``.

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

#: This **code** package — holds the shared worker entry script, not the
#: per-model manifests (those ship with the models).
WORKERS_DIR = Path(__file__).resolve().parent

#: The one entry script, shared by every model. Its basename stays
#: ``compute_worker.py`` on purpose: the spawn dedup in ``registry.py``
#: and the process scan in ``app/system/api/system_monitor.py`` both match
#: ``compute_worker.py\x00--model\x00<id>\x00`` in ``/proc/<pid>/cmdline``.
ENTRY_FILENAME = "compute_worker.py"

#: Suffix of the per-model manifest files (``<id>.json``).
MANIFEST_SUFFIX = ".json"

#: Alternate manifest file name — one fixed name per model folder; the id
#: then comes from the manifest's ``id`` field.
CANONICAL_MANIFEST_NAME = "worker.json"

#: Manifest 文件大小上限（字节）：只有小于这个尺寸的 json 才会被解析成
#: manifest。模型目录里还有 tokenizer.json 这类大文件（实测
#: ``Qwen3-TTS/tokenizer.json`` 是 11.4 MB），全量 json.loads 要 ~2s，而
#: discover() 在请求路径上每秒都跑一遍，会把 event loop 堵死。manifest 本身
#: 只有几百字节到 2KB，64KB 余量绰绰有余。
MAX_MANIFEST_BYTES = 64 * 1024


# --------------------------------------------------------------------------- #
# Model root
# --------------------------------------------------------------------------- #
def models_root() -> Path:
    """Return the root directory holding every model folder.

    ``WDNAI_MODELS_DIR`` (set at boot from ``base.model_path``) wins;
    otherwise :func:`app.core.config.default_models_root`
    (``/userdata/models`` → ``/mnt/oem/models`` → ``<HOME>/models``).

    Must agree with ``wdnai.model._models_root()``, which reads the same
    variable — otherwise the worker looks for weights where the SDK never
    does, and the failure only surfaces much later as "model files missing".
    """
    env = os.environ.get("WDNAI_MODELS_DIR")
    if env and env.strip():
        return Path(env.strip())
    from app.core.config import default_models_root
    root = default_models_root()
    os.environ.setdefault("WDNAI_MODELS_DIR", str(root))
    return root


def _model_dirs() -> List[Path]:
    """The model folders under the models root, sorted by name."""
    root = models_root()
    if not root.is_dir():
        return []
    try:
        dirs = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError as exc:
        LOG.warning("模型根目录 %s 不可读: %s", root, exc)
        return []
    return [d for d in dirs if not d.name.startswith((".", "_"))]


# --------------------------------------------------------------------------- #
# Manifest
# --------------------------------------------------------------------------- #
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
    #: Directory name the weights are expected in. **Not used to build
    #: paths** — the model directory is wherever the manifest was found —
    #: but kept because the migration script uses it to place the manifest
    #: and because it tells a human what the folder was called upstream.
    model_dir_name: str = ""
    #: Absolute path of the model directory this manifest was found in.
    #: This is the directory the weights are loaded from.
    model_dir: str = ""
    #: Environment variable the SDK reads to locate this model's files
    #: (``WDNAI_QWEN3_VL_4B_DIR`` and friends). Set before the spec is
    #: built so the SDK resolves the files inside :attr:`model_dir`.
    model_dir_env: str = ""
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
            "model_dir": self.model_dir,
            "model_dir_env": self.model_dir_env,
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
def _validate(raw: Any, stem: str, *, canonical: bool = False
              ) -> tuple[Optional[WorkerManifest], List[str]]:
    errors: List[str] = []
    if not isinstance(raw, dict):
        return None, ["manifest must contain a JSON object"]

    # ``id`` defaults to the filename stem (``qwen3-vl-4b.json`` →
    # ``qwen3-vl-4b``) — same implicit-rule idea as cards. Making it
    # explicit is allowed but must agree: a manifest that renames itself
    # away from its own filename would break ``--model <id>`` ↔
    # ``workers/<id>.json`` lookup and spawn the wrong model.
    #
    # The one exception is the canonical ``worker.json`` name: there the
    # file name carries no id, so ``id`` is mandatory and taken from the
    # manifest body.
    worker_id = raw.get("id") or ("" if canonical else stem)
    if not isinstance(worker_id, str) or not worker_id.strip():
        return None, ["'id' 缺失或不是非空字符串"
                      + ("（worker.json 必须显式声明 id）" if canonical else "")]
    if not canonical and worker_id != stem:
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
        "model_dir_name", "model_dir", "model_dir_env", "instance_name",
        "requires_cards", "memory_estimate_mb", "estimated_init_seconds",
        "description", "notes", "available_targets", "hidden", "display_order",
    }
    return WorkerManifest(
        id=worker_id,
        label=raw["label"],
        capability_types=list(caps),
        spec_factory=raw["spec_factory"],
        worker_name=raw.get("worker_name") or worker_id[:8],
        model_dir_name=raw.get("model_dir_name", ""),
        model_dir_env=raw.get("model_dir_env", ""),
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


def _discover_in_model_dir(model_dir: Path) -> List[WorkerManifest]:
    """Find the worker manifest inside one model folder.

    A model folder may hold other ``.json`` files (tokenizer configs and
    friends), so a file is only treated as a manifest when it carries a
    ``spec_factory`` key. ``worker.json`` is accepted as an alternate name;
    there the model id comes from the manifest body.
    """
    out: List[WorkerManifest] = []
    try:
        candidates = sorted(model_dir.glob(f"*{MANIFEST_SUFFIX}"))
    except OSError as exc:
        LOG.warning("模型目录 %s 不可读: %s", model_dir, exc)
        return out
    for path in candidates:
        if path.name.startswith((".", "_")):
            continue
        # 只解析「小」文件（见 MAX_MANIFEST_BYTES）。canonical 名超限时
        # 仍然解析 —— 让它走「解析失败 → status=fatal」的既有分支，而不是
        # 静默消失。
        try:
            oversized = path.stat().st_size > MAX_MANIFEST_BYTES
        except OSError:
            continue
        if oversized and path.name != CANONICAL_MANIFEST_NAME:
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # A model folder may legitimately contain unrelated JSON, so a
            # parse failure is only fatal for the canonical name (which is
            # always a manifest); otherwise warn and move on.
            if path.name == CANONICAL_MANIFEST_NAME:
                LOG.error("worker %s: unreadable manifest: %s", path, exc)
                out.append(WorkerManifest(
                    id=path.stem, label=path.stem, path=str(path),
                    status="fatal", errors=[f"manifest 解析失败: {exc}"],
                    model_dir=str(model_dir),
                ))
            else:
                LOG.warning("跳过无法解析的 json %s: %s", path, exc)
            continue
        if not isinstance(raw, dict) or "spec_factory" not in raw:
            continue
        canonical = path.name == CANONICAL_MANIFEST_NAME
        manifest, errors = _validate(raw, path.stem, canonical=canonical)
        if manifest is None:
            LOG.error("worker %s: invalid manifest: %s", path,
                      "; ".join(errors))
            out.append(WorkerManifest(
                id=str(raw.get("id") or path.stem),
                label=str(raw.get("label", path.stem)),
                path=str(path), status="fatal", errors=errors,
                model_dir=str(model_dir),
            ))
            continue
        manifest.path = str(path)
        manifest.model_dir = str(model_dir)
        out.append(manifest)
    return out


def discover() -> List[WorkerManifest]:
    """Scan the models root and return validated manifests.

    One model folder = one manifest. A folder that is not installed (not
    cloned, not copied) simply contributes nothing — the compute page shows
    one card per *installed* model.

    A malformed manifest is reported on that worker (``status="fatal"``)
    rather than raised: one bad file must not take down discovery for
    every other model. Files whose name starts with ``.`` or ``_`` are
    ignored, and manifests marked ``hidden`` are returned with
    ``hidden=True`` so callers can filter them out of the UI while keeping
    them live.
    """
    found: List[WorkerManifest] = []
    entry_missing = not (WORKERS_DIR / ENTRY_FILENAME).is_file()
    seen: Dict[str, str] = {}
    for model_dir in _model_dirs():
        for manifest in _discover_in_model_dir(model_dir):
            if manifest.id in seen:
                manifest.status = "fatal"
                manifest.errors = list(manifest.errors) + [
                    f"id {manifest.id!r} 与 {seen[manifest.id]} 重复"]
                LOG.error("worker %s: %s", manifest.id,
                          "; ".join(manifest.errors))
            else:
                seen[manifest.id] = manifest.path
            if entry_missing:
                manifest.status = "fatal"
                manifest.errors = list(manifest.errors) + [
                    f"入口文件缺失: {ENTRY_FILENAME}"]
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


def find_manifest_path(worker_id: str) -> Optional[str]:
    """Locate the manifest file backing ``worker_id``.

    Returns the absolute path, or ``None`` when the model is not installed.

    Accepts ``<root>/<ModelDir>/<worker_id>.json`` (canonical) and
    ``<root>/<ModelDir>/worker.json`` (id read from the body).

    Unlike :func:`discover` this does not parse every manifest: the worker
    calls it on hot paths (``load`` handling, comm naming) and only needs to
    locate its own file.
    """
    model_dirs = _model_dirs()
    for model_dir in model_dirs:
        cand = model_dir / f"{worker_id}{MANIFEST_SUFFIX}"
        if cand.is_file():
            return str(cand)
    for model_dir in model_dirs:
        cand = model_dir / CANONICAL_MANIFEST_NAME
        if not cand.is_file():
            continue
        try:
            raw = json.loads(cand.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(raw, dict) and raw.get("id") == worker_id:
            return str(cand)
    return None


def model_presets() -> Dict[str, Dict[str, Any]]:
    """``MODEL_PRESETS``-shaped dict built from the installed models.

    This is what :data:`app.core.compute.registry.MODEL_PRESETS` now is —
    the single source of truth moved from a hand-written literal to the
    scanned manifests, and every existing call site keeps working. Because
    the manifests live in the model folders, this dict describes **the
    models installed on this board** — exactly what the compute page
    should offer.
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
#: ``ModelSpec`` fields that hold a filesystem path.
_SPEC_PATH_FIELDS = (
    "rknn_path", "weight_path",
    "rknn_llm_path", "weight_llm_path",
    "rknn_vision_path", "weight_vision_path",
    "tts_model_dir", "omni_model_dir", "asr_model_dir", "image_model_dir",
    "embed_path", "tokenizer_path",
    "depth_local_path", "depth_global_path", "depth_head_path",
)

#: ``ModelSpec`` fields that hold a list of filesystem paths.
_SPEC_PATH_LIST_FIELDS = ("llm_seg_paths", "weight_seg_paths")


def _is_under(path: str, root: str) -> bool:
    return path == root or path.startswith(root + os.sep)


def _assert_spec_under_model_dir(spec: Any, manifest: WorkerManifest) -> None:
    """Fail loudly when the spec points outside the model folder.

    The SDK resolves each model's files through its own per-model directory
    override (``WDNAI_QWEN3_VL_4B_DIR`` and friends). If the installed SDK
    does not honour that variable — an older wdnai, or a model whose
    override has not been added yet — the factory silently falls back to
    its hardcoded directory name and builds a spec pointing somewhere else.
    Checking against the folder the manifest was found in turns that into
    an immediate, actionable error.

    Compares with :func:`os.path.abspath`, **not** :func:`os.path.realpath`:
    the SDK joins the override value with a file name without resolving
    symlinks, so the paths must be compared the same way — resolving would
    break whenever a weight file is a symlink into a shared store.
    """
    model_dir = manifest.model_dir
    if not model_dir:
        return
    root = os.path.abspath(model_dir)
    bad: List[str] = []
    for field_name in _SPEC_PATH_FIELDS:
        value = getattr(spec, field_name, "") or ""
        if value and not _is_under(os.path.abspath(value), root):
            bad.append(f"{field_name}={value}")
    for field_name in _SPEC_PATH_LIST_FIELDS:
        for value in (getattr(spec, field_name, None) or []):
            if value and not _is_under(os.path.abspath(value), root):
                bad.append(f"{field_name}={value}")
    if bad:
        env_name = manifest.model_dir_env
        raise RuntimeError(
            f"worker {manifest.id!r}: ModelSpec 路径不在模型目录 {model_dir} 内: "
            + "; ".join(bad)
            + (f" —— 已设置 {env_name}={model_dir}，但当前安装的 wdnai "
               f"没有认这个环境变量（该模型需要 SDK 侧补上对应覆盖）"
               if env_name else
               " —— 该 manifest 未声明 model_dir_env，SDK 用了自己的默认目录")
        )


# Why the spec must be the real ModelSpec and NOT the manifest preset dict:
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
            f"{manifest.path} against the installed wdnai")
    # Point the SDK at *this* model folder before it resolves any path —
    # that is what lets the folder be named freely (the HuggingFace repo
    # name) instead of being pinned to the name hardcoded inside the SDK.
    if manifest.model_dir_env and manifest.model_dir:
        os.environ[manifest.model_dir_env] = manifest.model_dir
    spec = factory()
    _assert_spec_under_model_dir(spec, manifest)
    _SPEC_CACHE[model_id] = spec
    return spec


def known_capability_types() -> set:
    """Every legal ``swcap_*`` value — the **closed vocabulary** from
    :class:`wdnai.SoftwareCapability`, not the union of whatever the
    installed workers declare.

    A card declaring a capability that *no worker provides* is a config
    error, not a *vocabulary* error. Keeping the two apart matters more
    now that the manifests ship with the models: the installed worker set
    differs from board to board, so a union-of-manifests vocabulary would
    make a card's validity depend on which models happen to be present —
    a board missing the grounding model would silently lose the
    ``video-target-localization`` card instead of reporting "功能未启用".
    """
    return set(SoftwareCapability.values())


__all__ = [
    "CANONICAL_MANIFEST_NAME",
    "ENTRY_FILENAME",
    "MANIFEST_SUFFIX",
    "WORKERS_DIR",
    "WorkerManifest",
    "build_model_spec",
    "discover",
    "find_manifest_path",
    "known_capability_types",
    "load_one",
    "model_presets",
    "models_root",
]
