from __future__ import annotations

from typing import Any

MISSING = object()


def usage_field(usage: Any, name: str, default: Any = MISSING) -> Any:
    if isinstance(usage, dict):
        return usage.get(name, default)
    return getattr(usage, name, default)


def usage_detail_dict(details: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    if details is MISSING or details is None:
        return {}
    if isinstance(details, dict):
        raw = details
    elif hasattr(details, "model_dump"):
        dumped = details.model_dump(mode="json", exclude_none=True)
        raw = dumped if isinstance(dumped, dict) else {}
    else:
        raw = {
            field: value
            for field in fields
            if (value := usage_field(details, field)) is not MISSING
        }
    return {field: raw[field] for field in fields if field in raw}


def anthropic_usage_dict(usage: Any) -> dict[str, Any]:
    """Normalize an Anthropic usage payload, SDK object or raw JSON alike.

    `usage_field` reads dicts and objects the same way, so both Anthropic
    providers share this normalizer: `output_tokens_details` is carried through
    and the core token counts default to `0` rather than `None`, so a turn bills
    identically whichever backend served it.
    """

    if not usage:
        return {}
    data: dict[str, Any] = {
        "input_tokens": usage_field(usage, "input_tokens", 0),
        "output_tokens": usage_field(usage, "output_tokens", 0),
    }
    for field in ("cache_read_input_tokens", "cache_creation_input_tokens"):
        value = usage_field(usage, field)
        if value is not MISSING:
            data[field] = value
    output_details = usage_detail_dict(
        usage_field(usage, "output_tokens_details"),
        ("thinking_tokens",),
    )
    if output_details:
        data["output_tokens_details"] = output_details
    return data
