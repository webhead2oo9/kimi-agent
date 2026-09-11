"""Tool policy in the dashboard's selected prompt frontmatter."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import paths
from config.fragments._fragment_cache import LastKnownGoodCache
from config.fragments.prompt import resolve_template_path
from utils.frontmatter import split_frontmatter_strict

log = logging.getLogger(__name__)
_TOOL_NAME = re.compile(r"[a-zA-Z0-9_-]{1,64}")


@dataclass(frozen=True, slots=True)
class DashboardToolPolicy:
    pinned_tools: frozenset[str] = frozenset()
    blocked_tools: frozenset[str] = frozenset()


_cache: LastKnownGoodCache[DashboardToolPolicy] = LastKnownGoodCache(max_entries=None)


def _names(meta: dict[str, Any], field: str) -> frozenset[str]:
    raw = meta.get(field, [])
    if not isinstance(raw, list) or len(raw) > 64:
        raise ValueError(f"{field} must be a list of at most 64 tool names")
    if any(not isinstance(name, str) or not _TOOL_NAME.fullmatch(name) for name in raw):
        raise ValueError(f"Invalid tool name in {field}")
    return frozenset(raw)


def load_dashboard_tool_policy(
    guild_id: str, *, config_dir: Path | None = None
) -> DashboardToolPolicy:
    """Read fresh per turn; an invalid reload cannot silently remove a deny."""
    fragment = resolve_template_path(
        config_dir or paths.default_config_dir(),
        channel_id="",
        guild_id=guild_id,
        command_template="dashboard",
    )
    key = _cache.key(fragment)
    try:
        meta, _ = split_frontmatter_strict(fragment.read_text(encoding="utf-8"))
        policy = DashboardToolPolicy(_names(meta, "pinned_tools"), _names(meta, "blocked_tools"))
    except (OSError, UnicodeError, ValueError) as exc:
        previous = _cache.last_good(key)
        if previous is None:
            raise RuntimeError(f"Could not load dashboard tool policy {fragment}") from exc
        log.error("Could not reload dashboard tool policy %s; retaining last-known-good", fragment)
        return previous
    _cache.remember(key, policy)
    return policy
