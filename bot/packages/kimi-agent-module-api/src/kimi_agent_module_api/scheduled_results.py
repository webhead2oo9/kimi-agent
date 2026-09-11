"""Durable subscriptions to confirmed Discord publications, separate from module jobs."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from kimi_agent_module_api.contracts import MessageRef, ModuleContractError

MAX_RESULT_FILE_BYTES = 8 * 1024 * 1024
MAX_RESULT_TOTAL_FILE_BYTES = 16 * 1024 * 1024


class ScheduledResultAccessError(ModuleContractError):
    """The subscription or its retained output is unavailable to this caller."""


@dataclass(frozen=True, slots=True)
class ScheduledResultAttachment:
    id: str
    filename: str
    size_bytes: int
    description: str | None = None


@dataclass(frozen=True, slots=True)
class ScheduledResultMessage:
    message: MessageRef
    content: str
    attachments: tuple[ScheduledResultAttachment, ...] = ()
    # The published Discord embed as JSON, capped at 32 KiB. No network fetches.
    embed_json: str | None = None
    embed_unavailable_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ScheduledResult:
    notification_id: str
    subscription: str
    task_id: str
    run_id: str
    revision: int
    guild_id: int
    owner_id: int
    published_at: float
    expires_at: float
    messages: tuple[ScheduledResultMessage, ...]
    outcome: Literal["completed"] = "completed"


class ScheduledResultFiles(Protocol):
    """Read only this notification's attachments during its handler invocation.

    The host rechecks access on every read. Reads are capped at 8 MiB each and
    16 MiB total per invocation, including repeated reads. Handles expire on
    return, cancellation, deletion, or retention expiry; copy bytes deliberately.
    """

    async def read(
        self, attachment_id: str, *, max_bytes: int = MAX_RESULT_FILE_BYTES
    ) -> bytes: ...


type ScheduledResultHandler = Callable[[ScheduledResult, ScheduledResultFiles], Awaitable[None]]


class ScheduledResults(Protocol):
    """A module-bound port. Declare each name in ``permissions.scheduled_results``.

    Register on every start. A name is scoped to one guild per registration;
    the same name may be registered in several guilds. Existing subscriptions
    survive downtime; new registrations never replay historical publications.
    Returning acknowledges the notification. Raising retries with the same ID.
    Use that ID to make your own writes/remote requests idempotent.
    """

    async def subscribe(
        self, name: str, *, guild_id: int, handler: ScheduledResultHandler
    ) -> None: ...

    async def unsubscribe(self, name: str, *, guild_id: int) -> None:
        """Forget this subscription and its pending/acknowledged notifications."""
        ...


def validate_result_subscription(name: str, guild_id: int) -> None:
    # Keep validation here usable by the host and standalone test fakes.
    if (
        not isinstance(name, str)
        or not 1 <= len(name) <= 64
        or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789_" for char in name)
    ):
        raise ModuleContractError("scheduled-result names use 1-64 lowercase letters, digits or _")
    if isinstance(guild_id, bool) or not isinstance(guild_id, int) or not 0 < guild_id < 2**64:
        raise ModuleContractError("scheduled-result subscriptions require a positive guild ID")


def validate_result_read_limit(max_bytes: int) -> None:
    if (
        isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or not (0 < max_bytes <= MAX_RESULT_FILE_BYTES)
    ):
        raise ScheduledResultAccessError("attachment read limit must be between 1 byte and 8 MiB")
