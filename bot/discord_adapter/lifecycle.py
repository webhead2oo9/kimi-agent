from __future__ import annotations

import asyncio
import logging
import time
from typing import Protocol

from workspace import WorkspaceManager
from memory.auto_retain import AutoRetainFlusher
from storage.conversations import ConversationStore
from tools.workspace.common import UserLocks

log = logging.getLogger(__name__)

# One retention sweep drains in bounded batches (one DELETE per batch) so a large
# first-run backlog doesn't hold the shared write lock for a single huge delete,
# capped per tick so the loop always yields back to event handling.
_RETENTION_BATCH_SIZE = 500
_RETENTION_MAX_PER_SWEEP = 5000
_SECONDS_PER_DAY = 86400


class AttachmentOrphanStore(Protocol):
    async def sweep_orphans(
        self,
        *,
        max_age_seconds: float,
        max_files: int,
    ) -> int: ...


class BrowserProfileStore(Protocol):
    async def sweep_expired(self) -> int: ...


class VideoSessionLifecycle(Protocol):
    async def sweep(self, *, now: float | None = None) -> tuple[int, bool]: ...


async def sweep_video_sessions_once(service: VideoSessionLifecycle) -> tuple[int, bool]:
    """Expire local sessions and best-effort the provider deletion outbox."""
    try:
        removed, complete = await service.sweep(now=time.time())
        if removed:
            log.info("Video session sweep: expired %d session(s)", removed)
        if not complete:
            log.warning("Video session sweep left provider deletions queued for retry")
        return removed, complete
    except Exception:
        log.exception("Video session sweep failed")
        return 0, False


async def video_session_sweeper(
    service: VideoSessionLifecycle,
    *,
    sweep_interval: int,
) -> None:
    # Run the startup pass inside the installed background task. READY must not
    # wait for a potentially large or network-degraded provider cleanup queue.
    while True:
        await sweep_video_sessions_once(service)
        await asyncio.sleep(sweep_interval)


async def sweep_attachment_orphans_once(
    store: AttachmentOrphanStore,
    *,
    max_age_seconds: float,
    max_files: int,
) -> int:
    """Best-effort cleanup for image stages left behind by process death."""
    try:
        removed = await store.sweep_orphans(
            max_age_seconds=max_age_seconds,
            max_files=max_files,
        )
        if removed:
            log.info("Attachment orphan sweep: removed %d file(s)", removed)
        return removed
    except Exception:
        log.exception("Attachment orphan sweep failed")
        return 0


async def attachment_orphan_sweeper(
    store: AttachmentOrphanStore,
    *,
    sweep_interval: int,
    max_age_seconds: float,
    max_files: int,
) -> None:
    """Periodically remove a bounded batch of abandoned image stages."""
    while True:
        await asyncio.sleep(sweep_interval)
        await sweep_attachment_orphans_once(
            store,
            max_age_seconds=max_age_seconds,
            max_files=max_files,
        )


async def workspace_sweeper(
    workspace_manager: WorkspaceManager,
    *,
    sweep_interval: int,
    workspace_locks: UserLocks | None = None,
    browser_profiles: BrowserProfileStore | None = None,
) -> None:
    """Prune expired workspace files."""
    while True:
        await asyncio.sleep(sweep_interval)
        try:
            if workspace_locks is None:
                removed = await workspace_manager.sweep_expired()
            else:
                async with workspace_locks.maintenance():
                    excluded = await workspace_locks.writer_keys()
                    removed = await workspace_manager.sweep_expired(
                        excluded_workspace_keys=excluded
                    )
            if removed:
                log.info("Workspace sweep: removed %d file(s)", removed)
        except Exception:
            log.exception("Workspace sweep failed")
        if browser_profiles is not None:
            try:
                removed_profiles = await browser_profiles.sweep_expired()
                if removed_profiles:
                    log.info("Browser profile sweep: removed %d profile(s)", removed_profiles)
            except Exception:
                log.exception("Browser profile sweep failed")


async def auto_retain_sweeper(
    flusher: AutoRetainFlusher,
    *,
    sweep_interval: int,
) -> None:
    """Background task that flushes idle conversations to Hindsight memory."""
    while True:
        await asyncio.sleep(sweep_interval)
        try:
            stats = await flusher.flush_once(time.time())
            if stats.retained or stats.failed:
                log.info(
                    "Auto-retain sweep: %d retained, %d skipped, %d failed",
                    stats.retained,
                    stats.skipped,
                    stats.failed,
                )
        except Exception:
            log.exception("Auto-retain sweep failed")


async def transcript_retention_sweeper(
    conversation_store: ConversationStore,
    *,
    retention_days: int,
    sweep_interval: int,
) -> None:
    """Background task that purges conversations idle past the retention window.

    Raw SQLite transcript only; distilled Hindsight memory is governed
    separately by /memory opt-out and the /privacy memory-delete path
    (docs/privacy.md). The caller only starts this task when ``retention_days > 0``.
    """
    retention_seconds = retention_days * _SECONDS_PER_DAY
    while True:
        await asyncio.sleep(sweep_interval)
        try:
            cutoff = time.time() - retention_seconds
            removed = 0
            while removed < _RETENTION_MAX_PER_SWEEP:
                batch = await conversation_store.delete_conversations_older_than(
                    cutoff, limit=_RETENTION_BATCH_SIZE
                )
                removed += batch
                if batch < _RETENTION_BATCH_SIZE:
                    break
            if removed:
                log.info(
                    "Transcript retention sweep: purged %d conversation(s) idle >%dd",
                    removed,
                    retention_days,
                )
        except Exception:
            log.exception("Transcript retention sweep failed")
