"""Tolerant extraction of one JSON object from a model's text response.

Models asked for "JSON only" still wrap it in a ``` fence or add a sentence of
prose around it. Every caller that asks a model for a JSON payload needs the
same forgiveness, so it lives here rather than being reinvented per feature.

Returns ``None`` rather than raising: a malformed payload is an ordinary
outcome for a model call, and each caller has its own idea of what to do about
it (refuse, retry, skip the scan).
"""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE_OPEN = re.compile(r"^```(?:json)?\s*", re.IGNORECASE)
_FENCE_CLOSE = re.compile(r"\s*```$")


def extract_json_object(text: str) -> dict[str, Any] | None:
    """Parse the JSON object in ``text``, tolerating fences and surrounding prose."""
    cleaned = text.strip()
    if not cleaned:
        return None
    if cleaned.startswith("```"):
        cleaned = _FENCE_OPEN.sub("", cleaned)
        cleaned = _FENCE_CLOSE.sub("", cleaned)

    payload = _loads_object(cleaned)
    if payload is not None:
        return payload

    # Fall back to the outermost {...} span, which survives a model that framed
    # its answer with an explanatory sentence.
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        return None
    return _loads_object(cleaned[start : end + 1])


def _loads_object(text: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None
