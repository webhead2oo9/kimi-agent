from __future__ import annotations

import pytest

from app import tool_surfaces
from app.tool_surfaces import (
    TOOL_SURFACES,
    declare_surface_tools,
    restore_surface_tools,
    snapshot_surface_tools,
    surface_tools,
)


@pytest.fixture(autouse=True)
def _isolated_table(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tool_surfaces, "_SURFACE_TOOLS", {})


def test_declarations_accumulate_and_dedupe() -> None:
    declare_surface_tools("eval_stub", ["alpha"])
    declare_surface_tools("eval_stub", ["alpha", "beta"])

    assert surface_tools("eval_stub") == frozenset({"alpha", "beta"})
    # Surfaces are independent.
    assert surface_tools("eval_record") == frozenset()


def test_unknown_surface_name_raises() -> None:
    # A typo must be loud: app/plugins.py rolls the plugin back, so the tool is
    # absent everywhere rather than live on a surface it meant to opt out of.
    with pytest.raises(ValueError, match="Unknown tool surface"):
        declare_surface_tools("eval-stub", ["alpha"])

    assert surface_tools("eval_record") == frozenset()


def test_every_surface_name_is_declarable() -> None:
    for surface in TOOL_SURFACES:
        declare_surface_tools(surface, ["alpha"])
        assert "alpha" in surface_tools(surface)


def test_snapshot_and_restore_round_trip() -> None:
    declare_surface_tools("eval_stub", ["kept"])
    snapshot = snapshot_surface_tools()

    declare_surface_tools("eval_stub", ["discarded"])
    declare_surface_tools("eval_record", ["also_discarded"])
    restore_surface_tools(snapshot)

    assert surface_tools("eval_stub") == frozenset({"kept"})
    assert surface_tools("eval_record") == frozenset()


def test_snapshot_is_not_a_live_view() -> None:
    declare_surface_tools("eval_stub", ["kept"])
    snapshot = snapshot_surface_tools()
    declare_surface_tools("eval_stub", ["later"])

    assert snapshot["eval_stub"] == frozenset({"kept"})
