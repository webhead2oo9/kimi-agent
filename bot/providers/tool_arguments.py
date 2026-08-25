from __future__ import annotations

import json
from typing import Any


def parse_tool_arguments(raw_args: Any) -> dict[str, Any]:
    if raw_args in (None, ""):
        return {}
    if isinstance(raw_args, dict):
        return raw_args

    raw_text = raw_args if isinstance(raw_args, str) else str(raw_args)
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        return {"_raw": raw_text}
    if not isinstance(parsed, dict):
        return {"_raw": raw_text}
    return parsed
