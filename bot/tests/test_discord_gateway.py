from __future__ import annotations

import asyncio
from datetime import datetime, UTC
from typing import Any

import pytest
import discord

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


class _SearchPermissions:
    def __init__(
        self,
        *,
        view: bool = True,
        history: bool = True,
        manage_threads: bool = False,
    ) -> None:
        self.view_channel = view
        self.read_message_history = history
        self.manage_threads = manage_threads


class _SearchMember(_Author):
    def __init__(self, id: int) -> None:
        super().__init__(id, str(id))
        self.guild: Any = None


class _SearchChannel:
    def __init__(
        self,
        id: int,
        name: str,
        channel_type: discord.ChannelType,
        *,
        parent_id: int | None = None,
        denied_ids: set[int] | None = None,
        thread_member_ids: set[int] | None = None,
        manager_ids: set[int] | None = None,
    ) -> None:
        self.id = id
        self.name = name
        self.type = channel_type
        self.parent_id = parent_id
        self.guild: Any = None
        self.denied_ids = denied_ids or set()
        self.thread_member_ids = thread_member_ids or set()
        self.manager_ids = manager_ids or set()
        self.public_archived: list[_SearchChannel] = []
        self.private_archived: list[_SearchChannel] = []
        self.joined_private_archived: list[_SearchChannel] | None = None
        self.archive_calls: list[tuple[bool, bool]] = []
        self.fetched_member_ids: list[int] = []

    def permissions_for(self, member: _SearchMember) -> _SearchPermissions:
        allowed = member.id not in self.denied_ids
        return _SearchPermissions(
            view=allowed,
            history=allowed,
            manage_threads=member.id in self.manager_ids,
        )

    async def fetch_members(self) -> list[_SearchMember]:
        raise AssertionError("bulk thread membership must not be used")

    async def fetch_member(self, member_id: int) -> _SearchMember:
        self.fetched_member_ids.append(member_id)
        if member_id not in self.thread_member_ids:
            raise discord.NotFound(_FakeResponse(), "not a thread member")
        return _SearchMember(member_id)

    def archived_threads(
        self,
        *,
        private: bool = False,
        joined: bool = False,
        limit: int | None = 100,
    ):
        self.archive_calls.append((private, joined))
        del limit

        async def iterate():
            private_threads = (
                self.joined_private_archived
                if joined and self.joined_private_archived is not None
                else self.private_archived
            )
            for thread in private_threads if private else self.public_archived:
                yield thread

        return iterate()


class _SearchGuild:
    def __init__(
        self,
        member: _SearchMember,
        bot_member: _SearchMember,
        channels: list[_SearchChannel],
        threads: list[_SearchChannel],
    ) -> None:
        self.id = 999
        self.me = bot_member
        self.channels = channels
        self.threads = threads
        self._all = {channel.id: channel for channel in [*channels, *threads]}
        member.guild = self
        bot_member.guild = self
        for channel in list(self._all.values()):
            channel.guild = self
            joined_private = channel.joined_private_archived or []
            for archived in [
                *channel.public_archived,
                *channel.private_archived,
                *joined_private,
            ]:
                archived.guild = self
                self._all[archived.id] = archived

    def get_channel_or_thread(self, channel_id: int) -> _SearchChannel | None:
        return self._all.get(channel_id)

    async def fetch_channel(self, channel_id: int) -> _SearchChannel:
        channel = self._all.get(channel_id)
        if channel is None:
            raise discord.NotFound(_FakeResponse(), "missing")
        return channel


class _FailingArchiveChannel(_SearchChannel):
    def archived_threads(
        self,
        *,
        private: bool = False,
        joined: bool = False,
        limit: int | None = 100,
    ):
        del private, joined, limit

        async def iterate():
            raise RuntimeError("archive lookup failed")
            yield

        return iterate()


class _SlowArchiveChannel(_SearchChannel):
    def archived_threads(
        self,
        *,
        private: bool = False,
        joined: bool = False,
        limit: int | None = 100,
    ):
        source = super().archived_threads(private=private, joined=joined, limit=limit)

        async def iterate():
            await asyncio.sleep(0.01)
            async for thread in source:
                yield thread

        return iterate()


class _InvalidDataGuild(_SearchGuild):
    async def fetch_channel(self, channel_id: int) -> _SearchChannel:
        del channel_id
        raise discord.InvalidData("channel belongs to another guild")


class _FakeResponse:
    status = 404
    reason = "missing"


def _search_gateway(
    guild: _SearchGuild,
    member: _SearchMember,
    bot_member: _SearchMember,
) -> DiscordGateway:
    source = _Message(555, member, "search")
    source.guild = guild
    gateway = DiscordGateway(bot_user_provider=lambda: bot_member)
    gateway.bind_turn_source("guild:100:main", "555", source)
    return gateway


def test_discord_search_scope_includes_accessible_channels_and_archived_threads() -> None:
    member = _SearchMember(123)
    bot_member = _SearchMember(999)
    parent = _SearchChannel(100, "general", discord.ChannelType.text)
    parent.public_archived = [
        _SearchChannel(102, "old-topic", discord.ChannelType.public_thread, parent_id=100)
    ]
    parent.private_archived = [
        _SearchChannel(
            103,
            "private-topic",
            discord.ChannelType.private_thread,
            parent_id=100,
            thread_member_ids={123, 999},
        )
    ]
    active = _SearchChannel(101, "live-topic", discord.ChannelType.public_thread, parent_id=100)
    excluded_parent = _SearchChannel(200, "staff", discord.ChannelType.text)
    excluded_parent.public_archived = [
        _SearchChannel(201, "staff-thread", discord.ChannelType.public_thread, parent_id=200)
    ]
    member_hidden = _SearchChannel(
        300,
        "member-hidden",
        discord.ChannelType.text,
        denied_ids={123},
    )
    bot_hidden = _SearchChannel(
        400,
        "bot-hidden",
        discord.ChannelType.text,
        denied_ids={999},
    )
    guild = _SearchGuild(
        member,
        bot_member,
        [parent, excluded_parent, member_hidden, bot_hidden],
        [active],
    )
    gateway = _search_gateway(guild, member, bot_member)

    resolved = asyncio.run(
        gateway.resolve_discord_search_channels(
            _ctx(),
            requested_channel_ids=None,
            excluded_channel_ids=frozenset({"200"}),
        )
    )

    assert resolved == {
        "100": "general",
        "101": "live-topic",
        "102": "old-topic",
        "103": "private-topic",
    }
    assert parent.private_archived[0].fetched_member_ids == [123, 999]


def test_discord_search_explicit_scope_rejects_excluded_or_inaccessible_channel() -> None:
    member = _SearchMember(123)
    bot_member = _SearchMember(999)
    excluded = _SearchChannel(200, "staff", discord.ChannelType.text)
    hidden = _SearchChannel(300, "hidden", discord.ChannelType.text, denied_ids={123})
    guild = _SearchGuild(member, bot_member, [excluded, hidden], [])
    gateway = _search_gateway(guild, member, bot_member)

    for channel_id, exclusions in (("200", frozenset({"200"})), ("300", frozenset())):
        with pytest.raises(ValueError, match="unavailable"):
            asyncio.run(
                gateway.resolve_discord_search_channels(
                    _ctx(),
                    requested_channel_ids=(channel_id,),
                    excluded_channel_ids=exclusions,
                )
            )


def test_discord_search_scope_fails_closed_when_thread_enumeration_fails() -> None:
    member = _SearchMember(123)
    bot_member = _SearchMember(999)
    parent = _FailingArchiveChannel(100, "general", discord.ChannelType.text)
    guild = _SearchGuild(member, bot_member, [parent], [])
    gateway = _search_gateway(guild, member, bot_member)

    with pytest.raises(ValueError, match="scope is unavailable"):
        asyncio.run(
            gateway.resolve_discord_search_channels(
                _ctx(),
                requested_channel_ids=None,
                excluded_channel_ids=frozenset(),
            )
        )


def test_discord_search_private_thread_uses_individual_membership_or_manager_access() -> None:
    member = _SearchMember(123)
    bot_member = _SearchMember(999)
    joined = _SearchChannel(
        101,
        "joined",
        discord.ChannelType.private_thread,
        parent_id=100,
        thread_member_ids={123, 999},
    )
    not_joined = _SearchChannel(
        102,
        "not-joined",
        discord.ChannelType.private_thread,
        parent_id=100,
        thread_member_ids={999},
    )
    managed = _SearchChannel(
        103,
        "managed",
        discord.ChannelType.private_thread,
        parent_id=100,
        thread_member_ids={999},
        manager_ids={123},
    )
    guild = _SearchGuild(member, bot_member, [], [joined, not_joined, managed])
    gateway = _search_gateway(guild, member, bot_member)

    resolved = asyncio.run(
        gateway.resolve_discord_search_channels(
            _ctx(),
            requested_channel_ids=None,
            excluded_channel_ids=frozenset(),
        )
    )

    assert resolved == {"101": "joined", "103": "managed"}
    assert joined.fetched_member_ids == [123, 999]
    assert not_joined.fetched_member_ids == [123]
    assert managed.fetched_member_ids == [999]


def test_discord_search_archive_inventory_is_cached_but_permissions_are_rechecked() -> None:
    member = _SearchMember(123)
    bot_member = _SearchMember(999)
    parent = _SearchChannel(100, "general", discord.ChannelType.text)
    archived = _SearchChannel(
        101,
        "old-topic",
        discord.ChannelType.public_thread,
        parent_id=100,
    )
    parent.public_archived = [archived]
    guild = _SearchGuild(member, bot_member, [parent], [])
    gateway = _search_gateway(guild, member, bot_member)

    first = asyncio.run(
        gateway.resolve_discord_search_channels(
            _ctx(), requested_channel_ids=None, excluded_channel_ids=frozenset()
        )
    )
    archived.denied_ids.add(123)
    second = asyncio.run(
        gateway.resolve_discord_search_channels(
            _ctx(), requested_channel_ids=None, excluded_channel_ids=frozenset()
        )
    )

    assert first == {"100": "general", "101": "old-topic"}
    assert second == {"100": "general"}
    assert parent.archive_calls == [(False, False), (True, True)]


def test_discord_search_archive_cache_single_flights_concurrent_cold_lookups() -> None:
    member = _SearchMember(123)
    bot_member = _SearchMember(999)
    parent = _SlowArchiveChannel(100, "general", discord.ChannelType.text)
    parent.public_archived = [
        _SearchChannel(101, "old-topic", discord.ChannelType.public_thread, parent_id=100)
    ]
    guild = _SearchGuild(member, bot_member, [parent], [])
    gateway = _search_gateway(guild, member, bot_member)

    async def resolve_twice() -> tuple[dict[str, str], dict[str, str]]:
        first, second = await asyncio.gather(
            gateway.resolve_discord_search_channels(
                _ctx(), requested_channel_ids=None, excluded_channel_ids=frozenset()
            ),
            gateway.resolve_discord_search_channels(
                _ctx(), requested_channel_ids=None, excluded_channel_ids=frozenset()
            ),
        )
        return first, second

    first, second = asyncio.run(resolve_twice())

    assert first == second == {"100": "general", "101": "old-topic"}
    assert parent.archive_calls == [(False, False), (True, True)]


def test_discord_search_archive_cache_prunes_unrelated_expired_entries() -> None:
    member = _SearchMember(123)
    bot_member = _SearchMember(999)
    parent = _SearchChannel(100, "general", discord.ChannelType.text)
    guild = _SearchGuild(member, bot_member, [parent], [])
    gateway = _search_gateway(guild, member, bot_member)
    expired_key = ("deleted-parent", "public")
    gateway._discord_search_archive_cache[expired_key] = (0.0, (object(),))

    asyncio.run(
        gateway.resolve_discord_search_channels(
            _ctx(), requested_channel_ids=None, excluded_channel_ids=frozenset()
        )
    )

    assert expired_key not in gateway._discord_search_archive_cache


def test_discord_search_archive_cache_key_tracks_bot_private_discovery_mode() -> None:
    member = _SearchMember(123)
    bot_member = _SearchMember(999)
    parent = _SearchChannel(100, "general", discord.ChannelType.text)
    joined = _SearchChannel(
        101,
        "joined",
        discord.ChannelType.private_thread,
        parent_id=100,
        thread_member_ids={123, 999},
    )
    managed_only = _SearchChannel(
        102,
        "managed-only",
        discord.ChannelType.private_thread,
        parent_id=100,
        thread_member_ids={123},
    )
    parent.private_archived = [joined, managed_only]
    parent.joined_private_archived = [joined]
    guild = _SearchGuild(member, bot_member, [parent], [])
    gateway = _search_gateway(guild, member, bot_member)

    before = asyncio.run(
        gateway.resolve_discord_search_channels(
            _ctx(), requested_channel_ids=None, excluded_channel_ids=frozenset()
        )
    )
    parent.manager_ids.add(999)
    joined.manager_ids.add(999)
    managed_only.manager_ids.add(999)
    after = asyncio.run(
        gateway.resolve_discord_search_channels(
            _ctx(), requested_channel_ids=None, excluded_channel_ids=frozenset()
        )
    )

    assert before == {"100": "general", "101": "joined"}
    assert after == {
        "100": "general",
        "101": "joined",
        "102": "managed-only",
    }
    assert parent.archive_calls == [
        (False, False),
        (True, True),
        (False, False),
        (True, False),
    ]


def test_discord_search_scope_stops_archive_walk_at_501_eligible_channels() -> None:
    member = _SearchMember(123)
    bot_member = _SearchMember(999)
    parents = [
        _SearchChannel(index, f"channel-{index}", discord.ChannelType.text)
        for index in range(1, 501)
    ]
    parents[0].public_archived = [
        _SearchChannel(1001, "overflow", discord.ChannelType.public_thread, parent_id=1)
    ]
    guild = _SearchGuild(member, bot_member, parents, [])
    gateway = _search_gateway(guild, member, bot_member)

    with pytest.raises(ValueError, match="at most 500"):
        asyncio.run(
            gateway.resolve_discord_search_channels(
                _ctx(), requested_channel_ids=None, excluded_channel_ids=frozenset()
            )
        )

    assert parents[0].archive_calls == [(False, False)]
    assert parents[1].archive_calls == []


def test_discord_search_category_exclusion_does_not_leak_or_hide_child_threads() -> None:
    member = _SearchMember(123)
    bot_member = _SearchMember(999)
    category = _SearchChannel(50, "category", discord.ChannelType.category)
    child = _SearchChannel(100, "general", discord.ChannelType.text, parent_id=50)
    active = _SearchChannel(101, "topic", discord.ChannelType.public_thread, parent_id=100)
    guild = _SearchGuild(member, bot_member, [category, child], [active])
    gateway = _search_gateway(guild, member, bot_member)

    resolved = asyncio.run(
        gateway.resolve_discord_search_channels(
            _ctx(), requested_channel_ids=None, excluded_channel_ids=frozenset({"50"})
        )
    )

    assert resolved == {"100": "general", "101": "topic"}


def test_discord_search_explicit_cross_guild_invalid_data_uses_generic_error() -> None:
    member = _SearchMember(123)
    bot_member = _SearchMember(999)
    guild = _InvalidDataGuild(member, bot_member, [], [])
    gateway = _search_gateway(guild, member, bot_member)

    with pytest.raises(ValueError, match="One or more channels are unavailable"):
        asyncio.run(
            gateway.resolve_discord_search_channels(
                _ctx(), requested_channel_ids=("777",), excluded_channel_ids=frozenset()
            )
        )


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
