#!/usr/bin/env python3
"""Audit the SDK-declared dependencies against what is actually installed.

Why this exists
---------------
The internal SDKs are always installed with ``pip install --no-deps -e``
(``update_rkwhl.sh`` / ``bootstrap_wdndds_native.sh`` / README §4).  That
is deliberate, not legacy:

* the dependency graph of the private SDKs contains **private packages**
  (``wdndds`` → ``wdnai``, the card plugins → ``wdnhid`` / ``wdnav``) that
  do not exist on PyPI, so letting pip resolve them fails outright;
* the operator owns the dependency set — ``requirements.txt`` pins
  ``transformers`` / ``tokenizers`` / ``opencv-python-headless`` with
  upper bounds that pip would otherwise happily break;
* ``av`` needs a hand-picked build (wdnav's README pins PyAV's main
  branch until the fix ships in a release).

But ``--no-deps`` has a sharp edge: **the SDKs' declared dependencies never
land in the venv on their own**, and the symptom shows up far away from
the cause —

* ``v4l2`` missing → the HDMI-IN endpoints return 503 forever
  (``No module named 'v4l2'``);
* ``tokenizers`` missing → the compute page reports a model startup failure.

This script turns that class of failure into an install-time error:

* every ``[project].dependencies`` entry of every SDK under
  ``.venv/src/`` is checked against the **installed** distributions
  (``importlib.metadata`` — *never* the index, so it works offline);
* the version constraint is verified when it can be parsed;
* the module is **really imported**, because "installed but broken" is a
  real class here (upstream ``v4l2`` 0.2 raises ``TypeError`` on import
  under Python 3 — see ``scripts/fix_v4l2_py3.py``).

Usage::

    .venv/bin/python scripts/check_sdk_deps.py            # audit + exit 1 on problems
    .venv/bin/python scripts/check_sdk_deps.py --quiet     # only print problems
    .venv/bin/python scripts/check_sdk_deps.py --src-dir DIR   # audit another tree

It is wired into ``update_rkwhl.sh`` (after every package has been
processed — a failing audit fails the whole run) and into
``bootstrap_wdndds_native.sh`` (step 7, warns but does not abort).

Exit codes::

    0  every declared dependency is installed and importable
    1  at least one dependency is missing / version-mismatched / unimportable
    2  the SDK source tree or the venv could not be located
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import importlib.metadata as md
import logging
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover — Python < 3.11
    tomllib = None  # type: ignore[assignment]

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SRC_DIR = REPO_ROOT / ".venv" / "src"
REQUIREMENTS = REPO_ROOT / "requirements.txt"

#: Private distributions — provided by the editable install of a sibling SDK,
#: **not on PyPI**. When one of them is missing it can only be fixed by
#: installing the SDKs through ``update_rkwhl.sh``; ``pip install <name>``
#: is the wrong remedy, so the hint has to be worded differently.
PRIVATE_DISTS = {
    "wdnav",
    "wdnai",
    "wdndds",
    "wdnhid",
    "wdnapps-hdmi-processor",
    "wdnapps-video-analyzer",
    "wdnapps_hdmi_processor",
    "wdnapps_video_analyzer",
}

#: Distribution name → import name (the same name by default, with
#: underscores).
_IMPORT_NAME = {
    "opencv-python": "cv2",
    "opencv-python-headless": "cv2",
    "av": "av",
    "pillow": "PIL",
    "python-multipart": "multipart",
    "pyyaml": "yaml",
    "protobuf": "google.protobuf",
    "sentencepiece": "sentencepiece",
}

#: Equivalent distributions that provide the same import. The headless build
#: already satisfies a declared ``opencv-python`` — the forced resolution
#: would install the **GUI build of opencv-python** as well, and the two
#: overwrite each other's ``cv2`` (see README §3).
_EQUIVALENT = {
    "opencv-python": ("opencv-python-headless",),
    "opencv-python-headless": ("opencv-python",),
}

_REQ_RE = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"\s*(?:\[(?P<extras>[^\]]*)\])?"
    r"\s*(?P<spec>[^;]*)"
    r"(?:;\s*(?P<marker>.*))?$"
)

#: Status
OK = "OK"
MISSING = "MISSING"
BAD_VERSION = "BAD_VERSION"
BROKEN = "BROKEN"
SKIPPED = "SKIPPED"


@dataclass
class Check:
    """One dependency of one SDK."""

    sdk: str
    raw: str
    name: str
    spec: str
    marker: str
    status: str = OK
    detail: str = ""


def _norm(name: str) -> str:
    """PEP 503 normalisation (lowercase, runs of -_. collapse to -)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _parse_requirement(raw: str) -> tuple[str, str, str] | None:
    """Return ``(name, specifier, marker)`` or ``None`` when unparseable."""
    m = _REQ_RE.match(raw)
    if not m:
        return None
    return m.group("name"), (m.group("spec") or "").strip(), (m.group("marker") or "").strip()


def _marker_applies(marker: str) -> bool:
    """Evaluate an environment marker.

    Uses ``packaging`` when available; when it is not, a marker we cannot
    evaluate is treated as **applying** (conservative: report a problem
    rather than hide one).
    """
    if not marker:
        return True
    try:
        from packaging.markers import Marker

        return bool(Marker(marker).evaluate())
    except Exception:  # noqa: BLE001 — packaging missing or marker unsupported
        return True


def _version_ok(installed: str, spec: str) -> bool:
    """Check ``installed`` against a PEP 440 specifier set.

    An unparseable specifier is accepted — the audit's job is to catch
    *missing* dependencies, not to become a second version resolver.
    """
    if not spec:
        return True
    try:
        from packaging.specifiers import SpecifierSet

        return installed in SpecifierSet(spec)
    except Exception:  # noqa: BLE001
        return True


def _import_name(dist: str) -> str:
    """Module to import for a distribution name."""
    if dist in _IMPORT_NAME:
        return _IMPORT_NAME[dist]
    return _norm(dist).replace("-", "_")


def _installed_version(dist: str) -> str | None:
    """Version of ``dist`` in this interpreter's environment, if installed."""
    try:
        return md.version(dist)
    except md.PackageNotFoundError:
        for alt in _EQUIVALENT.get(_norm(dist), ()):
            try:
                return md.version(alt)
            except md.PackageNotFoundError:
                continue
    return None


def _expand_equivalents(names: set[str]) -> set[str]:
    """Treat distribution names that provide the same import as each other
    as "pinned".

    requirements.txt pins ``opencv-python-headless`` while the SDKs declare
    ``opencv-python`` — both provide the same ``cv2`` package. Without this
    expansion the hint would wrongly say "requirements.txt does not have it
    either" and send people to install the GUI build of opencv (which
    fights the headless build over the same ``cv2``).
    """
    out = set(names)
    for name in list(out):
        out.update(_EQUIVALENT.get(name, ()))
    return out


def _requirements_pins() -> set[str]:
    """Distribution names pinned in ``requirements.txt``."""
    names: set[str] = set()
    if not REQUIREMENTS.is_file():
        return names
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        parsed = _parse_requirement(line)
        if parsed:
            names.add(_norm(parsed[0]))
    return _expand_equivalents(names)


def _read_dependencies(pyproject: Path) -> list[str]:
    """``[project].dependencies`` of a pyproject.toml (raw requirement strings)."""
    if tomllib is None:
        raise RuntimeError(
            "Python 3.11+ required (tomllib) — run with .venv/bin/python"
        )
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    deps = data.get("project", {}).get("dependencies", []) or []
    return [d for d in deps if isinstance(d, str)]


@contextlib.contextmanager
def _captured_stdio():
    """Capture everything written to fd 1 / fd 2 while the block runs.

    Importing some of these modules is *noisy* — transformers prints
    "None of PyTorch, TensorFlow >= 2.0, or Flax have been found" and
    some modules print banners from C code.  Neither belongs in an
    audit report, but both are useful when an import *fails*.  So:
    swallow during the import, re-surface on failure.

    Redirecting the file descriptors (not just ``sys.stderr``) is what
    catches the C-level writes; ``logging`` handlers hold their own
    reference to the stream, so rebinding ``sys.stderr`` would not
    silence them.
    """
    sys.stdout.flush()
    sys.stderr.flush()
    saved_out = os.dup(1)
    saved_err = os.dup(2)
    tmp = tempfile.TemporaryFile()
    try:
        os.dup2(tmp.fileno(), 1)
        os.dup2(tmp.fileno(), 2)
        yield tmp
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        os.dup2(saved_out, 1)
        os.dup2(saved_err, 2)
        os.close(saved_out)
        os.close(saved_err)
        tmp.close()


def _read_captured(tmp) -> str:
    try:
        tmp.seek(0)
        return tmp.read().decode("utf-8", "replace").strip()
    except Exception:  # noqa: BLE001
        return ""


def _mute_stdout_at_teardown() -> None:
    """Point fd 1 at /dev/null so the native banner that the interpreter
    prints while shutting down falls into the void.

    ``librga`` writes a line ``rga_api version 1.10.5_[9]`` straight to fd 1
    via ``write(2)`` during **process teardown** (ctypes releases the CDLL →
    dlclose).

    Observed with strace: that is one **independent write(1, ...) system
    call** that happens after the body of the script has run and the
    interpreter starts tearing down — **earlier even than Python's atexit
    handlers** — so "silencing it in atexit" is too late (tried, the banner
    shows up anyway).

    Therefore fd 1 has to be swapped right before main() returns. stderr is
    left alone: real errors still have to be visible.

    Only called under ``__main__`` — this script must not steal the stdout
    of its caller when it is imported as a module.
    """
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
    except OSError:  # pragma: no cover — /dev/null always exists on Linux
        return
    sys.stdout.flush()
    os.dup2(devnull, 1)


def _check_one(sdk: str, raw: str, pins: set[str]) -> Check:
    parsed = _parse_requirement(raw)
    if parsed is None:
        return Check(sdk, raw, "", "", "", SKIPPED, "the dependency declaration cannot be parsed")
    name, spec, marker = parsed
    norm = _norm(name)

    if not _marker_applies(marker):
        return Check(sdk, raw, name, spec, marker, SKIPPED,
                     f"the marker does not apply to this environment ({marker})")

    version = _installed_version(name)
    if version is None:
        if norm in PRIVATE_DISTS:
            detail = ("a private package that is not on PyPI — install the SDKs"
                      " with ./update_rkwhl.sh, do not pip install")
        elif norm in pins:
            detail = ("already pinned in requirements.txt — run "
                      ".venv/bin/pip install -r requirements.txt")
        else:
            detail = (f"requirements.txt does not have it either — add it to "
                      f"requirements.txt (.venv/bin/pip install '{name}{spec}')")
        return Check(sdk, raw, name, spec, marker, MISSING, detail)

    if not _version_ok(version, spec):
        return Check(sdk, raw, name, spec, marker, BAD_VERSION,
                     f"the installed version is {version}, which does not satisfy {spec}")

    module = _import_name(name)
    with _captured_stdio() as tmp:
        try:
            importlib.import_module(module)
        except Exception as e:  # noqa: BLE001 — every import failure has to be reported
            noise = _read_captured(tmp)
            detail = (f"installed ({version}) but the import fails: "
                      f"{type(e).__name__}: {e}")
            if noise:
                detail += f" | output during the import: {noise[:300]}"
            return Check(sdk, raw, name, spec, marker, BROKEN, detail)
    return Check(sdk, raw, name, spec, marker, OK, version)


def _discover_sdks(src_dir: Path) -> list[tuple[str, Path]]:
    """``(name, pyproject)`` for every SDK source tree under ``src_dir``."""
    out: list[tuple[str, Path]] = []
    if not src_dir.is_dir():
        return out
    for entry in sorted(src_dir.iterdir()):
        pyproject = entry / "pyproject.toml"
        if entry.is_dir() and pyproject.is_file():
            out.append((entry.name, pyproject))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Audit SDK-declared dependencies against the venv.",
    )
    ap.add_argument(
        "--src-dir", default=str(DEFAULT_SRC_DIR),
        help="the SDK source tree directory (default: .venv/src)",
    )
    ap.add_argument(
        "--quiet", action="store_true",
        help="only print the dependencies that have problems",
    )
    args = ap.parse_args(argv)

    src_dir = Path(args.src_dir).resolve()
    sdks = _discover_sdks(src_dir)
    if not sdks:
        print(
            f"[deps] FATAL: no SDK with a pyproject.toml found under {src_dir}",
            file=sys.stderr,
        )
        return 2

    pins = _requirements_pins()
    # transformers prints a "None of PyTorch..." warning while it is
    # imported, which has nothing to do with the audit — silence it so it
    # does not pollute the output. **Must be set before the import.**
    logging.getLogger("transformers").setLevel(logging.ERROR)

    checks: list[Check] = []
    for sdk_name, pyproject in sdks:
        try:
            deps = _read_dependencies(pyproject)
        except Exception as e:  # noqa: BLE001
            print(f"[deps] FATAL: cannot read {pyproject}: {e}", file=sys.stderr)
            return 2
        for raw in deps:
            checks.append(_check_one(sdk_name, raw, pins))

    problems = [c for c in checks if c.status not in (OK, SKIPPED)]

    name_w = max(len(n) for n, _ in sdks) + 1
    if not args.quiet:
        print(f"[deps] scanning the [project].dependencies of {len(sdks)} SDKs "
              f"under {src_dir} ({len(checks)} entries)")
    for sdk_name, _ in sdks:
        mine = [c for c in checks if c.sdk == sdk_name]
        if not mine:
            continue
        bad = [c for c in mine if c.status not in (OK, SKIPPED)]
        skipped = [c for c in mine if c.status == SKIPPED]
        if not bad and args.quiet:
            continue
        head = f"[deps] {sdk_name:<{name_w}}"
        if not bad:
            applied = [c.name for c in mine if c.status == OK]
            extra = (f"({len(skipped)} entries skipped, their marker does not apply)"
                     if skipped else "")
            print(f"{head}OK       {', '.join(applied)}{extra}")
            continue
        for c in bad:
            print(f"{head}{c.status:<11} {c.raw}  —— {c.detail}")
        if skipped:
            # When there are problems, say clearly "and this is what was
            # skipped as well" — otherwise the number of detail lines does
            # not match the number of dependencies and the reader thinks the
            # audit missed a few.
            print(f"{head}{'SKIPPED':<11} " +
                  ", ".join(f"{c.raw} ({c.detail})" for c in skipped))

    if not problems:
        if not args.quiet:
            print(f"[deps] everything is ready ({len(checks)} dependencies)")
        return 0

    # stdout is block buffered when it goes into a pipe, stderr is not — without
    # an explicit flush the summary ends up **before** the details, which
    # reads like "the conclusion first, the evidence later".
    sys.stdout.flush()
    print("", file=sys.stderr)
    print(f"[deps] FATAL: {len(problems)} dependencies are unusable", file=sys.stderr)
    missing_pins = sorted({
        _norm(c.name) for c in problems
        if c.status == MISSING and _norm(c.name) not in pins
        and _norm(c.name) not in PRIVATE_DISTS
    })
    if missing_pins:
        print(f"       requirements.txt does not have these either, add them first: "
              f"{', '.join(missing_pins)}", file=sys.stderr)
    print("       fix: .venv/bin/pip install -r requirements.txt",
          file=sys.stderr)
    if any(c.status == BROKEN for c in problems):
        print("       an import failure is usually \"installed but the version is wrong"
              "/ there is a bug\" — after installing, remember to run a patch script"
              " such as scripts/fix_v4l2_py3.py",
              file=sys.stderr)
    return 1


if __name__ == "__main__":
    _code = main()
    # The report has already gone to stdout — now swap fd 1 for /dev/null so
    # the native banner that the interpreter prints while shutting down
    # (librga writes it) falls into the void. Placed before the raise, and
    # after the module level import.
    _mute_stdout_at_teardown()
    raise SystemExit(_code)
