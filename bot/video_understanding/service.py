from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
import logging
import secrets
import time
from typing import Protocol

from video_understanding.client import (
    VideoEvidence,
    VideoInteractionError,
    VideoInteractionResult,
    VideoUsage,
)

log = logging.getLogger(__name__)

_DELETION_BATCH_SIZE = 100
_PRIVACY_DELETION_BATCH_SIZE = 4


class VideoInteractionClient(Protocol):
    async def start(
        self,
        *,
        url: str,
        question: str,
        model: str,
        thinking_level: str,
        max_output_tokens: int,
    ) -> VideoInteractionResult: ...

    async def ask(
        self,
        *,
        previous_interaction_id: str,
        question: str,
        model: str,
        thinking_level: str,
        max_output_tokens: int,
    ) -> VideoInteractionResult: ...

    async def delete(self, interaction_id: str) -> None: ...

    async def close(self) -> None: ...


class VideoSessionError(RuntimeError):
    """A model-facing session or provider failure."""

    def __init__(
        self,
        message: str,
        *,
        result: VideoInteractionResult | None = None,
    ) -> None:
        super().__init__(message)
        self.result = result


class VideoSessionRecord(Protocol):
    handle: str
    youtube_url: str
    youtube_video_id: str
    model: str
    latest_interaction_id: str
    interaction_count: int
    expires_at: float


class VideoDeletionRecord(Protocol):
    interaction_id: str
    retry_at: float


class VideoSessionRepository(Protocol):
    async def create_session(
        self,
        *,
        handle: str,
        conversation_id: int,
        actor_user_id: str,
        guild_id: str,
        youtube_url: str,
        youtube_video_id: str,
        model: str,
        interaction_id: str,
        now: float,
        expires_at: float,
    ) -> None: ...

    async def find_sessions(
        self,
        *,
        conversation_id: int,
        actor_user_id: str,
        guild_id: str,
        now: float,
        handle: str | None,
    ) -> Sequence[VideoSessionRecord]: ...

    async def advance_session(
        self,
        *,
        handle: str,
        actor_user_id: str,
        expected_interaction_id: str,
        interaction_id: str,
        now: float,
        expires_at: float,
        max_interactions: int,
    ) -> bool: ...

    async def delete_user_sessions(self, user_id: str) -> int: ...

    async def enqueue_deletion(
        self,
        *,
        interaction_id: str,
        actor_user_id: str,
        now: float,
    ) -> None: ...

    async def delete_expired(self, now: float, *, limit: int) -> int: ...

    async def pending_deletions(
        self,
        *,
        user_id: str | None,
        limit: int,
    ) -> Sequence[VideoDeletionRecord]: ...

    async def complete_deletion(self, interaction_id: str) -> None: ...

    async def fail_deletion(
        self,
        interaction_id: str,
        error: str,
        *,
        now: float | None = None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class VideoSessionConfig:
    model: str
    thinking_level: str
    max_output_tokens: int
    max_session_interactions: int
    session_ttl_minutes: int


@dataclass(frozen=True, slots=True)
class VideoAnalysis:
    session: str
    youtube_url: str
    answer: str
    evidence: tuple[VideoEvidence, ...]
    limitations: tuple[str, ...]
    model: str
    usage: VideoUsage


class VideoUnderstandingService:
    """Coordinates Gemini state with durable, actor-scoped local handles."""

    def __init__(
        self,
        *,
        client: VideoInteractionClient | None,
        get_store: Callable[[], VideoSessionRepository | None],
    ) -> None:
        self._client = client
        self._get_store = get_store

    @property
    def available(self) -> bool:
        return self._client is not None

    async def start(
        self,
        *,
        conversation_id: int,
        actor_user_id: str,
        guild_id: str,
        youtube_url: str,
        youtube_video_id: str,
        question: str,
        config: VideoSessionConfig,
    ) -> VideoAnalysis:
        client = self._require_client()
        store = self._require_store()
        try:
            result = await client.start(
                url=youtube_url,
                question=question,
                model=config.model,
                thinking_level=config.thinking_level,
                max_output_tokens=config.max_output_tokens,
            )
        except VideoInteractionError as exc:
            if exc.interaction_id:
                await self._queue_orphan(store, actor_user_id, exc.interaction_id)
            raise
        now = time.time()
        handle = f"video_{secrets.token_urlsafe(12)}"
        try:
            await store.create_session(
                handle=handle,
                conversation_id=conversation_id,
                actor_user_id=actor_user_id,
                guild_id=guild_id,
                youtube_url=youtube_url,
                youtube_video_id=youtube_video_id,
                model=config.model,
                interaction_id=result.interaction_id,
                now=now,
                expires_at=now + config.session_ttl_minutes * 60,
            )
        except Exception as exc:
            await self._queue_orphan(store, actor_user_id, result.interaction_id)
            raise VideoSessionError(
                "The video was analyzed, but its follow-up session could not be saved.",
                result=result,
            ) from exc
        return _analysis(handle, youtube_url, result)

    async def ask(
        self,
        *,
        conversation_id: int,
        actor_user_id: str,
        guild_id: str,
        session: str | None,
        question: str,
        config: VideoSessionConfig,
    ) -> VideoAnalysis:
        client = self._require_client()
        store = self._require_store()
        now = time.time()
        matches = await store.find_sessions(
            conversation_id=conversation_id,
            actor_user_id=actor_user_id,
            guild_id=guild_id,
            now=now,
            handle=session,
        )
        if not matches:
            raise VideoSessionError(
                "No active video session matches this conversation. Start one with a YouTube URL."
            )
        if len(matches) > 1:
            raise VideoSessionError(
                "Several video sessions are active. Pass the session returned by the relevant start call."
            )
        current = matches[0]
        if current.interaction_count >= config.max_session_interactions:
            raise VideoSessionError(
                "This video session reached its follow-up limit. Start a new session to continue."
            )

        try:
            result = await client.ask(
                previous_interaction_id=current.latest_interaction_id,
                question=question,
                model=current.model,
                thinking_level=config.thinking_level,
                max_output_tokens=config.max_output_tokens,
            )
        except VideoInteractionError as exc:
            if exc.interaction_id:
                await self._queue_orphan(store, actor_user_id, exc.interaction_id)
            raise
        try:
            advanced = await store.advance_session(
                handle=current.handle,
                actor_user_id=actor_user_id,
                expected_interaction_id=current.latest_interaction_id,
                interaction_id=result.interaction_id,
                now=now,
                expires_at=now + config.session_ttl_minutes * 60,
                max_interactions=config.max_session_interactions,
            )
        except Exception as exc:
            await self._queue_orphan(store, actor_user_id, result.interaction_id)
            raise VideoSessionError(
                "The follow-up was analyzed, but its session state could not be saved.",
                result=result,
            ) from exc
        if not advanced:
            await self._queue_orphan(store, actor_user_id, result.interaction_id)
            raise VideoSessionError(
                "That video session changed concurrently. Retry the follow-up against its latest state.",
                result=result,
            )
        return _analysis(current.handle, current.youtube_url, result)

    async def delete_user_data(self, user_id: str) -> tuple[int, bool]:
        """Delete local sessions and report whether provider cleanup remains queued."""
        store = self._require_store()
        removed = await store.delete_user_sessions(user_id)
        complete = await self._drain_deletions(
            user_id=user_id,
            limit=_PRIVACY_DELETION_BATCH_SIZE,
        )
        # Provider cleanup is independently durable in the outbox. It must not
        # keep the user's privacy barrier active after all local data is gone.
        return removed, not complete

    async def sweep(self, *, now: float | None = None) -> tuple[int, bool]:
        store = self._require_store()
        cutoff = time.time() if now is None else now
        removed = 0
        while removed < 5_000:
            batch_limit = min(500, 5_000 - removed)
            batch = await store.delete_expired(cutoff, limit=batch_limit)
            removed += batch
            if batch < batch_limit:
                break
        complete = await self._drain_deletions(user_id=None, limit=5_000)
        return removed, complete

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.close()

    def _require_client(self) -> VideoInteractionClient:
        if self._client is None:
            raise VideoSessionError("Video understanding is not configured.")
        return self._client

    def _require_store(self) -> VideoSessionRepository:
        store = self._get_store()
        if store is None:
            raise VideoSessionError("Video session storage is not ready yet.")
        return store

    async def _drain_deletions(self, *, user_id: str | None, limit: int) -> bool:
        store = self._require_store()
        batch_limit = min(limit, _DELETION_BATCH_SIZE)
        pending = await store.pending_deletions(user_id=user_id, limit=batch_limit + 1)
        if not pending:
            return True
        backlog = len(pending) > batch_limit
        pending = pending[:batch_limit]
        client = self._client
        if client is None:
            return False

        now = time.time()
        ready = [deletion for deletion in pending if deletion.retry_at <= now]
        deferred = len(ready) != len(pending)

        async def attempt(
            deletion: VideoDeletionRecord,
        ) -> tuple[VideoDeletionRecord, VideoInteractionError | None]:
            try:
                await client.delete(deletion.interaction_id)
            except VideoInteractionError as exc:
                return deletion, exc
            return deletion, None

        complete = not deferred
        results = await asyncio.gather(*(attempt(deletion) for deletion in ready))
        for deletion, error in results:
            if error is not None:
                complete = False
                await store.fail_deletion(
                    deletion.interaction_id,
                    str(error),
                    now=now,
                )
                log.warning("Gemini interaction deletion failed: %s", error)
            else:
                await store.complete_deletion(deletion.interaction_id)
        return complete and not backlog

    async def _queue_orphan(
        self,
        store: VideoSessionRepository,
        actor_user_id: str,
        interaction_id: str,
    ) -> None:
        queued = False
        try:
            await store.enqueue_deletion(
                interaction_id=interaction_id,
                actor_user_id=actor_user_id,
                now=time.time(),
            )
            queued = True
        except Exception:
            log.exception(
                "Could not queue orphaned Gemini video interaction %s",
                interaction_id,
            )
        client = self._client
        if client is None:
            return
        try:
            await client.delete(interaction_id)
        except VideoInteractionError as exc:
            if queued:
                await store.fail_deletion(interaction_id, str(exc), now=time.time())
            log.warning(
                "Could not delete orphaned Gemini video interaction %s",
                interaction_id,
                exc_info=True,
            )
        else:
            if queued:
                await store.complete_deletion(interaction_id)


def _analysis(handle: str, youtube_url: str, result: VideoInteractionResult) -> VideoAnalysis:
    return VideoAnalysis(
        session=handle,
        youtube_url=youtube_url,
        answer=result.answer,
        evidence=result.evidence,
        limitations=result.limitations,
        model=result.model,
        usage=result.usage,
    )
