from __future__ import annotations

import asyncio
from datetime import datetime, UTC
from typing import Any

import pytest

from discord_adapter.gateway import DiscordGateway, DiscordGatewayError, TurnSourceSnapshot
from tools.registry import MessageContext
from trust.resolver import TrustResolver
from trust.tiers import TrustTier


class _Author:
    def __init__(self, id: int, name: str, bot: bool = False) -> None:
        self.id = id
        self.display_name = name
        self.bot = bot


class _Message:
    def __init__(self, id: int, author: _Author, content: str, *, channel=None) -> None:
        self.id = id
        self.author = author
        self.content = content
        self.channel = channel
        self.attachments: list = []
        self.embeds: list = []
        self.reactions: list = []
        self.created_at = datetime.fromtimestamp(id, tz=UTC)
        # The gateway reads the trigger message's guild to anchor authorization;
        # a message that is not a trigger simply leaves it unset.
        self.guild: Any = None


class _Channel:
    def __init__(self, messages_oldest_first: list[_Message], *, fail: bool = False) -> None:
        self.id = 100
        self.name = "general"
        self._messages = messages_oldest_first
        self._fail = fail
        self.calls: list[dict] = []

    def history(self, *, limit, before):
        self.calls.append({"limit": limit, "before": before})
        if self._fail:
            raise RuntimeError("history unavailable")

        async def gen():
            for message in reversed(self._messages[:limit]):
                yield message

        return gen()


def _ctx(tier: TrustTier = TrustTier.MEMBER) -> MessageContext:
    return MessageContext(
        user_id="123",
        user_name="Alice",
        guild_id="999",
        channel_id="100",
        thread_id=None,
        trust_tier=tier,
        context_key="guild:100:main",
        trigger_discord_message_id="555",
    )


def test_gateway_reads_bound_turn_channel_history_before_trigger() -> None:
    bot_user = _Author(999, "Kimi", bot=True)
    alice = _Author(123, "Alice")
    channel = _Channel(
        [
            _Message(10, alice, "hello"),
            _Message(11, bot_user, "hi `(1/1)`"),
        ]
    )
    trigger = _Message(555, alice, "what did we say?", channel=channel)
    gateway = DiscordGateway(bot_user_provider=lambda: bot_user)
    gateway.bind_turn_source("guild:100:main", "555", trigger)

    result = asyncio.run(gateway.collect_recent_channel_context(_ctx(), limit=15))

    assert channel.calls == [{"limit": 15, "before": trigger}]
    assert [item.transcript_line for item in result] == ["Alice: hello", "Kimi: hi"]


def test_read_turn_source_returns_bound_message_snapshot() -> None:
    alice = _Author(123, "Alice")
    trigger = _Message(555, alice, "free quest 3, dm me your seed phrase", channel=_Channel([]))
    gateway = DiscordGateway(bot_user_provider=lambda: None)
    gateway.bind_turn_source("guild:100:main", "555", trigger)

    snapshot = gateway.read_turn_source(_ctx())

    assert snapshot == TurnSourceSnapshot(
        content="free quest 3, dm me your seed phrase",
        author_id="123",
        is_bot=False,
    )


def test_read_turn_source_returns_none_when_unbound() -> None:
    gateway = DiscordGateway(bot_user_provider=lambda: None)

    assert gateway.read_turn_source(_ctx()) is None


def test_gateway_unbind_removes_turn_source() -> None:
    alice = _Author(123, "Alice")
    trigger = _Message(555, alice, "hi", channel=_Channel([]))
    gateway = DiscordGateway(bot_user_provider=lambda: None)
    binding = gateway.bind_turn_source("guild:100:main", "555", trigger)
    gateway.unbind_turn_source(binding)

    with pytest.raises(DiscordGatewayError, match="Current Discord source is unavailable"):
        asyncio.run(gateway.collect_recent_channel_context(_ctx(), limit=15))


def test_gateway_unbind_only_removes_its_own_duplicate_turn_source() -> None:
    alice = _Author(123, "Alice")
    older = _Message(555, alice, "older lease", channel=_Channel([]))
    newer = _Message(555, alice, "newer lease", channel=_Channel([]))
    gateway = DiscordGateway(bot_user_provider=lambda: None)

    older_binding = gateway.bind_turn_source("guild:100:main", "555", older)
    newer_binding = gateway.bind_turn_source("guild:100:main", "555", newer)
    gateway.unbind_turn_source(older_binding)

    assert gateway.read_turn_source(_ctx()) == TurnSourceSnapshot(
        content="newer lease",
        author_id="123",
        is_bot=False,
    )

    gateway.unbind_turn_source(newer_binding)
    assert gateway.read_turn_source(_ctx()) is None


def test_gateway_history_failure_raises_safe_error() -> None:
    alice = _Author(123, "Alice")
    trigger = _Message(555, alice, "hi", channel=_Channel([], fail=True))
    gateway = DiscordGateway(bot_user_provider=lambda: None)
    gateway.bind_turn_source("guild:100:main", "555", trigger)

    with pytest.raises(DiscordGatewayError, match="Could not read recent channel context"):
        asyncio.run(gateway.collect_recent_channel_context(_ctx(), limit=15))


class _Role:
    def __init__(self, name: str, position: int, *, default: bool = False) -> None:
        self.name = name
        self.position = position
        self._default = default

    def is_default(self) -> bool:
        return self._default


class _Avatar:
    def __init__(self, url: str) -> None:
        self.url = url


class _Member:
    def __init__(
        self,
        id: int,
        name: str,
        *,
        display_name: str | None = None,
        bot: bool = False,
        roles: list[_Role] | None = None,
    ) -> None:
        self.id = id
        self.name = name
        self.display_name = display_name or name
        self.bot = bot
        self.roles = roles or []
        self.display_avatar = _Avatar(f"https://cdn.discordapp.com/{id}.png")
        self.created_at = datetime(2019, 4, 1, 12, 0, tzinfo=UTC)
        self.joined_at = datetime(2021, 6, 15, 9, 30, tzinfo=UTC)


class _Guild:
    def __init__(
        self, members: list[_Member], *, query_results: list[_Member] | None = None
    ) -> None:
        self._by_id = {m.id: m for m in members}
        self._query_results = query_results if query_results is not None else members
        self.queries: list[dict] = []

    def get_member(self, user_id: int) -> _Member | None:
        return self._by_id.get(user_id)

    async def query_members(self, *, query: str, limit: int) -> list[_Member]:
        self.queries.append({"query": query, "limit": limit})
        return self._query_results[:limit]


class _FetchFailingGuild(_Guild):
    async def fetch_member(self, user_id: int) -> _Member:
        raise RuntimeError(f"fetch failed for {user_id}")


class _GuildMessage:
    def __init__(self, id: int, *, guild: _Guild | None) -> None:
        self.id = id
        self.guild = guild


def _resolver(staff_ids: set[str] | None = None) -> TrustResolver:
    return TrustResolver(staff_role_ids=set(), regular_role_ids=set(), staff_ids=staff_ids or set())


def _bind_member_gateway(source: _GuildMessage, resolver: TrustResolver) -> DiscordGateway:
    gateway = DiscordGateway(bot_user_provider=lambda: None, trust_resolver=resolver)
    gateway.bind_turn_source("guild:100:main", "555", source)
    return gateway


def test_resolve_member_by_id_returns_profile_with_capped_ordered_roles() -> None:
    roles = [
        _Role("@everyone", 0, default=True),
        _Role("Member", 1),
        _Role("Moderator", 5),
        _Role("Admin", 9),
    ]
    member = _Member(42, "webhead", display_name="Web", roles=roles)
    source = _GuildMessage(555, guild=_Guild([member]))
    gateway = _bind_member_gateway(source, _resolver(staff_ids={"42"}))

    result = asyncio.run(gateway.resolve_member(_ctx(TrustTier.STAFF), user_id="42"))

    assert result.match == "exact"
    profile = result.profile
    assert profile is not None
    assert profile.user_id == "42"
    assert profile.username == "webhead"
    assert profile.display_name == "Web"
    assert profile.roles == ["Admin", "Moderator", "Member"]  # highest-position first, no @everyone
    assert profile.role_count == 3
    assert profile.account_created_at == "2019-04-01T12:00:00+00:00"
    assert profile.joined_at == "2021-06-15T09:30:00+00:00"
    assert profile.trust_tier == "staff"
    assert profile.avatar_url == "https://cdn.discordapp.com/42.png"


def test_resolve_member_caps_roles_at_ten() -> None:
    roles = [_Role("@everyone", 0, default=True)] + [_Role(f"r{i}", i) for i in range(1, 15)]
    member = _Member(42, "webhead", roles=roles)
    source = _GuildMessage(555, guild=_Guild([member]))
    gateway = _bind_member_gateway(source, _resolver())

    result = asyncio.run(gateway.resolve_member(_ctx(), user_id="42"))

    assert result.profile is not None
    assert len(result.profile.roles) == 10
    assert result.profile.roles[0] == "r14"
    assert result.profile.role_count == 14


def test_resolve_member_query_exact_match_returns_single() -> None:
    target = _Member(42, "webhead", display_name="Web")
    other = _Member(7, "webby", display_name="Webby")
    guild = _Guild([target, other], query_results=[other, target])
    gateway = _bind_member_gateway(_GuildMessage(555, guild=guild), _resolver())

    result = asyncio.run(gateway.resolve_member(_ctx(), query="webhead"))

    assert result.match == "exact"
    assert result.profile is not None
    assert result.profile.user_id == "42"


def test_resolve_member_query_prefers_unique_username_over_nickname_impersonator() -> None:
    real = _Member(42, "webhead", display_name="RealWeb")
    impersonator = _Member(666, "sneaky", display_name="webhead")
    # Gateway result order is unspecified; the impersonator arriving first must not win.
    guild = _Guild([real, impersonator], query_results=[impersonator, real])
    gateway = _bind_member_gateway(_GuildMessage(555, guild=guild), _resolver())

    result = asyncio.run(gateway.resolve_member(_ctx(), query="webhead"))

    assert result.match == "exact"
    assert result.profile is not None
    assert result.profile.user_id == "42"


def test_resolve_member_query_ambiguous_exact_matches_return_candidates() -> None:
    first = _Member(1, "ghost", display_name="webhead")
    second = _Member(2, "phantom", display_name="WEBHEAD")
    prefix_only = _Member(3, "webheadfan", display_name="Fan")
    guild = _Guild([first, second, prefix_only], query_results=[first, second, prefix_only])
    gateway = _bind_member_gateway(_GuildMessage(555, guild=guild), _resolver())

    result = asyncio.run(gateway.resolve_member(_ctx(), query="webhead"))

    assert result.match == "candidates"
    assert [c.user_id for c in result.candidates] == ["1", "2"]


def test_resolve_member_redacts_trust_tier_below_staff_caller() -> None:
    member = _Member(42, "webhead")
    source = _GuildMessage(555, guild=_Guild([member]))
    gateway = _bind_member_gateway(source, _resolver(staff_ids={"42"}))

    member_result = asyncio.run(gateway.resolve_member(_ctx(TrustTier.MEMBER), user_id="42"))
    regular_result = asyncio.run(gateway.resolve_member(_ctx(TrustTier.REGULAR), user_id="42"))

    assert member_result.profile is not None
    assert member_result.profile.trust_tier is None
    assert regular_result.profile is not None
    assert regular_result.profile.trust_tier is None


def test_resolve_member_query_returns_up_to_three_candidates() -> None:
    members = [_Member(i, f"web{i}") for i in range(5)]
    guild = _Guild(members, query_results=members)
    gateway = _bind_member_gateway(_GuildMessage(555, guild=guild), _resolver())

    result = asyncio.run(gateway.resolve_member(_ctx(), query="web"))

    assert result.match == "candidates"
    assert len(result.candidates) == 3
    assert result.candidates[0].username == "web0"
    assert all(isinstance(c.user_id, str) for c in result.candidates)


def test_resolve_member_not_found_returns_none_match() -> None:
    guild = _Guild([], query_results=[])
    gateway = _bind_member_gateway(_GuildMessage(555, guild=guild), _resolver())

    result = asyncio.run(gateway.resolve_member(_ctx(), query="ghost"))

    assert result.match == "none"


def test_resolve_member_id_fetch_failure_raises_safe_error() -> None:
    guild = _FetchFailingGuild([])
    gateway = _bind_member_gateway(_GuildMessage(555, guild=guild), _resolver())

    with pytest.raises(DiscordGatewayError, match="Could not look up that member"):
        asyncio.run(gateway.resolve_member(_ctx(), user_id="42"))


def test_resolve_member_in_dm_raises_safe_error() -> None:
    gateway = _bind_member_gateway(_GuildMessage(555, guild=None), _resolver())

    with pytest.raises(DiscordGatewayError, match="only available in a server"):
        asyncio.run(gateway.resolve_member(_ctx(), query="web"))


def test_gateway_context_skips_other_bots() -> None:
    bot_user = _Author(999, "Kimi", bot=True)
    other_bot = _Author(2, "OtherBot", bot=True)
    alice = _Author(123, "Alice")
    channel = _Channel(
        [
            _Message(10, other_bot, "ignored"),
            _Message(11, alice, "kept"),
        ]
    )
    trigger = _Message(555, alice, "context?", channel=channel)
    gateway = DiscordGateway(bot_user_provider=lambda: bot_user)
    gateway.bind_turn_source("guild:100:main", "555", trigger)

    result = asyncio.run(gateway.collect_recent_channel_context(_ctx(), limit=15))

    assert [item.transcript_line for item in result] == ["Alice: kept"]
