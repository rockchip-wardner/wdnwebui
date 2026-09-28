"""Thin JSONC (JSON with Comments) wrapper around the ``json5`` library.

Why this module exists
----------------------
``config.jsonc`` may contain ``//`` line comments and ``/* ... */`` block
comments — the standard ``json`` module rejects them. We use ``json5``,
a small pure-Python library, to parse and emit JSONC. This module just
wraps the two calls we need (``loads`` / ``dumps``) with the options
that match how the rest of the codebase reads and writes the config.

Comment round-trip policy
-------------------------
**User comments are not preserved across writes.** ``parse_jsonc``
strips comments during read (along with trailing commas and other JSON5
extensions), and ``dump_jsonc`` emits standard JSON-with-quoted-keys
with no comments. The user-facing contract is: "your comments stay in
the file as long as the backend doesn't re-save it; any save through
``PUT /api/config/...`` or ``POST /api/config/import`` discards them
and emits a freshly formatted JSONC file." Simpler is better here — the
previous key-path-based round-trip was correct on paper but brittle to
edge cases (key reordering, nested object comments inside arrays, etc.).
"""
from __future__ import annotations

from typing import Any

import json5


def parse_jsonc(text: str) -> Any:
    """Parse JSONC text into a Python value.

    Accepts standard JSON plus ``//`` and ``/* */`` comments and JSON5
    trailing commas / unquoted keys. Returns whatever ``json5.loads``
    returns — the caller is responsible for validating it's an object.
    """
    return json5.loads(text)


def dump_jsonc(data: Any, *, indent: int = 2) -> str:
    """Serialize ``data`` to JSONC text (no comments).

    Output options:

    * ``indent=2`` — two-space indentation, matches what the codebase
      was already writing before JSONC.
    * ``ensure_ascii=False`` — UTF-8 in string literals, so Chinese
      comments and string values stay readable instead of ``\\uXXXX``.
    * ``quote_keys=True`` + ``quote_style=ALWAYS_DOUBLE`` — keys are
      always double-quoted, even though ``json5`` would happily omit
      quotes. Keeps the file strictly JSON-compatible.
    * ``trailing_commas=True`` — keep trailing commas (valid JSONC).
    """
    return json5.dumps(
        data,
        indent=indent,
        ensure_ascii=False,
        quote_keys=True,
        quote_style=json5.lib.QuoteStyle.ALWAYS_DOUBLE,
        trailing_commas=True,
    )