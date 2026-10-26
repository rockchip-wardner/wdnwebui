#!/usr/bin/env python3
"""Make the PyPI ``v4l2`` 0.2 package importable on Python 3.

Why this script exists
----------------------
``wdnav``'s V4L2 layer (``wdnav/src/wdnav/v4l2.py``) does a **module
level** ``import v4l2 as _v4l2`` — the *third-party* PyPI package
``v4l2`` (pure-ctypes Video4L2 bindings, GPLv2), not the stdlib.
``wdnav``'s ``pyproject.toml`` declares ``v4l2>=0.2``, but the SDK is
installed with ``pip install --no-deps -e`` (README §4 / update_rkwhl.sh)
which skips dependency resolution — so the package never lands in the
venv on its own.  Without it::

    ModuleNotFoundError: No module named 'v4l2'

and every ``GET /api/usb-hid/source/hdmiin/snapshot`` returns 503.

Installing it is only half the job: the 0.2 release (2014) still
contains Python-2 idioms in two places::

    ) = range(1, 9) + [0x80]     # v4l2.py:197
    ) = range(0, 4) + [2]        # v4l2.py:248

On Python 3 ``range`` is not a sequence type, so both lines raise
**at import time**::

    TypeError: unsupported operand type(s) for +: 'range' and 'list'

That is a ``TypeError``, *not* an ``ImportError`` — the ``except
ImportError`` guard in ``app/core/hdmiin/capture.py`` does not catch
it, so a naive ``pip install v4l2`` turns a clean 503 into an
unhandled 500 traceback.  The fix is one token per line: wrap the
``range()`` in ``list()``.  Upstream's own docs recommend exactly
this "one-line sed patch on site-packages" (see the module docstring
of ``wdnav/v4l2.py``).

This script performs that patch, idempotently, and then verifies the
result by importing ``v4l2`` in a **fresh** interpreter (the current
one may have a cached/failed import).

Usage::

    .venv/bin/python scripts/fix_v4l2_py3.py          # patch + verify
    .venv/bin/python scripts/fix_v4l2_py3.py --check   # verify only

Exit codes::

    0  ok (patched, or already patched)
    1  failed (import still broken / file not writable)
    2  the ``v4l2`` package is not installed at all
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import site
import subprocess
import sys
import sysconfig
from pathlib import Path

# ``= range(1, 9) + [0x80]``  →  ``= list(range(1, 9)) + [0x80]``
#
# Deliberately narrow: only the "range(...) immediately followed by
# ``+ [literal list]``" shape is rewritten.  v4l2.py contains 20+ other
# ``range(...)`` uses (plain enum member lists) which are perfectly
# valid Python 3 and must not be touched.
_PY2_RANGE_PLUS_LIST = re.compile(
    r"(?<![\w.])range\((?P<args>[^()]*)\)\s*\+\s*(?P<tail>\[[^\[\]]*\])"
)


def _candidate_dirs() -> list[Path]:
    """Directories that may hold the installed ``v4l2`` module."""
    out: list[Path] = []
    for key in ("purelib", "platlib"):
        p = sysconfig.get_paths().get(key)
        if p:
            out.append(Path(p))
    try:
        out.extend(Path(p) for p in site.getsitepackages())
    except Exception:  # noqa: BLE001 — site may be unavailable in odd envs
        pass
    try:
        usp = site.getusersitepackages()
        if isinstance(usp, str):
            out.append(Path(usp))
    except Exception:  # noqa: BLE001
        pass
    seen: set[str] = set()
    uniq: list[Path] = []
    for d in out:
        if str(d) not in seen:
            seen.add(str(d))
            uniq.append(d)
    return uniq


def _find_v4l2_file() -> Path | None:
    """Locate the installed ``v4l2`` module *without* importing it.

    ``importlib.util.find_spec`` only locates the file (parent packages
    are imported, the module body is not), so it still works when the
    module is broken — which is exactly the case we need to repair.
    """
    try:
        spec = importlib.util.find_spec("v4l2")
    except (ImportError, ValueError):
        spec = None
    if spec is not None and spec.origin and spec.origin != "built-in":
        origin = Path(spec.origin)
        if origin.is_dir():          # installed as a package directory
            init = origin / "__init__.py"
            return init if init.is_file() else None
        return origin if origin.is_file() else None
    for d in _candidate_dirs():
        cand = d / "v4l2.py"
        if cand.is_file():
            return cand
    return None


def patch_file(path: Path) -> int:
    """Rewrite the py2 ``range() + list`` idiom. Returns #lines patched."""
    src = path.read_text(encoding="utf-8")
    new, n = _PY2_RANGE_PLUS_LIST.subn(
        lambda m: f"list(range({m.group('args')})) + {m.group('tail')}",
        src,
    )
    if n:
        path.write_text(new, encoding="utf-8")
    return n


def verify() -> tuple[bool, str]:
    """Import ``v4l2`` in a fresh interpreter. Returns (ok, detail)."""
    proc = subprocess.run(
        [sys.executable, "-c", "import v4l2; print(v4l2.__file__)"],
        capture_output=True,
        text=True,
    )
    if proc.returncode == 0:
        return True, proc.stdout.strip()
    return False, (proc.stderr.strip() or proc.stdout.strip())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Patch the PyPI v4l2 0.2 package for Python 3.",
    )
    ap.add_argument(
        "--check", action="store_true",
        help="verify only — do not modify site-packages",
    )
    args = ap.parse_args(argv)

    path = _find_v4l2_file()
    if path is None:
        print(
            "v4l2 is not installed in this interpreter.\n"
            "  install it with:  pip install -r requirements.txt\n"
            "  (v4l2>=0.2 — hard dependency of wdnav.v4l2)",
            file=sys.stderr,
        )
        return 2

    if not args.check:
        try:
            n = patch_file(path)
        except PermissionError:
            print(
                f"cannot write {path} (permission denied).\n"
                "  re-run with sudo, or install v4l2 as a user site-package.",
                file=sys.stderr,
            )
            return 1
        except OSError as e:
            print(f"cannot patch {path}: {e}", file=sys.stderr)
            return 1
        if n:
            print(f"patched {n} py2 idiom(s) in {path}")
        else:
            print(f"no py2 idiom left in {path} (already patched)")

    ok, detail = verify()
    if ok:
        print(f"OK: `import v4l2` works -> {detail}")
        return 0

    print(
        f"FAILED: `import v4l2` still broken in {sys.executable}:\n{detail}",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
