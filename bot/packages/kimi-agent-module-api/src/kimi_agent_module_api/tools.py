"""The LLM-tool surface: what a module registers at load and what a handler receives."""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from kimi_agent_module_api.files import ToolFiles
from kimi_agent_module_api.trust import TrustTier


@dataclass(frozen=True, slots=True)
class TriggeringDiscordMessageSnapshot:
    """Immutable evidence captured from the Discord message at turn entry."""

    message_id: int
    guild_id: int
    channel_id: int
    author_id: int
    content: str
    author_is_bot: bool


@dataclass(frozen=True, slots=True)
class ModuleToolContext:
    """Who is calling a module tool, and from where.

    Ids are Discord snowflakes as ``int``, matching every other SDK type.
    ``guild_id`` is ``None`` only for a tool registered with
    ``guild_only=False`` and called from a DM or personal chat; the host hides
    guild-only tools there. When ``guild_id`` is set, the host has already
    confirmed the module is active in that guild. ``channel_id`` is ``None``
    in personal chat, which is a slash interaction rather than a channel.
    """

    user_id: int
    user_name: str
    guild_id: int | None
    channel_id: int | None
    thread_id: int | None
    trust_tier: TrustTier
    # Operator per-tool configuration from ``<CONFIG_DIR>/tools/<tool>.md``.
    tool_configs: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    # Discord message that initiated this model turn. It is absent for personal
    # app commands and other surfaces that are not rooted in a Discord message.
    trigger_discord_message_id: int | None = None
    # Invocation-scoped read-only port. Declare permissions.tool_files and
    # require tools.files.v1; never retain this port beyond the handler.
    files: ToolFiles | None = None
    # Host-owned values captured before turn preparation can await. Unlike a
    # later Discord fetch, this evidence cannot change or disappear mid-turn.
    # Added after all API 2.2 fields to preserve positional construction.
    trigger_discord_message_snapshot: TriggeringDiscordMessageSnapshot | None = None


type ModuleToolHandler = Callable[[dict[str, Any], ModuleToolContext], Coroutine[Any, Any, str]]


class ModuleToolRegistry(Protocol):
    """Tool registration supported by every compatible host.

    Valid only inside ``ModuleSpec.create``; the host seals it afterwards.
    ``searchable`` tools stay hidden until the model activates them with
    ``browse_tools``. A tool is visible only where its module is active; with
    ``guild_only`` (the default) it is also hidden from DMs and personal chat,
    so its handler always sees a guild. ``guild_ids`` further scopes a tool to
    specific guilds (``None`` is everywhere; an empty set is nowhere).
    Results default to untrusted because module tools commonly return Discord,
    network, or user-authored data. Set ``untrusted=False`` only for output that
    is wholly controlled by the installed module.
    """

    def register(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        handler: ModuleToolHandler,
        *,
        min_tier: TrustTier = TrustTier.MEMBER,
        searchable: bool = False,
        owner_only: bool = False,
        guild_only: bool = True,
        guild_ids: frozenset[int] | None = None,
        untrusted: bool = True,
    ) -> None: ...


__all__ = [
    "ModuleToolContext",
    "ModuleToolHandler",
    "ModuleToolRegistry",
    "TriggeringDiscordMessageSnapshot",
]
