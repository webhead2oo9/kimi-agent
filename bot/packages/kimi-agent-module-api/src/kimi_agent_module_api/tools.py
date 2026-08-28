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
    ``guild_id`` is ``None`` in DMs and in personal chat; a tool whose target
    is a guild artifact must refuse such a caller rather than guess a guild.
    The host has already confirmed the module is active in ``guild_id`` before
    the handler runs.
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
    ``browse_tools``. ``guild_ids`` scopes a tool to specific guilds (``None``
    is everywhere; an empty set is nowhere).
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
        guild_ids: frozenset[int] | None = None,
    ) -> None: ...


__all__ = ["ModuleToolContext", "ModuleToolHandler", "ModuleToolRegistry"]
