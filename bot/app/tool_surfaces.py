"""Per-surface tool declarations contributed by plugins.

The offline eval harness neutralizes tools that would write to production or
burn network calls on repeat runs. Core owns the base membership for each
surface as a literal next to its consumer (``SAFE_STUB_TOOLS`` in
evals/stub_gateway.py, ``CASSETTE_RECORDED_TOOLS`` in evals/cassette.py).

A plugin's tools are invisible to those literals (public core must not name a
plugin's tools), so a plugin declares its own membership here through
``PluginContext.declare_surface_tools``. The merge mirrors
``agent/activity.py:register_tool_labels``: a process-global table the
composition root fills at startup, before any eval run. Declarations only ever
ADD to a surface, so a plugin can restrict its own tools and can never widen
another tool's reach.

An unknown surface name raises: ``app/plugins.py`` catches it, rolls the
plugin's registrations back, and skips it; a typo'd surface therefore removes
the tool entirely rather than leaving it live on a surface it meant to avoid.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

# Surface vocabulary. Each entry is consumed by exactly one site:
#   eval_stub    -> evals replace the handler with a canned ack; use for tools
#                   that WRITE to a shared production surface
#   eval_record  -> evals record/replay these through cassettes; use for
#                   network-backed read-only tools, so repeat runs are cheap
#                   and deterministic
TOOL_SURFACES = frozenset({"eval_stub", "eval_record"})

_SURFACE_TOOLS: dict[str, set[str]] = {}


def declare_surface_tools(surface: str, names: Iterable[str]) -> None:
    """Add ``names`` to ``surface``. Idempotent; unknown surface raises."""
    if surface not in TOOL_SURFACES:
        raise ValueError(
            f"Unknown tool surface {surface!r}; expected one of {sorted(TOOL_SURFACES)}"
        )
    _SURFACE_TOOLS.setdefault(surface, set()).update(names)


def surface_tools(surface: str) -> frozenset[str]:
    """Tool names plugins have declared for ``surface`` (empty if none)."""
    return frozenset(_SURFACE_TOOLS.get(surface, ()))


def reset_surface_tools() -> None:
    """Drop every declaration. For tests and the eval harness's fresh builds."""
    _SURFACE_TOOLS.clear()


def snapshot_surface_tools() -> dict[str, frozenset[str]]:
    """Copy the table so a failed plugin's declarations can be rolled back."""
    return {surface: frozenset(names) for surface, names in _SURFACE_TOOLS.items()}


def restore_surface_tools(snapshot: Mapping[str, frozenset[str]]) -> None:
    _SURFACE_TOOLS.clear()
    _SURFACE_TOOLS.update({surface: set(names) for surface, names in snapshot.items()})
