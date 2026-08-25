"""The value vocabulary the three operator-config surfaces share.

Deployment settings (`config/operator_settings.py`), plugin settings
(`config/plugin_settings.py`), and per-tool config (`tools/config_spec.py`) all
let an operator put a typed scalar in YAML frontmatter, and all three had their
own copy of the same five kind constants and the same ~40-line coercion. The
copies had already drifted: only the tool surface enforced an upper bound, so a
spec could declare `maximum` in one place and be silently ignored in the other
two.

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
    "SCALAR_KINDS",
    "coerce_scalar",
]

KIND_INT = "int"
KIND_FLOAT = "float"
KIND_BOOL = "bool"
KIND_TEXT = "text"
KIND_CHOICE = "choice"

# The kinds every surface understands. A surface may add its own (deployment
# settings also has an id-list kind) and handles those before delegating here.
SCALAR_KINDS = frozenset({KIND_INT, KIND_FLOAT, KIND_BOOL, KIND_TEXT, KIND_CHOICE})


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
