"""The LLM-tool surface: what a module registers at load and what a handler receives."""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from kimi_agent_module_api.trust import TrustTier


@dataclass(frozen=True, slots=True)
class ModuleToolContext:
    """Who is calling a module tool, and from where.

    Ids are Discord snowflakes as ``int``, matching every other SDK type.
    ``guild_id`` is ``None`` only for a tool registered with
    ``guild_only=False`` and called from a DM or personal chat; the host hides
    guild-only tools there. When ``guild_id`` is set, the host has already
    confirmed the module is active in that guild.
    """

    user_id: int
    user_name: str
    guild_id: int | None
    channel_id: int
    thread_id: int | None
    trust_tier: TrustTier
    # Operator per-tool configuration from ``<CONFIG_DIR>/tools/<tool>.md``.
    tool_configs: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)


type ModuleToolHandler = Callable[[dict[str, Any], ModuleToolContext], Coroutine[Any, Any, str]]


class ModuleToolRegistry(Protocol):
    """Tool registration supported by every compatible host.

    Valid only inside ``ModuleSpec.create``; the host seals it afterwards.
    ``searchable`` tools stay hidden until the model activates them with
    ``browse_tools``. A tool is visible only where its module is active; with
    ``guild_only`` (the default) it is also hidden from DMs and personal chat,
    so its handler always sees a guild. ``guild_ids`` further scopes a tool to
    specific guilds (``None`` is everywhere; an empty set is nowhere).
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
    ) -> None: ...


__all__ = ["ModuleToolContext", "ModuleToolHandler", "ModuleToolRegistry"]
