"""Centralized config.json loader / writer.

The path is taken from the ``PYRKAIWEB_CONFIG`` env var (set by
``run.py --config …``) and falls back to ``<repo>/config.json`` if
unset. The file is read once at first access and held in memory;
writes go through :meth:`ConfigStore.replace`, which validates the
payload and persists atomically (tmp + ``os.replace``).
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from threading import RLock
from typing import Any


_DEFAULT_CONFIG_PATH = (
    Path(__file__).resolve().parents[2] / "config.json"
)


def _resolve_config_path() -> Path:
    env = os.environ.get("PYRKAIWEB_CONFIG")
    if env:
        p = Path(env).expanduser()
        try:
            return p.resolve()
        except OSError:
            return p
    return _DEFAULT_CONFIG_PATH


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
        """Re-read the file from disk. Missing / unreadable / non-object
        payloads all collapse to ``{}`` so the rest of the app can treat
        the config as a JSON object unconditionally."""
        with self._lock:
            if not self._path.exists():
                self._data = {}
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
        previous config.json intact instead of a half-written file."""
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
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise


_store: ConfigStore | None = None


def get_config() -> ConfigStore:
    """Lazy singleton accessor. First call resolves the config path from
    the ``PYRKAIWEB_CONFIG`` env var (or falls back to the repo default)
    and reads the file."""
    global _store
    if _store is None:
        _store = ConfigStore()
    return _store