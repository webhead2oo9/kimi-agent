"""Per-tool operator configuration, read fresh each turn.

A tool declares a typed spec at registration (``tools/config_spec.py``); the
operator's values for it live in one frontmatter-only fragment per tool::

    <config_dir>/tools/discord_text_search.md

    ---
    max_results: 10
    ---

This module reads those fragments on every turn (the same pattern as the
``blocked_tools`` denylist in ``config/fragments/tool_policy.py``, whose fresh-read
plus last-known-good cache this deliberately mirrors), resolves them over the spec's
defaults, and hands the result to ``prepare_turn``, which stashes it on
``MessageContext.tool_configs``. An operator edit therefore takes effect on the
next message with no restart, which is the whole point: the ``.env`` capability
gates are boot-time only.

**Fail direction: fully open.** Where the denylist raises rather than risk
silently *granting* a tool, this returns defaults rather than risk taking a tool
down. Tool config only tunes behavior an operator opted into, and the defaults
are the shipped behavior, so:

* an absent fragment is defaults (the normal case, since most tools have no file),
* an unreadable or malformed one uses that path's last-known-good value, or
  defaults with a logged error if there is none,
* an unknown key inside a valid fragment is warned about and ignored, and one
  uncoercible value falls back to that field's default without touching the
  rest.

Nothing here raises: operator fragments are hand-edited files, so this loader
tolerates a partial mistake and keeps the last valid state rather than breaking
turns over one bad key.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


from config.fragments._fragment_cache import LastKnownGoodCache
from config import paths
from utils.frontmatter import split_frontmatter_strict
from tools.config_spec import ToolConfigField, default_config, resolve_config

log = logging.getLogger(__name__)

TOOL_CONFIG_SUBDIR = "tools"

# Registry tool names; also the fragment's filename, so it is a containment gate.
_TOOL_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

_cache: LastKnownGoodCache[dict[str, Any]] = LastKnownGoodCache()

_cache_key = LastKnownGoodCache.key
_forget = _cache.forget


def _remember(key: Path, config: dict[str, Any]) -> None:
    _cache.remember(key, dict(config))


def _last_good(key: Path) -> dict[str, Any] | None:
    config = _cache.last_good(key)
    # Copied out: the caller re-resolves the retained values against a spec that
    # may have changed, and must not mutate what the cache still holds.
    return None if config is None else dict(config)


def _parse_overrides(text: str) -> dict[str, Any]:
    """Parse one fragment's frontmatter, rejecting anything ambiguous.

    A body-only or empty document is a valid "no overrides" fragment; the
    loader never writes a body, but a hand-edited note above the
    frontmatter must not cost the operator their settings.
    """
    meta, _ = split_frontmatter_strict(text)
    return {str(key): value for key, value in meta.items()}


def tool_config_dir(config_dir: Path | None = None) -> Path:
    """Where the per-tool fragments live."""
    return (config_dir or paths.default_config_dir()) / TOOL_CONFIG_SUBDIR


def tool_config_path(name: str, config_dir: Path | None = None) -> Path:
    """Where one tool's fragment lives. The name is validated by the caller."""
    return tool_config_dir(config_dir) / f"{name}.md"


def load_tool_config(
    name: str,
    spec: Sequence[ToolConfigField],
    *,
    config_dir: Path | None = None,
) -> dict[str, Any]:
    """Resolve one tool's operator config over its defaults. Never raises."""
    if not _TOOL_NAME_RE.fullmatch(name):
        # Not reachable from the registry (its names are already this shape);
        # this keeps a caller-supplied name from ever building a path.
        log.error("Refusing to read tool config for implausible tool name %r", name)
        return default_config(spec)

    fragment = tool_config_path(name, config_dir)
    key = _cache_key(fragment)
    try:
        text = fragment.read_text(encoding="utf-8")
    except FileNotFoundError:
        # An absent fragment is an explicit "defaults", and it clears any cached
        # value so deleting a file actually reverts the tool.
        _forget(key)
        return default_config(spec)
    except (OSError, UnicodeError) as exc:
        return _retain_or_default(name, fragment, key, spec, exc)

    try:
        overrides = _parse_overrides(text)
    except ValueError as exc:
        return _retain_or_default(name, fragment, key, spec, exc)

    def warn(message: str) -> None:
        # Each report already states its own outcome (ignored, defaulted, or
        # partly salvaged), so this must not append one of its own.
        log.warning("Tool config %s: %s", fragment, message)

    resolved = resolve_config(spec, overrides, on_issue=warn)
    _remember(key, resolved)
    return dict(resolved)


def _retain_or_default(
    name: str,
    fragment: Path,
    key: Path,
    spec: Sequence[ToolConfigField],
    error: BaseException,
) -> dict[str, Any]:
    last_good = _last_good(key)
    if last_good is not None:
        log.error(
            "Could not reload tool config %s (%s); retaining the last-known-good values",
            fragment,
            error,
        )
        # The spec can change across a reload, so the retained values are
        # re-resolved rather than trusted verbatim.
        return resolve_config(spec, last_good)
    log.error(
        "Could not load tool config %s (%s); %s falls back to its defaults",
        fragment,
        error,
        name,
    )
    return default_config(spec)


def load_tool_configs(
    specs: Mapping[str, Sequence[ToolConfigField]],
    *,
    config_dir: Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Resolve every spec'd tool's config as one per-turn snapshot.

    Loading eagerly (rather than lazily at dispatch) keeps a turn's view of
    operator config consistent across its whole ReAct loop and keeps filesystem
    I/O out of tool dispatch. The cost is a handful of small file reads per turn,
    the same class as the three denylist fragments already read beside it.
    """
    return {
        name: load_tool_config(name, spec, config_dir=config_dir) for name, spec in specs.items()
    }
