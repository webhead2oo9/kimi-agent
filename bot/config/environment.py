"""Shared dotenv selection for core and operator plugins."""

from __future__ import annotations

import os
from pathlib import Path


def selected_env_file() -> str:
    """Return the configured dotenv path, failing loudly when it is missing."""
    name = os.environ.get("ENV_FILE", "").strip()
    if not name:
        return ".env"
    if not Path(name).is_file():
        raise RuntimeError(
            f"ENV_FILE points at {name!r}, which does not exist. Create it (start "
            "from .env.example) or unset ENV_FILE to use the default .env."
        )
    return name
