from __future__ import annotations

import logging
import re
from typing import Protocol

from memory.banks import user_bank_id
from memory.client import RecalledMemory
from providers.types import ContentPartType, ConversationMessage

log = logging.getLogger(__name__)

DEFAULT_USER_RECALL_TYPES = ["observation"]
DEFAULT_USER_RECALL_BUDGET = "mid"
DEFAULT_USER_RECALL_MAX_TOKENS = 2048
DEFAULT_USER_RECALL_CONTEXT_TURNS = 2
DEFAULT_USER_RECALL_MAX_QUERY_CHARS = 800

_MEMORY_TAG_RE = re.compile(
    r"<(?:hindsight_memories|relevant_memories)>.*?</(?:hindsight_memories|relevant_memories)>",
    re.DOTALL,
)


class UserRecallMemoryClient(Protocol):
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
    ) -> list[RecalledMemory]: ...


class UserMemoryPreferenceStore(Protocol):
    async def is_memory_enabled(self, user_id: str) -> bool: ...


class RecallConversationContext(Protocol):
    """The only thing recall needs from a conversation: its recent messages.

    A Protocol rather than importing `agent.context`, so this stays a leaf --
    `agent/turn.py` already imports `memory.mutations`, and reaching back up
    into `agent` from here closed that loop.
    """

    def get_history(self) -> list[ConversationMessage]: ...


async def recall_current_user_context(
    *,
    memory_client: UserRecallMemoryClient | None,
    preference_store: UserMemoryPreferenceStore | None,
    user_id: str,
    user_message: str,
    context: RecallConversationContext,
    guild_id: str | None = None,
    budget: str = DEFAULT_USER_RECALL_BUDGET,
    max_tokens: int = DEFAULT_USER_RECALL_MAX_TOKENS,
    types: list[str] | None = None,
) -> str:
    if memory_client is None or preference_store is None:
        return ""

    try:
        if not await preference_store.is_memory_enabled(user_id):
            return ""
    except Exception:
        log.exception("Failed to check memory preference for user %s", user_id)
        return ""

    query = compose_user_recall_query(
        user_message,
        context.get_history(),
        context_turns=DEFAULT_USER_RECALL_CONTEXT_TURNS,
        max_chars=DEFAULT_USER_RECALL_MAX_QUERY_CHARS,
    )
    if not query:
        return ""

    # Recall the user's global facts plus this guild's scoped memory; another
    # guild's conversation-derived memory is tagged with its own guild and so is
    # excluded. ``any`` (OR, includes untagged) is safe because every write path
    # tags its memory, so there are no untagged facts to leak across guilds.
    recall_tags = ["scope:global"]
    if guild_id:
        recall_tags.append(f"guild:{guild_id}")

    try:
        memories = await memory_client.recall(
            bank_id=user_bank_id(user_id),
            query=query,
            budget=budget,
            max_tokens=max_tokens,
            types=list(types or DEFAULT_USER_RECALL_TYPES),
            tags=recall_tags,
            tags_match="any",
        )
    except Exception:
        log.exception("Failed to recall user memory for user %s", user_id)
        return ""

    return format_recalled_memories(memories)


def compose_user_recall_query(
    user_message: str,
    history: list[ConversationMessage],
    *,
    context_turns: int = DEFAULT_USER_RECALL_CONTEXT_TURNS,
    max_chars: int = DEFAULT_USER_RECALL_MAX_QUERY_CHARS,
) -> str:
    latest = _clean_text(user_message)
    if not latest:
        return ""

    context_lines = [
        f"{message.role}: {text}"
        for message in _recent_non_tool_messages(history, context_turns=context_turns)
        if (text := _message_text(message))
    ]
    if not context_lines:
        return latest[:max_chars] if max_chars > 0 else latest

    query = "Prior context:\n" + "\n".join(context_lines) + "\n\nCurrent request:\n" + latest
    return _truncate_query(query, latest=latest, max_chars=max_chars)


def format_recalled_memories(memories: list[RecalledMemory]) -> str:
    lines: list[str] = []
    seen: set[str] = set()
    for memory in memories:
        text = _clean_text(memory.text)
        if not text:
            continue
        key = text.lower().rstrip(".")
        if key in seen:
            continue
        seen.add(key)
        type_suffix = f" [{memory.type}]" if memory.type else ""
        lines.append(f"- {text}{type_suffix}")
    return "\n".join(lines)


def _recent_non_tool_messages(
    history: list[ConversationMessage],
    *,
    context_turns: int,
) -> list[ConversationMessage]:
    if context_turns <= 0:
        return []
    candidates = [message for message in history if message.role != "tool"]
    return candidates[-context_turns * 2 :]


def _message_text(message: ConversationMessage) -> str:
    parts = [
        part.text or ""
        for part in message.content
        if part.type == ContentPartType.TEXT and part.text
    ]
    return _clean_text("\n".join(parts))


def _clean_text(text: str) -> str:
    stripped = _MEMORY_TAG_RE.sub("", text)
    return " ".join(stripped.split())


def _truncate_query(query: str, *, latest: str, max_chars: int) -> str:
    if max_chars <= 0 or len(query) <= max_chars:
        return query
    latest_block = f"Current request:\n{latest}"
    if len(latest_block) >= max_chars:
        return latest[:max_chars]

    prefix = "Prior context:\n"
    if not query.startswith(prefix) or "\n\nCurrent request:\n" not in query:
        return latest[:max_chars]

    context_text = query.removeprefix(prefix).split("\n\nCurrent request:\n", 1)[0]
    context_lines = [line for line in context_text.split("\n") if line]
    kept: list[str] = []
    for line in reversed(context_lines):
        candidate_lines = [line, *kept]
        candidate = "Prior context:\n" + "\n".join(candidate_lines) + "\n\n" + latest_block
        if len(candidate) > max_chars:
            break
        kept = candidate_lines

    if not kept:
        return latest[:max_chars]
    return "Prior context:\n" + "\n".join(kept) + "\n\n" + latest_block
