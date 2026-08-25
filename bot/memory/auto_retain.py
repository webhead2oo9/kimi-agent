from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from memory.mutations import user_memory_mutation
from storage.auto_retain import AutoRetainStore, SliceRow
from utils.format import iso_timestamp

log = logging.getLogger(__name__)

_SOURCE_KIND = "discord_auto_retain"


class AutoRetainMemoryClient(Protocol):
    async def retain(
        self,
        bank_id: str,
        content: str,
        context: str = "",
        tags: list[str] | None = None,
        document_id: str = "",
        metadata: dict[str, str] | None = None,
        timestamp: str | None = None,
        update_mode: str | None = None,
        retain_async: bool = False,
    ) -> bool: ...


class AutoRetainPreferenceStore(Protocol):
    async def is_memory_enabled(self, user_id: str) -> bool: ...


# (client, discord_id, display_name) -> bank_id; matches memory.banks.ensure_user_bank.
EnsureUserBank = Callable[[Any, str, str], Awaitable[str | None]]


@dataclass(frozen=True)
class FlushStats:
    retained: int = 0
    skipped: int = 0
    failed: int = 0


class AutoRetainFlusher:
    """Ships idle-conversation transcript slices to each enabled user's bank.

    The persisted SQLite transcript is the buffer; a per-(conversation, user)
    watermark makes flushing crash-safe and idempotent. Slices structurally
    contain only the subject user's messages plus the bot's replies. Other
    participants' messages never enter the subject's bank.
    """

    def __init__(
        self,
        *,
        store: AutoRetainStore,
        preference_store: AutoRetainPreferenceStore,
        memory_client: AutoRetainMemoryClient,
        ensure_user_bank: EnsureUserBank,
        get_bot_name: Callable[[], str],
        idle_seconds: float,
        backfill_horizon_seconds: float,
        min_user_chars: int,
        max_content_chars: int,
        max_flushes_per_sweep: int,
    ) -> None:
        self._store = store
        self._preferences = preference_store
        self._memory = memory_client
        self._ensure_user_bank = ensure_user_bank
        self._get_bot_name = get_bot_name
        self._idle_seconds = idle_seconds
        self._backfill_horizon_seconds = backfill_horizon_seconds
        self._min_user_chars = min_user_chars
        self._max_content_chars = max_content_chars
        self._max_flushes_per_sweep = max_flushes_per_sweep

    async def flush_once(self, now: float) -> FlushStats:
        idle_cutoff = now - self._idle_seconds
        backfill_cutoff = now - self._backfill_horizon_seconds
        pending = await self._store.pending(idle_cutoff)

        retained = skipped = failed = 0
        for item in pending:
            if retained >= self._max_flushes_per_sweep:
                break
            try:
                outcome = await self._flush_slice(item, backfill_cutoff)
            except Exception:
                failed += 1
                log.exception(
                    "Auto-retain flush failed for conversation %s user %s",
                    item.conversation_id,
                    item.user_id,
                )
                continue
            if outcome == "retained":
                retained += 1
            elif outcome == "skipped":
                skipped += 1
            else:
                failed += 1
        return FlushStats(retained=retained, skipped=skipped, failed=failed)

    async def _flush_slice(self, item, backfill_cutoff: float) -> str:
        conversation_id = item.conversation_id
        user_id = item.user_id
        # Preference checks, bank creation, retain calls, and watermark commit
        # share the same per-user boundary as forget_user_memory. A confirmed
        # deletion therefore cannot be followed by an already-running flush that
        # recreates the bank from its stale transcript slice.
        async with user_memory_mutation(user_id):
            # ``pending()`` is only a snapshot. A confirmed durable deletion may
            # have been committed while this slice waited for the per-user
            # mutation boundary. Do not create/retain into that user's bank; the
            # deletion attempt will either fast-forward this watermark or remove
            # the transcript, and a later sweep can safely reconsider the slice
            # if the request is ever cleared without doing either.
            if await self._store.has_pending_privacy_deletion(user_id):
                return "skipped"

            # ``flush_once`` discovers work before entering the per-user mutation
            # boundary. Delete-memory can fast-forward this watermark while the
            # item waits, so only the committed value read inside the guard is safe.
            current_watermark = await self._store.get_watermark(
                conversation_id,
                user_id,
            )
            after_id = current_watermark or 0
            end_id = await self._store.conversation_max_message_id(conversation_id)
            if end_id <= after_id:
                return "skipped"

            # First sight of a conversation that went idle long before the sweeper
            # could have seen it: mark as handled without ingesting history.
            if current_watermark is None and item.last_active_at < backfill_cutoff:
                await self._store.set_watermark(conversation_id, user_id, end_id)
                return "skipped"

            # Memory defaults on. Opt-out is honored at flush time; the watermark
            # still advances so re-enabling memory never ingests content from the
            # disabled window.
            if not await self._preferences.is_memory_enabled(user_id):
                await self._store.set_watermark(conversation_id, user_id, end_id)
                return "skipped"

            return await self._retain_slice(
                conversation_id=conversation_id,
                user_id=user_id,
                after_id=after_id,
                end_id=end_id,
            )

    async def _retain_slice(
        self,
        *,
        conversation_id: int,
        user_id: str,
        after_id: int,
        end_id: int,
    ) -> str:
        all_rows = await self._store.slice_rows(conversation_id, after_id, end_id)
        rows = _attribute_to_subject(all_rows, user_id)
        user_rows = [r for r in rows if r.role == "user" and r.content.strip()]
        user_chars = sum(len(r.content) for r in user_rows)
        if not user_rows or user_chars < self._min_user_chars:
            await self._store.set_watermark(conversation_id, user_id, end_id)
            return "skipped"

        user_name = user_rows[-1].user_name or "Member"
        bot_name = self._get_bot_name()
        lines = [self._format_line(r, user_name, bot_name) for r in rows if r.content.strip()]
        meta = await self._store.conversation_meta(conversation_id)
        anchor = user_rows[-1]
        last_user_ts = user_rows[-1].source_created_at

        bank_id = await self._ensure_user_bank(self._memory, user_id, user_name)
        if bank_id is None:
            return "failed"
        # Conversation-derived memory is scoped to its originating guild so recall
        # in another community never surfaces it; a guild-less slice stays unscoped
        # (no ``guild:`` tag) and is therefore never recalled, which fails closed.
        retain_tags = ["source:auto_retain", "scope:user"]
        if meta.guild_id:
            retain_tags.append(f"guild:{meta.guild_id}")
        for part_index, part in enumerate(_split_parts(lines, self._max_content_chars)):
            document_id = f"auto-retain:{user_id}:{conversation_id}:{after_id + 1}"
            if part_index:
                document_id = f"{document_id}:p{part_index}"
            stored = await self._memory.retain(
                bank_id=bank_id,
                content="\n".join(part),
                context=(
                    f"Discord conversation between {user_name} (user) and "
                    f"{bot_name} (assistant). Retain information about "
                    f"{user_name}: what they said, asked about, prefer, own, and "
                    "are working on. Do not retain facts about other people "
                    "mentioned in passing. Write every retained fact in English, "
                    "even when the conversation contains other languages."
                ),
                tags=retain_tags,
                document_id=document_id,
                metadata={
                    "source_kind": _SOURCE_KIND,
                    "source_version": "1",
                    "subject_user_id": user_id,
                    "conversation_id": str(conversation_id),
                    "guild_id": meta.guild_id or "",
                    "channel_id": meta.channel_id or "",
                    "channel_name": meta.channel_name,
                    "anchor_message_id": str(anchor.id),
                    "anchor_source_created_at": str(anchor.source_created_at),
                    "start_message_id": str(after_id + 1),
                    "end_message_id": str(end_id),
                },
                timestamp=iso_timestamp(last_user_ts) if last_user_ts else None,
                update_mode="replace",
                retain_async=False,
            )
            if not stored:
                # Leave the watermark; the next sweep retries the whole slice and
                # the deterministic document id makes the retry a replace.
                return "failed"

        await self._store.set_watermark(conversation_id, user_id, end_id)
        log.info(
            "Auto-retained conversation %s for user %s (messages %d-%d, %d chars)",
            conversation_id,
            user_id,
            after_id + 1,
            end_id,
            user_chars,
        )
        return "retained"

    @staticmethod
    def _format_line(row: SliceRow, user_name: str, bot_name: str) -> str:
        author = bot_name if row.role == "assistant" else (row.user_name or user_name)
        epoch = row.source_created_at
        stamp = f" ({iso_timestamp(epoch)})" if epoch else ""
        return f"{author}{stamp}: {row.content}"


def _attribute_to_subject(rows: list[SliceRow], user_id: str) -> list[SliceRow]:
    """Keep the subject's messages and only the bot replies answering them.

    Assistant rows are attributed to the most recent preceding user turn in the
    full interleaved transcript; replies to other participants (or with no
    preceding user turn in the slice) are dropped so their content never enters
    the subject's bank secondhand.
    """
    kept: list[SliceRow] = []
    last_user_id: str | None = None
    for row in rows:
        if row.role == "user":
            last_user_id = row.user_id
            if row.user_id == user_id:
                kept.append(row)
        elif row.role == "assistant" and last_user_id == user_id:
            kept.append(row)
    return kept


def _split_parts(lines: list[str], max_chars: int) -> list[list[str]]:
    parts: list[list[str]] = []
    current: list[str] = []
    size = 0
    for line in lines:
        line_size = len(line) + 1
        if current and size + line_size > max_chars:
            parts.append(current)
            current = []
            size = 0
        current.append(line)
        size += line_size
    if current:
        parts.append(current)
    return parts
