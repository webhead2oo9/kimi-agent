"""Declared Discord actions: gate, target policy, guild scoping."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

import discord
import pytest

from discord_adapter.module_actions import (
    DiscordActionError,
    DiscordActionsImpl,
    TargetProtected,
    TrustLookupImpl,
)
from kimi_agent_module_api.contracts import MessageRef, OutgoingEmbed, UndeclaredDiscordAction
from modules.actions import DeclaredDiscordActions
from trust.tiers import TrustTier


class _Member:
    def __init__(self, user_id: int, *, bot: bool = False) -> None:
        self.id = user_id
        self.bot = bot
        self.guild = SimpleNamespace(id=1)
        self.display_name = f"user{user_id}"
        self.roles: list[Any] = []
        self.joined_at = None
        self.timed_out_until = None
        self.timeouts: list[tuple[Any, str | None]] = []

    async def timeout(self, until: Any, *, reason: str | None = None) -> None:
        self.timeouts.append((until, reason))


class _Guild:
    def __init__(self, members: dict[int, _Member]) -> None:
        self.id = 1
        self._members = members
        self.bans: list[tuple[int, str | None, int]] = []
        self.kicks: list[tuple[int, str | None]] = []

    def get_member(self, user_id: int) -> _Member | None:
        return self._members.get(user_id)

    async def fetch_member(self, user_id: int) -> _Member:
        raise discord.NotFound(SimpleNamespace(status=404, reason="nope"), "not found")

    async def ban(
        self, member: _Member, *, reason: str | None, delete_message_seconds: int
    ) -> None:
        self.bans.append((member.id, reason, delete_message_seconds))

    async def kick(self, member: _Member, *, reason: str | None) -> None:
        self.kicks.append((member.id, reason))


class _Channel:
    def __init__(self, guild: _Guild) -> None:
        self.id = 2
        self.guild = guild
        self.sent: list[tuple[Any, dict[str, Any]]] = []
        self.messages: dict[int, Any] = {}

    async def send(self, content: Any, **kwargs: Any) -> Any:
        self.sent.append((content, kwargs))
        return SimpleNamespace(id=99)

    async def fetch_message(self, message_id: int) -> Any:
        try:
            return self.messages[message_id]
        except KeyError as exc:
            raise discord.NotFound(SimpleNamespace(status=404, reason="nope"), "gone") from exc


class _Bot:
    def __init__(self, guild: _Guild, channel: _Channel, *, bot_user_id: int = 1000) -> None:
        self._guild = guild
        self._channel = channel
        self.user = SimpleNamespace(id=bot_user_id)

    def get_guild(self, guild_id: int) -> _Guild | None:
        return self._guild if guild_id == self._guild.id else None

    def get_channel(self, channel_id: int) -> _Channel | None:
        return self._channel if channel_id == self._channel.id else None

    def get_user(self, user_id: int) -> Any:
        return None


class _Resolver:
    def __init__(self, staff: set[int]) -> None:
        self.staff = staff

    def resolve(self, member: Any, user_id: str, guild_id: str | None = None) -> TrustTier:
        return TrustTier.STAFF if int(user_id) in self.staff else TrustTier.MEMBER


def _actions(
    *, staff: set[int] | None = None, override: bool = False, active: bool = True
) -> tuple[DiscordActionsImpl, _Guild, _Channel]:
    members = {10: _Member(10), 20: _Member(20), 30: _Member(30, bot=True)}
    guild = _Guild(members)
    channel = _Channel(guild)
    bot = _Bot(guild, channel)
    trust = TrustLookupImpl(bot, _Resolver(staff or {10}))  # type: ignore[arg-type]
    impl = DiscordActionsImpl(
        bot=bot,  # type: ignore[arg-type]
        trust=trust,
        module_name="mod",
        is_guild_active=lambda _g: active,
        override_target_policy=override,
    )
    return impl, guild, channel


@pytest.mark.asyncio
async def test_gate_blocks_undeclared_actions_before_touching_discord() -> None:
    impl, guild, _ = _actions()
    gated = DeclaredDiscordActions(impl, "mod", frozenset({"kick"}))
    with pytest.raises(UndeclaredDiscordAction):
        await gated.ban(1, 20, actor_id=10, reason="x")
    await gated.kick(1, 20, actor_id=10, reason="spam  bot")
    assert guild.kicks == [(20, "[mod] spam bot")]
    assert guild.bans == []
    with pytest.raises(ValueError):
        DeclaredDiscordActions(impl, "mod", frozenset({"nuke"}))


@pytest.mark.asyncio
async def test_target_policy_protects_bot_self_bots_and_equal_tier() -> None:
    impl, guild, _ = _actions(staff={10, 20})
    with pytest.raises(TargetProtected):
        await impl.ban(1, 1000, actor_id=10, reason="bot")
    with pytest.raises(TargetProtected):
        await impl.ban(1, 10, actor_id=10, reason="self")
    with pytest.raises(TargetProtected):
        await impl.ban(1, 30, actor_id=10, reason="a bot")
    with pytest.raises(TargetProtected):
        await impl.ban(1, 20, actor_id=10, reason="peer staff")
    assert guild.bans == []
    with pytest.raises(DiscordActionError):
        await impl.ban(1, 404, actor_id=10, reason="missing member")


@pytest.mark.asyncio
async def test_override_lets_a_declared_module_act_on_equal_tier() -> None:
    impl, guild, _ = _actions(staff={10, 20}, override=True)
    await impl.ban(1, 20, actor_id=10, reason="r", delete_message_seconds=10**9)
    assert guild.bans == [(20, "[mod] r", 604_800)]


@pytest.mark.asyncio
async def test_timeout_bounds_duration_and_requires_positive() -> None:
    impl, guild, _ = _actions()
    with pytest.raises(ValueError):
        await impl.timeout(1, 20, actor_id=10, reason="r", duration_seconds=0)
    before = discord.utils.utcnow()
    await impl.timeout(1, 20, actor_id=10, reason="r", duration_seconds=10**9)
    until, reason = guild.get_member(20).timeouts[0]  # type: ignore[union-attr]
    assert reason == "[mod] r"
    assert until - before <= dt.timedelta(days=28, seconds=5)


@pytest.mark.asyncio
async def test_inactive_guild_is_refused_everywhere() -> None:
    impl, _, _ = _actions(active=False)
    with pytest.raises(DiscordActionError):
        await impl.kick(1, 20, actor_id=10, reason="r")
    with pytest.raises(DiscordActionError):
        await impl.send_message(2, "hi")
    assert await impl.fetch_member(1, 20) is None


@pytest.mark.asyncio
async def test_send_and_fetch_use_snapshots_and_safe_mentions() -> None:
    impl, _, channel = _actions()
    ref = await impl.send_message(
        2, "@everyone hi", embed=OutgoingEmbed(title="t", fields=(("a", "b", True),))
    )
    assert ref == MessageRef(1, 2, 99)
    content, kwargs = channel.sent[0]
    assert content == "@everyone hi"
    assert kwargs["allowed_mentions"].everyone is False
    assert kwargs["embed"].title == "t"

    channel.messages[5] = SimpleNamespace(
        id=5,
        guild=SimpleNamespace(id=1),
        channel=channel,
        author=SimpleNamespace(id=20, bot=False),
        content="c",
        attachments=[],
        jump_url="u",
        created_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
    )
    snap = await impl.fetch_message(MessageRef(1, 2, 5))
    assert snap is not None and snap.author_id == 20
    assert await impl.fetch_message(MessageRef(1, 2, 6)) is None
    member = await impl.fetch_member(1, 20)
    assert member is not None and member.display_name == "user20"


@pytest.mark.asyncio
async def test_trust_lookup_maps_core_tiers() -> None:
    impl, _, _ = _actions(staff={10})
    assert await impl._trust.tier(1, 10) == "staff"
    assert await impl._trust.tier(1, 20) == "member"
