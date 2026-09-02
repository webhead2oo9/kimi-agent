from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import cast

import tools.user_memory as user_memory
from memory.client import MemoryClient
from tools.registry import UNTRUSTED_CONTEXT_NOTE, MessageContext, ToolRegistry
from trust.tiers import TrustTier


class EnabledPreferenceStore:
    async def is_memory_enabled(self, user_id: str) -> bool:
        return True


class DisabledPreferenceStore:
    async def is_memory_enabled(self, user_id: str) -> bool:
        return False


class RecordingMemory:
    def __init__(self, memories=None, answer: str = "") -> None:
        self.memories = memories or []
        self.answer = answer
        self.calls: list[dict] = []
        self.reflect_calls: list[dict] = []

    async def recall(self, **kwargs):
        self.calls.append(kwargs)
        return self.memories

    async def reflect(self, **kwargs):
        self.reflect_calls.append(kwargs)
        return self.answer


def _ctx() -> MessageContext:
    return MessageContext(
        user_id="123",
        user_name="webhead",
        guild_id="999",
        channel_id="111",
        thread_id=None,
        trust_tier=TrustTier.MEMBER,
    )


def _registry(memory: RecordingMemory) -> ToolRegistry:
    registry = ToolRegistry()
    user_memory.init_user_memory_tools(registry, cast(MemoryClient, memory))
    return registry


def test_init_user_memory_tools_registers_into_explicit_registry() -> None:
    registry = ToolRegistry()
    memory = RecordingMemory()

    user_memory.init_user_memory_tools(
        registry,
        cast(MemoryClient, memory),
        recall_types=["world"],
    )

    assert registry.has_tool("recall_user")
    assert registry.has_tool("reflect_user")
    entries = {entry.name: entry for entry in registry.get_all_tools()}
    assert entries["recall_user"].untrusted is True
    assert entries["reflect_user"].untrusted is True


def test_recall_user_returns_saved_memory_without_source_metadata(monkeypatch) -> None:
    memory = RecordingMemory(
        [
            SimpleNamespace(
                text="webhead uses a Quest 3.",
                type="world",
                document_id="user-memory:123:111:abcd",
                metadata={
                    "source_kind": "discord_user_memory",
                    "source_version": "1",
                    "subject_user_id": "123",
                    "conversation_id": "42",
                    "anchor_message_id": "7",
                    "anchor_discord_message_id": "111",
                    "channel_id": "222",
                    "channel_name": "vr-help",
                    "anchor_source_created_at": "1760000000.123",
                },
            )
        ]
    )
    monkeypatch.setattr(user_memory, "_memory", memory)
    monkeypatch.setattr(user_memory, "_preference_store", EnabledPreferenceStore())

    raw = asyncio.run(_registry(memory).dispatch("recall_user", {"query": "headset"}, _ctx()))

    payload = json.loads(raw)
    assert payload["context_is_untrusted"] is True
    assert payload["note"] == UNTRUSTED_CONTEXT_NOTE
    assert payload["results"] == [
        {
            "text": "webhead uses a Quest 3.",
            "type": "world",
        }
    ]


def test_reflect_user_returns_answer_for_memory_enabled_user(monkeypatch) -> None:
    memory = RecordingMemory(answer="webhead favors a tethered PCVR setup.")
    monkeypatch.setattr(user_memory, "_memory", memory)
    monkeypatch.setattr(user_memory, "_preference_store", EnabledPreferenceStore())

    raw = asyncio.run(_registry(memory).dispatch("reflect_user", {"query": "my vr setup"}, _ctx()))

    payload = json.loads(raw)
    assert payload == {
        "context_is_untrusted": True,
        "note": UNTRUSTED_CONTEXT_NOTE,
        "answer": "webhead favors a tethered PCVR setup.",
    }
    assert len(memory.reflect_calls) == 1
    assert memory.reflect_calls[0]["bank_id"] == "user:123"
    assert memory.reflect_calls[0]["budget"] == "mid"


def test_reflect_user_blocks_opted_out_user(monkeypatch) -> None:
    memory = RecordingMemory(answer="should not be returned")
    monkeypatch.setattr(user_memory, "_memory", memory)
    monkeypatch.setattr(user_memory, "_preference_store", DisabledPreferenceStore())

    raw = asyncio.run(_registry(memory).dispatch("reflect_user", {"query": "my vr setup"}, _ctx()))

    payload = json.loads(raw)
    assert payload == {
        "result": "Memory is disabled for this user.",
        "context_is_untrusted": True,
        "note": UNTRUSTED_CONTEXT_NOTE,
    }
    assert memory.reflect_calls == []


def test_reflect_user_requires_query(monkeypatch) -> None:
    memory = RecordingMemory(answer="unused")
    monkeypatch.setattr(user_memory, "_memory", memory)
    monkeypatch.setattr(user_memory, "_preference_store", EnabledPreferenceStore())

    raw = asyncio.run(_registry(memory).dispatch("reflect_user", {"query": ""}, _ctx()))

    payload = json.loads(raw)
    assert payload == {"error": "Query is required"}
    assert memory.reflect_calls == []


def test_reflect_user_handles_empty_answer(monkeypatch) -> None:
    memory = RecordingMemory(answer="")
    monkeypatch.setattr(user_memory, "_memory", memory)
    monkeypatch.setattr(user_memory, "_preference_store", EnabledPreferenceStore())

    raw = asyncio.run(_registry(memory).dispatch("reflect_user", {"query": "my vr setup"}, _ctx()))

    payload = json.loads(raw)
    assert payload == {
        "result": "No memories to reason about for this user.",
        "context_is_untrusted": True,
        "note": UNTRUSTED_CONTEXT_NOTE,
    }
    assert len(memory.reflect_calls) == 1
