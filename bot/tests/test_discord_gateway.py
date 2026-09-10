from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime, UTC
from types import SimpleNamespace
from typing import Any

import pytest
import discord

from discord_adapter.gateway import DiscordGateway, DiscordGatewayError
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
    older_channel = _Channel([])
    newer_channel = _Channel([])
    older = _Message(555, alice, "older lease", channel=older_channel)
    newer = _Message(555, alice, "newer lease", channel=newer_channel)
    gateway = DiscordGateway(bot_user_provider=lambda: None)

    older_binding = gateway.bind_turn_source("guild:100:main", "555", older)
    newer_binding = gateway.bind_turn_source("guild:100:main", "555", newer)
    gateway.unbind_turn_source(older_binding)

    asyncio.run(gateway.collect_recent_channel_context(_ctx(), limit=3))
    assert older_channel.calls == []
    assert newer_channel.calls == [{"limit": 3, "before": newer}]

    gateway.unbind_turn_source(newer_binding)
    with pytest.raises(DiscordGatewayError, match="Current Discord source is unavailable"):
        asyncio.run(gateway.collect_recent_channel_context(_ctx(), limit=3))


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


@pytest.mark.asyncio
async def test_channel_discovery_pages_large_inventory_and_closes_archive_iterator():
    member, bot = _SearchMember(123), _SearchMember(999)
    parent = _SearchChannel(1, "general", discord.ChannelType.text)
    active = _SearchChannel(2, "active", discord.ChannelType.public_thread, parent_id=1)
    archived = [
        _SearchChannel(i, f"archived-{i}", discord.ChannelType.public_thread, parent_id=1)
        for i in range(3, 604)
    ]
    parent.public_archived = [active, *archived]
    hidden = _SearchChannel(700, "hidden", discord.ChannelType.text, denied_ids={123})
    excluded = _SearchChannel(701, "excluded", discord.ChannelType.text)
    private = _SearchChannel(702, "private", discord.ChannelType.private_thread, parent_id=1)
    parent.private_archived = [private]
    guild = _SearchGuild(member, bot, [parent, hidden, excluded], [active])
    gateway = _search_gateway(guild, member, bot)
    ids = []
    cursor = None
    while True:
        page = await asyncio.wait_for(
            gateway.discover_discord_channels(
                _ctx(), excluded_channel_ids=frozenset({"701"}), cursor=cursor
            ),
            timeout=2,
        )
        assert len(page["sources"]) <= 200
        assert all(not lock.locked() for lock in gateway._discord_search_archive_locks.values())
        ids.extend(page["sources"])
        if not page["has_more"]:
            assert page["next_cursor"] is None
            break
        cursor = page["next_cursor"]
    assert ids == [str(i) for i in range(1, 604)]


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor", [None, "", "0"])
async def test_channel_discovery_first_page_does_not_fetch_archives(cursor):
    member, bot = _SearchMember(123), _SearchMember(999)
    channels = [_SearchChannel(i, str(i), discord.ChannelType.text) for i in range(1, 202)]
    gateway = _search_gateway(_SearchGuild(member, bot, channels, []), member, bot)
    page = await gateway.discover_discord_channels(
        _ctx(), excluded_channel_ids=frozenset(), cursor=cursor
    )
    assert len(page["sources"]) == 200
    assert page["next_cursor"] == "200"
    assert all(not channel.archive_calls for channel in channels)


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [{"limit": 201}, {"limit": True}, {"cursor": "-1"}])
async def test_channel_discovery_rejects_invalid_pagination(args):
    gateway = DiscordGateway(bot_user_provider=lambda: None)
    with pytest.raises(ValueError):
        await gateway.discover_discord_channels(_ctx(), excluded_channel_ids=frozenset(), **args)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["search", "discovery"])
async def test_channel_scope_rejects_other_guild_context(operation):
    member, bot = _SearchMember(123), _SearchMember(999)
    channel = _SearchChannel(1, "source-server", discord.ChannelType.text)
    guild = _SearchGuild(member, bot, [channel], [])
    gateway = _search_gateway(guild, member, bot)
    ctx = _ctx()

    wrong_ctx = replace(ctx, guild_id="123456")
    with pytest.raises(ValueError, match="scope is unavailable"):
        if operation == "search":
            await gateway.resolve_discord_search_channels(
                wrong_ctx, requested_channel_ids=None, excluded_channel_ids=frozenset()
            )
        else:
            await gateway.discover_discord_channels(wrong_ctx, excluded_channel_ids=frozenset())
    assert not channel.archive_calls


@pytest.mark.asyncio
async def test_discovery_uses_each_invoking_guild_separately():
    gateway = DiscordGateway(bot_user_provider=lambda: None)

    for guild_id, channel_id in [(999, 100), (888, 200)]:
        member, bot = _SearchMember(123), _SearchMember(9999)
        channel = _SearchChannel(channel_id, f"server-{guild_id}", discord.ChannelType.text)
        guild = _SearchGuild(member, bot, [channel], [])
        guild.id = guild_id
        source = _Message(guild_id, member, "discover")
        source.guild = guild
        ctx = replace(
            _ctx(),
            guild_id=str(guild_id),
            context_key=f"guild:{guild_id}",
            trigger_discord_message_id=str(guild_id),
        )
        gateway.bind_turn_source(ctx.context_key, ctx.trigger_discord_message_id, source)
        page = await gateway.discover_discord_channels(ctx, excluded_channel_ids=frozenset())
        assert page["sources"] == {str(channel_id): f"server-{guild_id}"}
        scope = await gateway.resolve_discord_search_channels(
            ctx, requested_channel_ids=None, excluded_channel_ids=frozenset()
        )
        assert scope == page["sources"]


@pytest.mark.asyncio
async def test_full_discovery_page_survives_failing_archive_endpoint():
    member, bot = _SearchMember(123), _SearchMember(999)
    channels = [_FailingArchiveChannel(i, str(i), discord.ChannelType.text) for i in range(1, 201)]
    gateway = _search_gateway(_SearchGuild(member, bot, channels, []), member, bot)
    page = await gateway.discover_discord_channels(_ctx(), excluded_channel_ids=frozenset())
    assert page["sources"] == {str(i): str(i) for i in range(1, 201)}
    assert page["next_cursor"] == "200"
    assert page["has_more"] is True


@pytest.mark.asyncio
async def test_discovery_exact_page_boundary_has_empty_terminal_page():
    member, bot = _SearchMember(123), _SearchMember(999)
    channel = _SearchChannel(1, "general", discord.ChannelType.text)
    gateway = _search_gateway(_SearchGuild(member, bot, [channel], []), member, bot)
    page = await gateway.discover_discord_channels(
        _ctx(), excluded_channel_ids=frozenset(), limit=1
    )
    last = await gateway.discover_discord_channels(
        _ctx(), excluded_channel_ids=frozenset(), cursor=page["next_cursor"], limit=1
    )
    assert last == {"sources": {}, "next_cursor": None, "has_more": False}


class _HistoryChannel(_SearchChannel):
    def __init__(self, id=200, name="development", **kwargs):
        channel_type = kwargs.pop("channel_type", discord.ChannelType.text)
        super().__init__(id, name, channel_type, **kwargs)
        self.messages = [
            SimpleNamespace(
                id=index,
                author=_Author(123, "Alice"),
                content=f"message {index}",
                created_at=datetime.fromtimestamp(index, tz=UTC),
                jump_url=f"https://discord.com/channels/999/{id}/{index}",
                attachments=[],
            )
            for index in range(1, 61)
        ]
        self.history_calls = []
        self.history_error = None

    async def history(self, *, limit, before=None, after=None, around=None, oldest_first=False):
        self.history_calls.append(
            {"limit": limit, "before": before, "after": after, "around": around}
        )
        if self.history_error:
            raise self.history_error
        messages = list(self.messages)
        if around:
            # Model Discord's even-limit quirk, including a missing anchor that
            # still returns nearby messages from the selected channel.
            messages.sort(key=lambda message: abs(message.id - around.id))
            messages = messages[: 2 * (limit // 2) + 1]
        else:
            if before:
                messages = [
                    message
                    for message in messages
                    if (
                        message.created_at < before
                        if isinstance(before, datetime)
                        else message.id < before.id
                    )
                ]
            if after:
                messages = [
                    message
                    for message in messages
                    if (
                        message.created_at > after
                        if isinstance(after, datetime)
                        else message.id > after.id
                    )
                ]
        messages.sort(key=lambda message: message.id, reverse=not oldest_first)
        for message in messages if around else messages[:limit]:
            yield message


def _history_setup(*, target=None, excluded=frozenset()):
    member, bot = _SearchMember(123), _SearchMember(999)
    current = _HistoryChannel(100, "general")
    target = target or _HistoryChannel()
    guild = _SearchGuild(member, bot, [current, target], [])
    gateway = _search_gateway(guild, member, bot)
    gateway._search_excluded_channel_ids = excluded
    return gateway, guild, current, target


@pytest.mark.asyncio
@pytest.mark.parametrize("selector", ["development", "#Development", "200", "<#200>"])
async def test_history_reads_last_30_messages_from_selected_channel(selector):
    gateway, _guild, current, target = _history_setup()

    result = await gateway.collect_channel_history(_ctx(), {"channel": selector, "limit": 30})

    assert result["channel_id"] == "200"
    assert [message["id"] for message in result["messages"]] == [str(i) for i in range(60, 30, -1)]
    assert result["next_cursor"] == "31"
    assert current.history_calls == []
    assert len(target.history_calls) == 1


@pytest.mark.asyncio
async def test_history_without_channel_selection_uses_current_channel():
    gateway, _guild, current, target = _history_setup()

    result = await gateway.collect_channel_history(_ctx(), {"order": "desc", "limit": 30})

    assert result["channel_id"] == "100"
    assert len(current.history_calls) == 1
    assert target.history_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("selector", ["development", "200", "<#200>"])
@pytest.mark.parametrize("restriction", ["member", "bot", "excluded", "other_guild"])
async def test_history_selection_rejects_unreadable_and_other_guild_channels(selector, restriction):
    gateway, _guild, current, target = _history_setup()
    if restriction == "member":
        target.denied_ids.add(123)
    elif restriction == "bot":
        target.denied_ids.add(999)
    elif restriction == "excluded":
        gateway._search_excluded_channel_ids = frozenset({"200"})
    else:
        target.guild = SimpleNamespace(id=1000)

    with pytest.raises(ValueError, match="unavailable"):
        await gateway.collect_channel_history(
            _ctx(), {"channel": selector, "around_message_id": "30"}
        )

    assert current.history_calls == []
    assert target.history_calls == []


@pytest.mark.asyncio
async def test_history_channel_names_are_resolved_in_each_request_guild():
    first, _guild, _current, target = _history_setup()
    second, other_guild, _other_current, other_target = _history_setup(target=_HistoryChannel(300))
    other_guild.id = 1000

    first_page = await first.collect_channel_history(_ctx(), {"channel": "development"})
    second_page = await second.collect_channel_history(
        replace(_ctx(), guild_id="1000"), {"channel": "development"}
    )

    assert first_page["channel_id"] == "200"
    assert second_page["channel_id"] == "300"
    assert len(target.history_calls) == len(other_target.history_calls) == 1


@pytest.mark.asyncio
async def test_history_rejects_ambiguous_names_but_accepts_explicit_id():
    gateway, guild, current, target = _history_setup()
    current.name = target.name

    with pytest.raises(ValueError, match="Multiple accessible channels"):
        await gateway.collect_channel_history(_ctx(), {"channel": "development"})
    assert current.history_calls == target.history_calls == []

    result = await gateway.collect_channel_history(_ctx(), {"channel_id": "200"})
    assert result["channel_id"] == "200"
    current.denied_ids.add(123)
    result = await gateway.collect_channel_history(_ctx(), {"channel": "development"})
    assert result["channel_id"] == "200"


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [True, False])
@pytest.mark.parametrize("selector", ["private-topic", "201"])
async def test_history_active_private_thread_selection_requires_both_members(allowed, selector):
    gateway, guild, _current, target = _history_setup()
    thread = _HistoryChannel(
        201,
        "private-topic",
        channel_type=discord.ChannelType.private_thread,
        parent_id=200,
        thread_member_ids={123, 999} if allowed else {999},
    )
    thread.guild = guild
    guild.threads.append(thread)
    guild._all[201] = thread

    if allowed:
        result = await gateway.collect_channel_history(_ctx(), {"channel": selector})
        assert result["channel_id"] == "201"
    else:
        with pytest.raises(ValueError, match="unavailable"):
            await gateway.collect_channel_history(_ctx(), {"channel": selector})
        assert thread.history_calls == []
    assert target.history_calls == []


@pytest.mark.asyncio
async def test_history_parent_exclusion_covers_thread_selection():
    gateway, guild, _current, target = _history_setup(excluded=frozenset({"100"}))
    target.type = discord.ChannelType.public_thread
    target.parent_id = 100
    guild.channels.remove(target)
    guild.threads.append(target)

    for args in ({"channel": "development"}, {"channel_id": "200"}):
        with pytest.raises(ValueError, match="unavailable"):
            await gateway.collect_channel_history(_ctx(), args)
    assert target.history_calls == []


@pytest.mark.asyncio
async def test_scheduled_history_uses_owners_guild_identity_for_channel_selection():
    gateway, guild, _current, _target = _history_setup()
    member = _SearchMember(123)
    member.guild = guild
    ctx = replace(_ctx(), scheduled_run_id="run-1", platform_member=member)

    result = await gateway.collect_channel_history(ctx, {"channel": "development", "limit": 30})
    assert result["channel_id"] == "200"

    with pytest.raises(ValueError, match="identity is unavailable"):
        await gateway.collect_channel_history(replace(ctx, guild_id="1000"), {"channel_id": "200"})


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 2, 21, 30, 100])
@pytest.mark.parametrize("order", ["asc", "desc"])
async def test_message_context_keeps_anchor_and_both_sides_within_requested_count(limit, order):
    gateway, _guild, current, target = _history_setup()
    args = {"channel_id": "200", "around_message_id": "30", "limit": limit, "order": order}

    result = await gateway.collect_channel_history(_ctx(), args)
    ids = [int(message["id"]) for message in result["messages"]]

    assert 30 in ids
    assert ids == sorted(ids, reverse=order == "desc")
    assert len(ids) <= limit
    if limit >= 3:
        assert 29 in ids and 31 in ids
    assert target.history_calls[0]["around"].id == 30
    assert current.history_calls == []

    older = await gateway.collect_channel_history(_ctx(), result["older"])
    newer = await gateway.collect_channel_history(_ctx(), result["newer"])
    assert all(int(message["id"]) < min(ids) for message in older["messages"])
    assert all(int(message["id"]) > max(ids) for message in newer["messages"])
    if min(ids) > 1:
        assert int(older["messages"][0]["id"]) == min(ids) - 1
    if max(ids) < 60:
        assert int(newer["messages"][0]["id"]) == max(ids) + 1


@pytest.mark.asyncio
async def test_message_context_preserves_anchor_when_text_budget_is_reached():
    gateway, _guild, _current, target = _history_setup()
    for message in target.messages:
        message.content = "x" * 4000

    result = await gateway.collect_channel_history(
        _ctx(), {"channel_id": "200", "around_message_id": "30", "limit": 30}
    )

    assert result["truncated"] is True
    assert "30" in [message["id"] for message in result["messages"]]
    assert sum(len(json.dumps(message)) for message in result["messages"]) <= 32_000


@pytest.mark.asyncio
async def test_message_context_rejects_oversized_anchor_instead_of_exceeding_text_budget():
    gateway, _guild, _current, target = _history_setup()
    target.messages[29].content = "x" * 32_000

    with pytest.raises(ValueError, match="exceeds the channel context text budget"):
        await gateway.collect_channel_history(
            _ctx(), {"channel_id": "200", "around_message_id": "30"}
        )


@pytest.mark.asyncio
async def test_message_context_rejects_missing_anchor_in_selected_channel():
    gateway, _guild, _current, target = _history_setup()
    target.messages = [message for message in target.messages if message.id != 30]

    with pytest.raises(ValueError, match="Message unavailable in the selected channel"):
        await gateway.collect_channel_history(
            _ctx(), {"channel_id": "200", "around_message_id": "30"}
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    [
        {"channel": "development", "channel_id": "200"},
        {"channel": ["development"]},
        {"channel": []},
        {"channel": False},
        {"channel": "<#-1>"},
        {"channel": "#"},
        {"channel_id": str(2**64)},
        {"channel_id": "200", "around_message_id": "-1"},
        {"channel_id": "200", "around_message_id": "0"},
        {"channel_id": "200", "around_message_id": "１２３"},
        {"channel_id": "200", "around_message_id": "30", "cursor": "40"},
        {"channel_id": "200", "around_message_id": "30", "before": "2026-09-10T00:00:00Z"},
        {"channel_id": "200", "around_message_id": "30", "after": "2026-09-10T00:00:00Z"},
        {"channel_id": "200", "around_message_id": "30", "order": "random"},
    ],
)
async def test_invalid_history_selectors_and_mixed_windows_never_fetch_messages(args):
    gateway, _guild, current, target = _history_setup()

    with pytest.raises(ValueError):
        await gateway.collect_channel_history(_ctx(), args)
    assert current.history_calls == target.history_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [{}, {"around_message_id": "30"}])
async def test_history_http_failure_is_reported_safely(extra):
    gateway, _guild, _current, target = _history_setup()
    target.history_error = discord.NotFound(_FakeResponse(), "internal provider details")

    with pytest.raises(DiscordGatewayError, match="Could not read channel history"):
        await gateway.collect_channel_history(_ctx(), {"channel_id": "200", **extra})


def test_dashboard_source_binding_is_root_scoped_without_discord_message_id() -> None:
    gateway = DiscordGateway(bot_user_provider=lambda: None)
    ctx = _ctx()
    ctx.context_key = "dashboard:999:123:opaque"
    ctx.trigger_discord_message_id = ""
    source = object()
    binding = gateway.bind_turn_source(ctx.context_key, "", source)
    assert gateway._turn_source(ctx) is source
    ctx.context_key = "dashboard:999:123:another"
    assert gateway._turn_source(ctx) is None
    assert gateway.bind_turn_source("guild:100:main", "", source) is None
    gateway.unbind_turn_source(binding)
    ctx.context_key = "dashboard:999:123:opaque"
    assert gateway._turn_source(ctx) is None
