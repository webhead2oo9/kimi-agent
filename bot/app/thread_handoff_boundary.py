"""Discord-facing half of thread handoff.

``app/threads.py:ThreadHandoffManager`` owns the persistent enrollment state and
is deliberately Discord-free; everything that has to touch ``discord.py`` to
create, target, point at, or close a managed thread lives here. Splitting it out
keeps ``app/runtime.py`` a composition root and event dispatcher rather than
also being the thread-handoff implementation.

The manager is reached through a callable rather than held directly: it is
constructed during ``on_ready`` (and stays ``None`` when handoff is disabled),
so this boundary must read it live on every call.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal
from weakref import WeakValueDictionary

import discord

from config.fragments.channel_pins import (
    load_channel_thread_auto_respond,
    load_channel_thread_handoff,
    resolve_tristate,
)
from utils.format import sanitize_author_name
from config.fragments.guild_config import (
    load_guild_blocked_tools,
    load_guild_thread_auto_respond,
    load_guild_thread_handoff,
    load_guild_thread_targets,
)
from app.turn_entry import resolve_parent_channel_id
from config.fragments.tool_policy import (
    load_blocked_tools,
    load_global_blocked_tools,
    thread_handoff_creation_allowed,
    thread_state_blocked_tools,
)
from tools.registry import MessageContext
from tools.threads import (
    ThreadCloseRequest,
    ThreadRequest,
    ThreadTarget,
    match_thread_target,
)

if TYPE_CHECKING:
    from app.threads import ThreadHandoffManager
    from config.settings import Settings

log = logging.getLogger(__name__)

THREAD_HANDOFF_CREATE_ATTEMPTS = 2
THREAD_HANDOFF_CREATE_RETRY_DELAY_SECONDS = 2.5
THREAD_HANDOFF_REACTION = "🧵"

# A cross-channel thread hangs off an anchor message in the target channel. The
# asker is named in plaintext rather than mentioned: the ping they get is the
# pointer reply back in the channel they were already reading, so the anchor
# introduces the thread to the target channel without notifying anyone there.
CROSS_CHANNEL_ANCHOR = "Hey {name}, brought your question over here! 🧵"
# Sent back in the source channel as a reply to the asker, which is what
# actually notifies them; Thread.add_user only puts it in their sidebar.
CROSS_CHANNEL_POINTER = "Moved this over to {thread}, see you there! 🧵"
# A cross-channel thread is opened for someone who is not looking at that
# channel, so an abandoned one should tidy itself away rather than linger: 24h
# over Discord's 3-day/1-week options. Archiving is not deletion: nothing is
# lost, and a reply un-archives it.
CROSS_CHANNEL_ARCHIVE_MINUTES: Literal[1440] = 1440


def _can_open_thread(permissions: discord.Permissions) -> bool:
    """Whether this permission set allows opening and using a thread here.

    Applied to the asker and to the bot alike, which is what makes cross-channel
    targeting an escalation-free affordance: it can only reach channels the
    person asking could already have posted in themselves.
    """
    return (
        permissions.view_channel
        and permissions.send_messages
        and permissions.create_public_threads
        and permissions.send_messages_in_threads
    )


async def _delete_message_quietly(message: discord.Message | discord.PartialMessage) -> None:
    """Best-effort cleanup of a message the turn no longer wants to have sent."""
    try:
        await message.delete()
    except discord.HTTPException:
        log.warning("Could not delete message %s", message.id, exc_info=True)


class ThreadHandoffBoundary:
    """Thread-handoff operations that need the live Discord client."""

    def __init__(
        self,
        *,
        get_bot: Callable[[], Any],
        settings: Settings,
        get_manager: Callable[[], ThreadHandoffManager | None],
    ) -> None:
        self._get_bot = get_bot
        self.settings = settings
        self._get_manager = get_manager
        self._thread_creation_locks: WeakValueDictionary[tuple[int, int], asyncio.Lock] = (
            WeakValueDictionary()
        )

    @property
    def bot(self) -> Any:
        """Live client reference: ``KimiApplication.bot`` is rebindable."""
        return self._get_bot()

    @property
    def thread_handoff(self) -> ThreadHandoffManager | None:
        """Live manager reference; ``None`` until on_ready builds it."""
        return self._get_manager()

    def _thread_state_blocked_tools(self, message: discord.Message) -> frozenset[str]:
        """Mask the thread-state tools this turn has nothing to act on.

        Outside a managed thread that hides all of them; inside one it hides
        whichever of pause/resume does not apply, so exactly one of the pair is
        ever offered.
        """
        channel = message.channel
        thread_id = channel.id if isinstance(channel, discord.Thread) else None
        manager = self.thread_handoff
        if thread_id is None or manager is None or not manager.is_managed(thread_id):
            return thread_state_blocked_tools(managed=False, auto_responding=False)
        return thread_state_blocked_tools(
            managed=True,
            auto_responding=manager.is_auto_responding(thread_id),
        )

    def _thread_handoff_creation_allowed(self, message: discord.Message) -> bool:
        """Resolve the live creation policy for the message's parent channel."""
        channel_id = resolve_parent_channel_id(message.channel)
        guild_id = str(message.guild.id) if message.guild else ""
        blocked = load_blocked_tools(guild_id, channel_id)
        return thread_handoff_creation_allowed(
            blocked,
            channel=load_channel_thread_handoff(channel_id),
            guild=load_guild_thread_handoff(guild_id),
        )

    def _thread_auto_respond_default(self, message: discord.Message) -> bool:
        """The operator's default mode for a thread opened from this channel.

        Only consulted when the model did not pass ``auto_reply``; whoever is in
        the thread can change its mode afterwards regardless.
        """
        channel_id = resolve_parent_channel_id(message.channel)
        guild_id = str(message.guild.id) if message.guild else ""
        return resolve_tristate(
            load_channel_thread_auto_respond(channel_id),
            load_guild_thread_auto_respond(guild_id),
        )

    def _thread_target_candidates(
        self,
        guild: discord.Guild,
        member: discord.Member,
    ) -> list[ThreadTarget]:
        """The allowlisted channels this member may have a thread opened in.

        The gate cross-channel creation rests on: **the bot does nothing in
        another channel the asker could not do themselves**, so targeting is
        never a way to reach past your own permissions and needs no separate
        rate limit or trust tier of its own.

        Five filters, all fail-closed. A target must be on the guild's
        ``thread_targets`` allowlist, be inside the deployment's channel
        allowlist (a guild fragment must not reach past the boundary that says
        where this bot operates at all), be a plain **non-announcement** text
        channel (a forum post is already a thread, so there is no message to
        anchor one to), be writable by both the member and the bot, and be
        allowed to host a bot thread by its own policy. Being listed here is
        not consent, so a channel that turned handoff off, by either the
        tri-state or a ``move_to_thread`` denylist entry, stays off the list.
        """
        allowed = load_guild_thread_targets(str(guild.id))
        if not allowed:
            return []
        me = guild.me
        if me is None:
            return []
        guild_key = str(guild.id)
        guild_handoff = load_guild_thread_handoff(guild_key)
        # Both scopes are the same for every candidate; read each fragment once
        # and inject it, rather than re-reading them per channel.
        deployment_blocked = load_global_blocked_tools()
        guild_blocked = load_guild_blocked_tools(guild_key)
        deployment_channels = self.settings.allowed_channels
        candidates: list[ThreadTarget] = []
        for raw_id in sorted(allowed):
            channel = guild.get_channel(int(raw_id))
            if channel is None:
                continue
            if not isinstance(channel, discord.TextChannel) or channel.is_news():
                log.warning(
                    "thread_targets entry %s in guild %s is not a plain text channel "
                    "(forums and announcement channels cannot host a bot-opened "
                    "thread); ignoring it",
                    raw_id,
                    guild.id,
                )
                continue
            if deployment_channels and channel.id not in deployment_channels:
                log.warning(
                    "thread_targets entry %s in guild %s is outside the deployment "
                    "channel allowlist; ignoring it",
                    raw_id,
                    guild.id,
                )
                continue
            if not all(_can_open_thread(channel.permissions_for(who)) for who in (member, me)):
                continue
            if not thread_handoff_creation_allowed(
                load_blocked_tools(
                    guild_key,
                    raw_id,
                    load_global=lambda: deployment_blocked,
                    load_guild=lambda _gid: guild_blocked,
                ),
                channel=load_channel_thread_handoff(raw_id),
                guild=guild_handoff,
            ):
                continue
            candidates.append(ThreadTarget(channel_id=channel.id, name=channel.name))
        return candidates

    def resolve_thread_target(self, ctx: MessageContext, raw: str) -> ThreadTarget:
        """Resolve a written channel reference for ``move_to_thread``.

        The seam ``tools/threads.py`` is given at registration: it supplies the
        Discord state (allowlist, channel types, permissions) and delegates the
        actual matching to the pure ``match_thread_target``. Raises
        ``ValueError``, whose message becomes the tool error the model reads and
        can correct against in the same turn.
        """
        if not ctx.guild_id:
            raise ValueError("I can only start a thread in another channel inside a server.")
        guild = self.bot.get_guild(int(ctx.guild_id))
        if guild is None:
            raise ValueError("I can't see this server's channels right now.")
        # Message.author is a concrete Member even when the privileged Members
        # intent/cache is unavailable. Prefer that turn-scoped object; the cache
        # lookup is a fallback for direct callers.
        member = ctx.platform_member
        if not (
            isinstance(member, discord.Member)
            and member.id == int(ctx.user_id)
            and member.guild.id == guild.id
        ):
            member = guild.get_member(int(ctx.user_id)) if ctx.user_id.isdigit() else None
        if member is None:
            # Without the member we cannot check what they may do, and the whole
            # gate is "nothing they couldn't do themselves", so refuse.
            raise ValueError("I can't check where you're able to post right now.")
        return match_thread_target(raw, self._thread_target_candidates(guild, member))

    def can_manage_thread(self, ctx: MessageContext, thread_id: int) -> bool:
        """Check the caller's effective Discord Manage Threads permission.

        Creator and configured STAFF authorization live in ``tools/threads.py``;
        this Discord-aware seam covers native permission overwrites without
        importing Discord into the tool module. Every failed identity or cache
        lookup is a refusal.
        """
        if not ctx.guild_id or not ctx.guild_id.isdigit() or not ctx.user_id.isdigit():
            return False
        guild = self.bot.get_guild(int(ctx.guild_id))
        if guild is None:
            return False
        thread = guild.get_thread(thread_id)
        if not isinstance(thread, discord.Thread):
            return False
        member = ctx.platform_member
        if not (
            isinstance(member, discord.Member)
            and member.id == int(ctx.user_id)
            and member.guild.id == guild.id
        ):
            member = guild.get_member(int(ctx.user_id))
        if member is None:
            return False
        try:
            return bool(thread.permissions_for(member).manage_threads)
        except AttributeError, TypeError:
            return False

    async def _create_thread_with_retry(
        self,
        create: Callable[[], Any],
        *,
        subject: str,
    ) -> discord.Thread | None:
        """Run a thread-creation coroutine factory with the standard retry.

        Shared by the two creation shapes (off the trigger message, off a
        cross-channel anchor). ``Forbidden`` never retries; a missing
        permission does not become present two seconds later.
        """
        for attempt in range(1, THREAD_HANDOFF_CREATE_ATTEMPTS + 1):
            try:
                return await create()
            except discord.Forbidden:
                log.warning(
                    "Thread handoff creation forbidden for %s; replying in channel",
                    subject,
                    exc_info=True,
                )
                return None
            except discord.HTTPException:
                if attempt >= THREAD_HANDOFF_CREATE_ATTEMPTS:
                    log.warning(
                        "Thread handoff creation failed for %s after %d attempts; "
                        "replying in channel",
                        subject,
                        THREAD_HANDOFF_CREATE_ATTEMPTS,
                        exc_info=True,
                    )
                    return None
                log.warning(
                    "Thread handoff creation failed for %s on attempt %d/%d; retrying in %.1fs",
                    subject,
                    attempt,
                    THREAD_HANDOFF_CREATE_ATTEMPTS,
                    THREAD_HANDOFF_CREATE_RETRY_DELAY_SECONDS,
                    exc_info=True,
                )
                await asyncio.sleep(THREAD_HANDOFF_CREATE_RETRY_DELAY_SECONDS)
        return None

    async def _open_cross_channel_thread(
        self,
        message: discord.Message,
        request: ThreadRequest,
    ) -> discord.Thread | None:
        """Open the thread in another channel, off a freshly posted anchor.

        Re-runs the full candidate gate against the live guild rather than
        trusting the id on the request: the tool resolved it a model turn ago,
        and this is the boundary that actually posts.
        """
        assert request.target_channel_id is not None
        guild = message.guild
        member = message.author if isinstance(message.author, discord.Member) else None
        if guild is None or member is None:
            return None
        allowed = {target.channel_id for target in self._thread_target_candidates(guild, member)}
        if request.target_channel_id not in allowed:
            log.warning(
                "Cross-channel thread target %s is no longer usable for user %s in "
                "guild %s; replying in channel",
                request.target_channel_id,
                member.id,
                guild.id,
            )
            return None
        channel = guild.get_channel(request.target_channel_id)
        if not isinstance(channel, discord.TextChannel):
            return None

        anchor_text = CROSS_CHANNEL_ANCHOR.format(name=sanitize_author_name(member.display_name))
        try:
            anchor = await channel.send(
                anchor_text,
                # The display name is user-controlled text; a literal @everyone
                # inside one would otherwise ping wherever the bot has the
                # permission. Nobody in the target channel is notified by this.
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            log.warning(
                "Could not post the cross-channel thread anchor in %s; replying in channel",
                request.target_channel_id,
                exc_info=True,
            )
            return None

        thread = await self._create_thread_with_retry(
            lambda: anchor.create_thread(
                name=request.name,
                auto_archive_duration=CROSS_CHANNEL_ARCHIVE_MINUTES,
            ),
            subject=f"anchor {anchor.id} in channel {channel.id}",
        )
        if thread is None:
            await _delete_message_quietly(anchor)
            return None
        try:
            await thread.add_user(member)
        except discord.HTTPException:
            # Not fatal: the pointer reply back in the source channel is what
            # actually tells them, and a public thread stays readable regardless.
            log.warning(
                "Could not add user %s to cross-channel thread %s",
                member.id,
                thread.id,
                exc_info=True,
            )
        return thread

    def _existing_message_thread(self, message: discord.Message) -> discord.Thread | None:
        """Return the thread attached to a starter message, if Discord knows it."""

        thread = getattr(message, "thread", None)
        if isinstance(thread, discord.Thread):
            return thread
        guild = getattr(message, "guild", None)
        get_thread = getattr(guild, "get_thread", None)
        if callable(get_thread):
            thread = get_thread(message.id)
            if isinstance(thread, discord.Thread):
                return thread
        return None

    def _thread_creation_lock(self, message: discord.Message) -> asyncio.Lock:
        key = (message.channel.id, message.id)
        lock = self._thread_creation_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._thread_creation_locks[key] = lock
        return lock

    async def _adopt_managed_handoff_thread(
        self,
        message: discord.Message,
    ) -> discord.Thread | None:
        """Adopt a thread another delivery path already created for this message."""

        manager = self.thread_handoff
        if manager is None or isinstance(message.channel, discord.Thread):
            return None
        async with self._thread_creation_lock(message):
            thread = self._existing_message_thread(message)
            if thread is None or not manager.is_managed(thread.id):
                return None
            return thread

    async def _create_handoff_thread(
        self,
        message: discord.Message,
        request: ThreadRequest,
        conv_id: int | None,
        *,
        creator_user_id: str | None = None,
    ) -> discord.Thread | None:
        """Create the handoff thread and enroll it.

        Off the trigger message by default; off an anchor in another channel
        when the request carries a target. Returns None (reply falls back to the
        channel) when handoff is disabled by startup or live operator policy,
        a same-channel handoff is asked for from inside a thread, or Discord
        rejects creation (missing permission, message already has a thread).
        This boundary check covers every creation caller, including automatic
        and command-driven handoffs.
        """
        manager = self.thread_handoff
        if manager is None or conv_id is None:
            return None
        if not self._thread_handoff_creation_allowed(message):
            return None
        if request.target_channel_id is not None:
            thread = await self._open_cross_channel_thread(message, request)
        elif isinstance(message.channel, discord.Thread):
            return None
        else:
            async with self._thread_creation_lock(message):
                thread = self._existing_message_thread(message)
                if thread is None:
                    thread = await self._create_thread_with_retry(
                        lambda: message.create_thread(name=request.name),
                        subject=f"message {message.id}",
                    )
                if thread is None:
                    return None
                await self._enroll_handoff_thread(
                    manager,
                    thread,
                    message,
                    request,
                    conv_id,
                    creator_user_id=creator_user_id,
                )
                return thread
        if thread is None:
            return None
        await self._enroll_handoff_thread(
            manager,
            thread,
            message,
            request,
            conv_id,
            creator_user_id=creator_user_id,
        )
        return thread

    async def _enroll_handoff_thread(
        self,
        manager: ThreadHandoffManager,
        thread: discord.Thread,
        message: discord.Message,
        request: ThreadRequest,
        conv_id: int,
        *,
        creator_user_id: str | None,
    ) -> None:
        # An explicit auto_reply from the model wins; otherwise the channel/guild
        # default decides whether this thread starts answering unprompted.
        auto_respond = (
            request.auto_respond
            if request.auto_respond is not None
            else self._thread_auto_respond_default(message)
        )
        await manager.enroll(
            thread.id,
            conv_id,
            creator_user_id=creator_user_id or str(message.author.id),
            auto_respond=auto_respond,
        )

    async def _send_cross_channel_pointer(
        self,
        message: discord.Message,
        thread: discord.Thread,
    ) -> None:
        """Point the asker at the thread from the channel they were reading.

        This is the notification. The anchor over in the target channel names
        them in plaintext and pings nobody, and ``Thread.add_user`` only puts
        the thread in their sidebar. The ping therefore rides here, on a reply
        to their own message, exactly like an ordinary answer would.

        Deliberately not persisted: the transcript is mapped under the channel
        the reply landed in (the new thread), and a stub filed under the source
        channel would seed later turns from the wrong place.
        """
        try:
            await message.reply(
                CROSS_CHANNEL_POINTER.format(thread=f"<#{thread.id}>"),
                mention_author=True,
                allowed_mentions=discord.AllowedMentions(
                    everyone=False,
                    roles=False,
                    users=False,
                    replied_user=True,
                ),
            )
        except discord.HTTPException:
            # A missing pointer is worth a log, not a failed turn: the answer
            # itself already landed in the thread.
            log.warning(
                "Could not post the cross-channel pointer for thread %s",
                thread.id,
                exc_info=True,
            )

    async def _discard_cross_channel_thread(self, thread: discord.Thread) -> None:
        """Delete the anchor of a cross-channel thread that never got its reply.

        A thread created from a message shares that message's id, so the anchor
        is addressable without a fetch, and deleting it takes the empty thread
        with it. Otherwise the target channel keeps a message introducing a
        thread that has nothing in it.
        """
        parent = thread.parent
        if not isinstance(parent, discord.TextChannel):
            return
        await _delete_message_quietly(parent.get_partial_message(thread.id))

    async def _close_handoff_thread(
        self,
        channel: discord.abc.Messageable,
        request: ThreadCloseRequest,
    ) -> None:
        """Lock/archive a managed thread after the final reply is sent."""
        if self.thread_handoff is None:
            return
        if not isinstance(channel, discord.Thread):
            return
        if channel.id != request.thread_id:
            log.warning(
                "Ignoring thread close request for %s while replying in %s",
                request.thread_id,
                channel.id,
            )
            return
        try:
            await channel.edit(
                locked=True,
                archived=True,
                reason="Thread handoff closed",
            )
        except discord.HTTPException:
            log.warning(
                "Thread handoff close failed for thread %s",
                request.thread_id,
                exc_info=True,
            )
        finally:
            await self.thread_handoff.leave(request.thread_id)
