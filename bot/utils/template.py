"""Lightweight template variable resolution for prompt and config text."""

from __future__ import annotations

from datetime import datetime


def resolve(text: str) -> str:
    """Replace template placeholders with live values.

    Supported placeholders:
        <date>: e.g. "Monday June 22nd, 2026"

    Uses the host's local wall clock, not UTC: this bot serves a Pacific-time
    community, and a UTC date rolls over to "tomorrow" mid-evening locally.
    """
    now = datetime.now().astimezone()
    day = now.day
    suffix = "th" if 11 <= day <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")

    replacements = {
        "<date>": now.strftime(f"%A %B {day}{suffix}, %Y"),
    }

    for token, value in replacements.items():
        text = text.replace(token, value)
    return text
