"""Per-card uploads directory manager (v0.4 plugin refactor).

Unifies the scattered ``Path(__file__).parent / "uploads"`` paths and
the hand-rolled ``relative_to()`` checks that lived in each card's
``api.py`` (originally ``api/tts.py:118-126`` etc.). Single source of
truth for "where can card X write files".

Usage::

    # In a card's api.py / service.py:
    from app.core.uploads import get_card_uploads_root, safe_path

    uploads_root = get_card_uploads_root("video_detection")
    target = safe_path("video_detection", user_input_relpath)
    target.write_bytes(...)

Card ids are sourced from the card's own ``manifest.json["uploads_dir"]``
field; the convention is ``uploads/<card_id>/<rest>``.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Union

LOG = logging.getLogger("core.uploads")

# Resolved at module-import time: ``app/uploads/`` — the top-level shared
# staging directory for every card's writable files. All per-card
# subdirectories live below this root.
_APP_ROOT = Path(__file__).resolve().parent.parent
_UPLOADS_ROOT = _APP_ROOT / "uploads"


def get_card_uploads_root(card_id: str) -> Path:
    """Return ``app/uploads/<card_id>/`` absolute path; create if missing.

    ``card_id`` is the card's manifest ``id`` field (e.g. ``"video_detection"``).
    Used as a subdirectory name — must be a simple slug (lowercase, hyphen,
    underscore). Raises ``ValueError`` on anything that smells like a path
    traversal attempt (``..``, ``/``, ``\\``).
    """
    _validate_card_id(card_id)
    p = _UPLOADS_ROOT / card_id
    p.mkdir(parents=True, exist_ok=True)
    return p


def safe_path(card_id: str, rel: Union[str, Path]) -> Path:
    """Resolve ``rel`` relative to ``uploads/<card_id>/`` and validate.

    Returns the absolute ``Path``. Rejects any ``rel`` that escapes the
    card's directory (via ``..``, absolute path, symlink, etc.).

    The card is responsible for further validation (allowed extensions,
    size limits, etc.); this function only enforces the "stays in your own
    uploads dir" invariant.
    """
    _validate_card_id(card_id)
    root = get_card_uploads_root(card_id).resolve()
    if isinstance(rel, str):
        rel_path = Path(rel)
    else:
        rel_path = rel
    if rel_path.is_absolute():
        # Reject absolute paths outright — callers should always pass
        # something relative to their own uploads dir.
        raise ValueError(
            f"safe_path({card_id!r}, {str(rel)!r}): absolute paths not allowed"
        )
    # Resolve relative to root; resolve() canonicalises ``..`` and symlinks.
    candidate = (root / rel_path).resolve()
    try:
        # Python 3.9+: use is_relative_to for clarity.
        if hasattr(candidate, "is_relative_to"):
            ok = candidate.is_relative_to(root)
        else:  # pragma: no cover
            ok = str(candidate).startswith(str(root))
    except ValueError:  # pragma: no cover
        ok = False
    if not ok:
        raise ValueError(
            f"safe_path({card_id!r}, {str(rel)!r}): path escapes uploads root"
        )
    return candidate


def _validate_card_id(card_id: str) -> None:
    """Defensive check on card_id — must be a simple slug."""
    if not isinstance(card_id, str) or not card_id:
        raise ValueError("card_id must be a non-empty string")
    if card_id in (".", ".."):
        raise ValueError(f"card_id {card_id!r} not allowed")
    if "/" in card_id or "\\" in card_id:
        raise ValueError(
            f"card_id {card_id!r} must not contain path separators"
        )
    if ".." in card_id:
        raise ValueError(f"card_id {card_id!r} must not contain '..'")
    # Allow lowercase + digits + hyphen + underscore. Reject anything else.
    for ch in card_id:
        if not (ch.isalnum() or ch in "-_"):
            raise ValueError(
                f"card_id {card_id!r} contains illegal character {ch!r}"
            )
    if not card_id[0].isalnum():
        raise ValueError(
            f"card_id {card_id!r} must start with alphanumeric"
        )
