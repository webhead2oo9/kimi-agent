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
from kimi_agent_module_api.contracts import (
    ButtonSpec,
    MessageRef,
    OutgoingEmbed,
    UndeclaredDiscordAction,
)
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
        self.name = "general"
        self.topic = "Guild discussion"
        self.parent_id = None
        self.sent: list[tuple[Any, dict[str, Any]]] = []
        self.messages: dict[int, Any] = {}
        self.fetches: list[int] = []

    async def send(self, content: Any, **kwargs: Any) -> Any:
        self.sent.append((content, kwargs))
        return SimpleNamespace(id=99)

    async def fetch_message(self, message_id: int) -> Any:
        self.fetches.append(message_id)
        try:
            return self.messages[message_id]
        except KeyError as exc:
            raise discord.NotFound(SimpleNamespace(status=404, reason="nope"), "gone") from exc

    def history(
        self,
        *,
        limit: int,
        after: Any = None,
        before: Any = None,
        oldest_first: bool,
    ) -> Any:
        messages = sorted(self.messages.values(), key=lambda message: message.id)
        if after is not None:
            messages = [message for message in messages if message.id > after.id]
        if before is not None:
            messages = [message for message in messages if message.id < before.id]
        if not oldest_first:
            messages.reverse()

        async def iterate() -> Any:
            for message in messages[:limit]:
                yield message

        return iterate()

    async def pins(self) -> list[Any]:
        return [message for message in self.messages.values() if getattr(message, "pinned", False)]

    def permissions_for(self, _member: Any) -> Any:
        return SimpleNamespace(view_channel=True, read_message_history=True)


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
async def test_audit_reason_caps_the_combined_prefix_and_preserves_leading_correlation() -> None:
    impl, guild, _ = _actions()
    marker = "[kimi-case:abc123]"
    await impl.kick(1, 20, actor_id=10, reason=f"{marker} {'x' * 600}")
    audit_reason = guild.kicks[0][1]
    assert audit_reason is not None
    assert len(audit_reason) == 512
    assert audit_reason.startswith(f"[mod] {marker} ")


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
async def test_bot_actor_may_act_below_staff_only() -> None:
    impl, guild, _ = _actions(staff={10})
    await impl.kick(1, 20, actor_id=None, reason="automated")
    assert guild.kicks == [(20, "[mod] automated")]
    with pytest.raises(TargetProtected):
        await impl.kick(1, 10, actor_id=None, reason="staff")


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

    await impl.send_message(
        2,
        "controls",
        components=(ButtonSpec(key="confirm", label="Confirm"),),
    )
    view = channel.sent[1][1]["view"]
    assert isinstance(view, discord.ui.View)
    button = view.children[0]
    assert isinstance(button, discord.ui.Button)
    assert button.custom_id == "m:mod:confirm"

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
async def test_history_channel_and_access_reads_return_public_snapshots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    impl, _, channel = _actions()
    monkeypatch.setattr(discord, "TextChannel", _Channel)
    for message_id in range(1, 5):
        channel.messages[message_id] = SimpleNamespace(
            id=message_id,
            guild=SimpleNamespace(id=1),
            channel=channel,
            author=SimpleNamespace(id=20, bot=False, display_name="reader"),
            content=f"message {message_id}",
            attachments=[],
            embeds=[],
            pinned=message_id == 2,
            reference=None,
            edited_at=None,
            jump_url=f"https://discord.com/channels/1/2/{message_id}",
            created_at=dt.datetime(2026, 1, message_id, tzinfo=dt.UTC),
        )

    snapshot = await impl.fetch_channel(1, 2)
    page = await impl.fetch_messages(1, 2, before_message_id=4, limit=2)
    pins = await impl.fetch_pins(1, 2)

    assert snapshot is not None
    assert (snapshot.kind, snapshot.name, snapshot.topic) == (
        "text",
        "general",
        "Guild discussion",
    )
    assert [message.ref.message_id for message in page.messages] == [2, 3]
    assert page.next_cursor == 2 and page.has_more is True
    assert [message.ref.message_id for message in pins] == [2]
    assert await impl.can_view_channel(1, 20, 2) is True


@pytest.mark.asyncio
async def test_fetch_and_delete_reject_message_refs_for_another_guild() -> None:
    impl, _, channel = _actions()
    deleted: list[bool] = []

    async def delete() -> None:
        deleted.append(True)

    channel.messages[5] = SimpleNamespace(delete=delete)
    mismatched = MessageRef(999, 2, 5)

    assert await impl.fetch_message(mismatched) is None
    with pytest.raises(DiscordActionError, match="not in guild 999"):
        await impl.delete_message(mismatched)
    assert channel.fetches == []
    assert deleted == []


@pytest.mark.asyncio
async def test_trust_lookup_maps_core_tiers() -> None:
    impl, _, _ = _actions(staff={10})
    assert await impl._trust.tier(1, 10) == "staff"
    assert await impl._trust.tier(1, 20) == "member"
