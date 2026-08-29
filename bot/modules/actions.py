"""Declaration gate in front of any ``DiscordActions`` implementation."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from kimi_agent_module_api.contracts import (
    ALL_DISCORD_ACTIONS,
    ChannelSnapshot,
    DiscordActions,
    InviteSnapshot,
    MemberSnapshot,
    MessageRef,
    MessagePage,
    MessageSnapshot,
    OutgoingEmbed,
    RoleSnapshot,
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

    async def fetch_channel(self, guild_id: int, channel_id: int) -> ChannelSnapshot | None:
        self._gate("fetch_channel")
        return await self._inner.fetch_channel(guild_id, channel_id)

    async def fetch_messages(
        self,
        guild_id: int,
        channel_id: int,
        *,
        after_message_id: int | None = None,
        before_message_id: int | None = None,
        limit: int = 100,
    ) -> MessagePage:
        self._gate("fetch_messages")
        return await self._inner.fetch_messages(
            guild_id,
            channel_id,
            after_message_id=after_message_id,
            before_message_id=before_message_id,
            limit=limit,
        )

    async def fetch_pins(self, guild_id: int, channel_id: int) -> tuple[MessageSnapshot, ...]:
        self._gate("fetch_pins")
        return await self._inner.fetch_pins(guild_id, channel_id)

    async def fetch_public_threads(
        self, guild_id: int, parent_channel_id: int
    ) -> tuple[ChannelSnapshot, ...]:
        self._gate("fetch_public_threads")
        return await self._inner.fetch_public_threads(guild_id, parent_channel_id)

    async def fetch_roles(self, guild_id: int) -> tuple[RoleSnapshot, ...]:
        self._gate("fetch_roles")
        return await self._inner.fetch_roles(guild_id)

    async def fetch_invites(self, guild_id: int) -> tuple[InviteSnapshot, ...]:
        self._gate("fetch_invites")
        return await self._inner.fetch_invites(guild_id)

    async def can_view_channel(self, guild_id: int, user_id: int, channel_id: int) -> bool:
        self._gate("can_view_channel")
        return await self._inner.can_view_channel(guild_id, user_id, channel_id)


__all__ = ["DeclaredDiscordActions"]
