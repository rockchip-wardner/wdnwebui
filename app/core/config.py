"""Centralized config.jsonc loader / writer.

The path is taken from the ``WDNWEBUI_CONFIG`` env var (set by
``run.py --config …``) and falls back to ``<repo>/config.jsonc`` if
unset. The file is read once at first access and held in memory;
writes go through :meth:`ConfigStore.replace`, which validates the
payload and persists atomically (tmp + ``os.replace``).

The on-disk file is JSONC (JSON with ``//`` and ``/* */`` comments) —
see :mod:`app.services.jsonc` for the parse/dump layer. User-written
comments are accepted on read but **discarded on write**: any save
through the API emits a freshly formatted JSONC file with no comments.
This keeps the persistence path simple and predictable; users who want
their comments preserved should hand-edit the file directly without
going through ``PUT /api/config/...``.
"""
from __future__ import annotations

import json
import os
import secrets
import tempfile
from pathlib import Path
from threading import RLock
from typing import Any

from app.core.compute.jsonc import dump_jsonc, parse_jsonc

try:
    import pwd as _pwd
except ImportError:  # pragma: no cover — 非 POSIX
    _pwd = None


_DEFAULT_CONFIG_PATH = (
    Path(__file__).resolve().parents[2] / "config.jsonc"
)


_DEVICE_NAME_LENGTH = 8
_DEVICE_NAME_ALPHABET = (
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
)


def _resolve_config_path() -> Path:
    env = os.environ.get("WDNWEBUI_CONFIG")
    if env:
        p = Path(env).expanduser()
        try:
            return p.resolve()
        except OSError:
            return p
    return _DEFAULT_CONFIG_PATH


def _random_device_name() -> str:
    """8-char alphanumeric identifier (mixed case + digits)."""
    return "".join(
        secrets.choice(_DEVICE_NAME_ALPHABET)
        for _ in range(_DEVICE_NAME_LENGTH)
    )


# Models-root candidates in priority order. The first one that actually
# exists wins (see :func:`default_models_root`); this helper does NOT
# auto-create the directory — an empty ``models/`` tree without weights
# in it is just clutter.
_MODEL_ROOT_CANDIDATES: tuple[Path, ...] = (
    Path("/userdata/models"),
    Path("/mnt/oem/models"),
)


def model_root_candidates(current_root: Path | str | None = None) -> list[Path]:
    """模型根目录候选 —— 网页端「模型下载」的目录选择器用的就是这份清单。

    顺序：

    1. **当前配置的模型根**（``base.model_path``）—— 它是默认值，必须在
       列表里，否则 operator 把模型路径改到 ``/data/models`` 时就没得选；
    2. ``/userdata/models``；
    3. ``/mnt/oem/models``；
    4. ``<HOME>/models``（``<HOME>`` = 操作员家目录，见 :func:`_user_home`）。

    ⚠️ 这份清单必须和基础配置里「RKNN 模型权重存放的绝对路径」那段说明
    文字**同源**，否则用户在下载框里选的目录和配置里写的探测顺序对不上。
    """
    out: list[Path] = []
    if current_root:
        p = Path(current_root)
        if p not in out:
            out.append(p)
    for cand in [*_MODEL_ROOT_CANDIDATES, _user_home() / "models"]:
        if cand not in out:
            out.append(cand)
    return out


def _user_home() -> Path:
    """操作员（装机用户）的家目录。

    ⚠️ **不能是 ``/root``** —— 服务（systemd ``wdnwebui.service``）以 root
    跑，``$HOME=/root``，而 ``/root`` 通常在**小分区**上（根分区只有 14G）。
    有些板子会把 ``/home`` 挂到固态硬盘 / 外接盘上，模型放那儿才对 ——
    所以 ``<HOME>`` 的含义是 ``/home/<用户名>``，不是 ``$HOME``。

    解析顺序（先准后粗）：

    1. ``SUDO_USER`` —— sudo 之前的那个用户（手动 sudo 跑时最准）；
    2. **装机目录的属主** —— 服务进程的 cwd（``WorkingDirectory``）归谁所有，
       那人的家目录就是操作员的家目录。systemd 场景下最可靠
       （``/home/<user>/work/wdnwebui`` 归 ``<user>`` 所有）；
    3. 系统用户库里**唯一一个** uid≥1000 且家在 ``/home`` 下的用户；
    4. 兜底 ``Path.home()``（实在认不出来时）。

    Cheap; called from :func:`default_models_root` and
    :func:`default_resource_root` once per config seed. No caching
    needed.
    """
    if _pwd is None:
        return Path.home()

    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        try:
            return Path(_pwd.getpwnam(sudo_user).pw_dir)
        except KeyError:
            pass  # 用户已不在 NSS 里，继续往下找

    try:
        st = os.stat(os.getcwd())
        if st.st_uid != 0:
            return Path(_pwd.getpwuid(st.st_uid).pw_dir)
    except (KeyError, OSError):
        pass

    # 系统用户库里唯一的「有家目录的普通用户」
    cands = [p.pw_dir for p in _pwd.getpwall()
             if 1000 <= p.pw_uid < 65534 and p.pw_dir.startswith("/home/")]
    if len(cands) == 1:
        return Path(cands[0])
    return Path.home()


def default_models_root() -> Path:
    """Return the first existing entry of :data:`_MODEL_ROOT_CANDIDATES`,
    falling back to ``<HOME>/models`` if none exist.

    This is the basis for ``base.model_path``; the trailing ``/`` is
    appended by the caller (``default_base_paths``) so the result
    matches the historical JSON shape. Pure (no I/O beyond ``is_dir()``).

    ``<HOME>`` is resolved via :func:`_user_home` — the operator's home
    (``/home/<user>``), not ``/root``.
    """
    for cand in _MODEL_ROOT_CANDIDATES:
        if cand.is_dir():
            return cand
    return _user_home() / "models"


def default_resource_root() -> Path:
    """Resolve and create the asset root.

    On-device (``/userdata`` mounted): returns ``/userdata/assets``.
    Off-device (dev / CI): returns ``<HOME>/assets``.

    The directory is auto-created (``parents=True, exist_ok=True``) so
    wizard uploads and TTS output have a writable destination after a
    fresh boot. ``OSError`` is silently swallowed: a read-only
    filesystem or container without write access should not crash the
    import graph; the first actual write attempt will surface a
    meaningful error.

    Sub-tree (created lazily as needed):

        ``<assets>/uploads``  — wizard video uploads (shared by the 3
                                 video apps; same filename overwrites)
        ``<assets>/tts``       — TTS-generated audio (read by the
                                 ``/api/tts/output`` endpoint)

    Naming history: was ``.../test`` until v0.x; the resource dir was
    then briefly called ``.../upload`` (just the leaf), but the current
    layout uses ``assets`` as the parent and per-purpose second-level
    subdirs (``uploads/``, ``tts/``) so the root name reflects the
    broader role of "device-side assets directory".
    """
    if Path("/userdata").is_dir():
        root = Path("/userdata/assets")
    else:
        root = _user_home() / "assets"
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Read-only fs / permission denied / container quirk — let the
        # first real write attempt surface the real error.
        pass
    return root


def default_base_paths() -> dict[str, str]:
    """Return the on-disk ``base.model_path`` / ``base.resource_dir``
    defaults as strings, suitable for JSONC serialization.

    Resolves via :func:`default_models_root` and
    :func:`default_resource_root` so:

    * ``model_path`` follows the priority
      ``/userdata/models/`` → ``/mnt/oem/models/`` → ``<HOME>/models/``.
    * ``resource_dir`` is always ``<resolved_root>/assets`` — either
      ``/userdata/assets`` on device or ``<HOME>/assets`` off-device.
      Per-purpose subdirs (``uploads/``, ``tts/``) live under it.

    Imported by both :class:`ConfigStore._seed_defaults` (first-run
    template written to ``config.jsonc`` on disk) and
    ``app/system/api/config.py:_BASE_DEFAULTS`` (lazy fill on
    ``GET /api/config/base``); both call sites get the same answer, so
    the two config-generation paths cannot drift.

    Pure (the resource-dir ``mkdir`` is performed by
    :func:`default_resource_root` when the helpers are actually
    invoked — typically at module import time of the config API
    module, which is fine since ``exist_ok=True`` makes it idempotent).
    """
    return {
        "model_path":   f"{default_models_root()}/",
        "resource_dir": str(default_resource_root()),
    }


class ConfigStore:
    """In-memory cache of a JSON-object config file."""

    def __init__(self) -> None:
        self._path: Path = _resolve_config_path()
        self._data: dict[str, Any] = {}
        self._lock = RLock()
        self.reload()

    @property
    def path(self) -> Path:
        return self._path

    def reload(self) -> dict[str, Any]:
        """Re-read the file from disk.

        Missing file → write the first-run template (random
        ``device_name`` + empty ``compute.autostart``) so a fresh
        deployment starts from a known-good config. An
        existing-but-empty / corrupted / non-object payload still
        collapses to ``{}`` — we only auto-seed when the file is
        genuinely missing, never overwrite a user-wiped one.

        Parses as JSONC (so ``//`` and ``/* */`` comments are tolerated)
        via :func:`app.services.jsonc.parse_jsonc`. User-written
        comments are accepted on the read but are *not* stored —
        re-saves will emit a comment-free JSONC file.
        """
        with self._lock:
            if not self._path.exists():
                self._seed_defaults()
                return self._data
            try:
                with self._path.open("r", encoding="utf-8") as f:
                    raw = parse_jsonc(f.read())
            except (OSError, ValueError):
                # json5 raises ValueError on parse failure (subclass of
                # json.JSONDecodeError would also be caught here).
                self._data = {}
                return self._data
            if not isinstance(raw, dict):
                self._data = {}
                return self._data
            self._data = raw
            return self._data

    def _seed_defaults(self) -> None:
        """Write the first-run template to disk.

        Acquires ``self._lock`` itself so it's safe to call from any
        context (including the recursive re-entry from
        :py:meth:`reload` while the lock is already held; ``RLock``
        handles that).

        The template mirrors the current shipping config.jsonc:

        * ``base`` — device-level config:
          * paths: ``device_name`` (random), ``model_path``,
            ``resource_dir``
          * device-attached fields: ``hardware_capabilities``（来自
            :data:`app.services.compute_registry.HARDWARE_CAPABILITIES`
            全 True）
        * ``compute.autostart`` — seeds as an **empty list**: nothing
          starts at boot until the user declares a
          ``{model, npu_index}`` entry. That matches the schema's
          contract (only listed models are started; everything else is
          inert and gets enabled from the compute page), so a fresh
          device boots with zero workers holding NPU memory rather than
          every preset racing to load onto one card.
        * ``app.prompts`` — the three configurable schema fields
          (VLMDoc.text / .level, TargetDoc.desc) seeded with the SDK's
          :mod:`app.services.detector` ``DEFAULT_*_DESC`` constants,
          so the system lands in a known-good prompt state on first
          boot. v0.11 起从 ``compute.app_prompts`` 挪到独立 ``app``
          section，3 层语义更清晰。

        All three sibling modules are imported lazily to avoid
        ``config → compute_registry → config`` and
        ``config → detector → config`` circular imports; by the time
        ``_seed_defaults`` actually fires (file missing at startup)
        both ``compute_registry`` and ``detector`` are already loaded
        by the app import graph.
        """
        from app.core.compute.registry import (
            _hardware_capability_default_state,
        )
        from app.core.llm.detector_base import (
            DEFAULT_TARGET_DOC_DESC_DESC,
            DEFAULT_VLM_DOC_LEVEL_DESC,
            DEFAULT_VLM_DOC_TEXT_DESC,
        )
        with self._lock:
            _base_defaults = default_base_paths()
            self._data = {
                "base": {
                    "device_name":          _random_device_name(),
                    "model_path":            _base_defaults["model_path"],
                    "resource_dir":          _base_defaults["resource_dir"],
                    "hardware_capabilities": _hardware_capability_default_state(),
                },
                "compute": {
                    "autostart": [],
                },
                "app": {
                    "prompts": {
                        "video_detection": {
                            "VLMDoc": {
                                "text": DEFAULT_VLM_DOC_TEXT_DESC,
                                "level": DEFAULT_VLM_DOC_LEVEL_DESC,
                            },
                        },
                        "video_target_detection": {
                            "TargetDoc": {
                                "desc": DEFAULT_TARGET_DOC_DESC_DESC,
                            },
                        },
                    },
                },
            }
            self._persist()

    def snapshot(self) -> dict[str, Any]:
        """Return a deep copy so callers can't mutate the cached state."""
        with self._lock:
            return json.loads(json.dumps(self._data))

    def replace(self, new_data: dict[str, Any]) -> dict[str, Any]:
        """Validate, swap, and persist. Raises ``ValueError`` for non-object
        inputs and ``OSError`` for filesystem failures."""
        if not isinstance(new_data, dict):
            raise ValueError("config must be a JSON object")
        with self._lock:
            self._data = json.loads(json.dumps(new_data))
            self._persist()
            return self.snapshot()

    def get_section(self, name: str, default: Any = None) -> Any:
        """Read a top-level key (e.g. ``"base"``) as a deep-copied value.

        Missing keys yield a deep copy of ``default`` (or ``{}`` if
        ``default`` is ``None``) so callers always get something they
        own and can mutate freely."""
        with self._lock:
            if name in self._data:
                val = self._data[name]
            elif default is None:
                val = {}
            else:
                val = default
            if isinstance(val, (dict, list)):
                return json.loads(json.dumps(val))
            return val

    def set_section(self, name: str, value: Any) -> dict[str, Any]:
        """Replace a single top-level section without disturbing other
        sections. Persists atomically and returns the new full snapshot.

        ``value`` is JSON round-tripped so ``set_section("base", {})``
        wipes just the ``base`` block, not the rest of the config."""
        with self._lock:
            self._data[name] = json.loads(json.dumps(value))
            self._persist()
            return self.snapshot()

    def _persist(self) -> None:
        """Atomic write: serialize to a sibling temp file, then ``os.replace``.

        ``os.replace`` is atomic on POSIX, so a crash mid-write leaves the
        previous config.jsonc intact instead of a half-written file.

        Serializes via :func:`app.services.jsonc.dump_jsonc` so the
        output is JSONC-format-compatible (quoted keys, trailing commas,
        UTF-8 native). User-written comments in the previous file are
        not preserved — see the module docstring for the rationale.

        落盘后强制 ``chmod 0o777`` —— ``tempfile.mkstemp`` 默认 mode 600，
        ``os.replace`` 之后目标文件继承这个 mode，不管 sudo 还是普通用户
        启动都会被锁成 root-only 600。这里统一刷成 0777 让同机其它用户
        （包括 web 前端 / 调试脚本）都能直接读写。
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=".config.", suffix=".jsonc.tmp",
            dir=str(self._path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(dump_jsonc(self._data))
                f.write("\n")
            os.replace(tmp_name, self._path)
            try:
                os.chmod(self._path, 0o777)
            except OSError:
                # 非 POSIX / 容器只读 fs 之类，chmod 失败不影响主流程。
                pass
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise


_store: ConfigStore | None = None


def get_config() -> ConfigStore:
    """Lazy singleton accessor. First call resolves the config path from
    the ``WDNWEBUI_CONFIG`` env var (or falls back to the repo default)
    and reads the file."""
    global _store
    if _store is None:
        _store = ConfigStore()
    return _store