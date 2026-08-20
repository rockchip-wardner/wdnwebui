"""Centralized config.json loader / writer.

The path is taken from the ``WDNWEBUI_CONFIG`` env var (set by
``run.py --config …``) and falls back to ``<repo>/config.json`` if
unset. The file is read once at first access and held in memory;
writes go through :meth:`ConfigStore.replace`, which validates the
payload and persists atomically (tmp + ``os.replace``).
"""
from __future__ import annotations

import json
import os
import secrets
import tempfile
from pathlib import Path
from threading import RLock
from typing import Any


_DEFAULT_CONFIG_PATH = (
    Path(__file__).resolve().parents[2] / "config.json"
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
        ``device_name`` + every model from ``MODEL_PRESETS`` registered
        as ``compute.services``) so a fresh deployment boots with
        ``boot_compute_services`` actually able to spawn workers. An
        existing-but-empty / corrupted / non-object payload still
        collapses to ``{}`` — we only auto-seed when the file is
        genuinely missing, never overwrite a user-wiped one.
        """
        with self._lock:
            if not self._path.exists():
                self._seed_defaults()
                return self._data
            try:
                with self._path.open("r", encoding="utf-8") as f:
                    raw = json.load(f)
            except (OSError, json.JSONDecodeError):
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

        The template mirrors the current shipping config.json:

        * ``base`` — default paths + a freshly randomized
          ``device_name``
        * ``compute.services`` — every model from
          :data:`app.services.compute_registry.MODEL_PRESETS`, so
          ``boot_compute_services`` has work to do
        * ``compute.hardware_capabilities`` — every entry in
          :data:`app.services.compute_registry.HARDWARE_CAPABILITIES`
          enabled (``True``), matching the on-device reality of a
          full-featured board
        * ``compute.app_prompts`` — the three configurable schema
          fields (VLMDoc.text / .level, TargetDoc.desc) seeded with
          the SDK's :mod:`app.services.detector` ``DEFAULT_*_DESC``
          constants, so the system lands in a known-good prompt state
          on first boot

        All three sibling modules are imported lazily to avoid
        ``config → compute_registry → config`` and
        ``config → detector → config`` circular imports; by the time
        ``_seed_defaults`` actually fires (file missing at startup)
        both ``compute_registry`` and ``detector`` are already loaded
        by the app import graph.
        """
        from app.services.compute_registry import (
            MODEL_PRESETS,
            _hardware_capability_default_state,
        )
        from app.services.detector import (
            DEFAULT_TARGET_DOC_DESC_DESC,
            DEFAULT_VLM_DOC_LEVEL_DESC,
            DEFAULT_VLM_DOC_TEXT_DESC,
        )
        with self._lock:
            self._data = {
                "base": {
                    "device_name": _random_device_name(),
                    "model_path": "/userdata/models/",
                    "resource_dir": "/userdata",
                },
                "compute": {
                    "services": list(MODEL_PRESETS.keys()),
                    "hardware_capabilities": _hardware_capability_default_state(),
                    "app_prompts": {
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
        previous config.json intact instead of a half-written file.

        落盘后强制 ``chmod 0o777`` —— ``tempfile.mkstemp`` 默认 mode 600，
        ``os.replace`` 之后目标文件继承这个 mode，不管 sudo 还是普通用户
        启动都会被锁成 root-only 600。这里统一刷成 0777 让同机其它用户
        （包括 web 前端 / 调试脚本）都能直接读写。
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=".config.", suffix=".json.tmp",
            dir=str(self._path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
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