#!/usr/bin/env python3
"""``scripts/remove_card.py <card_id>`` — atomic card removal (v0.4 plugin refactor).

Implements the 16-B removal flow:

    1. Verify the card exists at ``app/apps/<card_id>/manifest.json``.
    2. Stop compute instances tied to the card's software capabilities.
       LAN peer workers are NOT killed (only local spawned workers).
    3. Remove the card directory (``shutil.rmtree``).
    4. Remove ``app/uploads/<card_id>/``.
    5. Clear the card's ``app.prompts`` entry in ``config.jsonc``.
    6. Print a summary + remind user to restart the service.

Default mode is ``--dry-run``: only print what would happen. Pass
``--yes`` (or ``-y``) to actually execute.

Usage::

    python scripts/remove_card.py audio_analysis           # dry-run
    python scripts/remove_card.py audio_analysis --yes     # actually remove
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from pathlib import Path

LOG = logging.getLogger("remove_card")

APP_ROOT = Path(__file__).resolve().parent.parent
APPS_DIR = APP_ROOT / "app" / "apps"
UPLOADS_DIR = APP_ROOT / "app" / "uploads"
CONFIG_PATH = APP_ROOT / "config.jsonc"


def parse_jsonc(path: Path) -> dict:
    """Tiny permissive JSONC reader. Strips ``// ...`` and ``/* ... */``."""
    raw = path.read_text(encoding="utf-8")
    out = []
    i = 0
    while i < len(raw):
        c = raw[i]
        if c == "/" and i + 1 < len(raw) and raw[i + 1] == "/":
            # Line comment
            nl = raw.find("\n", i)
            i = nl + 1 if nl >= 0 else len(raw)
            continue
        if c == "/" and i + 1 < len(raw) and raw[i + 1] == "*":
            end = raw.find("*/", i + 2)
            i = end + 2 if end >= 0 else len(raw)
            continue
        out.append(c)
        i += 1
    cleaned = "".join(out)
    return json.loads(cleaned) if cleaned.strip() else {}


def load_manifest(card_id: str) -> dict:
    path = APPS_DIR / card_id / "manifest.json"
    if not path.is_file():
        raise FileNotFoundError(f"manifest not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def stop_compute_for_card(card_id: str, sw_caps: list[str], dry_run: bool) -> list[str]:
    """Stop local compute instances tied to the card's sw capabilities.

    Returns the list of actions that were (or would be) performed.
    LAN peer workers are NOT touched.
    """
    actions: list[str] = []
    if not sw_caps:
        actions.append(f"  (no software_capabilities — nothing to stop)")
        return actions
    try:
        from app.core.compute.registry import get_registry  # type: ignore
        reg = get_registry()
    except Exception as e:
        actions.append(f"  WARN: cannot import compute registry: {e}")
        return actions

    snap = reg.snapshot()
    for cap in sw_caps:
        # Each instance has capability_types[]; find matches.
        for inst in snap.get("instances", []) or []:
            preset_caps = inst.get("capability_types") or []
            if cap in preset_caps:
                if dry_run:
                    actions.append(
                        f"  [dry-run] would stop instance "
                        f"model={inst.get('model_id')!r} npu={inst.get('npu_index')} status={inst.get('status')}"
                    )
                else:
                    try:
                        reg.stop_instance_local(
                            model_id=inst.get("model_id"),
                            npu_index=inst.get("npu_index"),
                        )
                        actions.append(
                            f"  stopped instance "
                            f"model={inst.get('model_id')!r} npu={inst.get('npu_index')}"
                        )
                    except Exception as e:
                        actions.append(
                            f"  ERROR stopping "
                            f"model={inst.get('model_id')!r} npu={inst.get('npu_index')}: {e}"
                        )
    return actions


def clear_app_prompts(card_id: str, dry_run: bool) -> list[str]:
    """Delete ``config.jsonc["app"]["prompts"][card_id]`` if present."""
    actions: list[str] = []
    if not CONFIG_PATH.is_file():
        actions.append(f"  (no config.jsonc — skipping)")
        return actions
    cfg = parse_jsonc(CONFIG_PATH)
    prompts = cfg.get("app", {}).get("prompts", {}) if isinstance(cfg, dict) else {}
    if not isinstance(prompts, dict) or card_id not in prompts:
        actions.append(f"  (no app.prompts.{card_id} — skipping)")
        return actions
    if dry_run:
        actions.append(f"  [dry-run] would delete config.jsonc[app.prompts.{card_id}] = {prompts[card_id]!r}")
    else:
        del prompts[card_id]
        # Write back as plain JSON (drops the JSONC comments)
        # Preserve JSONC-ish formatting: indent=2, ensure_ascii=False
        CONFIG_PATH.write_text(
            json.dumps(cfg, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        actions.append(f"  deleted config.jsonc[app.prompts.{card_id}]")
    return actions


def remove_dir(path: Path, dry_run: bool) -> list[str]:
    actions: list[str] = []
    if not path.exists():
        actions.append(f"  (no {path.relative_to(APP_ROOT)} — skipping)")
        return actions
    if dry_run:
        actions.append(f"  [dry-run] would rmtree {path.relative_to(APP_ROOT)}/")
    else:
        shutil.rmtree(path)
        actions.append(f"  removed {path.relative_to(APP_ROOT)}/")
    return actions


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Remove a card (delete its folder + uploads + prompts entry)."
    )
    parser.add_argument("card_id", help="e.g. 'audio_analysis'")
    parser.add_argument(
        "--yes", "-y", action="store_true",
        help="Actually execute the removal (default is dry-run).",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Verbose logging.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
    )

    dry_run = not args.yes

    print(f"=== Remove card: {args.card_id} (mode: {'DRY-RUN' if dry_run else 'EXECUTE'}) ===")

    # 1. Verify manifest exists
    try:
        manifest = load_manifest(args.card_id)
    except FileNotFoundError as e:
        print(f"FATAL: {e}", file=sys.stderr)
        return 2
    sw_caps = manifest.get("software_capabilities") or []
    print(f"\n[1] Manifest loaded. software_capabilities = {sw_caps}")

    # 2. Stop compute
    print(f"\n[2] Stop compute instances for software capabilities:")
    actions = stop_compute_for_card(args.card_id, sw_caps, dry_run)
    for a in actions:
        print(a)

    # 3. Remove card directory
    card_dir = APPS_DIR / args.card_id
    print(f"\n[3] Remove card directory:")
    for a in remove_dir(card_dir, dry_run):
        print(a)

    # 4. Remove uploads
    uploads = UPLOADS_DIR / args.card_id
    print(f"\n[4] Remove uploads:")
    for a in remove_dir(uploads, dry_run):
        print(a)

    # 5. Clear config.jsonc
    print(f"\n[5] Clear config.jsonc app.prompts entry:")
    for a in clear_app_prompts(args.card_id, dry_run):
        print(a)

    # 6. Reminder
    print(f"\n[6] Reminder: restart wdnwebui service so manifest re-scan picks up the change:")
    print(f"    sudo systemctl restart wdnwebui")
    print(f"\n=== {'Dry-run complete' if dry_run else 'Removal complete'} ===")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
