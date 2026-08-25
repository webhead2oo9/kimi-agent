from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

UNTRUSTED_CONTEXT_KEY = "context_is_untrusted"


def tool_error(message: str) -> str:
    return json.dumps({"error": message})


def untrusted_payload(payload: dict[str, Any], note: str) -> dict[str, Any]:
    # Trust keys are spread last so a payload carrying an upstream response can
    # never override the envelope (e.g. a colliding "context_is_untrusted" key).
    return {**payload, UNTRUSTED_CONTEXT_KEY: True, "note": note}


def json_untrusted_payload(payload: dict[str, Any], note: str) -> str:
    return json.dumps(untrusted_payload(payload, note))


def get_int(
    value: object,
    *,
    name: str,
    default: int | None = None,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """Parse an integer argument, raising outside either bound.

    Not interchangeable with `tools/workspace/common.py:clamped_int`, which
    takes the same arguments and clamps above the maximum instead of raising.
    """
    if value is None:
        if default is None:
            raise ValueError(f"{name} must be an integer")
        parsed = default
    elif isinstance(value, bool) or not isinstance(value, int | str):
        raise ValueError(f"{name} must be an integer")
    else:
        try:
            parsed = int(value)
        except ValueError as exc:
            raise ValueError(f"{name} must be an integer") from exc

    if minimum is not None and parsed < minimum:
        raise ValueError(_bounds_message(name, minimum, maximum))
    if maximum is not None and parsed > maximum:
        raise ValueError(_bounds_message(name, minimum, maximum))
    return parsed


def get_string(
    args: Mapping[str, object],
    name: str,
    *,
    required: bool = False,
    max_chars: int | None = None,
    message: str | None = None,
) -> str:
    raw = args.get(name)
    if raw is None:
        value = ""
    elif not isinstance(raw, str):
        raise ValueError(f"{name} must be a string")
    else:
        value = raw.strip()
    if required and not value:
        raise ValueError(message or f"{name} is required")
    if max_chars is not None and len(value) > max_chars:
        raise ValueError(f"{name} must be {max_chars} characters or fewer")
    return value


def _bounds_message(name: str, minimum: int | None, maximum: int | None) -> str:
    if minimum is not None and maximum is not None:
        return f"{name} must be between {minimum} and {maximum}"
    if minimum is not None:
        return f"{name} must be at least {minimum}"
    return f"{name} must be at most {maximum}"
