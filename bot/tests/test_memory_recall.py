import asyncio
from typing import Any

from agent.context import ConversationContext
from memory.client import RecalledMemory
from memory.recall import (
    DEFAULT_USER_RECALL_MAX_TOKENS,
    DEFAULT_USER_RECALL_TYPES,
    compose_user_recall_query,
    format_recalled_memories,
    recall_current_user_context,
)
from providers.types import ContentPart, ConversationMessage


class RecordingMemoryClient:
    def __init__(
        self,
        memories: list[RecalledMemory] | None = None,
        *,
        raise_on_recall: bool = False,
    ) -> None:
        self.memories = memories or []
        self.raise_on_recall = raise_on_recall
        self.recall_calls: list[dict[str, Any]] = []

    async def recall(
        self,
        *,
        bank_id: str,
        query: str,
        budget: str = "mid",
        max_tokens: int = 4096,
        types: list[str] | None = None,
        tags: list[str] | None = None,
        tags_match: str = "any",
    ) -> list[RecalledMemory]:
        self.recall_calls.append(
            {
                "bank_id": bank_id,
                "query": query,
                "budget": budget,
                "max_tokens": max_tokens,
                "types": types,
                "tags": tags,
                "tags_match": tags_match,
            }
        )
        if self.raise_on_recall:
            raise RuntimeError("recall failed")
        return self.memories


class MemoryPreferences:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self.calls: list[str] = []

    async def is_memory_enabled(self, user_id: str) -> bool:
        self.calls.append(user_id)
        return self.enabled


def test_recall_types_default_agrees_between_settings_and_the_code_fallback() -> None:
    """The same default lives in two places and they must not drift.

    Auto-recall defaults to the consolidated `observation` layer only; raw
    `world`/`experience` are opt-in (see docs/memory.md). `MEMORY_RECALL_TYPES`
    carries that default for a configured deployment, while
    `DEFAULT_USER_RECALL_TYPES` is the fallback used when the setting resolves
    empty (`tools/user_memory.py` and `memory/recall.py` both apply it as
    ``recall_types or DEFAULT_USER_RECALL_TYPES``). If the two disagree, which
    layer gets recalled silently depends on whether the operator set the value.
    """
    from config.settings import Settings

    settings_default = Settings(_env_file=None).user_memory_recall_types  # type: ignore[call-arg]

    assert DEFAULT_USER_RECALL_TYPES == ["observation"]
    assert settings_default == DEFAULT_USER_RECALL_TYPES


def test_compose_user_recall_query_includes_recent_context() -> None:
    history = [
        ConversationMessage(
            role="user",
            content=[ContentPart.from_text("I switched to a Quest 3 last week.")],
        ),
        ConversationMessage(
            role="assistant",
            content=[ContentPart.from_text("Got it.")],
        ),
    ]

    query = compose_user_recall_query(
        "What headset settings should I use?",
        history,
        context_turns=2,
        max_chars=500,
    )

    assert "Prior context:" in query
    assert "user: I switched to a Quest 3 last week." in query
    assert "Current request:" in query
    assert "What headset settings should I use?" in query


def test_recall_current_user_context_uses_current_user_bank_and_safe_options() -> None:
    ctx = ConversationContext(
        key="guild:channel:main",
        messages=[
            ConversationMessage(
                role="user",
                content=[ContentPart.from_text("I use a Quest 3 over Air Link.")],
            )
        ],
    )
    memory = RecordingMemoryClient(
        [RecalledMemory(text="webhead uses a Quest 3 over Air Link.", type="world")]
    )
    preferences = MemoryPreferences(enabled=True)

    recalled = asyncio.run(
        recall_current_user_context(
            memory_client=memory,
            preference_store=preferences,
            user_id="123",
            user_message="What bitrate should I use?",
            context=ctx,
        )
    )

    assert "webhead uses a Quest 3 over Air Link. [world]" in recalled
    assert preferences.calls == ["123"]
    assert memory.recall_calls == [
        {
            "bank_id": "user:123",
            "query": (
                "Prior context:\n"
                "user: I use a Quest 3 over Air Link.\n\n"
                "Current request:\n"
                "What bitrate should I use?"
            ),
            "budget": "mid",
            "max_tokens": DEFAULT_USER_RECALL_MAX_TOKENS,
            "types": DEFAULT_USER_RECALL_TYPES,
            "tags": ["scope:global"],
            "tags_match": "any",
        }
    ]


def test_recall_current_user_context_scopes_tags_to_guild() -> None:
    memory = RecordingMemoryClient(
        [RecalledMemory(text="webhead uses a Quest 3.", type="observation")]
    )

    asyncio.run(
        recall_current_user_context(
            memory_client=memory,
            preference_store=MemoryPreferences(enabled=True),
            user_id="123",
            user_message="What bitrate?",
            context=ConversationContext(key="k"),
            guild_id="777",
        )
    )

    # Global facts plus this guild's scoped memory; other guilds are excluded.
    assert memory.recall_calls[0]["tags"] == ["scope:global", "guild:777"]
    assert memory.recall_calls[0]["tags_match"] == "any"


def test_recall_current_user_context_skips_when_user_opted_out() -> None:
    memory = RecordingMemoryClient([RecalledMemory(text="webhead uses a Quest 3.", type="world")])

    recalled = asyncio.run(
        recall_current_user_context(
            memory_client=memory,
            preference_store=MemoryPreferences(enabled=False),
            user_id="123",
            user_message="What headset?",
            context=ConversationContext(key="k"),
        )
    )

    assert recalled == ""
    assert memory.recall_calls == []


def test_recall_current_user_context_degrades_to_empty_on_recall_failure() -> None:
    recalled = asyncio.run(
        recall_current_user_context(
            memory_client=RecordingMemoryClient(raise_on_recall=True),
            preference_store=MemoryPreferences(enabled=True),
            user_id="123",
            user_message="What headset?",
            context=ConversationContext(key="k"),
        )
    )

    assert recalled == ""


def test_format_recalled_memories_returns_empty_for_no_results() -> None:
    assert format_recalled_memories([]) == ""


def test_format_recalled_memories_dedupes_same_text_across_types() -> None:
    formatted = format_recalled_memories(
        [
            RecalledMemory(text="webhead uses Exa.ai as their search provider.", type="world"),
            RecalledMemory(
                text="webhead uses Exa.ai as their search provider.", type="observation"
            ),
            RecalledMemory(text="webhead prefers concise answers.", type="experience"),
        ]
    )

    assert formatted.splitlines() == [
        "- webhead uses Exa.ai as their search provider. [world]",
        "- webhead prefers concise answers. [experience]",
    ]
