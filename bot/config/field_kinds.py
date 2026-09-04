"""Shared scalar vocabulary and coercion for operator configuration.

Deployment settings (`config/operator_settings.py`), plugin settings
(`config/plugin_settings.py`), and per-tool config (`tools/config_spec.py`) all
accept typed scalars in YAML frontmatter. This module keeps their common kinds
and minimum/maximum enforcement consistent.

This owns the kinds and the coercion. Each surface keeps its own spec dataclass
(they carry genuinely different extras) and its own kinds beyond these.

Stdlib-only on purpose: `tools/config_spec.py` is imported at tool-declaration
time, long before the settings overlay exists, and `tests/test_import_isolation.py`
enforces that it drags in no runtime state.
"""

from __future__ import annotations

import math
from typing import Any

__all__ = [
    "KIND_BOOL",
    "KIND_CHOICE",
    "KIND_FLOAT",
    "KIND_INT",
    "KIND_TEXT",
    "coerce_scalar",
]

KIND_INT = "int"
KIND_FLOAT = "float"
KIND_BOOL = "bool"
KIND_TEXT = "text"
KIND_CHOICE = "choice"


def coerce_scalar(
    kind: str,
    raw: Any,
    *,
    choices: tuple[str, ...] = (),
    minimum: int | float | None = None,
    maximum: int | float | None = None,
) -> Any:
    """Convert one frontmatter value to its typed form, or raise `ValueError`.

    Callers decide what a failure means: the tool loader turns it into "use the
    default", while the settings overlay turns it into a startup error.

    `bool` is rejected for numeric kinds because Python makes it an `int`, so
    `max_results: true` would otherwise silently become 1.
    """

    if kind == KIND_BOOL:
        if not isinstance(raw, bool):
            raise ValueError("expected true or false")
        return raw

    if kind in (KIND_INT, KIND_FLOAT):
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError("expected a number" if kind == KIND_FLOAT else "expected an integer")
        if kind == KIND_INT and not isinstance(raw, int):
            raise ValueError("expected an integer")
        value = float(raw) if kind == KIND_FLOAT else raw
        if kind == KIND_FLOAT and not math.isfinite(value):
            raise ValueError("expected a finite number")
        if minimum is not None and value < minimum:
            raise ValueError(f"must be at least {minimum}")
        if maximum is not None and value > maximum:
            raise ValueError(f"must be at most {maximum}")
        return value

    if kind == KIND_CHOICE:
        if not isinstance(raw, str):
            raise ValueError("expected a choice")
        text = raw.strip()
        if text not in choices:
            raise ValueError(f"expected one of {', '.join(choices)}")
        return text

    if kind == KIND_TEXT:
        if not isinstance(raw, str):
            raise ValueError("expected a string")
        return raw.strip()

    raise ValueError(f"unknown config kind {kind!r}")
