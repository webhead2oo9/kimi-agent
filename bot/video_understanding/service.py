from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
import logging
import secrets
import time
from typing import Protocol, overload

from utils.asyncio import await_uncancellable
from video_understanding.client import (
    UploadedVideoFile,
    VideoByteSource,
    VideoEvidence,
    VideoInteractionCallCancelled,
    VideoInteractionError,
    VideoInteractionResult,
    VideoUploadRequest,
    VideoUsage,
)

log = logging.getLogger(__name__)

_DELETION_BATCH_SIZE = 100
_PRIVACY_DELETION_BATCH_SIZE = 4
_STALE_UPLOAD_GRACE_SECONDS = 3_600


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

    async def upload_video(self, request: VideoUploadRequest) -> UploadedVideoFile: ...

    async def start_from_file(
        self,
        *,
        file_uri: str,
        mime_type: str,
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

    async def delete_file(self, name: str) -> None: ...

    async def close(self) -> None: ...


class VideoSessionError(RuntimeError):
    """A model-facing session or provider failure."""

    @overload
    def __init__(
        self,
        message: str,
        *,
        result: None = None,
        catalog_model: None = None,
    ) -> None: ...

    @overload
    def __init__(
        self,
        message: str,
        *,
        result: VideoInteractionResult,
        catalog_model: str,
    ) -> None: ...

    def __init__(
        self,
        message: str,
        *,
        result: VideoInteractionResult | None = None,
        catalog_model: str | None = None,
    ) -> None:
        super().__init__(message)
        if result is not None and not catalog_model:
            raise TypeError("catalog_model is required when a video result is provided")
        self.result = result
        self.catalog_model = catalog_model


class VideoResultCancelled(asyncio.CancelledError):
    """Cancellation raised after a provider returned a billable result."""

    def __init__(
        self,
        *,
        result: VideoInteractionResult,
        catalog_model: str,
    ) -> None:
        super().__init__()
        if not catalog_model:
            raise ValueError("catalog_model must not be empty")
        self.result = result
        self.catalog_model = catalog_model


class VideoInteractionCancelled(asyncio.CancelledError):
    """Cancellation raised while cleaning up a billable malformed result."""

    def __init__(self, *, error: VideoInteractionError) -> None:
        super().__init__()
        if not error.catalog_model:
            raise ValueError("cancelled video interaction has no catalog_model attribution")
        self.error = error
        self.catalog_model = error.catalog_model


async def _finish_persistence[T](
    operation: Awaitable[T],
) -> tuple[T, asyncio.CancelledError | None]:
    """Finish a session write and preserve whether its caller was cancelled.

    A SQLite commit can complete before its awaiter resumes. Running the write
    in a shielded task lets callers distinguish a successful commit followed by
    cancellation from a cancelled or failed write whose provider result must be
    orphan-cleaned.
    """
    task = asyncio.ensure_future(operation)
    try:
        result = await await_uncancellable(task)
    except asyncio.CancelledError as cancellation:
        if task.done() and not task.cancelled() and task.exception() is None:
            return task.result(), cancellation
        raise
    return result, None


class VideoSessionRecord(Protocol):
    @property
    def handle(self) -> str: ...

    @property
    def source_kind(self) -> str: ...

    @property
    def source_display_name(self) -> str: ...

    @property
    def source_locator(self) -> str: ...

    @property
    def source_byte_size(self) -> int | None: ...

    @property
    def youtube_url(self) -> str: ...

    @property
    def youtube_video_id(self) -> str: ...

    @property
    def catalog_model(self) -> str: ...

    @property
    def model(self) -> str: ...

    @property
    def latest_interaction_id(self) -> str: ...

    @property
    def interaction_count(self) -> int: ...

    @property
    def expires_at(self) -> float: ...


class VideoDeletionRecord(Protocol):
    @property
    def interaction_id(self) -> str: ...

    @property
    def retry_at(self) -> float: ...


class VideoFileDeletionRecord(Protocol):
    @property
    def file_name(self) -> str: ...

    @property
    def retry_at(self) -> float: ...


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
        catalog_model: str,
        interaction_id: str,
        now: float,
        expires_at: float,
    ) -> None: ...

    async def reserve_provider_file(
        self,
        *,
        file_name: str,
        conversation_id: int,
        actor_user_id: str,
        guild_id: str,
        mime_type: str,
        byte_size: int,
        now: float,
    ) -> None: ...

    async def create_uploaded_session(
        self,
        *,
        handle: str,
        conversation_id: int,
        actor_user_id: str,
        guild_id: str,
        source_kind: str,
        source_display_name: str,
        source_locator: str,
        source_byte_size: int,
        model: str,
        catalog_model: str,
        interaction_id: str,
        file_name: str,
        now: float,
        expires_at: float,
    ) -> None: ...

    async def release_provider_file(self, file_name: str, actor_user_id: str) -> bool: ...

    async def delete_stale_provider_files(self, cutoff: float, *, limit: int) -> int: ...

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

    async def pending_file_deletions(
        self,
        *,
        user_id: str | None,
        limit: int,
    ) -> Sequence[VideoFileDeletionRecord]: ...

    async def complete_deletion(self, interaction_id: str) -> None: ...

    async def complete_file_deletion(self, file_name: str) -> None: ...

    async def fail_deletion(
        self,
        interaction_id: str,
        error: str,
        *,
        now: float | None = None,
    ) -> None: ...

    async def fail_file_deletion(
        self,
        file_name: str,
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
    catalog_model: str


@dataclass(frozen=True, slots=True)
class UploadedVideoSource:
    kind: str
    display_name: str
    locator: str
    mime_type: str
    byte_size: int
    bytes: VideoByteSource


@dataclass(frozen=True, slots=True)
class VideoAnalysis:
    session: str
    source_kind: str
    source_display_name: str
    source_locator: str
    youtube_url: str
    answer: str
    evidence: tuple[VideoEvidence, ...]
    limitations: tuple[str, ...]
    model: str
    usage: VideoUsage
    catalog_model: str
    usage_present: bool = True


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
        catalog_model = config.catalog_model
        result, interaction_cancellation = await self._run_interaction_call(
            client.start(
                url=youtube_url,
                question=question,
                model=config.model,
                thinking_level=config.thinking_level,
                max_output_tokens=config.max_output_tokens,
            ),
            store,
            actor_user_id,
            catalog_model=catalog_model,
        )
        now = time.time()
        handle = f"video_{secrets.token_urlsafe(12)}"
        try:
            _, persistence_cancellation = await _finish_persistence(
                store.create_session(
                    handle=handle,
                    conversation_id=conversation_id,
                    actor_user_id=actor_user_id,
                    guild_id=guild_id,
                    youtube_url=youtube_url,
                    youtube_video_id=youtube_video_id,
                    catalog_model=catalog_model,
                    model=config.model,
                    interaction_id=result.interaction_id,
                    now=now,
                    expires_at=now + config.session_ttl_minutes * 60,
                )
            )
        except asyncio.CancelledError as exc:
            await self._cleanup_cancelled_result(store, actor_user_id, result.interaction_id)
            raise VideoResultCancelled(
                result=result,
                catalog_model=catalog_model,
            ) from (interaction_cancellation or exc)
        except Exception as exc:
            await self._cleanup_billable_result(
                store,
                actor_user_id,
                result,
                catalog_model=catalog_model,
            )
            if interaction_cancellation is not None:
                raise VideoResultCancelled(
                    result=result,
                    catalog_model=catalog_model,
                ) from exc
            raise VideoSessionError(
                "The video was analyzed, but its follow-up session could not be saved.",
                result=result,
                catalog_model=catalog_model,
            ) from exc
        cancellation = interaction_cancellation or persistence_cancellation
        if cancellation is not None:
            raise VideoResultCancelled(
                result=result,
                catalog_model=catalog_model,
            ) from cancellation
        return _analysis(
            handle=handle,
            source_kind="youtube",
            source_display_name="YouTube video",
            source_locator=youtube_url,
            youtube_url=youtube_url,
            result=result,
            catalog_model=catalog_model,
        )

    async def start_uploaded(
        self,
        *,
        conversation_id: int,
        actor_user_id: str,
        guild_id: str,
        source: UploadedVideoSource,
        question: str,
        config: VideoSessionConfig,
    ) -> VideoAnalysis:
        client = self._require_client()
        store = self._require_store()
        catalog_model = config.catalog_model
        file_id = f"kv-{secrets.token_hex(16)}"
        file_name = f"files/{file_id}"
        reserved_at = time.time()
        await store.reserve_provider_file(
            file_name=file_name,
            conversation_id=conversation_id,
            actor_user_id=actor_user_id,
            guild_id=guild_id,
            mime_type=source.mime_type,
            byte_size=source.byte_size,
            now=reserved_at,
        )

        try:
            uploaded = await client.upload_video(
                VideoUploadRequest(
                    file_id=file_id,
                    display_name=source.display_name,
                    mime_type=source.mime_type,
                    declared_size_bytes=source.byte_size,
                    source=source.bytes,
                )
            )
        except asyncio.CancelledError:
            # The upload reservation is the ownership proof for this newly
            # created provider file. Releasing it conditionally queues remote
            # deletion, while an already-claimed reservation remains untouched.
            await self._cleanup_cancelled_result(
                store,
                actor_user_id,
                "",
                file_name=file_name,
            )
            raise
        except VideoInteractionError as exc:
            exc.catalog_model = catalog_model
            await self._cleanup_interaction_error(
                store,
                actor_user_id,
                exc,
                file_name=file_name,
            )
            raise
        except Exception:
            await await_uncancellable(self._release_file_or_log(store, actor_user_id, file_name))
            raise

        result, interaction_cancellation = await self._run_interaction_call(
            client.start_from_file(
                file_uri=uploaded.uri,
                mime_type=uploaded.mime_type,
                question=question,
                model=config.model,
                thinking_level=config.thinking_level,
                max_output_tokens=config.max_output_tokens,
            ),
            store,
            actor_user_id,
            catalog_model=catalog_model,
            file_name=file_name,
        )

        now = time.time()
        handle = f"video_{secrets.token_urlsafe(12)}"
        try:
            _, persistence_cancellation = await _finish_persistence(
                store.create_uploaded_session(
                    handle=handle,
                    conversation_id=conversation_id,
                    actor_user_id=actor_user_id,
                    guild_id=guild_id,
                    source_kind=source.kind,
                    source_display_name=source.display_name,
                    source_locator=source.locator,
                    source_byte_size=source.byte_size,
                    catalog_model=catalog_model,
                    model=config.model,
                    interaction_id=result.interaction_id,
                    file_name=file_name,
                    now=now,
                    expires_at=now + config.session_ttl_minutes * 60,
                )
            )
        except asyncio.CancelledError as exc:
            await self._cleanup_cancelled_result(
                store,
                actor_user_id,
                result.interaction_id,
                file_name=file_name,
            )
            raise VideoResultCancelled(
                result=result,
                catalog_model=catalog_model,
            ) from (interaction_cancellation or exc)
        except Exception as exc:
            await self._cleanup_billable_result(
                store,
                actor_user_id,
                result,
                catalog_model=catalog_model,
                file_name=file_name,
            )
            if interaction_cancellation is not None:
                raise VideoResultCancelled(
                    result=result,
                    catalog_model=catalog_model,
                ) from exc
            raise VideoSessionError(
                "The video was analyzed, but its follow-up session could not be saved.",
                result=result,
                catalog_model=catalog_model,
            ) from exc
        cancellation = interaction_cancellation or persistence_cancellation
        if cancellation is not None:
            raise VideoResultCancelled(
                result=result,
                catalog_model=catalog_model,
            ) from cancellation
        return _analysis(
            handle=handle,
            source_kind=source.kind,
            source_display_name=source.display_name,
            source_locator=source.locator,
            youtube_url="",
            result=result,
            catalog_model=catalog_model,
        )

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
        lookup_now = time.time()
        matches = await store.find_sessions(
            conversation_id=conversation_id,
            actor_user_id=actor_user_id,
            guild_id=guild_id,
            now=lookup_now,
            handle=session,
        )
        if not matches:
            raise VideoSessionError(
                "No active video session matches this conversation. Start one with a video source."
            )
        if len(matches) > 1:
            raise VideoSessionError(
                "Several video sessions are active. Pass the session returned by the relevant start call."
            )
        current = matches[0]
        catalog_model = current.catalog_model
        if current.interaction_count >= config.max_session_interactions:
            raise VideoSessionError(
                "This video session reached its follow-up limit. Start a new session to continue."
            )

        result, interaction_cancellation = await self._run_interaction_call(
            client.ask(
                previous_interaction_id=current.latest_interaction_id,
                question=question,
                model=current.model,
                thinking_level=config.thinking_level,
                max_output_tokens=config.max_output_tokens,
            ),
            store,
            actor_user_id,
            catalog_model=catalog_model,
        )
        now = time.time()
        try:
            advanced, persistence_cancellation = await _finish_persistence(
                store.advance_session(
                    handle=current.handle,
                    actor_user_id=actor_user_id,
                    expected_interaction_id=current.latest_interaction_id,
                    interaction_id=result.interaction_id,
                    now=now,
                    expires_at=now + config.session_ttl_minutes * 60,
                    max_interactions=config.max_session_interactions,
                )
            )
        except asyncio.CancelledError as exc:
            await self._cleanup_cancelled_result(store, actor_user_id, result.interaction_id)
            raise VideoResultCancelled(
                result=result,
                catalog_model=catalog_model,
            ) from (interaction_cancellation or exc)
        except Exception as exc:
            await self._cleanup_billable_result(
                store,
                actor_user_id,
                result,
                catalog_model=catalog_model,
            )
            if interaction_cancellation is not None:
                raise VideoResultCancelled(
                    result=result,
                    catalog_model=catalog_model,
                ) from exc
            raise VideoSessionError(
                "The follow-up was analyzed, but its session state could not be saved.",
                result=result,
                catalog_model=catalog_model,
            ) from exc
        if not advanced:
            await self._cleanup_billable_result(
                store,
                actor_user_id,
                result,
                catalog_model=catalog_model,
            )
            cancellation = interaction_cancellation or persistence_cancellation
            if cancellation is not None:
                raise VideoResultCancelled(
                    result=result,
                    catalog_model=catalog_model,
                ) from cancellation
            raise VideoSessionError(
                "That video session changed concurrently. Retry the follow-up against its latest state.",
                result=result,
                catalog_model=catalog_model,
            )
        cancellation = interaction_cancellation or persistence_cancellation
        if cancellation is not None:
            raise VideoResultCancelled(
                result=result,
                catalog_model=catalog_model,
            ) from cancellation
        return _analysis(
            handle=current.handle,
            source_kind=current.source_kind,
            source_display_name=current.source_display_name,
            source_locator=current.source_locator,
            youtube_url=current.youtube_url,
            result=result,
            catalog_model=catalog_model,
        )

    async def delete_user_data(self, user_id: str) -> tuple[int, bool]:
        """Delete local sessions and report whether provider cleanup remains queued."""
        store = self._require_store()
        removed = await store.delete_user_sessions(user_id)
        interactions_complete = await self._drain_deletions(
            user_id=user_id,
            limit=_PRIVACY_DELETION_BATCH_SIZE,
        )
        files_complete = await self._drain_file_deletions(
            user_id=user_id,
            limit=_PRIVACY_DELETION_BATCH_SIZE,
        )
        return removed, not (interactions_complete and files_complete)

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
        await store.delete_stale_provider_files(
            cutoff - _STALE_UPLOAD_GRACE_SECONDS,
            limit=100,
        )
        interactions_complete = await self._drain_deletions(user_id=None, limit=5_000)
        files_complete = await self._drain_file_deletions(user_id=None, limit=5_000)
        return removed, interactions_complete and files_complete

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
                await store.fail_deletion(deletion.interaction_id, str(error), now=now)
                log.warning("Gemini interaction deletion failed: %s", error)
            else:
                await store.complete_deletion(deletion.interaction_id)
        return complete and not backlog

    async def _drain_file_deletions(self, *, user_id: str | None, limit: int) -> bool:
        store = self._require_store()
        batch_limit = min(limit, _DELETION_BATCH_SIZE)
        pending = await store.pending_file_deletions(user_id=user_id, limit=batch_limit + 1)
        if not pending:
            return True
        backlog = len(pending) > batch_limit
        pending = pending[:batch_limit]
        client = self._client
        if client is None:
            return False

        now = time.time()
        ready = [deletion for deletion in pending if deletion.retry_at <= now]
        complete = len(ready) == len(pending)

        async def attempt(
            deletion: VideoFileDeletionRecord,
        ) -> tuple[VideoFileDeletionRecord, VideoInteractionError | None]:
            try:
                await client.delete_file(deletion.file_name)
            except VideoInteractionError as exc:
                return deletion, exc
            return deletion, None

        results = await asyncio.gather(*(attempt(deletion) for deletion in ready))
        for deletion, error in results:
            if error is not None:
                complete = False
                await store.fail_file_deletion(deletion.file_name, str(error), now=now)
                log.warning("Gemini file deletion failed: %s", error)
            else:
                await store.complete_file_deletion(deletion.file_name)
        return complete and not backlog

    async def _run_interaction_call(
        self,
        operation: Awaitable[VideoInteractionResult],
        store: VideoSessionRepository,
        actor_user_id: str,
        *,
        catalog_model: str,
        file_name: str | None = None,
    ) -> tuple[VideoInteractionResult, asyncio.CancelledError | None]:
        try:
            return await operation, None
        except VideoInteractionCallCancelled as cancellation:
            if cancellation.error is None:
                assert cancellation.result is not None
                return cancellation.result, cancellation
            cancellation.error.catalog_model = catalog_model
            await self._cleanup_interaction_error(
                store,
                actor_user_id,
                cancellation.error,
                file_name=file_name,
            )
            raise VideoInteractionCancelled(error=cancellation.error) from cancellation
        except VideoInteractionError as error:
            error.catalog_model = catalog_model
            await self._cleanup_interaction_error(
                store,
                actor_user_id,
                error,
                file_name=file_name,
            )
            raise
        except asyncio.CancelledError:
            if file_name is not None:
                await self._cleanup_cancelled_result(
                    store,
                    actor_user_id,
                    "",
                    file_name=file_name,
                )
            raise
        except Exception:
            if file_name is not None:
                await await_uncancellable(
                    self._release_file_or_log(store, actor_user_id, file_name)
                )
            raise

    async def _cleanup_interaction_error(
        self,
        store: VideoSessionRepository,
        actor_user_id: str,
        error: VideoInteractionError,
        *,
        file_name: str | None = None,
    ) -> None:
        async def cleanup() -> None:
            if error.interaction_id:
                await self._queue_orphan(store, actor_user_id, error.interaction_id)
            if file_name is not None:
                await self._release_file_or_log(store, actor_user_id, file_name)

        try:
            await await_uncancellable(cleanup())
        except asyncio.CancelledError as cancellation:
            raise VideoInteractionCancelled(error=error) from cancellation
        except Exception:
            # Cleanup is secondary to returning provider usage and attribution.
            log.exception("Could not finish Gemini video error cleanup")

    async def _cleanup_billable_result(
        self,
        store: VideoSessionRepository,
        actor_user_id: str,
        result: VideoInteractionResult,
        *,
        catalog_model: str,
        file_name: str | None = None,
    ) -> None:
        async def cleanup() -> None:
            await self._queue_orphan(store, actor_user_id, result.interaction_id)
            if file_name is not None:
                await self._release_file_or_log(store, actor_user_id, file_name)

        try:
            await await_uncancellable(cleanup())
        except asyncio.CancelledError as cancellation:
            raise VideoResultCancelled(
                result=result,
                catalog_model=catalog_model,
            ) from cancellation
        except Exception:
            # Cleanup is secondary to returning provider usage and attribution.
            log.exception("Could not finish billable Gemini video cleanup")

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
            log.exception("Could not queue orphaned Gemini video interaction %s", interaction_id)
        client = self._client
        if client is None:
            return
        try:
            await client.delete(interaction_id)
        except Exception as exc:
            if queued:
                try:
                    await store.fail_deletion(interaction_id, str(exc), now=time.time())
                except Exception:
                    log.exception(
                        "Could not mark orphaned Gemini video interaction %s for retry",
                        interaction_id,
                    )
            log.warning(
                "Could not delete orphaned Gemini video interaction %s",
                interaction_id,
                exc_info=True,
            )
        else:
            if queued:
                try:
                    await store.complete_deletion(interaction_id)
                except Exception:
                    log.exception(
                        "Could not complete orphaned Gemini video interaction %s cleanup",
                        interaction_id,
                    )

    async def _release_file_or_log(
        self,
        store: VideoSessionRepository,
        actor_user_id: str,
        file_name: str,
    ) -> None:
        try:
            await store.release_provider_file(file_name, actor_user_id)
        except Exception:
            log.exception("Could not release orphaned Gemini video file %s", file_name)
            return
        try:
            await self._drain_file_deletions(user_id=actor_user_id, limit=1)
        except Exception:
            log.exception("Could not drain orphaned Gemini video file %s", file_name)

    async def _cleanup_cancelled_result(
        self,
        store: VideoSessionRepository,
        actor_user_id: str,
        interaction_id: str,
        *,
        file_name: str | None = None,
    ) -> None:
        async def cleanup() -> None:
            try:
                if interaction_id:
                    await self._queue_orphan(store, actor_user_id, interaction_id)
            finally:
                if file_name is not None:
                    await self._release_file_or_log(store, actor_user_id, file_name)

        task = asyncio.create_task(cleanup())
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        try:
            task.result()
        except Exception:
            log.exception("Could not finish cancelled Gemini video cleanup")


def _analysis(
    *,
    handle: str,
    source_kind: str,
    source_display_name: str,
    source_locator: str,
    youtube_url: str,
    result: VideoInteractionResult,
    catalog_model: str,
) -> VideoAnalysis:
    return VideoAnalysis(
        session=handle,
        source_kind=source_kind,
        source_display_name=source_display_name,
        source_locator=source_locator,
        youtube_url=youtube_url,
        answer=result.answer,
        evidence=result.evidence,
        limitations=result.limitations,
        model=result.model,
        usage=result.usage,
        catalog_model=catalog_model,
        usage_present=result.usage_present,
    )
