from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import discord

from agent.backfill import BackfilledMessage, collect_channel_context
from agent.discord_references import ResolvedDiscordReferenceHint
from discord_adapter.reference_hints import resolve_discord_reference_hints
from discord_adapter.io import (
    AttachmentDeliveryPlan,
    prepare_attachment_delivery,
    send_prepared_response as send_prepared_discord_response,
    send_response as send_discord_response,
)
from tools.registry import MessageContext
from trust.tiers import TrustTier

if TYPE_CHECKING:
    from tools.embeds import EmbedSpec
    from trust.resolver import TrustResolver

log = logging.getLogger(__name__)

MEMBER_QUERY_LIMIT = 10
MAX_MEMBER_CANDIDATES = 3
MAX_MEMBER_ROLES = 10
_DISCORD_SEARCH_CHANNEL_LIMIT = 500
_DISCORD_SEARCH_ARCHIVE_CACHE_TTL_SECONDS = 30.0
_DISCORD_SEARCH_ARCHIVE_CACHE_MAX_ENTRIES = 1024
_SEARCH_THREAD_CHANNEL_TYPES = frozenset(
    {
        discord.ChannelType.news_thread,
        discord.ChannelType.public_thread,
        discord.ChannelType.private_thread,
    }
)
_SEARCH_MESSAGE_CHANNEL_TYPES = frozenset(
    {
        discord.ChannelType.text,
        discord.ChannelType.voice,
        discord.ChannelType.news,
        discord.ChannelType.news_thread,
        discord.ChannelType.public_thread,
        discord.ChannelType.private_thread,
        discord.ChannelType.stage_voice,
        discord.ChannelType.forum,
        discord.ChannelType.media,
    }
)


class DiscordGatewayError(RuntimeError):
    """User-safe Discord gateway failure."""


@dataclass
class MemberProfile:
    """Primitives-only profile for a resolved member; no ``discord.*`` types leak out."""

    user_id: str
    username: str
    display_name: str
    is_bot: bool
    avatar_url: str
    account_created_at: str | None
    joined_at: str | None
    roles: list[str]
    role_count: int
    # Resolved trust tier, disclosed to STAFF callers only (None = redacted): the
    # resolved tier can reveal hidden STAFF_USER_IDS allowlist membership (role-less
    # staff), unlike roles, which are already public in Discord.
    trust_tier: str | None


@dataclass
class MemberCandidate:
    """Slim disambiguation entry returned when a name query is not an exact match."""

    user_id: str
    username: str
    display_name: str


@dataclass
class MemberLookup:
    """Result of a member lookup: an exact profile, candidates, or nothing matched."""

    match: str  # "exact" | "candidates" | "none"
    profile: MemberProfile | None = None
    candidates: list[MemberCandidate] = field(default_factory=list)


@dataclass(frozen=True)
class TurnSourceSnapshot:
    """Primitives-only view of the bound trigger message; no ``discord.*`` types leak out."""

    content: str
    author_id: str
    is_bot: bool


@dataclass(frozen=True)
class TurnSourceBinding:
    """Owner token for one live trigger-message binding."""

    key: tuple[str, str]
    binding_id: int


class DiscordGateway:
    """Narrow adapter for live Discord operations used by bot runtime and tools."""

    def __init__(
        self,
        *,
        bot_user_provider: Callable[[], Any | None],
        trust_resolver: TrustResolver | None = None,
    ) -> None:
        self._bot_user_provider = bot_user_provider
        self._trust_resolver = trust_resolver
        self._turn_sources: dict[tuple[str, str], dict[int, Any]] = {}
        self._next_turn_source_binding_id = 0
        self._discord_search_archive_cache: OrderedDict[
            tuple[str, str], tuple[float, tuple[Any, ...]]
        ] = OrderedDict()
        self._discord_search_archive_locks: dict[tuple[str, str], asyncio.Lock] = {}

    def bind_turn_source(
        self,
        context_key: str,
        trigger_discord_message_id: str,
        message: Any,
    ) -> TurnSourceBinding | None:
        if not context_key or not trigger_discord_message_id:
            return None
        key = (context_key, trigger_discord_message_id)
        self._next_turn_source_binding_id += 1
        binding = TurnSourceBinding(
            key=key,
            binding_id=self._next_turn_source_binding_id,
        )
        self._turn_sources.setdefault(key, {})[binding.binding_id] = message
        return binding

    def unbind_turn_source(
        self,
        binding: TurnSourceBinding | None,
    ) -> None:
        if binding is None:
            return
        sources = self._turn_sources.get(binding.key)
        if sources is None:
            return
        sources.pop(binding.binding_id, None)
        if not sources:
            self._turn_sources.pop(binding.key, None)

    def _turn_source(self, ctx: MessageContext) -> Any | None:
        sources = self._turn_sources.get((ctx.context_key, ctx.trigger_discord_message_id))
        if not sources:
            return None
        # Concurrent duplicate invocations normally bind the same Discord
        # message. Prefer the newest live lease, while retaining older leases so
        # one turn's finally block cannot tear down another turn's source.
        newest_id = next(reversed(sources))
        return sources[newest_id]

    def read_turn_source(self, ctx: MessageContext) -> TurnSourceSnapshot | None:
        """Primitives-only snapshot of the message that triggered this turn, or None.

        Returns None when the source binding is gone (e.g. the turn already finished).
        """
        source = self._turn_source(ctx)
        if source is None:
            return None
        author = getattr(source, "author", None)
        return TurnSourceSnapshot(
            content=str(getattr(source, "content", "") or ""),
            author_id=str(getattr(author, "id", "") or ""),
            is_bot=bool(getattr(author, "bot", False)),
        )

    async def resolve_reference_hints(
        self,
        source_message: object,
        content: str,
        *,
        excluded_channel_ids: frozenset[str],
    ) -> tuple[ResolvedDiscordReferenceHint, ...]:
        """Resolve automatic hints without exposing inaccessible Discord metadata."""

        return await resolve_discord_reference_hints(
            source_message,
            content,
            excluded_channel_ids=excluded_channel_ids,
        )

    async def resolve_discord_search_channels(
        self,
        ctx: MessageContext,
        *,
        requested_channel_ids: tuple[str, ...] | None,
        excluded_channel_ids: frozenset[str],
    ) -> dict[str, str]:
        """Resolve a positive, caller-readable channel filter for guild search."""
        guild, member, bot_member = self._discord_search_actors(ctx)
        try:
            if requested_channel_ids is not None:
                return await self._resolve_requested_search_channels(
                    guild,
                    member,
                    bot_member,
                    requested_channel_ids,
                    excluded_channel_ids,
                )
            return await self._resolve_all_search_channels(
                guild,
                member,
                bot_member,
                excluded_channel_ids,
            )
        except ValueError:
            raise
        except Exception as exc:
            log.warning("Could not resolve Discord search channel scope", exc_info=True)
            raise ValueError("Discord search channel scope is unavailable.") from exc

    def _discord_search_actors(self, ctx: MessageContext) -> tuple[Any, Any, Any]:
        source = self._turn_source(ctx)
        guild = getattr(source, "guild", None)
        member = getattr(source, "author", None)
        if (
            source is None
            or guild is None
            or str(getattr(guild, "id", "")) != str(ctx.guild_id or "")
            or str(getattr(member, "id", "")) != ctx.user_id
        ):
            raise ValueError("Discord search channel scope is unavailable.")
        member_guild = getattr(member, "guild", guild)
        if str(getattr(member_guild, "id", "")) != str(ctx.guild_id):
            raise ValueError("Discord search channel scope is unavailable.")
        bot_member = getattr(guild, "me", None)
        if bot_member is None:
            raise ValueError("Discord search channel scope is unavailable.")
        return guild, member, bot_member

    async def _resolve_requested_search_channels(
        self,
        guild: Any,
        member: Any,
        bot_member: Any,
        requested_channel_ids: tuple[str, ...],
        excluded_channel_ids: frozenset[str],
    ) -> dict[str, str]:
        channels: dict[str, str] = {}
        unavailable = "One or more channels are unavailable for Discord text search."
        for requested_id in requested_channel_ids:
            channel = guild.get_channel_or_thread(int(requested_id))
            if channel is None:
                try:
                    channel = await guild.fetch_channel(int(requested_id))
                except (
                    discord.Forbidden,
                    discord.NotFound,
                    discord.InvalidData,
                    discord.HTTPException,
                ) as exc:
                    raise ValueError(unavailable) from exc
            if (
                str(getattr(channel, "id", "")) != requested_id
                or str(getattr(getattr(channel, "guild", None), "id", "")) != str(guild.id)
                or not _is_search_message_channel(channel)
                or _search_channel_is_excluded(channel, excluded_channel_ids)
                or not await _search_channel_accessible(channel, member, bot_member)
            ):
                raise ValueError(unavailable)
            channels[requested_id] = _search_channel_name(channel)
        return channels

    async def _resolve_all_search_channels(
        self,
        guild: Any,
        member: Any,
        bot_member: Any,
        excluded_channel_ids: frozenset[str],
    ) -> dict[str, str]:
        channels: dict[str, str] = {}
        archive_parents: list[Any] = []
        for channel in guild.channels:
            if _search_channel_is_excluded(channel, excluded_channel_ids):
                continue
            if _is_search_message_channel(channel) and await _search_channel_accessible(
                channel, member, bot_member
            ):
                channels[str(channel.id)] = _search_channel_name(channel)
                _check_discord_search_scope_size(channels)
            if hasattr(channel, "archived_threads") and await _search_channel_accessible(
                channel, member, bot_member
            ):
                archive_parents.append(channel)

        for thread in guild.threads:
            await _add_search_thread(
                channels,
                thread,
                member,
                bot_member,
                excluded_channel_ids,
            )
            _check_discord_search_scope_size(channels)

        for parent in archive_parents:
            async for thread in self._iter_archived_search_threads(parent, bot_member):
                await _add_search_thread(
                    channels,
                    thread,
                    member,
                    bot_member,
                    excluded_channel_ids,
                )
                _check_discord_search_scope_size(channels)
        return channels

    async def _iter_archived_search_threads(
        self,
        parent: Any,
        bot_member: Any,
    ) -> AsyncIterator[Any]:
        discovery_mode = _archive_discovery_mode(parent, bot_member)
        cache_key = (str(getattr(parent, "id", "")), discovery_mode)
        cached_threads = self._cached_discord_search_archives(cache_key)
        if cached_threads is not None:
            for thread in cached_threads:
                yield thread
            return

        lock = self._discord_search_archive_locks.setdefault(cache_key, asyncio.Lock())
        cached_after_lock: tuple[Any, ...] | None = None
        async with lock:
            cached_threads = self._cached_discord_search_archives(cache_key)
            if cached_threads is not None:
                cached_after_lock = cached_threads
            else:
                discovered: list[Any] = []
                completed = False
                try:
                    async for thread in parent.archived_threads(limit=None):
                        discovered.append(thread)
                        yield thread
                    if discovery_mode != "public":
                        async for thread in parent.archived_threads(
                            private=True,
                            joined=discovery_mode == "joined_private",
                            limit=None,
                        ):
                            discovered.append(thread)
                            yield thread
                    completed = True
                finally:
                    if completed:
                        self._store_discord_search_archives(cache_key, tuple(discovered))
        if cached_after_lock is not None:
            for thread in cached_after_lock:
                yield thread

    def _cached_discord_search_archives(
        self,
        cache_key: tuple[str, str],
    ) -> tuple[Any, ...] | None:
        self._prune_discord_search_archive_cache()
        cached = self._discord_search_archive_cache.get(cache_key)
        if cached is None:
            return None
        self._discord_search_archive_cache.move_to_end(cache_key)
        return cached[1]

    def _store_discord_search_archives(
        self,
        cache_key: tuple[str, str],
        threads: tuple[Any, ...],
    ) -> None:
        self._prune_discord_search_archive_cache()
        self._discord_search_archive_cache[cache_key] = (
            time.monotonic() + _DISCORD_SEARCH_ARCHIVE_CACHE_TTL_SECONDS,
            threads,
        )
        self._discord_search_archive_cache.move_to_end(cache_key)
        while len(self._discord_search_archive_cache) > _DISCORD_SEARCH_ARCHIVE_CACHE_MAX_ENTRIES:
            self._discord_search_archive_cache.popitem(last=False)
        self._prune_discord_search_archive_locks()

    def _prune_discord_search_archive_cache(self) -> None:
        now = time.monotonic()
        for key, (expires_at, _) in list(self._discord_search_archive_cache.items()):
            if expires_at <= now:
                self._discord_search_archive_cache.pop(key, None)
        self._prune_discord_search_archive_locks()

    def _prune_discord_search_archive_locks(self) -> None:
        for key, lock in list(self._discord_search_archive_locks.items()):
            if key not in self._discord_search_archive_cache and not lock.locked():
                self._discord_search_archive_locks.pop(key, None)

    async def collect_recent_channel_context(
        self,
        ctx: MessageContext,
        *,
        limit: int = 15,
    ) -> list[BackfilledMessage]:
        source = self._turn_source(ctx)
        if source is None:
            raise DiscordGatewayError("Current Discord source is unavailable.")
        channel = getattr(source, "channel", None)
        if channel is None or not hasattr(channel, "history"):
            raise DiscordGatewayError("Current Discord channel history is unavailable.")
        try:
            return await collect_channel_context(
                channel,
                before=source,
                limit=max(1, min(int(limit), 100)),
                bot_user=self._bot_user_provider(),
            )
        except DiscordGatewayError:
            raise
        except Exception as exc:
            log.warning("Could not read recent Discord channel context", exc_info=True)
            raise DiscordGatewayError("Could not read recent channel context.") from exc

    async def resolve_member(
        self,
        ctx: MessageContext,
        *,
        user_id: str | None = None,
        query: str | None = None,
    ) -> MemberLookup:
        source = self._turn_source(ctx)
        if source is None:
            raise DiscordGatewayError("Current Discord source is unavailable.")
        guild = getattr(source, "guild", None)
        if guild is None:
            raise DiscordGatewayError("Member lookup is only available in a server.")

        try:
            if user_id:
                member = await self._fetch_member_by_id(guild, user_id)
                if member is None:
                    return MemberLookup(match="none")
                return MemberLookup(match="exact", profile=self._profile(member, ctx.trust_tier))

            members = list(await guild.query_members(query=str(query), limit=MEMBER_QUERY_LIMIT))
        except DiscordGatewayError:
            raise
        except Exception as exc:
            log.warning("Could not resolve Discord member", exc_info=True)
            raise DiscordGatewayError("Could not look up that member.") from exc

        if not members:
            return MemberLookup(match="none")
        exact = self._find_exact(members, str(query))
        if len(exact) == 1:
            return MemberLookup(match="exact", profile=self._profile(exact[0], ctx.trust_tier))
        # More than one exact name match: never silently pick one (gateway order is
        # unspecified and display names are self-chosen), surface the ambiguity.
        ambiguous = exact if exact else members
        candidates = [self._candidate(m) for m in ambiguous[:MAX_MEMBER_CANDIDATES]]
        return MemberLookup(match="candidates", candidates=candidates)

    async def _fetch_member_by_id(self, guild: Any, user_id: str) -> Any | None:
        try:
            uid = int(user_id)
        except TypeError, ValueError:
            return None
        member = guild.get_member(uid)
        if member is not None:
            return member
        fetch = getattr(guild, "fetch_member", None)
        if fetch is None:
            return None
        try:
            return await fetch(uid)
        except discord.NotFound:
            return None

    @staticmethod
    def _find_exact(members: list[Any], query: str) -> list[Any]:
        """All members whose username or display name casefold-equals the query.

        A single username match wins outright: display names (nicknames/global
        names) are self-chosen, so a nickname set to another member's username
        must not be able to hijack the exact match. With no unique username
        match, every exact match is returned so the caller can disambiguate.
        """
        needle = query.casefold()
        exact = [
            member
            for member in members
            if needle
            in {
                str(getattr(member, "name", "")).casefold(),
                str(getattr(member, "display_name", "")).casefold(),
            }
        ]
        by_username = [
            member for member in exact if str(getattr(member, "name", "")).casefold() == needle
        ]
        if len(by_username) == 1:
            return by_username
        return exact

    @staticmethod
    def _candidate(member: Any) -> MemberCandidate:
        return MemberCandidate(
            user_id=str(member.id),
            username=str(getattr(member, "name", "")),
            display_name=str(getattr(member, "display_name", "")),
        )

    def _profile(self, member: Any, caller_tier: TrustTier) -> MemberProfile:
        roles = [r for r in getattr(member, "roles", []) if not _is_default_role(r)]
        roles.sort(key=lambda r: getattr(r, "position", 0), reverse=True)
        # Redacted below STAFF: the resolved tier would disclose hidden
        # STAFF_USER_IDS allowlist membership (role-less staff) to any member.
        trust_tier: str | None = None
        if caller_tier >= TrustTier.STAFF and self._trust_resolver is not None:
            member_guild_id = getattr(getattr(member, "guild", None), "id", None)
            guild_id = str(member_guild_id) if member_guild_id is not None else None
            trust_tier = self._trust_resolver.resolve(member, str(member.id), guild_id).value
        return MemberProfile(
            user_id=str(member.id),
            username=str(getattr(member, "name", "")),
            display_name=str(getattr(member, "display_name", "")),
            is_bot=bool(getattr(member, "bot", False)),
            avatar_url=_avatar_url(member),
            account_created_at=_iso(getattr(member, "created_at", None)),
            joined_at=_iso(getattr(member, "joined_at", None)),
            roles=[str(r.name) for r in roles[:MAX_MEMBER_ROLES]],
            role_count=len(roles),
            trust_tier=trust_tier,
        )

    async def send_response(
        self,
        channel: discord.abc.Messageable,
        content: str,
        *,
        reference: discord.Message | None = None,
        output_files: list[str] | None = None,
        output_file_descriptions: dict[str, str] | None = None,
        allowed_file_roots: list[str | Path] | None = None,
        embed: EmbedSpec | None = None,
        mention_author: bool = False,
    ) -> list[discord.Message]:
        return await send_discord_response(
            channel,
            content,
            reference=reference,
            output_files=output_files,
            output_file_descriptions=output_file_descriptions,
            allowed_file_roots=allowed_file_roots,
            embed=embed,
            mention_author=mention_author,
        )

    def prepare_attachment_delivery(
        self,
        channel: discord.abc.Messageable,
        *,
        output_files: list[str],
        allowed_file_roots: list[str | Path] | None,
        embed: EmbedSpec | None,
        effective_limit_bytes: int | None = None,
        notice_text: str | None = None,
    ) -> AttachmentDeliveryPlan:
        return prepare_attachment_delivery(
            channel,
            output_files=output_files,
            allowed_file_roots=allowed_file_roots,
            embed=embed,
            effective_limit_bytes=effective_limit_bytes,
            notice_text=notice_text,
        )

    async def send_prepared_response(
        self,
        channel: discord.abc.Messageable,
        content: str,
        plan: AttachmentDeliveryPlan,
        *,
        reference: discord.Message | None = None,
        mention_author: bool = False,
    ) -> list[discord.Message]:
        return await send_prepared_discord_response(
            channel,
            content,
            plan,
            reference=reference,
            mention_author=mention_author,
        )

    async def add_status_reaction(self, message: Any, emoji: str) -> None:
        try:
            await message.add_reaction(emoji)
        except discord.HTTPException:
            log.debug("Could not add Discord status reaction %s", emoji, exc_info=True)

    async def remove_status_reaction(self, message: Any, emoji: str) -> None:
        bot_user = self._bot_user_provider()
        if bot_user is None:
            return
        try:
            await message.remove_reaction(emoji, bot_user)
        except discord.HTTPException:
            log.debug("Could not remove Discord status reaction %s", emoji, exc_info=True)


def _is_default_role(role: Any) -> bool:
    is_default = getattr(role, "is_default", None)
    if callable(is_default):
        try:
            return bool(is_default())
        except Exception:
            return False
    return False


def _is_search_message_channel(channel: Any) -> bool:
    return getattr(channel, "type", None) in _SEARCH_MESSAGE_CHANNEL_TYPES


def _search_channel_is_excluded(channel: Any, excluded_channel_ids: frozenset[str]) -> bool:
    channel_id = str(getattr(channel, "id", ""))
    parent_id = str(getattr(channel, "parent_id", "") or "")
    return channel_id in excluded_channel_ids or (
        getattr(channel, "type", None) in _SEARCH_THREAD_CHANNEL_TYPES
        and parent_id in excluded_channel_ids
    )


def _search_channel_name(channel: Any) -> str:
    return str(getattr(channel, "name", "") or "")


def _archive_discovery_mode(parent: Any, bot_member: Any) -> str:
    if getattr(parent, "type", None) is not discord.ChannelType.text:
        return "public"
    bot_permissions = parent.permissions_for(bot_member)
    if bool(getattr(bot_permissions, "manage_threads", False)):
        return "all_private"
    return "joined_private"


async def _search_channel_accessible(channel: Any, member: Any, bot_member: Any) -> bool:
    permissions_for = getattr(channel, "permissions_for", None)
    if not callable(permissions_for):
        return False
    try:
        member_permissions = permissions_for(member)
        bot_permissions = permissions_for(bot_member)
    except AttributeError, TypeError:
        return False
    if not all(
        bool(getattr(permissions, "view_channel", False))
        and bool(getattr(permissions, "read_message_history", False))
        for permissions in (member_permissions, bot_permissions)
    ):
        return False
    if getattr(channel, "type", None) is not discord.ChannelType.private_thread:
        return True

    need_member = not bool(getattr(member_permissions, "manage_threads", False))
    need_bot = not bool(getattr(bot_permissions, "manage_threads", False))
    if not need_member and not need_bot:
        return True
    if need_member and not await _private_thread_has_member(channel, member):
        return False
    return not need_bot or await _private_thread_has_member(channel, bot_member)


async def _private_thread_has_member(channel: Any, member: Any) -> bool:
    fetch_member = getattr(channel, "fetch_member", None)
    member_id = getattr(member, "id", None)
    if not callable(fetch_member) or member_id is None:
        return False
    try:
        await fetch_member(int(member_id))
        return True
    except discord.Forbidden, discord.NotFound, discord.HTTPException:
        return False


async def _add_search_thread(
    channels: dict[str, str],
    thread: Any,
    member: Any,
    bot_member: Any,
    excluded_channel_ids: frozenset[str],
) -> None:
    channel_id = str(getattr(thread, "id", ""))
    if (
        not channel_id
        or channel_id in channels
        or _search_channel_is_excluded(thread, excluded_channel_ids)
        or not await _search_channel_accessible(thread, member, bot_member)
    ):
        return
    channels[channel_id] = _search_channel_name(thread)


def _check_discord_search_scope_size(channels: dict[str, str]) -> None:
    if len(channels) > _DISCORD_SEARCH_CHANNEL_LIMIT:
        raise ValueError(
            "Discord text search can search at most 500 channels at once. "
            "Pass a narrower comma-separated channels filter."
        )


def _iso(value: Any) -> str | None:
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return str(isoformat())
    return None


def _avatar_url(member: Any) -> str:
    avatar = getattr(member, "display_avatar", None)
    if avatar is None:
        return ""
    url = getattr(avatar, "url", None)
    return str(url) if url else str(avatar)
