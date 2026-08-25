from __future__ import annotations

import re
from datetime import datetime

from utils import template

_WEEKDAYS = {
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
}
_DATE_RE = re.compile(r"^[A-Z][a-z]+ [A-Z][a-z]+ \d{1,2}(st|nd|rd|th), \d{4}$")


def test_date_format_includes_weekday() -> None:
    resolved = template.resolve("Today is <date>.")
    assert resolved.startswith("Today is ")
    assert resolved.endswith(".")
    body = resolved[len("Today is ") : -1]
    assert _DATE_RE.match(body), body
    assert body.split(" ", 1)[0] in _WEEKDAYS


def test_date_uses_local_not_utc() -> None:
    # Local wall clock, not UTC, or the date rolls a day early each evening.
    local = datetime.now().astimezone()
    resolved = template.resolve("<date>")
    assert str(local.year) in resolved
    assert local.strftime("%A") in resolved


def test_non_placeholder_text_unchanged() -> None:
    assert template.resolve("no tokens here <unknown>") == "no tokens here <unknown>"
