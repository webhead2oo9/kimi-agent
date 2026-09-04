from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from providers.types import ConversationMessage
from storage.conversations import (
    CHANNEL_SHARED,
    ConversationAccessScope,
    ConversationStore,
)
from tools.registry import TurnOutbox


@dataclass
class ConversationContext:
    key: str
    db_conversation_id: int = 0
    messages: list[ConversationMessage] = field(default_factory=list)
    max_history: int = 20
    user_id: str = ""
    user_name: str = ""
    channel_name: str = ""
    activated_tools: set[str] = field(default_factory=set)
    # Tools the model loaded via browse_tools this turn. Tracked separately from
    # activated_tools so an explicit load of a currently channel-pinned tool
    # still reaches conversation_activated_tools (the pin-merged activation
    # baseline would otherwise mask it and unpinning would deactivate it).
    explicitly_loaded_tools: set[str] = field(default_factory=set)
    # Operator denylist (guild ∪ channel blocked_tools) for this turn. Set in
    # prepare_turn from the on-disk fragments, never persisted: the fragments
    # stay the source of truth and unblocking takes effect next turn. The
    # registry hides these from the model's tool list/catalog and masks them at
    # dispatch (see tools/registry.py).
    blocked_tools: frozenset[str] = frozenset()
    # Fully resolved per-tool operator config for this turn, keyed by tool name.
    # Set in prepare_turn from config/tools/<name>.md, copied onto
    # MessageContext for handlers, and never persisted: the fragments stay the
    # source of truth and an edit takes effect next turn (config/fragments/tool_config.py).
    tool_configs: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    # True for durable background runs (coding tasks) whose single MessageContext
    # spans many ReAct iterations and minutes of wall time. Tools that hold
    # turn-scoped resources (the browser's netns lease) release per call instead.
    background_task: bool = False
    pending_outbox: TurnOutbox = field(default_factory=TurnOutbox)
    participants: dict[str, str] = field(default_factory=dict)

    def add_participant(self, user_id: str, user_name: str) -> None:
        self.participants[user_id] = user_name

    def add_messages(self, messages: Sequence[ConversationMessage]) -> None:
        self.messages.extend(messages)
        self._trim()

    def get_history(self) -> list[ConversationMessage]:
        return list(self.messages)

    def _trim(self) -> None:
        if len(self.messages) > self.max_history:
            self.messages = self.messages[-self.max_history :]
        while self.messages and self.messages[0].role == "tool":
            self.messages.pop(0)


class ContextManager:
    """Builds a fresh context per turn from the durable transcript.

    Response turns do not share an in-process cache. Recent user/assistant
    transcript rows are loaded from SQLite, while explicit extra context (for
    example, tool-read channel context or recalled memory) is injected elsewhere.
    The persisted transcript is written separately by the caller via
    ConversationStore.save_channel_messages.
    """

    def __init__(self, store: ConversationStore) -> None:
        self._store = store

    async def build_turn_context(
        self,
        key: str,
        channel_name: str = "",
        before_discord_message_id: str | None = None,
        *,
        root_discord_message_id: str | None = None,
        owner_user_id: str | None = None,
        access_scope: ConversationAccessScope = CHANNEL_SHARED,
    ) -> ConversationContext:
        creation_metadata: dict[str, str] = {}
        if root_discord_message_id is not None:
            creation_metadata["root_discord_message_id"] = root_discord_message_id
        if owner_user_id is not None:
            creation_metadata["owner_user_id"] = owner_user_id
        creation_metadata["access_scope"] = access_scope
        conv_id = await self._store.get_or_create(
            key,
            channel_name,
            **creation_metadata,
        )
        history = await self._store.load_recent_conversation_messages(
            conv_id,
            limit=ConversationContext.max_history,
            before_discord_message_id=before_discord_message_id,
        )
        activated_tools = await self._store.load_activated_tools(conv_id)
        return ConversationContext(
            key=key,
            db_conversation_id=conv_id,
            messages=history,
            activated_tools=activated_tools,
        )

    async def add_activated_tools(
        self,
        context: ConversationContext,
        names: set[str],
    ) -> None:
        await self._store.add_activated_tools(context.db_conversation_id, names)

    async def has_loaded_message(
        self,
        context: ConversationContext,
        discord_message_id: str,
    ) -> bool:
        return bool(discord_message_id) and any(
            message.source_discord_message_id == discord_message_id for message in context.messages
        )
