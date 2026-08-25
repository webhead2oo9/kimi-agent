from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import discord

from storage.conversations import (
    CHANNEL_SHARED,
    OWNER_ONLY,
    ConversationAccessScope,
)

if TYPE_CHECKING:
    from app.threads import ThreadHandoffManager


@dataclass(frozen=True)
class ResolvedConversation:
    key: str
    db_conversation_id: int | None
    owner_user_id: str | None = None
    access_scope: ConversationAccessScope = CHANNEL_SHARED
    allow_bot_authored_reply_context: bool = False


async def resolve_conversation_for_message(
    message: discord.Message,
    *,
    allow_new_root: bool,
    conversation_store: Any | None,
    thread_handoff: ThreadHandoffManager | None,
) -> ResolvedConversation | None:
    context_channel_id = str(message.channel.id)
    reply_message_id = referenced_message_id(message)
    allow_bot_authored_reply_context = False
    if conversation_store is not None and reply_message_id is not None:
        # No-mention continuation only when replying to one of the bot's own
        # messages. Replying to a human message must require a fresh @mention.
        resolved = await conversation_store.get_continuation_conversation_for_reply(
            reply_message_id,
            channel_id=context_channel_id,
            requester_user_id=str(message.author.id),
        )
        if resolved is not None:
            return ResolvedConversation(
                key=resolved.key,
                db_conversation_id=resolved.id,
                owner_user_id=resolved.owner_user_id,
                access_scope=resolved.access_scope,
            )
        # An owner-only reply cannot expose its persisted rope to a different
        # user. The referenced Discord message itself may still be this bot's
        # public answer, though, so mark the fresh root to quote only that
        # visible message as ephemeral reply context.
        mapped = await conversation_store.get_conversation_by_discord_message(
            reply_message_id,
            channel_id=context_channel_id,
        )
        requester_user_id = str(message.author.id)
        allow_bot_authored_reply_context = bool(
            mapped is not None
            and mapped.access_scope == OWNER_ONLY
            and (not mapped.owner_user_id or mapped.owner_user_id != requester_user_id)
        )

    # A bot-managed handoff thread: every message in it continues the mapped
    # root (docs/thread-handoff.md). This asks whether the thread is *managed*,
    # not whether it is auto-responding. A paused thread keeps its transcript,
    # so a mention in it continues the same conversation. A stale id whose row
    # is gone falls through to a fresh thread-scoped root.
    if (
        conversation_store is not None
        and thread_handoff is not None
        and isinstance(message.channel, discord.Thread)
        and thread_handoff.is_managed(message.channel.id)
    ):
        record = await conversation_store.get_thread_conversation(str(message.channel.id))
        if record is not None:
            requester_user_id = str(message.author.id)
            if record.access_scope != OWNER_ONLY or (
                bool(record.owner_user_id) and record.owner_user_id == requester_user_id
            ):
                return ResolvedConversation(
                    key=record.key,
                    db_conversation_id=record.id,
                    owner_user_id=record.owner_user_id,
                    access_scope=record.access_scope,
                )
            # The mapping is live but private to somebody else. Keep the managed
            # thread enrolled for its owner, while routing this requester to a
            # fresh root below rather than exposing the private transcript.
        else:
            thread_handoff.forget(message.channel.id)

    if not allow_new_root:
        return None

    key = conversation_key_for_message(message)
    if conversation_store is None:
        return ResolvedConversation(
            key=key,
            db_conversation_id=None,
            owner_user_id=str(message.author.id),
            allow_bot_authored_reply_context=allow_bot_authored_reply_context,
        )

    conv_id = await conversation_store.get_or_create(
        key,
        getattr(message.channel, "name", "DM"),
        guild_id=str(message.guild.id) if message.guild else None,
        channel_id=context_channel_id,
        thread_id=(
            str(message.channel.id) if isinstance(message.channel, discord.Thread) else None
        ),
        root_discord_message_id=str(message.id),
        owner_user_id=str(message.author.id),
    )
    return ResolvedConversation(
        key=key,
        db_conversation_id=conv_id,
        owner_user_id=str(message.author.id),
        allow_bot_authored_reply_context=allow_bot_authored_reply_context,
    )


def referenced_message_id(message: discord.Message) -> str | None:
    reference = getattr(message, "reference", None)
    if reference is None:
        return None
    reference_channel_id = getattr(reference, "channel_id", None)
    if reference_channel_id is not None and str(reference_channel_id) != str(message.channel.id):
        return None
    message_id = getattr(reference, "message_id", None)
    if message_id is None:
        resolved = getattr(reference, "resolved", None)
        message_id = getattr(resolved, "id", None)
    return str(message_id) if message_id is not None else None


def conversation_key_for_message(message: discord.Message) -> str:
    guild_id = str(message.guild.id) if message.guild else None
    channel_id = str(message.channel.id)
    thread_id = str(message.channel.id) if isinstance(message.channel, discord.Thread) else None
    root_id = str(message.id)
    if guild_id:
        return f"guild:{guild_id}:channel:{channel_id}:thread:{thread_id or 'main'}:root:{root_id}"
    return f"dm:{message.author.id}:root:{root_id}"


def response_lock_key(
    message: discord.Message,
    *,
    resolved_conversation: ResolvedConversation | None = None,
) -> str:
    if resolved_conversation is not None:
        return resolved_conversation.key
    return conversation_key_for_message(message)
