"""Declaration gate in front of any ``DiscordActions`` implementation."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from kimi_agent_module_api.contracts import (
    ALL_DISCORD_ACTIONS,
    DiscordActions,
    MemberSnapshot,
    MessageRef,
    MessageSnapshot,
    OutgoingEmbed,
    UndeclaredDiscordAction,
)


class DeclaredDiscordActions:
    """Forwards to ``inner`` only for actions the module declared."""

    def __init__(self, inner: DiscordActions, module_name: str, declared: frozenset[str]) -> None:
        unknown = declared - ALL_DISCORD_ACTIONS
        if unknown:
            raise ValueError(f"unknown Discord actions {sorted(unknown)!r}")
        self._inner = inner
        self._module_name = module_name
        self._declared = declared

    def _gate(self, action: str) -> None:
        if action not in self._declared:
            raise UndeclaredDiscordAction(self._module_name, action)

    async def send_message(
        self,
        channel_id: int,
        content: str | None = None,
        *,
        embed: OutgoingEmbed | None = None,
        reply_to: MessageRef | None = None,
        components: Sequence[Any] = (),
    ) -> MessageRef:
        self._gate("send_message")
        return await self._inner.send_message(
            channel_id, content, embed=embed, reply_to=reply_to, components=components
        )

    async def send_dm(
        self, user_id: int, content: str, *, embed: OutgoingEmbed | None = None
    ) -> bool:
        self._gate("send_dm")
        return await self._inner.send_dm(user_id, content, embed=embed)

    async def edit_message(
        self, ref: MessageRef, content: str | None = None, *, embed: OutgoingEmbed | None = None
    ) -> None:
        self._gate("edit_message")
        await self._inner.edit_message(ref, content, embed=embed)

    async def delete_message(self, ref: MessageRef, *, reason: str = "") -> None:
        self._gate("delete_message")
        await self._inner.delete_message(ref, reason=reason)

    async def ban(
        self,
        guild_id: int,
        user_id: int,
        *,
        actor_id: int | None,
        reason: str,
        delete_message_seconds: int = 0,
    ) -> None:
        self._gate("ban")
        await self._inner.ban(
            guild_id,
            user_id,
            actor_id=actor_id,
            reason=reason,
            delete_message_seconds=delete_message_seconds,
        )

    async def kick(self, guild_id: int, user_id: int, *, actor_id: int | None, reason: str) -> None:
        self._gate("kick")
        await self._inner.kick(guild_id, user_id, actor_id=actor_id, reason=reason)

    async def timeout(
        self,
        guild_id: int,
        user_id: int,
        *,
        actor_id: int | None,
        reason: str,
        duration_seconds: int,
    ) -> None:
        self._gate("timeout")
        await self._inner.timeout(
            guild_id, user_id, actor_id=actor_id, reason=reason, duration_seconds=duration_seconds
        )

    async def fetch_message(self, ref: MessageRef) -> MessageSnapshot | None:
        self._gate("fetch_message")
        return await self._inner.fetch_message(ref)

    async def fetch_member(self, guild_id: int, user_id: int) -> MemberSnapshot | None:
        self._gate("fetch_member")
        return await self._inner.fetch_member(guild_id, user_id)


__all__ = ["DeclaredDiscordActions"]
