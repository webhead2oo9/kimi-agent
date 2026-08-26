from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, UTC
from pathlib import Path
from typing import TYPE_CHECKING, Any

import discord

from agent.activity import ActivityUpdate, tool_display_label
from branding import DEFAULT_BOT_NAME

if TYPE_CHECKING:
    from tools.embeds import EmbedSpec

log = logging.getLogger(__name__)

DISCORD_MAX_LENGTH = 2000
CHUNK_THRESHOLD = 1900
DISCORD_DEFAULT_FILE_SIZE_LIMIT_BYTES = 10 * 1024 * 1024
ACTIVITY_UPDATE_MIN_INTERVAL_SECONDS = 1.0
ACTIVITY_LOG_DELETE_DELAY_SECONDS = 3.0
ACTIVITY_IDLE_NUDGE_SECONDS = 30.0
# Once the surface is idle ("still thinking…"), re-render this often to tick the
# elapsed counter so a long single step reads as alive, not hung. 0 disables the
# heartbeat (single stale flip, the original behavior).
ACTIVITY_STALE_HEARTBEAT_SECONDS = 15.0
DEFAULT_TEXT_INVOCATION_NAME = DEFAULT_BOT_NAME


@dataclass(frozen=True)
class OmittedAttachment:
    path: str
    filename: str
    size_bytes: int
    limit_bytes: int
    reason: str = "oversize"


@dataclass(frozen=True)
class AttachmentDeliveryPlan:
    files: tuple[Path, ...]
    embed: EmbedSpec | None
    omitted: tuple[OmittedAttachment, ...]
    effective_limit_bytes: int
    notice_text: str | None = None


class SentMessages(list[Any]):
    """Delivered messages plus whether any expected chunk failed permanently."""

    def __init__(self) -> None:
        super().__init__()
        self.delivery_failed = False
        self.delivery_permanent = False
        self.delivery_error = ""
        self.attachment_plan: AttachmentDeliveryPlan | None = None
        self.prepared_content = ""


def _bot_invocation_names(
    bot_user: discord.ClientUser | None,
    *,
    bot_name: str,
) -> tuple[str, ...]:
    names: set[str] = set()
    configured_name = bot_name.strip()
    if configured_name:
        names.add(configured_name)
    if bot_user is not None:
        for attr in ("display_name", "global_name", "name"):
            value = getattr(bot_user, attr, None)
            if isinstance(value, str) and value.strip():
                names.add(value.strip())
    return tuple(sorted(names, key=len, reverse=True))


def _name_pattern(name: str) -> str:
    return r"\s+".join(re.escape(part) for part in name.split())


def _is_text_invocation(
    content: str,
    *,
    bot_user: discord.ClientUser | None,
    bot_name: str,
) -> bool:
    text = content.strip()
    if not text:
        return False

    for name in _bot_invocation_names(bot_user, bot_name=bot_name):
        pattern = _name_pattern(name)
        if re.match(rf"^(?:hey|hi)\b[\s,!:;-]+{pattern}\b", text, re.IGNORECASE):
            return True
        if re.match(rf"^{pattern}\b[\s,!:;-]+help\b", text, re.IGNORECASE):
            return True
    return False


def _strip_text_invocation(
    content: str,
    *,
    bot_user: discord.ClientUser | None,
    bot_name: str,
) -> str:
    text = content.strip()
    if not text:
        return text

    for name in _bot_invocation_names(bot_user, bot_name=bot_name):
        pattern = _name_pattern(name)
        help_match = re.match(
            rf"^{pattern}\b[\s,!:;-]+(?P<rest>help\b.*)$",
            text,
            re.IGNORECASE,
        )
        if help_match:
            return help_match.group("rest").strip()

        greeting_match = re.match(
            rf"^(?:hey|hi)\b[\s,!:;-]+{pattern}\b(?P<rest>.*)$",
            text,
            re.IGNORECASE,
        )
        if greeting_match:
            rest = greeting_match.group("rest").lstrip(" \t,!:;-")
            return rest.strip() if rest.strip() else text
    return text


def _channel_id_for_allowlist(message: discord.Message) -> int | None:
    """Return the channel ID to check against the allowlist, or None for DMs."""
    if isinstance(message.channel, discord.DMChannel):
        return None
    if isinstance(message.channel, discord.Thread) and message.channel.parent_id:
        return message.channel.parent_id
    return message.channel.id


def is_eligible_to_respond(
    message: discord.Message,
    *,
    bot_user: discord.ClientUser | None,
    allowed_channels: set[int] | None = None,
    allowed_guilds: set[int] | None = None,
) -> bool:
    """Author/type/guild/channel gates that must hold before ANY response.

    Shared by ``on_message`` (an early, cheap reject) and ``should_respond``.
    Skipping them is what let the bot respond to its own replies (a
    self-sustaining loop) and answer in channels removed from the allowlist.

    ``allowed_guilds=None`` disables the guild gate for isolated callers.
    Runtime always supplies a set, including an empty set when no guild has
    been activated, so an invited but unconfigured guild fails closed.
    """
    if message.author == bot_user:
        return False
    if message.author.bot:
        return False
    if message.type not in (discord.MessageType.default, discord.MessageType.reply):
        return False
    if allowed_guilds is not None:
        guild = getattr(message, "guild", None)
        if guild is not None and guild.id not in allowed_guilds:
            return False
    if allowed_channels:
        cid = _channel_id_for_allowlist(message)
        if cid is not None and cid not in allowed_channels:
            return False
    return True


def _is_user_only_integration(interaction: discord.Interaction) -> bool:
    """True when the interaction arrived via the user installation alone.

    Fails closed: if the integration markers are unavailable (older discord.py,
    a test stub, or an unexpected interaction shape) this returns ``False`` so
    the guild allowlist still applies, preserving the stricter behavior.
    """
    is_user = getattr(interaction, "is_user_integration", None)
    is_guild = getattr(interaction, "is_guild_integration", None)
    if not callable(is_user) or not callable(is_guild):
        # Require BOTH markers so a partial/unexpected shape stays gated rather
        # than granting the exemption on incomplete information.
        return False
    try:
        return bool(is_user()) and not bool(is_guild())
    except Exception:
        return False


def is_allowed_guild_interaction(
    interaction: discord.Interaction,
    *,
    allowed_guilds: set[int] | None = None,
) -> bool:
    """Return whether an app-command interaction passes the guild gate.

    The gate is a guild-*membership* boundary: it governs the guilds the bot,
    added to a server, will operate in. A user-installed (user-integration)
    invocation is the user carrying their personal app into a channel; the bot
    was never added to that guild, so the gate does not apply. Only
    guild-integration invocations (and the ambiguous/unknown ones) are gated.
    """
    if allowed_guilds is None:
        return True
    if _is_user_only_integration(interaction):
        return True
    guild_id = getattr(interaction, "guild_id", None)
    return guild_id is None or guild_id in allowed_guilds


def should_respond(
    message: discord.Message,
    *,
    bot_user: discord.ClientUser | None,
    bot_name: str,
    responds_without_mention: Callable[[int], bool],
    allowed_channels: set[int] | None = None,
    allowed_guilds: set[int] | None = None,
) -> bool:
    if not is_eligible_to_respond(
        message,
        bot_user=bot_user,
        allowed_channels=allowed_channels,
        allowed_guilds=allowed_guilds,
    ):
        return False

    if isinstance(message.channel, discord.DMChannel):
        return False

    # Inside a thread the bot created via thread handoff, every human message
    # continues the conversation without a mention (docs/thread-handoff.md).
    # The predicate is answered by ThreadHandoffManager, so this never fires for
    # ordinary threads the bot was merely mentioned in, nor for a managed
    # thread someone has paused, which falls through to the gates below and so
    # behaves exactly like an ordinary channel.
    if isinstance(message.channel, discord.Thread) and responds_without_mention(message.channel.id):
        return True

    # Respond on a real bot mention. We deliberately do NOT use
    # ClientUser.mentioned_in, which short-circuits True on
    # message.mention_everyone, letting any @everyone/@here mass-ping trigger a
    # full turn. The reply-ping path lands the bot in message.mentions, so this
    # membership check still covers replies with the ping toggle on.
    if bot_user and any(user.id == bot_user.id for user in message.mentions):
        return True

    # Also allow a small set of explicit text invocations for users who do not
    # want to @mention the bot in busy channels.
    return bool(
        _is_text_invocation(
            getattr(message, "content", ""),
            bot_user=bot_user,
            bot_name=bot_name,
        )
    )


def can_send_reply(channel: Any, *, bot_member: Any | None) -> bool:
    """Whether the bot can post a reply in ``channel``.

    A cheap gate the mention path runs BEFORE the (paid) model turn: when the
    bot is @mentioned in a channel it can read but not send to, running the turn
    burns an LLM call (and tool calls) on a reply ``send_response`` can only fail
    to deliver. DMs never reach here.

    Fails OPEN: when permissions cannot be resolved (no bot member, an
    unexpected channel shape, or ``permissions_for`` raising) this returns True
    so a reply is never wrongly suppressed; the only regression risk versus
    today's behavior is running a turn we cannot deliver, which is the status quo.
    """
    perms_for = getattr(channel, "permissions_for", None)
    if bot_member is None or not callable(perms_for):
        return True
    try:
        perms = perms_for(bot_member)
    except Exception:
        return True
    # Threads gate sending on send_messages_in_threads, not send_messages.
    if isinstance(channel, discord.Thread):
        return bool(getattr(perms, "send_messages_in_threads", False))
    return bool(getattr(perms, "send_messages", False))


def strip_mention(
    content: str,
    *,
    bot_user: discord.ClientUser | None,
    bot_name: str,
) -> str:
    if bot_user is None:
        return _strip_text_invocation(
            content,
            bot_user=bot_user,
            bot_name=bot_name,
        )
    mention_patterns = [f"<@{bot_user.id}>", f"<@!{bot_user.id}>"]
    for pattern in mention_patterns:
        content = content.replace(pattern, "")
    return _strip_text_invocation(
        content,
        bot_user=bot_user,
        bot_name=bot_name,
    )


_FENCE_MARKER = "```"
# Language tags worth carrying across a split (``` info strings like "python").
_FENCE_INFO_RE = re.compile(r"^[A-Za-z0-9_+#.-]{0,20}$")
# Longest fence-reopen prefix is "```" + 20-char info + "\n" = 24 chars; forcing
# splits to consume more than that keeps the loop strictly shrinking even when
# every chunk ends inside a fence.
_MIN_SPLIT_PROGRESS = 32
_URL_SCHEME_RE = re.compile(r"https?://", re.IGNORECASE)
_URL_ALWAYS_TRAILING = frozenset(".,!?;:'\"*_~")
_URL_BALANCED_TRAILING = {")": "(", "]": "[", "}": "{"}


def _reply_reference(message: discord.Message | None) -> Any:
    """Build the reference for a reply send.

    ``fail_if_not_exists=False`` makes Discord degrade to a plain (non-reply)
    message when the referenced trigger was deleted mid-turn, instead of
    rejecting the whole send with a 400 Unknown Message.
    """
    if message is None:
        return None
    to_reference = getattr(message, "to_reference", None)
    if to_reference is None:
        return message
    return to_reference(fail_if_not_exists=False)


def _unclosed_fence_reopen(chunk: str) -> str | None:
    """Return the prefix that reopens a code fence (e.g. ``"```python\\n"``)
    when the chunk ends inside one, else None.

    Tracks ``` parity through the chunk; the opening fence's info string is
    carried over so the reopened block keeps its syntax highlighting.
    """
    open_info: str | None = None
    idx = 0
    while True:
        idx = chunk.find(_FENCE_MARKER, idx)
        if idx == -1:
            break
        if open_info is None:
            line_end = chunk.find("\n", idx + len(_FENCE_MARKER))
            if line_end == -1:
                line_end = len(chunk)
            info = chunk[idx + len(_FENCE_MARKER) : line_end].strip()
            open_info = info if _FENCE_INFO_RE.fullmatch(info) else ""
        else:
            open_info = None
        idx += len(_FENCE_MARKER)
    if open_info is None:
        return None
    return f"{_FENCE_MARKER}{open_info}\n"


def _markdown_link_end(text: str, start: int) -> int | None:
    """Return the end of a complete inline Markdown link beginning at ``[``."""

    label_depth = 0
    cursor = start
    while cursor < len(text):
        char = text[cursor]
        if char == "\\":
            cursor += 2
            continue
        if char == "[":
            label_depth += 1
        elif char == "]":
            label_depth -= 1
            if label_depth == 0:
                break
        cursor += 1
    if label_depth != 0 or cursor + 1 >= len(text) or text[cursor + 1] != "(":
        return None

    destination_depth = 1
    cursor += 2
    while cursor < len(text):
        char = text[cursor]
        if char == "\\":
            cursor += 2
            continue
        if char == "(":
            destination_depth += 1
        elif char == ")":
            destination_depth -= 1
            if destination_depth == 0:
                return cursor + 1
        cursor += 1
    return None


def _url_content_end(candidate: str) -> int:
    """Exclude prose/Markdown punctuation while keeping balanced URL delimiters."""

    end = len(candidate)
    while end:
        char = candidate[end - 1]
        if char in _URL_ALWAYS_TRAILING:
            end -= 1
            continue
        opener = _URL_BALANCED_TRAILING.get(char)
        if opener is not None and candidate[:end].count(char) > candidate[:end].count(opener):
            end -= 1
            continue
        break
    return end


def suppress_link_previews(content: str) -> str:
    """Wrap standalone HTTP(S) URLs without disturbing Discord Markdown.

    Discord treats ``<https://example.com>`` as a clickable link without an
    automatic preview. Code spans/fences, existing angle autolinks, and inline
    Markdown links are copied verbatim; uploaded files and explicit embeds live
    outside ``content`` and are therefore unaffected.
    """

    output: list[str] = []
    cursor = 0
    while cursor < len(content):
        if content[cursor] == "`":
            run_end = cursor + 1
            while run_end < len(content) and content[run_end] == "`":
                run_end += 1
            marker = content[cursor:run_end]
            closing = content.find(marker, run_end)
            if closing == -1:
                output.append(content[cursor:])
                break
            closing += len(marker)
            output.append(content[cursor:closing])
            cursor = closing
            continue

        if content[cursor] == "<":
            closing = content.find(">", cursor + 1)
            if closing != -1:
                output.append(content[cursor : closing + 1])
                cursor = closing + 1
                continue

        if content[cursor] == "[":
            link_end = _markdown_link_end(content, cursor)
            if link_end is not None:
                output.append(content[cursor:link_end])
                cursor = link_end
                continue

        scheme = _URL_SCHEME_RE.match(content, cursor)
        if scheme is None:
            output.append(content[cursor])
            cursor += 1
            continue

        candidate_end = scheme.end()
        while (
            candidate_end < len(content)
            and not content[candidate_end].isspace()
            and content[candidate_end] not in "<>`"
        ):
            candidate_end += 1
        candidate = content[cursor:candidate_end]
        url_end = _url_content_end(candidate)
        url = candidate[:url_end]
        if url_end == len(scheme.group(0)):
            output.append(candidate)
        else:
            output.extend(("<", url, ">", candidate[url_end:]))
        cursor = candidate_end

    return "".join(output)


def chunk_message(text: str) -> list[str]:
    if len(text) <= DISCORD_MAX_LENGTH:
        return [text]

    chunks: list[str] = []
    while text:
        if len(text) <= CHUNK_THRESHOLD:
            chunks.append(text)
            break

        split_at = text.rfind("\n", 0, CHUNK_THRESHOLD)
        delimiter = "\n"
        if split_at == -1:
            split_at = text.rfind(" ", 0, CHUNK_THRESHOLD)
            delimiter = " "
        if split_at < _MIN_SPLIT_PROGRESS:
            split_at = CHUNK_THRESHOLD
            delimiter = ""

        chunk = text[:split_at]
        # Consume exactly the one delimiter character the split landed on: a
        # broader strip would also eat the continuation line's indentation
        # (corrupting split code blocks) or collapse intentional blank-line and
        # space runs. Nothing may be stripped after a forced hard cut.
        rest = text[split_at:].removeprefix(delimiter) if delimiter else text[split_at:]
        reopen = _unclosed_fence_reopen(chunk)
        if reopen is not None:
            # The split landed inside a code block: close the fence so this
            # chunk renders, and reopen it (same language tag) at the start of
            # the remainder so the next chunk renders too.
            chunk = f"{chunk.rstrip()}\n{_FENCE_MARKER}"
            rest = reopen + rest
        chunks.append(chunk)
        text = rest

    if len(chunks) > 1:
        chunks = [f"{c}\n`({i + 1}/{len(chunks)})`" for i, c in enumerate(chunks)]

    return chunks


class _ActivityNarrationReporter:
    """Shared narration/throttle/idle machinery for the live "thinking" surfaces.

    Subclasses supply the transport: how to first paint the surface, how to
    rewrite it, and what to do on finish. The mention path paints a throwaway
    channel message it deletes afterward; the interaction path edits the
    deferred response in place so the narration becomes the final reply.
    """

    def __init__(
        self,
        *,
        min_interval_seconds: float = ACTIVITY_UPDATE_MIN_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        idle_nudge_seconds: float = ACTIVITY_IDLE_NUDGE_SECONDS,
        stale_heartbeat_seconds: float = ACTIVITY_STALE_HEARTBEAT_SECONDS,
    ) -> None:
        self._min_interval = max(0.0, min_interval_seconds)
        self._clock = clock
        self._sleep = sleep
        self._idle_nudge = max(0.0, idle_nudge_seconds)
        self._stale_heartbeat = max(0.0, stale_heartbeat_seconds)
        # Seconds the current step has been idle, ticked by the heartbeat watcher and
        # rendered into the "still thinking…" line. Deterministic (summed sleeps, not a
        # wall clock) so it reflects configured intervals regardless of the injected clock.
        self._stale_elapsed_seconds = 0.0
        self._current_content = ""
        self._last_update_at: float | None = None
        self._pending_content: str | None = None
        self._flush_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._closed = False
        # True once the surface carries visible content (the first paint landed).
        self._started = False
        # Single non-accumulating block: the latest model sentence (_narration)
        # with its tool(s) as muted subtext, or the live status label when silent.
        self._narration = ""
        self._tools: list[str] = []
        # Latest plan-tool checklist, rendered as muted lines under the block while
        # the turn is live; dropped from the closed/final render.
        self._plan: list[dict[str, str]] = []
        self._status = ""
        self._stale = False
        self._has_committed = False
        self._idle_task: asyncio.Task[None] | None = None

    async def __call__(self, update: ActivityUpdate) -> None:
        await self.update(update.label, phase=update.phase)

    async def update(self, label: str, *, phase: str = "status") -> None:
        async with self._lock:
            if self._closed:
                return
            self._status = label
            # A fresh "thinking" step means the previous tool finished. Drop its
            # subtext so a stale tool name doesn't linger under the last sentence.
            if phase == "thinking":
                self._tools = []
            self._stale = False
            self._schedule_idle_locked()
            await self._apply_content_locked(self._render_capped())

    async def update_plan(self, steps: list[dict[str, str]]) -> None:
        # Does not set _has_committed: a plan-only turn still deletes the throwaway
        # message on finish, keeping the checklist a live-only surface.
        async with self._lock:
            if self._closed:
                return
            self._plan = list(steps)
            self._stale = False
            self._schedule_idle_locked()
            await self._apply_content_locked(self._render_capped())

    async def commit_step(self, narration: str, tool_names: list[str]) -> None:
        async with self._lock:
            if self._closed:
                return
            text = (narration or "").strip()
            names = [name for name in tool_names if name]
            if not text and not names:
                return
            if text:
                self._narration = text
            if names:
                self._tools = names
            self._stale = False
            self._has_committed = True
            self._schedule_idle_locked()
            updated = await self._apply_content_locked(self._render_capped())
            if updated:
                await self._after_commit_paint_locked()

    @property
    def committed_message_id(self) -> int | None:
        if not self._has_committed:
            return None
        return self._surface_message_id()

    def _render_capped(self) -> str:
        if self._closed:
            block = _format_narration_step(self._narration, self._tools if self._narration else [])
        elif self._stale:
            elapsed = _format_elapsed(self._stale_elapsed_seconds)
            block = (
                f"{self._narration}\n-# still thinking… {elapsed}"
                if self._narration
                else f"Still thinking… {elapsed}"
            )
        elif self._narration:
            block = _format_narration_step(self._narration, self._tools)
        elif self._tools and not self._status:
            # A silent tool call before the live status arrives.
            block = _format_narration_step("", self._tools)
        else:
            block = self._status
        if not self._closed and self._plan:
            plan_block = _format_plan_block(self._plan, DISCORD_MAX_LENGTH - len(block) - 1)
            if plan_block:
                block = f"{block}\n{plan_block}" if block else plan_block
        return _neutralize_mentions(block or "")[:DISCORD_MAX_LENGTH]

    def _schedule_idle_locked(self) -> None:
        if self._idle_task is not None and not self._idle_task.done():
            self._idle_task.cancel()
        if self._closed or self._idle_nudge <= 0:
            self._idle_task = None
            return
        self._idle_task = asyncio.create_task(self._idle_watch())

    async def _idle_watch(self) -> None:
        try:
            await self._sleep(self._idle_nudge)
            waited = self._idle_nudge
            async with self._lock:
                if self._closed or self._stale:
                    return
                self._stale = True
                self._stale_elapsed_seconds = waited
                await self._apply_content_locked(self._render_capped())
            # Heartbeat: keep ticking the elapsed counter so a long single step reads as
            # alive, not hung. Each tick only re-renders the "still thinking…" line. No
            # provider work, no new tokens.
            while self._stale_heartbeat > 0:
                await self._sleep(self._stale_heartbeat)
                waited += self._stale_heartbeat
                async with self._lock:
                    if self._closed or not self._stale:
                        return
                    self._stale_elapsed_seconds = waited
                    await self._apply_content_locked(self._render_capped())
        except asyncio.CancelledError:
            # Fire-and-forget watcher: a newer update/commit or finish() replaced it.
            return

    async def _apply_content_locked(self, content: str) -> bool:
        if self._closed:
            return False
        if not content or content == self._current_content:
            return bool(content and self._started)
        now = float(self._clock())
        if not self._started:
            if await self._paint_initial(content):
                self._started = True
                self._current_content = content
                self._last_update_at = now
                return True
            return False

        if self._last_update_at is not None and now - self._last_update_at < self._min_interval:
            self._pending_content = content
            if self._flush_task is None or self._flush_task.done():
                delay = self._min_interval - (now - self._last_update_at)
                self._flush_task = asyncio.create_task(self._flush_after(delay))
            return True

        self._pending_content = None
        return await self._edit_locked(content, now)

    async def _flush_after(self, delay: float) -> None:
        await self._sleep(max(0.0, delay))
        async with self._lock:
            if self._closed or not self._started or self._pending_content is None:
                return
            content = self._pending_content
            self._pending_content = None
            await self._edit_locked(content, float(self._clock()))

    async def _edit_locked(self, content: str, now: float) -> bool:
        if not self._started:
            return False
        if await self._paint_update(content):
            self._current_content = content
            self._last_update_at = now
            return True
        return False

    async def finish(self) -> None:
        async with self._lock:
            already_closed = self._closed
            self._closed = True
            self._status = ""
            self._stale = False
            self._pending_content = None
            flush_task = self._flush_task
            self._flush_task = None
            idle_task = self._idle_task
            self._idle_task = None
            has_steps = self._has_committed
            final_content = self._render_capped() if has_steps else ""
            current = self._current_content
            started = self._started
        for task in (flush_task, idle_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        if already_closed or not started:
            return
        await self._on_finish(has_steps=has_steps, final_content=final_content, current=current)

    # --- transport hooks ---------------------------------------------------

    async def _paint_initial(self, content: str) -> bool:
        """Paint the surface for the first time. Return True on success."""
        raise NotImplementedError

    async def _paint_update(self, content: str) -> bool:
        """Rewrite the already-painted surface. Return True on success."""
        raise NotImplementedError

    async def _on_finish(self, *, has_steps: bool, final_content: str, current: str) -> None:
        return

    async def _after_commit_paint_locked(self) -> None:
        return

    def _surface_message_id(self) -> int | None:
        return None


class DiscordActivityReporter(_ActivityNarrationReporter):
    """Live status that sends a throwaway channel message and edits it in place.

    Used on the mention path: the narration message is deleted after the turn
    and the real answer is delivered as a separate reply.
    """

    def __init__(
        self,
        channel: discord.abc.Messageable,
        *,
        reference: discord.Message | None = None,
        min_interval_seconds: float = ACTIVITY_UPDATE_MIN_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        on_committed_message: Callable[[int], Awaitable[None]] | None = None,
        delete_after_seconds: float | None = ACTIVITY_LOG_DELETE_DELAY_SECONDS,
        idle_nudge_seconds: float = ACTIVITY_IDLE_NUDGE_SECONDS,
        stale_heartbeat_seconds: float = ACTIVITY_STALE_HEARTBEAT_SECONDS,
    ) -> None:
        super().__init__(
            min_interval_seconds=min_interval_seconds,
            clock=clock,
            sleep=sleep,
            idle_nudge_seconds=idle_nudge_seconds,
            stale_heartbeat_seconds=stale_heartbeat_seconds,
        )
        self._channel = channel
        self._reference = _reply_reference(reference)
        self._on_committed_message = on_committed_message
        self._delete_after_seconds = delete_after_seconds
        self._message: discord.Message | None = None
        self._committed_notified = False

    async def _paint_initial(self, content: str) -> bool:
        try:
            send_kwargs: dict[str, Any] = {}
            if self._reference is not None:
                send_kwargs["reference"] = self._reference
                send_kwargs["mention_author"] = False
            self._message = await self._channel.send(content, **send_kwargs)
            return True
        except discord.HTTPException:
            log.debug("Could not send Discord activity status", exc_info=True)
        return False

    async def _paint_update(self, content: str) -> bool:
        if self._message is None:
            return False
        try:
            await self._message.edit(content=content)
            return True
        except discord.HTTPException:
            log.debug("Could not edit Discord activity status", exc_info=True)
        return False

    def _surface_message_id(self) -> int | None:
        return int(self._message.id) if self._message is not None else None

    async def _after_commit_paint_locked(self) -> None:
        if self._committed_notified or self._message is None:
            return
        self._committed_notified = True
        if self._on_committed_message is None:
            return
        try:
            await self._on_committed_message(int(self._message.id))
        except Exception:
            log.debug("Committed-message callback failed", exc_info=True)

    async def _on_finish(self, *, has_steps: bool, final_content: str, current: str) -> None:
        message = self._message
        if message is None:
            return
        if has_steps:
            if final_content and final_content != current:
                try:
                    await message.edit(content=final_content)
                except discord.HTTPException:
                    log.debug("Could not finalize Discord activity log", exc_info=True)
            try:
                await message.delete(delay=self._delete_after_seconds)
            except discord.HTTPException:
                log.debug("Could not delete Discord activity log", exc_info=True)
            return
        try:
            await message.delete()
        except discord.HTTPException:
            log.debug("Could not delete Discord activity status", exc_info=True)


def _neutralize_mentions(text: str) -> str:
    return text.replace("@everyone", "@\u200beveryone").replace("@here", "@\u200bhere")


def _format_elapsed(seconds: float) -> str:
    """Compact elapsed label for the idle heartbeat, e.g. "(45s)" or "(1m30s)"."""
    total = max(0, int(seconds))
    if total < 60:
        return f"({total}s)"
    minutes, secs = divmod(total, 60)
    return f"({minutes}m{secs:02d}s)"


def _format_narration_step(narration: str, tool_names: list[str]) -> str:
    text = (narration or "").strip()
    # Map raw tool names to friendly labels and dedupe (preserving order) so parallel
    # calls to one tool show its label once, not repeated.
    labels: list[str] = []
    for name in tool_names:
        if not name:
            continue
        label = tool_display_label(name)
        if label not in labels:
            labels.append(label)
    # Tool labels ride on a Discord subtext line ("-# ...") so they render small and
    # muted beneath the model's prose: the machinery recedes, the message leads.
    footer = f"-# {', '.join(labels)}" if labels else ""
    if text and footer:
        return f"{text}\n{footer}"
    return text or footer


_PLAN_MAX_PENDING_LINES = 3
_PLAN_MIN_BLOCK_CHARS = 24
_PLAN_DONE_NAMES_MAX_CHARS = 120


def _format_plan_block(steps: list[dict[str, str]], max_chars: int) -> str:
    """Muted checklist lines for the plan-tool state, self-capped to max_chars.

    Degrades until it fits: full form -> truncated completed-names run -> count-only
    completed line -> pending lines folded into "+K more" -> clipped minimal form.
    Below a small floor it renders nothing rather than a mangled fragment.
    """
    if not steps or max_chars < _PLAN_MIN_BLOCK_CHARS:
        return ""
    done = [s.get("content", "") for s in steps if s.get("status") == "completed"]
    rest = [s for s in steps if s.get("status") != "completed"]
    active_idx = next((i for i, s in enumerate(rest) if s.get("status") == "in_progress"), None)
    active = [f"-# → {rest[active_idx].get('content', '')}"] if active_idx is not None else []
    tail = [
        (s.get("content", ""), s.get("status", "")) for i, s in enumerate(rest) if i != active_idx
    ]

    def done_line(with_names: bool) -> list[str]:
        if not done:
            return []
        if not with_names:
            return [f"-# ✓ {len(done)} done"]
        names = ", ".join(done)
        if len(names) > _PLAN_DONE_NAMES_MAX_CHARS:
            names = names[: _PLAN_DONE_NAMES_MAX_CHARS - 1] + "…"
        return [f"-# ✓ {len(done)} done · {names}"]

    for with_names in (True, False):
        for keep in range(min(_PLAN_MAX_PENDING_LINES, len(tail)), -1, -1):
            lines = done_line(with_names) + active
            lines += [
                f"-# {'→' if status == 'in_progress' else '○'} {content}"
                for content, status in tail[:keep]
            ]
            if len(tail) > keep:
                lines.append(f"-# +{len(tail) - keep} more")
            block = "\n".join(lines)
            if len(block) <= max_chars:
                return block
    return "\n".join(done_line(False) + active)[: max_chars - 1] + "…"


def build_embed(spec: EmbedSpec) -> discord.Embed:
    """Convert a plain ``EmbedSpec`` into a ``discord.Embed``.

    This is the single place ``discord.Embed`` is constructed, keeping the rest of the
    pipeline Discord-agnostic. A workspace image arrives as an ``attachment://<name>``
    reference and resolves against the file uploaded on the same message.
    """
    embed = discord.Embed()
    if spec.title is not None:
        embed.title = spec.title
    if spec.description is not None:
        embed.description = spec.description
    if spec.url is not None:
        embed.url = spec.url
    if spec.color is not None:
        embed.colour = discord.Colour(spec.color)
    if spec.author_name is not None:
        embed.set_author(
            name=spec.author_name,
            url=spec.author_url,
            icon_url=spec.author_icon_url,
        )
    if spec.footer_text is not None or spec.footer_icon_url is not None:
        embed.set_footer(text=spec.footer_text or "", icon_url=spec.footer_icon_url)
    if spec.image is not None:
        embed.set_image(url=spec.image)
    if spec.thumbnail_url is not None:
        embed.set_thumbnail(url=spec.thumbnail_url)
    for name, value, inline in spec.fields:
        embed.add_field(name=name, value=value, inline=inline)
    if spec.timestamp:
        embed.timestamp = datetime.now(UTC)
    return embed


def _embed_attachment_name(spec: EmbedSpec | None) -> str | None:
    if spec is None or spec.image is None:
        return None
    prefix = "attachment://"
    if not spec.image.startswith(prefix):
        return None
    name = spec.image[len(prefix) :].strip()
    return Path(name).name or None


def _files_for_first_message(
    existing_files: list[Path],
    required_attachment_name: str | None,
) -> list[Path]:
    if required_attachment_name is None:
        return existing_files[:10]
    required = next(
        (path for path in existing_files if path.name == required_attachment_name),
        None,
    )
    if required is None:
        return existing_files[:10]
    selected = [required]
    selected.extend(path for path in existing_files if path != required)
    return selected[:10]


async def send_response(
    channel: discord.abc.Messageable,
    content: str,
    reference: discord.Message | None = None,
    output_files: list[str] | None = None,
    allowed_file_roots: list[str | Path] | None = None,
    embed: EmbedSpec | None = None,
    mention_author: bool = False,
) -> SentMessages:
    plan = prepare_attachment_delivery(
        channel,
        output_files=output_files or [],
        allowed_file_roots=allowed_file_roots,
        embed=embed,
    )
    prepared_content = apply_attachment_delivery_notice(content, plan)
    return await send_prepared_response(
        channel,
        prepared_content,
        plan,
        reference=reference,
        mention_author=mention_author,
    )


async def send_prepared_response(
    channel: discord.abc.Messageable,
    content: str,
    plan: AttachmentDeliveryPlan,
    *,
    reference: discord.Message | None = None,
    mention_author: bool = False,
) -> SentMessages:
    content = suppress_link_previews(content)
    chunks = chunk_message(content)
    reference = _reply_reference(reference)
    sent_messages = SentMessages()
    sent_messages.attachment_plan = plan
    sent_messages.prepared_content = content
    suppress_notice_mentions = bool(attachment_delivery_notice(plan))
    for i, chunk in enumerate(chunks):
        first_message_files: list[Path] = []
        try:
            files: list[discord.File] = []
            embed_obj: discord.Embed | None = None
            if i == 0:
                if plan.files:
                    first_message_files = list(plan.files)
                    for path in first_message_files:
                        try:
                            files.append(discord.File(str(path)))
                        except OSError as e:
                            log.warning(
                                "Skipping generated output file before upload: %s (%s)",
                                path,
                                e,
                            )
                if plan.embed is not None:
                    embed_obj = build_embed(plan.embed)

            # Never send a truly empty message (no text, no embed, no files).
            content_arg = chunk if chunk.strip() else None
            if content_arg is None and not files and embed_obj is None:
                continue

            send_kwargs: dict[str, Any] = {}
            if suppress_notice_mentions:
                send_kwargs["allowed_mentions"] = discord.AllowedMentions.none()
            if i == 0 and reference:
                send_kwargs["reference"] = reference
                send_kwargs["mention_author"] = mention_author
            if files:
                send_kwargs["files"] = files
            if embed_obj is not None:
                send_kwargs["embeds"] = [embed_obj]
            sent_messages.append(await channel.send(content_arg, **send_kwargs))
        except discord.HTTPException as e:
            log.error("Failed to send message chunk %d: %s", i, e)
            try:
                retry_kwargs: dict[str, Any] = {}
                if suppress_notice_mentions:
                    retry_kwargs["allowed_mentions"] = discord.AllowedMentions.none()
                if i == 0 and reference:
                    retry_kwargs["reference"] = reference
                    retry_kwargs["mention_author"] = mention_author
                if first_message_files:
                    retry_kwargs["files"] = [
                        discord.File(str(path)) for path in first_message_files
                    ]
                if embed_obj is not None:
                    retry_kwargs["embeds"] = [embed_obj]
                sent_messages.append(await channel.send(content_arg, **retry_kwargs))
            except (discord.HTTPException, OSError) as retry_error:
                log.error("Failed to send message chunk %d on retry: %s", i, retry_error)
                sent_messages.delivery_failed = True
                sent_messages.delivery_permanent = isinstance(
                    retry_error,
                    discord.NotFound | discord.Forbidden,
                )
                sent_messages.delivery_error = type(retry_error).__name__
                break
    return sent_messages


def prepare_attachment_delivery(
    channel: discord.abc.Messageable,
    *,
    output_files: list[str],
    allowed_file_roots: list[str | Path] | None,
    embed: EmbedSpec | None,
    effective_limit_bytes: int | None = None,
    notice_text: str | None = None,
) -> AttachmentDeliveryPlan:
    existing_files = _validated_output_files(output_files, allowed_file_roots)
    skipped = len(output_files) - len(existing_files)
    if skipped:
        log.warning("Skipping %d missing or invalid generated output file(s)", skipped)

    effective_limit = (
        effective_limit_bytes
        if isinstance(effective_limit_bytes, int)
        and not isinstance(effective_limit_bytes, bool)
        and effective_limit_bytes > 0
        else _effective_file_size_limit(channel)
    )
    deliverable: list[Path] = []
    omitted: list[OmittedAttachment] = []
    for path in existing_files:
        try:
            size = path.stat().st_size
        except OSError as exc:
            log.warning("Skipping generated output file before size validation: %s (%s)", path, exc)
            continue
        if size > effective_limit:
            omitted.append(
                OmittedAttachment(
                    path=str(path),
                    filename=path.name,
                    size_bytes=size,
                    limit_bytes=effective_limit,
                )
            )
            continue
        deliverable.append(path)

    required_embed_attachment = _embed_attachment_name(embed)
    selected = _files_for_first_message(deliverable, required_embed_attachment)
    if len(deliverable) > 10:
        log.warning("Only attaching first 10 of %d generated files", len(deliverable))

    selected_names = {path.name for path in selected}
    prepared_embed = embed
    if required_embed_attachment is not None and required_embed_attachment not in selected_names:
        log.warning(
            "Skipping embed because required attachment is unavailable: %s",
            required_embed_attachment,
        )
        prepared_embed = None

    return AttachmentDeliveryPlan(
        files=tuple(selected),
        embed=prepared_embed,
        omitted=tuple(omitted),
        effective_limit_bytes=effective_limit,
        notice_text=notice_text,
    )


def apply_attachment_delivery_notice(
    content: str,
    plan: AttachmentDeliveryPlan,
    *,
    after_first_line: bool = False,
) -> str:
    notice = attachment_delivery_notice(plan)
    if not notice:
        return content
    if not content:
        return notice
    if not after_first_line:
        return f"{notice}\n\n{content}"
    first_line, separator, remainder = content.partition("\n")
    if not separator:
        return f"{content}\n{notice}"
    return f"{first_line}\n{notice}\n\n{remainder}"


def attachment_delivery_notice(plan: AttachmentDeliveryPlan) -> str:
    if plan.notice_text is not None:
        return plan.notice_text
    if not plan.omitted:
        return ""
    limit = _display_file_size_exact(plan.effective_limit_bytes)
    lines = [
        (
            "Delivery notice: Discord did not attach these files because each exceeds "
            f"this destination's {limit} per-file limit."
        )
    ]
    for omitted in plan.omitted:
        filename = _plain_notice_filename(omitted.filename)
        lines.append(f"File {filename}: {_display_file_size_exact(omitted.size_bytes)}")
    lines.append(
        "Ignore any claim below that an omitted file was attached. "
        "Ask me to make smaller artifacts if needed."
    )
    return "\n".join(lines)


def _effective_file_size_limit(channel: discord.abc.Messageable) -> int:
    guild = getattr(channel, "guild", None)
    raw_limit = getattr(guild, "filesize_limit", None)
    if isinstance(raw_limit, int) and not isinstance(raw_limit, bool) and raw_limit > 0:
        return raw_limit
    return DISCORD_DEFAULT_FILE_SIZE_LIMIT_BYTES


def _display_file_size(size_bytes: int) -> str:
    units = ((1024**3, "GiB"), (1024**2, "MiB"), (1024, "KiB"))
    for divisor, suffix in units:
        if size_bytes >= divisor:
            value = size_bytes / divisor
            rendered = f"{value:.0f}" if value.is_integer() else f"{value:.1f}"
            return f"{rendered} {suffix}"
    return f"{size_bytes} bytes"


def _display_file_size_exact(size_bytes: int) -> str:
    rendered = _display_file_size(size_bytes)
    if size_bytes < 1024:
        return rendered
    return f"{rendered} ({size_bytes:,} bytes)"


def _plain_notice_filename(filename: str) -> str:
    """Keep a filename recognizable without emitting Discord control syntax."""

    translation = str.maketrans(
        {
            "@": "＠",
            "`": "｀",
            "*": "＊",
            "_": "＿",
            "~": "～",
            "|": "｜",
            "<": "＜",
            ">": "＞",
            "[": "［",
            "]": "］",
            "\\": "＼",
        }
    )
    cleaned = filename.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    return cleaned.translate(translation) or "unnamed file"


def _validated_output_files(
    output_files: list[str],
    allowed_file_roots: list[str | Path] | None,
) -> list[Path]:
    # Fail closed: with no allowed roots (None or empty), no output file passes
    # containment. Production always supplies the roots a tool registered; a
    # missing list must never mean "attach anything".
    allowed_roots: list[Path] = []
    for root in allowed_file_roots or []:
        try:
            allowed_roots.append(Path(root).resolve(strict=False))
        except OSError:
            log.warning("Skipping invalid attachment root: %s", root)

    existing_files: list[Path] = []
    for raw_path in output_files:
        path = Path(raw_path)
        try:
            if path.is_symlink() or not path.exists() or not path.is_file():
                continue
            resolved = path.resolve(strict=False)
        except OSError:
            continue

        if not any(resolved.is_relative_to(root) for root in allowed_roots):
            log.warning("Skipping output file outside allowed roots: %s", path)
            continue
        existing_files.append(resolved)
    return existing_files
