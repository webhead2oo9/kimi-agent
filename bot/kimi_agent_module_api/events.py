"""Normalized core Discord events published on the module event bus.

Each dataclass carries what a subscriber cannot re-fetch after the fact:
deleted or pre-edit content, removed members' roles, role and timeout deltas,
audit-log changes. Anything still live is fetched through a declared Discord
action instead. Payloads never contain Discord SDK objects.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from kimi_agent_module_api.contracts import (
    CORE_TOPIC_PREFIX,
    AttachmentSnapshot,
    MemberSnapshot,
    MessageRef,
    MessageSnapshot,
)

TOPIC_MESSAGE = f"{CORE_TOPIC_PREFIX}.message"
TOPIC_MESSAGE_EDIT = f"{CORE_TOPIC_PREFIX}.message_edit"
TOPIC_MESSAGE_DELETE = f"{CORE_TOPIC_PREFIX}.message_delete"
TOPIC_MEMBER_JOIN = f"{CORE_TOPIC_PREFIX}.member_join"
TOPIC_MEMBER_REMOVE = f"{CORE_TOPIC_PREFIX}.member_remove"
TOPIC_MEMBER_UPDATE = f"{CORE_TOPIC_PREFIX}.member_update"
TOPIC_AUDIT_LOG_ENTRY = f"{CORE_TOPIC_PREFIX}.audit_log_entry"

CORE_TOPICS: frozenset[str] = frozenset(
    {
        TOPIC_MESSAGE,
        TOPIC_MESSAGE_EDIT,
        TOPIC_MESSAGE_DELETE,
        TOPIC_MEMBER_JOIN,
        TOPIC_MEMBER_REMOVE,
        TOPIC_MEMBER_UPDATE,
        TOPIC_AUDIT_LOG_ENTRY,
    }
)


@dataclass(frozen=True, slots=True)
class MessageEvent:
    message: MessageSnapshot
    author_is_bot: bool


@dataclass(frozen=True, slots=True)
class MessageEditEvent:
    ref: MessageRef
    author_id: int
    before_content: str | None
    after_content: str
    edited_at: float


@dataclass(frozen=True, slots=True)
class MessageDeleteEvent:
    ref: MessageRef
    author_id: int | None
    cached_content: str | None
    cached_attachments: tuple[AttachmentSnapshot, ...]


@dataclass(frozen=True, slots=True)
class MessageBulkDeleteEvent:
    refs: tuple[MessageRef, ...]


@dataclass(frozen=True, slots=True)
class MemberJoinEvent:
    member: MemberSnapshot
    account_created_at: float


@dataclass(frozen=True, slots=True)
class MemberRemoveEvent:
    guild_id: int
    user_id: int
    roles_at_removal: tuple[int, ...]
    display_name: str = ""
    is_bot: bool = False


@dataclass(frozen=True, slots=True)
class MemberUpdateEvent:
    guild_id: int
    user_id: int
    roles_added: tuple[int, ...]
    roles_removed: tuple[int, ...]
    timed_out_until_before: float | None
    timed_out_until_after: float | None
    nickname_before: str | None
    nickname_after: str | None
    display_name: str = ""
    is_bot: bool = False
    # Names for every role id in roles_added / roles_removed.
    role_names: Mapping[int, str] = field(default_factory=dict)


type AuditAction = Literal[
    "ban", "unban", "kick", "timeout", "timeout_cleared", "member_update", "other"
]


@dataclass(frozen=True, slots=True)
class AuditLogEntryEvent:
    guild_id: int
    entry_id: int
    action: AuditAction
    raw_action: str
    actor_id: int | None
    target_id: int | None
    reason: str | None
    changes: tuple[tuple[str, Any, Any], ...]
    created_at: float
    target_display_name: str | None = None
    # Timeout expiry (unix seconds) for `timeout` actions.
    until: float | None = None
    # True when this bot performed the action (e.g. through a module command).
    actor_is_self: bool = False
