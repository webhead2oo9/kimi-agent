"""The minimal LLM-tool surface available during module loading."""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Mapping
from typing import Any, Protocol

from community_agent_module_api.trust import TrustTier


class ModuleToolContext(Protocol):
    user_id: str
    user_name: str
    guild_id: str | None
    channel_id: str
    thread_id: str | None
    trust_tier: TrustTier
    tool_configs: Mapping[str, Mapping[str, Any]]


type ModuleToolHandler = Callable[[dict[str, Any], ModuleToolContext], Coroutine[Any, Any, str]]


class ModuleToolRegistry(Protocol):
    """Tool registration supported by every compatible host."""

    def register(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        handler: ModuleToolHandler,
        min_tier: TrustTier = TrustTier.MEMBER,
        searchable: bool = False,
        *,
        owner_only: bool = False,
        guild_ids: frozenset[str] | None = None,
    ) -> None: ...


__all__ = ["ModuleToolContext", "ModuleToolHandler", "ModuleToolRegistry"]
