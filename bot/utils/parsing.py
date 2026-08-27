"""Argument parsing shared by tool handlers and skill editing.

These parse values the *model* supplied, so they fail loudly rather than
coercing: a plausible-looking success for an operation the model did not ask
for is worse than an error it can read and correct.
"""

from __future__ import annotations

_TRUE_WORDS = {"1", "true", "yes", "on"}
_FALSE_WORDS = {"0", "false", "no", "off"}


def as_bool(value: object, *, name: str = "value", default: bool) -> bool:
    """Parse a boolean argument; an unrecognized string raises.

    Silent coercion is the bug this prevents: `attach: "flase"` becoming False
    un-attaches a deliverable, and `replace_all: "flase"` edits one occurrence
    where the caller meant all of them.
    """

    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        word = value.strip().lower()
        if word in _TRUE_WORDS:
            return True
        if word in _FALSE_WORDS:
            return False
        raise ValueError(f"{name} must be true or false, got {value!r}")
    if isinstance(value, int):
        return bool(value)
    raise ValueError(f"{name} must be true or false")
