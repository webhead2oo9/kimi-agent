"""On-demand Discord channel-history collection for explicit context tools.

`get_channel_context` pulls recent messages from `channel.history` only when the
model asks for prior discussion. Each message becomes one model-facing transcript
line ("Name: content" for humans, newline-neutralized; the bot's own messages
chunk-marker stripped and labeled with the bot's name). Nothing here is
persisted: the lines are transient tool output for the current turn.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from emoji import demojize

from utils.format import sanitize_author_name
from utils.image_types import looks_like_image_attachment

_CHUNK_MARKER = re.compile(r"\s*`\(\d+/\d+\)`\s*$")


def strip_chunk_marker(text: str) -> str:
    """Drop a trailing ``(n/m)`` chunk marker the bot appends to split replies."""
    return _CHUNK_MARKER.sub("", text).rstrip()


def clean_message_text(text: str) -> str:
    """Neutralize newlines so an aggregated ``Name: content`` line cannot forge
    extra speaker turns in the model context or the memory-eval transcript.

    Applied to human + trigger content only, NOT the bot's own assistant
    messages, whose markdown/code newlines must survive.
    """
    return text.replace("\n", " ").replace("\r", " ")


def message_source_timestamp(message: Any) -> float | None:
    created_at = getattr(message, "created_at", None)
    if created_at is None:
        return None
    timestamp = getattr(created_at, "timestamp", None)
    if callable(timestamp):
        return float(timestamp())
    try:
        return float(created_at)
    except TypeError, ValueError:
        return None


def _emoji_token(emoji: Any) -> str:
    """Render one reaction emoji as a ``:shortcode:``.

    Unicode reactions go through ``demojize`` (``👍`` -> ``:thumbs_up:``); custom
    Discord emoji (``discord.Emoji``/``PartialEmoji``) use their ``.name``.
    """
    if isinstance(emoji, str):
        return demojize(emoji, delimiters=(":", ":"))  # raw char if unknown
    name = getattr(emoji, "name", None)
    return f":{name}:" if name else str(emoji)


def format_reactions(message: Any) -> str:
    """Summarize a message's reactions as `` [reactions: :name:×N, ...]``.

    Returns ``""`` when there are none. The leading space lets callers append it
    directly to the message text. Contains no newlines, so it is safe to mix into
    the newline-neutralized ``Name: content`` model lines.
    """
    reactions = getattr(message, "reactions", None)
    if not reactions:
        return ""
    parts = [f"{_emoji_token(r.emoji)}×{getattr(r, 'count', 0) or 0}" for r in reactions]
    return f" [reactions: {', '.join(parts)}]"


@dataclass(frozen=True)
class ChannelContextImage:
    """One image attachment on a recent message, addressable by id.

    The transcript line names a file; this names *where it is*, so a tool that
    loads an image out of Discord can be pointed at it. Only the ids and the
    filename travel; without a CDN URL, nothing here can be fetched directly.
    """

    message_id: str
    attachment_index: int  # 1-based physical position on the message
    filename: str
    # Who posted it, sanitized the same way the transcript line is. A captioned
    # image never has its filename in the transcript, so without the author there
    # is nothing tying "the one Bob posted" to an id and the model has to guess.
    author_name: str = ""


@dataclass(frozen=True)
class BackfilledMessage:
    transcript_line: str  # one "Name: content" line for the combined model transcript
    images: tuple[ChannelContextImage, ...] = ()


def _image_attachments(message: Any, *, author_name: str) -> tuple[ChannelContextImage, ...]:
    """Index the message's image-like attachments by physical position.

    The index counts *all* attachments, not just the image-like ones, so it lines
    up with the position a fetcher will read on the live message. Image-likeness
    uses the same predicate the fetcher does, so nothing is listed here that would
    be refused there.
    """
    message_id = str(getattr(message, "id", "") or "")
    if not message_id:
        return ()
    out: list[ChannelContextImage] = []
    for position, attachment in enumerate(getattr(message, "attachments", None) or [], start=1):
        filename = str(getattr(attachment, "filename", "") or "")
        if not looks_like_image_attachment(filename, getattr(attachment, "content_type", None)):
            continue
        out.append(
            ChannelContextImage(
                message_id=message_id,
                attachment_index=position,
                filename=filename,
                author_name=author_name,
            )
        )
    return tuple(out)


def _content_for(message: Any) -> str | None:
    text = (message.content or "").strip()
    if text:
        return text
    attachments = getattr(message, "attachments", None)
    if attachments:
        names = ", ".join(a.filename for a in attachments)
        return f"[attachment: {names}]"
    if getattr(message, "embeds", None):
        return "[embed]"
    return None


async def collect_channel_context(
    channel: Any,
    *,
    before: Any,
    limit: int,
    bot_user: Any,
) -> list[BackfilledMessage]:
    """Return the last ``limit`` channel messages before ``before`` as context.

    Messages are returned in chronological order as "Name: content" transcript
    lines: humans newline-neutralized, the bot's own messages chunk-marker
    stripped and labeled with the bot's name so the combined view is unambiguous.
    Other bots are skipped, as are empty messages with no attachments. Any
    reactions present at capture time are appended as `` [reactions: :name:×N]``
    (see ``format_reactions``). Image attachments are additionally reported as
    ``ChannelContextImage`` ids so a visual tool can be pointed at a picture the
    transcript can only name.
    """
    out: list[BackfilledMessage] = []
    bot_name = (
        sanitize_author_name(bot_user.display_name)
        if bot_user is not None and getattr(bot_user, "display_name", None)
        else "assistant"
    )
    async for message in channel.history(limit=limit, before=before):
        is_self = bot_user is not None and message.author.id == bot_user.id
        if not is_self and message.author.bot:
            continue
        raw = _content_for(message)
        if raw is None:
            continue
        reactions = format_reactions(message)
        if is_self:
            name = bot_name
            clean = strip_chunk_marker(raw) + reactions
            transcript_line = f"{bot_name}: {clean}"
        else:
            name = sanitize_author_name(message.author.display_name)
            text = clean_message_text(raw) + reactions
            transcript_line = f"{name}: {text}"
        out.append(
            BackfilledMessage(
                transcript_line=transcript_line,
                images=_image_attachments(message, author_name=name),
            )
        )
    out.reverse()  # history() is newest-first; context must be chronological
    return out
