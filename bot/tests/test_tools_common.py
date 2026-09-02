from __future__ import annotations

import json

import pytest

from tools._common import (
    get_int,
    get_string,
    tool_error,
)


def test_tool_error_serializes_standard_error_payload() -> None:
    assert json.loads(tool_error("x")) == {"error": "x"}


def test_get_int_accepts_int_and_integer_string() -> None:
    assert get_int(3, name="limit", minimum=1, maximum=5) == 3
    assert get_int("3", name="limit", minimum=1, maximum=5) == 3


def test_get_int_rejects_bool_and_non_integer_strings() -> None:
    with pytest.raises(ValueError, match="limit must be an integer"):
        get_int(True, name="limit")
    with pytest.raises(ValueError, match="limit must be an integer"):
        get_int("three", name="limit")


def test_get_int_applies_defaults_and_bounds() -> None:
    assert get_int(None, name="limit", default=2, minimum=1, maximum=3) == 2
    with pytest.raises(ValueError, match="limit must be an integer"):
        get_int(None, name="limit")
    with pytest.raises(ValueError, match="limit must be between 1 and 3"):
        get_int(4, name="limit", minimum=1, maximum=3)


def test_get_string_required_empty_and_max_length() -> None:
    assert get_string({"query": "  hello  "}, "query", required=True) == "hello"
    assert get_string({}, "query") == ""
    with pytest.raises(ValueError, match="Query required"):
        get_string({"query": " "}, "query", required=True, message="Query required")
    with pytest.raises(ValueError, match="query must be 5 characters or fewer"):
        get_string({"query": "toolong"}, "query", required=True, max_chars=5)


@pytest.mark.parametrize("value", [None, ["query"], {"query": "value"}, 123])
def test_get_string_rejects_null_and_non_string_values(value: object) -> None:
    message = "query is required" if value is None else "query must be a string"
    with pytest.raises(ValueError, match=message):
        get_string({"query": value}, "query", required=True)
