#!/usr/bin/env python
"""Verify that the installed card plugin packages really registered their
``wdnwebui.cards`` entry points.

Called by ``verify_plugin_cards`` of ``update_rkwhl.sh``, and it can be run
on its own:

    .venv/bin/python scripts/check_plugin_entrypoints.py wdnapps_video_analyzer

Why this exists
---------------
These plugin packages are pure Python, so there is no ``.so`` to assert. But
they have an even more fatal failure mode: **pip reports success and not a
single card shows up**, without any error along the way.

The most common cause is that the entry point name and the ``id`` in
``manifest.json`` do not match — the package directory uses underscores
(``cards/video_detection/``) while the card id consistently uses hyphens
(``"video-detection"``), so copying the module name gets every one of them
wrong. After ``discover()`` of the host marks it FATAL it **silently skips**
that card, so "it is installed" and "the card works" are two different things
and have to be asserted here.

``importlib.util.find_spec`` is used on purpose instead of
``import_module``: the former only imports the parent package
(``cards/__init__.py`` is empty, conveniently) and **does not execute the
leaf module**; the latter would pull the whole ``app.core.*`` chain up
through ``api.py`` (compute registry, decoder initialisation), which is both
slow and has side effects in an update script.

Output convention: one line per card, ``OK <id> (<dir>)`` or ``BAD: <reason>``,
**always exit 0** — the decision is left to the caller grepping for it, which
avoids the error-prone shell dance of passing an exit code around.
"""
from __future__ import annotations

import json
import sys
from importlib.metadata import entry_points
from importlib.util import find_spec
from pathlib import Path

GROUP = "wdnwebui.cards"


def check_pkg(pkg: str) -> int:
    """Return the number of problems found for ``pkg``."""
    try:
        eps = sorted(entry_points(group=GROUP), key=lambda e: e.name)
    except Exception as e:  # noqa: BLE001
        print(f"BAD: cannot read the {GROUP} entry point group: {e}")
        return 1

    # The underscores/hyphens of the entry point name differ from the card
    # id, so the ownership is decided by the top level package name — that
    # way a change in the spelling of the package name does not make the
    # cards unfindable.
    mine = [e for e in eps if e.value.split(".")[0] == pkg.replace("-", "_")]

    if not mine:
        print(f"BAD: {pkg} registered no {GROUP} entry point")
        return 1

    bad = 0
    for e in mine:
        try:
            spec = find_spec(e.value)
        except Exception as ex:  # noqa: BLE001
            print(f"BAD: {e.name}: cannot locate the module {e.value}: {ex}")
            bad += 1
            continue
        if spec is None or not spec.submodule_search_locations:
            print(f"BAD: {e.name}: {e.value} is not a package, cannot locate the card directory")
            bad += 1
            continue

        d = Path(list(spec.submodule_search_locations)[0])
        mf = d / "manifest.json"
        if not mf.is_file():
            print(f"BAD: {e.name}: the card directory has no manifest.json ({d})")
            bad += 1
            continue

        try:
            declared = str(
                json.loads(mf.read_text(encoding="utf-8")).get("id", "")
            ).strip()
        except Exception as ex:  # noqa: BLE001
            print(f"BAD: {e.name}: cannot parse manifest.json: {ex}")
            bad += 1
            continue

        if declared != e.name:
            print(
                f"BAD: {e.name}: the manifest id {declared!r} != the entry point name "
                f"{e.name!r} — the host marks it FATAL and silently skips this card"
            )
            bad += 1
            continue

        if not (d / "static").is_dir():
            print(f"BAD: {e.name}: the card directory has no static/ — the card is found but the frontend gets a 404")
            bad += 1
            continue

        print(f"OK {e.name} ({d})")

    return bad


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: check_plugin_entrypoints.py <package name> [<package name> ...]", file=sys.stderr)
        print("       without arguments every wdnwebui.cards plugin package is checked", file=sys.stderr)
        return 2
    total = sum(check_pkg(p) for p in argv[1:])
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
