from __future__ import annotations

import json
from typing import cast

import pytest

import tools.community as community
from memory.client import MemoryClient, RecalledMemory
from tools.registry import UNTRUSTED_CONTEXT_NOTE, MessageContext, ToolRegistry
from trust.tiers import TrustTier


class _Client:
    async def recall(self, **kwargs):
        return [
            RecalledMemory(
                text="Quest 3 has stick drift.",
                type="memory",
                tags=["scope:public", "source:learned", "verified:external", "confidence:high"],
            )
        ]


@pytest.fixture(autouse=True)
def _stub_ensure_bank(monkeypatch):
    async def _ensure(client, guild_id):
        return "community" if guild_id else None

    monkeypatch.setattr(community, "ensure_community_bank", _ensure)


def _ctx():
    return MessageContext(
        user_id="1",
        user_name="U",
        guild_id="g1",
        channel_id="c",
        thread_id=None,
        trust_tier=TrustTier.MEMBER,
    )


def _registry(client: object) -> ToolRegistry:
    registry = ToolRegistry()
    community.init_community_tools(registry, cast(MemoryClient, client))
    return registry


def test_community_read_tools_are_registered_as_untrusted() -> None:
    registry = _registry(_Client())
    entries = {entry.name: entry for entry in registry.get_all_tools()}

    assert entries["recall_community"].untrusted is True
    assert entries["reflect_community"].untrusted is True
    assert entries["teach"].untrusted is False


@pytest.mark.asyncio
async def test_recall_includes_confidence_without_stale_citation() -> None:
    registry = _registry(_Client())
    out = json.loads(await registry.dispatch("recall_community", {"query": "drift"}, _ctx()))
    assert out["context_is_untrusted"] is True
    assert out["note"] == UNTRUSTED_CONTEXT_NOTE
    item = out["results"][0]
    assert item["confidence"] == "high"
    assert "citation" not in item


@pytest.mark.asyncio
async def test_recall_staff_taught_omits_stale_citation() -> None:
    class _C:
        async def recall(self, **kwargs):
            return [
                RecalledMemory(
                    text="t",
                    type="memory",
                    tags=["scope:public", "source:taught", "confidence:high", "topic:how-to"],
                )
            ]

    payload = json.loads(await _registry(_C()).dispatch("recall_community", {"query": "x"}, _ctx()))
    assert payload["context_is_untrusted"] is True
    item = payload["results"][0]
    assert item["confidence"] == "high"


@pytest.mark.asyncio
async def test_reflect_community_returns_untrusted_answer() -> None:
    class _C:
        async def reflect(self, **kwargs):
            return "Synthesized community knowledge."

    payload = json.loads(
        await _registry(_C()).dispatch("reflect_community", {"query": "x"}, _ctx())
    )
    assert payload == {
        "context_is_untrusted": True,
        "note": UNTRUSTED_CONTEXT_NOTE,
        "answer": "Synthesized community knowledge.",
    }


@pytest.mark.asyncio
async def test_recall_no_confidence_tag_yields_empty() -> None:
    class _C:
        async def recall(self, **kwargs):
            return [RecalledMemory(text="t", type="memory", tags=["scope:public"])]

    item = json.loads(await _registry(_C()).dispatch("recall_community", {"query": "x"}, _ctx()))[
        "results"
    ][0]
    assert item["confidence"] == ""
