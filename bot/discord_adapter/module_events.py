"""Normalize discord.py gateway events into the module event contract.

Payloads carry stable IDs and whatever cannot be re-fetched later (deleted
content, pre-edit content, removed roles, audit-log changes); never SDK
objects. Modules that need live state call a declared Discord action.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import discord
from discord.ext import commands

from kimi_agent_module_api import events as ev
from kimi_agent_module_api.contracts import (
    AttachmentSnapshot,
    MemberSnapshot,
    MessageRef,
    MessageSnapshot,
)

log = logging.getLogger(__name__)

type Publish = Callable[[str, object], None]


def _ts(value: Any) -> float | None:
    return float(value.timestamp()) if value is not None else None


def attachment_snapshot(attachment: discord.Attachment) -> AttachmentSnapshot:
    return AttachmentSnapshot(
        attachment_id=int(attachment.id),
        filename=str(attachment.filename),
        url=str(attachment.url),
        size=int(attachment.size),
        content_type=attachment.content_type,
    )


def message_ref(message: discord.Message) -> MessageRef:
    return MessageRef(
        guild_id=int(message.guild.id) if message.guild is not None else 0,
        channel_id=int(message.channel.id),
        message_id=int(message.id),
        parent_channel_id=(
            int(parent) if (parent := getattr(message.channel, "parent_id", None)) else None
        ),
    )


def message_snapshot(message: discord.Message) -> MessageSnapshot:
    reference = getattr(message, "reference", None)
    return MessageSnapshot(
        ref=message_ref(message),
        author_id=int(message.author.id),
        content=message.content or "",
        attachments=tuple(attachment_snapshot(a) for a in message.attachments),
        jump_url=str(message.jump_url),
        created_at=_ts(message.created_at) or 0.0,
        author_display_name=str(getattr(message.author, "display_name", message.author)),
        author_is_bot=bool(getattr(message.author, "bot", False)),
        embed_image_urls=_embed_image_urls(message),
        reply_to_message_id=(
            int(reference.message_id)
            if reference is not None and reference.message_id is not None
            else None
        ),
        pinned=bool(getattr(message, "pinned", False)),
        edited_at=_ts(getattr(message, "edited_at", None)),
        embed_texts=_embed_texts(message),
    )


def _embed_image_urls(message: discord.Message) -> tuple[str, ...]:
    urls: list[str] = []
    for embed in getattr(message, "embeds", ()) or ():
        for part in (getattr(embed, "image", None), getattr(embed, "thumbnail", None)):
            url = getattr(part, "proxy_url", None) or getattr(part, "url", None)
            if url:
                urls.append(str(url))
    return tuple(urls)


def _embed_texts(message: discord.Message) -> tuple[str, ...]:
    texts: list[str] = []
    for embed in getattr(message, "embeds", ()) or ():
        parts = [
            str(value).strip()
            for value in (
                getattr(embed, "title", None),
                getattr(embed, "description", None),
                getattr(getattr(embed, "author", None), "name", None),
                getattr(embed, "url", None),
            )
            if value
        ]
        for field in getattr(embed, "fields", ()) or ():
            name = str(getattr(field, "name", "")).strip()
            value = str(getattr(field, "value", "")).strip()
            if name or value:
                parts.append(f"{name}: {value}".strip(": "))
        footer = str(getattr(getattr(embed, "footer", None), "text", "")).strip()
        if footer:
            parts.append(footer)
        if parts:
            texts.append(" â€” ".join(parts))
    return tuple(texts)


def member_snapshot(member: discord.Member) -> MemberSnapshot:
    return MemberSnapshot(
        guild_id=int(member.guild.id),
        user_id=int(member.id),
        display_name=str(member.display_name),
        role_ids=tuple(int(role.id) for role in member.roles),
        is_bot=bool(member.bot),
        joined_at=_ts(member.joined_at),
        timed_out_until=_ts(getattr(member, "timed_out_until", None)),
    )


_AUDIT_ACTIONS: dict[Any, ev.AuditAction] = {}


def _audit_action(action: Any) -> ev.AuditAction:
    if not _AUDIT_ACTIONS:
        actions = discord.AuditLogAction
        _AUDIT_ACTIONS.update(
            {
                actions.ban: "ban",
                actions.unban: "unban",
                actions.kick: "kick",
                actions.member_update: "member_update",
            }
        )
    return _AUDIT_ACTIONS.get(action, "other")


def audit_entry_event(
    entry: discord.AuditLogEntry, *, self_user_id: int | None = None
) -> ev.AuditLogEntryEvent | None:
    if entry.guild is None:
        return None
    changes: list[tuple[str, Any, Any]] = []
    before = getattr(entry, "before", None)
    after = getattr(entry, "after", None)
    for attribute in ("timed_out_until", "roles", "nick", "deaf", "mute"):
        old = getattr(before, attribute, None) if before is not None else None
        new = getattr(after, attribute, None) if after is not None else None
        if old is None and new is None:
            continue
        changes.append((attribute, _jsonable(old), _jsonable(new)))
    action = _audit_action(entry.action)
    until: float | None = None
    if action == "member_update" and any(name == "timed_out_until" for name, _, _ in changes):
        after_until = getattr(after, "timed_out_until", None)
        until = _ts(after_until) if after_until is not None else None
        action = "timeout" if until is not None else "timeout_cleared"
    target = getattr(entry, "target", None)
    target_id = getattr(target, "id", None) if target is not None else None
    if target_id is None:
        # discord.py keeps the raw id even when the target was never cached.
        target_id = getattr(entry, "_target_id", None)
    return ev.AuditLogEntryEvent(
        guild_id=int(entry.guild.id),
        entry_id=int(entry.id),
        action=action,
        raw_action=str(getattr(entry.action, "name", entry.action)),
        actor_id=int(entry.user_id) if entry.user_id else None,
        target_id=int(target_id) if target_id is not None else None,
        reason=entry.reason,
        changes=tuple(changes),
        created_at=_ts(entry.created_at) or 0.0,
        target_display_name=(
            getattr(target, "display_name", None) or getattr(target, "name", None)
        ),
        until=until,
        actor_is_self=bool(
            entry.user_id and self_user_id is not None and int(entry.user_id) == self_user_id
        ),
    )


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if hasattr(value, "timestamp"):
        return _ts(value)
    if isinstance(value, list | tuple):
        return [getattr(item, "id", item) for item in value]
    return str(value)


class ModuleEventPublisher:
    """Installs gateway listeners on the bot and publishes normalized events."""

    def __init__(self, bot: commands.Bot, publish: Publish) -> None:
        self._bot = bot
        self._publish = publish
        self._listeners: list[tuple[Any, str]] = []

    def install(self) -> None:
        for name, callback in (
            ("on_message", self.on_message),
            ("on_message_edit", self.on_message_edit),
            ("on_message_delete", self.on_message_delete),
            ("on_raw_message_delete", self.on_raw_message_delete),
            ("on_raw_bulk_message_delete", self.on_raw_bulk_message_delete),
            ("on_member_join", self.on_member_join),
            ("on_member_remove", self.on_member_remove),
            ("on_member_update", self.on_member_update),
            ("on_audit_log_entry_create", self.on_audit_log_entry_create),
        ):
            self._bot.add_listener(callback, name)
            self._listeners.append((callback, name))

    def uninstall(self) -> None:
        for callback, name in self._listeners:
            self._bot.remove_listener(callback, name)
        self._listeners.clear()

    def _safe(self, topic: str, payload: object) -> None:
        try:
            self._publish(topic, payload)
        except Exception:
            log.exception("Failed to publish %s", topic)

    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None:
            return
        self._safe(
            ev.TOPIC_MESSAGE,
            ev.MessageEvent(
                message=message_snapshot(message), author_is_bot=bool(message.author.bot)
            ),
        )

    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if after.guild is None:
            return
        self._safe(
            ev.TOPIC_MESSAGE_EDIT,
            ev.MessageEditEvent(
                ref=message_ref(after),
                author_id=int(after.author.id),
                before_content=before.content if before is not None else None,
                after_content=after.content or "",
                edited_at=_ts(after.edited_at) or 0.0,
            ),
        )

    async def on_message_delete(self, message: discord.Message) -> None:
        if message.guild is None:
            return
        self._safe(
            ev.TOPIC_MESSAGE_DELETE,
            ev.MessageDeleteEvent(
                ref=message_ref(message),
                author_id=int(message.author.id) if message.author is not None else None,
                cached_content=message.content or None,
                cached_attachments=tuple(attachment_snapshot(a) for a in message.attachments),
            ),
        )

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        if payload.guild_id is None:
            return
        # discord.py dispatches on_message_delete as well when the raw payload
        # carries a cached message. Let that path publish the richer snapshot;
        # this raw listener remains the fallback for uncached deletions.
        if payload.cached_message is not None:
            return
        self._safe(
            ev.TOPIC_MESSAGE_DELETE,
            ev.MessageDeleteEvent(
                ref=MessageRef(
                    int(payload.guild_id), int(payload.channel_id), int(payload.message_id)
                ),
                author_id=None,
                cached_content=None,
                cached_attachments=(),
            ),
        )

    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        if payload.guild_id is None:
            return
        cached = {int(message.id): message for message in payload.cached_messages}
        self._safe(
            ev.TOPIC_MESSAGE_BULK_DELETE,
            ev.MessageBulkDeleteEvent(
                refs=tuple(
                    (
                        message_ref(cached[int(message_id)])
                        if int(message_id) in cached
                        else MessageRef(
                            int(payload.guild_id), int(payload.channel_id), int(message_id)
                        )
                    )
                    for message_id in sorted(payload.message_ids)
                )
            ),
        )

    async def on_member_join(self, member: discord.Member) -> None:
        self._safe(
            ev.TOPIC_MEMBER_JOIN,
            ev.MemberJoinEvent(
                member=member_snapshot(member),
                account_created_at=_ts(member.created_at) or 0.0,
            ),
        )

    async def on_member_remove(self, member: discord.Member) -> None:
        self._safe(
            ev.TOPIC_MEMBER_REMOVE,
            ev.MemberRemoveEvent(
                guild_id=int(member.guild.id),
                user_id=int(member.id),
                roles_at_removal=tuple(int(role.id) for role in member.roles),
                display_name=str(member.display_name),
                is_bot=bool(member.bot),
            ),
        )

    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        before_roles = {int(role.id) for role in before.roles}
        after_roles = {int(role.id) for role in after.roles}
        self._safe(
            ev.TOPIC_MEMBER_UPDATE,
            ev.MemberUpdateEvent(
                guild_id=int(after.guild.id),
                user_id=int(after.id),
                roles_added=tuple(sorted(after_roles - before_roles)),
                roles_removed=tuple(sorted(before_roles - after_roles)),
                timed_out_until_before=_ts(getattr(before, "timed_out_until", None)),
                timed_out_until_after=_ts(getattr(after, "timed_out_until", None)),
                nickname_before=before.nick,
                nickname_after=after.nick,
                display_name=str(after.display_name),
                is_bot=bool(after.bot),
                role_names={
                    int(role.id): str(role.name)
                    for role in (*before.roles, *after.roles)
                    if int(role.id) in (before_roles ^ after_roles)
                },
            ),
        )

    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry) -> None:
        bot_user = getattr(self._bot, "user", None)
        event = audit_entry_event(
            entry, self_user_id=int(bot_user.id) if bot_user is not None else None
        )
        if event is not None:
            self._safe(ev.TOPIC_AUDIT_LOG_ENTRY, event)


__all__ = [
    "ModuleEventPublisher",
    "attachment_snapshot",
    "audit_entry_event",
    "member_snapshot",
    "message_ref",
    "message_snapshot",
]
