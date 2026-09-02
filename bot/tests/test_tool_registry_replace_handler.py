"""ToolRegistry.replace_handler keeps every entry field, including runtime gates."""

from __future__ import annotations

from typing import Any

import pytest

from tools.registry import ToolRegistry
from trust.tiers import TrustTier


async def _a(_args: dict[str, Any], _ctx: Any) -> str:
    return "a"


async def _b(_args: dict[str, Any], _ctx: Any) -> str:
    return "b"


def test_replace_handler_preserves_gates() -> None:
    registry = ToolRegistry()
    registry.register(
        "t",
        "desc",
        {"type": "object"},
        _a,
        min_tier=TrustTier.STAFF,
        searchable=True,
        owner_only=True,
        guild_ids=frozenset({"7"}),
        available=lambda guild: guild == "7",
        untrusted=True,
    )

    registry.replace_handler("t", _b)

    (entry,) = [e for e in registry.get_all_tools() if e.name == "t"]
    assert entry.handler is _b
    assert entry.min_tier is TrustTier.STAFF
    assert entry.searchable and entry.owner_only
    assert entry.guild_ids == frozenset({"7"})
    assert entry.available is not None and entry.available("7") and not entry.available("8")
    assert entry.untrusted is True


def test_replace_handler_requires_a_registered_tool() -> None:
    with pytest.raises(KeyError):
        ToolRegistry().replace_handler("missing", _b)
