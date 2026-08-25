"""Shared defaults and normalization for the bot's identity."""

from __future__ import annotations

import re
import unicodedata

DEFAULT_BOT_NAME = "Kimi"
DEFAULT_BOT_SLUG = "kimi"
PROVIDER_IDENTITY_MAX_LENGTH = 64


def provider_identity(value: object) -> str:
    """Return a short ASCII identity safe for HTTP provider headers."""

    ascii_name = unicodedata.normalize("NFKD", str(value)).encode("ascii", errors="ignore").decode()
    ascii_name = " ".join(ascii_name.split())
    safe_name = re.sub(r"[^A-Za-z0-9 ._()+-]+", "-", ascii_name)
    safe_name = " ".join(safe_name.split()).strip(" ._+-")
    safe_name = safe_name[:PROVIDER_IDENTITY_MAX_LENGTH].rstrip(" ._+-")
    return safe_name or DEFAULT_BOT_NAME
