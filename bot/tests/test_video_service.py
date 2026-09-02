from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from video_understanding.client import (
    UploadedVideoFile,
    VideoInteractionCallCancelled,
    VideoInteractionError,
    VideoInteractionResult,
    VideoUsage,
)
from video_understanding.service import (
    UploadedVideoSource,
    VideoInteractionCancelled,
    VideoResultCancelled,
    VideoSessionConfig,
    VideoSessionError,
    VideoUnderstandingService,
)


@dataclass
class Session:
    handle: str
    youtube_url: str
    youtube_video_id: str
    latest_interaction_id: str
    model: str = "gemini-3.7-flash"
    catalog_model: str = ""
    interaction_count: int = 1
    expires_at: float = 99999999999
    source_kind: str = "youtube"
    source_display_name: str = "YouTube video"
    source_locator: str = "https://youtu.be/abcdefghijk"
    source_byte_size: int | None = None


@dataclass
class Deletion:
    interaction_id: str
    retry_at: float = 0


@dataclass
class FileDeletion:
    file_name: str
    retry_at: float = 0


@dataclass
class FakeStore:
    sessions: list[Session] = field(default_factory=list)
    pending: list[Deletion] = field(default_factory=list)
    created: list[dict[str, Any]] = field(default_factory=list)
    advances: list[dict[str, Any]] = field(default_factory=list)
    deleted_users: list[str] = field(default_factory=list)
    reserved_files: dict[str, dict[str, Any]] = field(default_factory=dict)
    pending_files: list[FileDeletion] = field(default_factory=list)

    async def create_session(self, **kwargs: Any) -> None:
        self.created.append(kwargs)
        self.sessions.append(
            Session(
                handle=kwargs["handle"],
                youtube_url=kwargs["youtube_url"],
                youtube_video_id=kwargs["youtube_video_id"],
                latest_interaction_id=kwargs["interaction_id"],
                source_locator=kwargs["youtube_url"],
                model=kwargs["model"],
                catalog_model=kwargs["catalog_model"],
            )
        )

    async def reserve_provider_file(self, **kwargs: Any) -> None:
        self.reserved_files[kwargs["file_name"]] = kwargs

    async def create_uploaded_session(self, **kwargs: Any) -> None:
        reservation = self.reserved_files.get(kwargs["file_name"])
        if reservation is None:
            raise ValueError("missing reservation")
        self.sessions.append(
            Session(
                handle=kwargs["handle"],
                youtube_url="",
                youtube_video_id="",
                latest_interaction_id=kwargs["interaction_id"],
                source_kind=kwargs["source_kind"],
                source_display_name=kwargs["source_display_name"],
                source_locator=kwargs["source_locator"],
                source_byte_size=kwargs["source_byte_size"],
                model=kwargs["model"],
                catalog_model=kwargs["catalog_model"],
            )
        )

    async def release_provider_file(self, file_name: str, actor_user_id: str) -> bool:
        if self.reserved_files.pop(file_name, None) is None:
            return False
        self.pending_files.append(FileDeletion(file_name))
        return True

    async def delete_stale_provider_files(self, cutoff: float, *, limit: int) -> int:
        return 0

    async def find_sessions(self, **kwargs: Any) -> tuple[Session, ...]:
        handle = kwargs.get("handle")
        return tuple(item for item in self.sessions if handle is None or item.handle == handle)

    async def advance_session(self, **kwargs: Any) -> bool:
        self.advances.append(kwargs)
        current = next((item for item in self.sessions if item.handle == kwargs["handle"]), None)
        if current is None or current.latest_interaction_id != kwargs["expected_interaction_id"]:
            return False
        current.latest_interaction_id = kwargs["interaction_id"]
        current.interaction_count += 1
        return True

    async def delete_user_sessions(self, user_id: str) -> int:
        self.deleted_users.append(user_id)
        return 1

    async def enqueue_deletion(self, **kwargs: Any) -> None:
        if not any(item.interaction_id == kwargs["interaction_id"] for item in self.pending):
            self.pending.append(Deletion(kwargs["interaction_id"]))

    async def delete_expired(self, now: float, *, limit: int) -> int:
        return 0

    async def pending_deletions(self, **kwargs: Any) -> tuple[Deletion, ...]:
        return tuple(self.pending[: kwargs["limit"]])

    async def pending_file_deletions(self, **kwargs: Any) -> tuple[FileDeletion, ...]:
        return tuple(self.pending_files[: kwargs["limit"]])

    async def complete_deletion(self, interaction_id: str) -> None:
        self.pending = [item for item in self.pending if item.interaction_id != interaction_id]

    async def complete_file_deletion(self, file_name: str) -> None:
        self.pending_files = [item for item in self.pending_files if item.file_name != file_name]

    async def fail_deletion(
        self,
        interaction_id: str,
        error: str,
        *,
        now: float | None = None,
    ) -> None:
        return None

    async def fail_file_deletion(
        self,
        file_name: str,
        error: str,
        *,
        now: float | None = None,
    ) -> None:
        return None


@dataclass
class FakeClient:
    starts: list[dict[str, Any]] = field(default_factory=list)
    asks: list[dict[str, Any]] = field(default_factory=list)
    deletes: list[str] = field(default_factory=list)
    fail_delete: bool = False
    start_error: VideoInteractionError | None = None
    ask_error: VideoInteractionError | None = None
    upload_error: VideoInteractionError | None = None
    file_deletes: list[str] = field(default_factory=list)

    async def start(self, **kwargs: Any) -> VideoInteractionResult:
        self.starts.append(kwargs)
        if self.start_error is not None:
            raise self.start_error
        return _result("remote-start")

    async def upload_video(self, request: Any) -> UploadedVideoFile:
        if self.upload_error is not None:
            raise self.upload_error
        return UploadedVideoFile(
            name=f"files/{request.file_id}",
            display_name=request.display_name,
            mime_type=request.mime_type,
            size_bytes=request.declared_size_bytes,
            uri=f"https://generativelanguage.googleapis.com/v1beta/files/{request.file_id}",
            state="ACTIVE",
            duration_seconds=10,
        )

    async def start_from_file(self, **kwargs: Any) -> VideoInteractionResult:
        self.starts.append(kwargs)
        return _result("remote-file-start")

    async def ask(self, **kwargs: Any) -> VideoInteractionResult:
        self.asks.append(kwargs)
        if self.ask_error is not None:
            raise self.ask_error
        return _result("remote-ask")

    async def delete(self, interaction_id: str) -> None:
        self.deletes.append(interaction_id)
        if self.fail_delete:
            raise VideoInteractionError("unavailable")

    async def delete_file(self, name: str) -> None:
        self.file_deletes.append(name)

    async def close(self) -> None:
        return None


def _result(interaction_id: str) -> VideoInteractionResult:
    return VideoInteractionResult(
        interaction_id=interaction_id,
        model="gemini-3.7-flash",
        answer="answer",
        evidence=(),
        limitations=(),
        usage=VideoUsage(),
    )


def _config(
    *,
    model: str = "gemini-3.7-flash",
    catalog_model: str = "",
) -> VideoSessionConfig:
    return VideoSessionConfig(
        model=model,
        thinking_level="low",
        max_output_tokens=4096,
        max_session_interactions=5,
        session_ttl_minutes=60,
        catalog_model=catalog_model,
    )


@pytest.mark.asyncio
async def test_start_then_follow_up_uses_previous_interaction() -> None:
    store = FakeStore()
    client = FakeClient()
    service = VideoUnderstandingService(  # type: ignore[arg-type]
        client=client,
        get_store=lambda: store,
    )

    started = await service.start(
        conversation_id=1,
        actor_user_id="user",
        guild_id="guild",
        youtube_url="https://www.youtube.com/watch?v=abcdefghijk",
        youtube_video_id="abcdefghijk",
        question="Summarize it",
        config=_config(),
    )
    followed = await service.ask(
        conversation_id=1,
        actor_user_id="user",
        guild_id="guild",
        session=started.session,
        question="What evidence supports that?",
        config=_config(),
    )

    assert started.session.startswith("video_")
    assert client.asks[0]["previous_interaction_id"] == "remote-start"
    assert followed.session == started.session
    assert store.advances[0]["interaction_id"] == "remote-ask"


@pytest.mark.asyncio
async def test_cancellation_during_youtube_interaction_persists_returned_result() -> None:
    interaction_ready = asyncio.Event()
    release_interaction = asyncio.Event()

    class FinishAfterCancellationClient(FakeClient):
        async def start(self, **kwargs: Any) -> VideoInteractionResult:
            self.starts.append(kwargs)
            interaction_ready.set()
            try:
                await release_interaction.wait()
            except asyncio.CancelledError as cancellation:
                await release_interaction.wait()
                raise VideoInteractionCallCancelled(
                    result=_result("remote-start")
                ) from cancellation
            return _result("remote-start")

    store = FakeStore()
    client = FinishAfterCancellationClient()
    service = VideoUnderstandingService(client=client, get_store=lambda: store)
    task = asyncio.create_task(
        service.start(
            conversation_id=1,
            actor_user_id="user",
            guild_id="guild",
            youtube_url="https://www.youtube.com/watch?v=abcdefghijk",
            youtube_video_id="abcdefghijk",
            question="Summarize it",
            config=_config(catalog_model="video-catalog"),
        )
    )
    await interaction_ready.wait()

    task.cancel()
    await asyncio.sleep(0)
    release_interaction.set()
    with pytest.raises(VideoResultCancelled) as cancellation:
        await task

    assert cancellation.value.result.interaction_id == "remote-start"
    assert cancellation.value.catalog_model == "video-catalog"
    assert [item.latest_interaction_id for item in store.sessions] == ["remote-start"]
    assert client.deletes == []


@pytest.mark.asyncio
async def test_cancellation_during_uploaded_interaction_persists_returned_result() -> None:
    interaction_ready = asyncio.Event()
    release_interaction = asyncio.Event()

    class FinishAfterCancellationClient(FakeClient):
        async def start_from_file(self, **kwargs: Any) -> VideoInteractionResult:
            self.starts.append(kwargs)
            interaction_ready.set()
            try:
                await release_interaction.wait()
            except asyncio.CancelledError as cancellation:
                await release_interaction.wait()
                raise VideoInteractionCallCancelled(
                    result=_result("remote-file-start")
                ) from cancellation
            return _result("remote-file-start")

    async def source() -> Any:
        yield b"video"

    store = FakeStore()
    client = FinishAfterCancellationClient()
    service = VideoUnderstandingService(client=client, get_store=lambda: store)
    task = asyncio.create_task(
        service.start_uploaded(
            conversation_id=1,
            actor_user_id="user",
            guild_id="guild",
            source=UploadedVideoSource(
                kind="attachment",
                display_name="clip.mp4",
                locator="clip.mp4",
                mime_type="video/mp4",
                byte_size=5,
                bytes=source(),
            ),
            question="What happens?",
            config=_config(catalog_model="video-catalog"),
        )
    )
    await interaction_ready.wait()

    task.cancel()
    await asyncio.sleep(0)
    release_interaction.set()
    with pytest.raises(VideoResultCancelled) as cancellation:
        await task

    assert cancellation.value.result.interaction_id == "remote-file-start"
    assert cancellation.value.catalog_model == "video-catalog"
    assert [item.latest_interaction_id for item in store.sessions] == ["remote-file-start"]
    assert len(store.reserved_files) == 1
    assert client.deletes == []
    assert client.file_deletes == []


@pytest.mark.asyncio
async def test_cancellation_during_follow_up_persists_returned_result() -> None:
    interaction_ready = asyncio.Event()
    release_interaction = asyncio.Event()

    class FinishAfterCancellationClient(FakeClient):
        async def ask(self, **kwargs: Any) -> VideoInteractionResult:
            self.asks.append(kwargs)
            interaction_ready.set()
            try:
                await release_interaction.wait()
            except asyncio.CancelledError as cancellation:
                await release_interaction.wait()
                raise VideoInteractionCallCancelled(result=_result("remote-ask")) from cancellation
            return _result("remote-ask")

    store = FakeStore(
        sessions=[
            Session(
                "video_old",
                "https://youtu.be/abcdefghijk",
                "abcdefghijk",
                "remote-old",
                model="old-upstream",
                catalog_model="old-catalog",
            )
        ]
    )
    client = FinishAfterCancellationClient()
    service = VideoUnderstandingService(client=client, get_store=lambda: store)
    task = asyncio.create_task(
        service.ask(
            conversation_id=1,
            actor_user_id="user",
            guild_id="guild",
            session="video_old",
            question="Follow up",
            config=_config(model="new-upstream", catalog_model="new-catalog"),
        )
    )
    await interaction_ready.wait()

    task.cancel()
    await asyncio.sleep(0)
    release_interaction.set()
    with pytest.raises(VideoResultCancelled) as cancellation:
        await task

    assert cancellation.value.result.interaction_id == "remote-ask"
    assert cancellation.value.catalog_model == "old-catalog"
    assert store.sessions[0].latest_interaction_id == "remote-ask"
    assert store.sessions[0].interaction_count == 2
    assert client.deletes == []


@pytest.mark.asyncio
async def test_cancellation_during_malformed_follow_up_preserves_error_usage() -> None:
    interaction_ready = asyncio.Event()
    release_interaction = asyncio.Event()

    class ErrorAfterCancellationClient(FakeClient):
        async def ask(self, **kwargs: Any) -> VideoInteractionResult:
            self.asks.append(kwargs)
            interaction_ready.set()
            error = VideoInteractionError(
                "malformed",
                interaction_id="remote-malformed",
                model="old-upstream",
                usage=VideoUsage(input_tokens=10),
            )
            try:
                await release_interaction.wait()
            except asyncio.CancelledError as cancellation:
                await release_interaction.wait()
                raise VideoInteractionCallCancelled(error=error) from cancellation
            raise error

    store = FakeStore(
        sessions=[
            Session(
                "video_old",
                "https://youtu.be/abcdefghijk",
                "abcdefghijk",
                "remote-old",
                model="old-upstream",
                catalog_model="old-catalog",
            )
        ]
    )
    client = ErrorAfterCancellationClient()
    service = VideoUnderstandingService(client=client, get_store=lambda: store)
    task = asyncio.create_task(
        service.ask(
            conversation_id=1,
            actor_user_id="user",
            guild_id="guild",
            session="video_old",
            question="Follow up",
            config=_config(model="new-upstream", catalog_model="new-catalog"),
        )
    )
    await interaction_ready.wait()

    task.cancel()
    await asyncio.sleep(0)
    release_interaction.set()
    with pytest.raises(VideoInteractionCancelled) as cancellation:
        await task

    assert cancellation.value.error.usage == VideoUsage(input_tokens=10)
    assert cancellation.value.error.catalog_model == "old-catalog"
    assert client.deletes == ["remote-malformed"]


@pytest.mark.asyncio
async def test_cancellation_after_youtube_session_commit_keeps_provider_state() -> None:
    write_applied = asyncio.Event()
    release_write = asyncio.Event()

    class CommitThenWaitStore(FakeStore):
        async def create_session(self, **kwargs: Any) -> None:
            await super().create_session(**kwargs)
            write_applied.set()
            await release_write.wait()

    store = CommitThenWaitStore()
    client = FakeClient()
    service = VideoUnderstandingService(client=client, get_store=lambda: store)
    task = asyncio.create_task(
        service.start(
            conversation_id=1,
            actor_user_id="user",
            guild_id="guild",
            youtube_url="https://www.youtube.com/watch?v=abcdefghijk",
            youtube_video_id="abcdefghijk",
            question="Summarize it",
            config=_config(catalog_model="video-catalog"),
        )
    )
    await write_applied.wait()

    task.cancel()
    await asyncio.sleep(0)
    release_write.set()
    with pytest.raises(VideoResultCancelled) as cancellation:
        await task

    assert cancellation.value.catalog_model == "video-catalog"
    assert [item.latest_interaction_id for item in store.sessions] == ["remote-start"]
    assert client.deletes == []
    assert store.pending == []


@pytest.mark.asyncio
async def test_cancellation_after_uploaded_session_commit_keeps_provider_state() -> None:
    write_applied = asyncio.Event()
    release_write = asyncio.Event()

    class CommitThenWaitStore(FakeStore):
        async def create_uploaded_session(self, **kwargs: Any) -> None:
            await super().create_uploaded_session(**kwargs)
            write_applied.set()
            await release_write.wait()

    async def source() -> Any:
        yield b"video"

    store = CommitThenWaitStore()
    client = FakeClient()
    service = VideoUnderstandingService(client=client, get_store=lambda: store)
    task = asyncio.create_task(
        service.start_uploaded(
            conversation_id=1,
            actor_user_id="user",
            guild_id="guild",
            source=UploadedVideoSource(
                kind="attachment",
                display_name="clip.mp4",
                locator="clip.mp4",
                mime_type="video/mp4",
                byte_size=5,
                bytes=source(),
            ),
            question="What happens?",
            config=_config(catalog_model="video-catalog"),
        )
    )
    await write_applied.wait()

    task.cancel()
    await asyncio.sleep(0)
    release_write.set()
    with pytest.raises(VideoResultCancelled) as cancellation:
        await task

    assert cancellation.value.catalog_model == "video-catalog"
    assert [item.latest_interaction_id for item in store.sessions] == ["remote-file-start"]
    assert len(store.reserved_files) == 1
    assert client.deletes == []
    assert client.file_deletes == []
    assert store.pending == []
    assert store.pending_files == []


@pytest.mark.asyncio
async def test_cancellation_after_follow_up_commit_keeps_provider_state() -> None:
    write_applied = asyncio.Event()
    release_write = asyncio.Event()

    class CommitThenWaitStore(FakeStore):
        async def advance_session(self, **kwargs: Any) -> bool:
            advanced = await super().advance_session(**kwargs)
            write_applied.set()
            await release_write.wait()
            return advanced

    store = CommitThenWaitStore(
        sessions=[
            Session(
                "video_old",
                "https://youtu.be/abcdefghijk",
                "abcdefghijk",
                "remote-old",
                model="old-upstream",
                catalog_model="old-catalog",
            )
        ]
    )
    client = FakeClient()
    service = VideoUnderstandingService(client=client, get_store=lambda: store)
    task = asyncio.create_task(
        service.ask(
            conversation_id=1,
            actor_user_id="user",
            guild_id="guild",
            session="video_old",
            question="Follow up",
            config=_config(model="new-upstream", catalog_model="new-catalog"),
        )
    )
    await write_applied.wait()

    task.cancel()
    await asyncio.sleep(0)
    release_write.set()
    with pytest.raises(VideoResultCancelled) as cancellation:
        await task

    assert cancellation.value.catalog_model == "old-catalog"
    assert store.sessions[0].latest_interaction_id == "remote-ask"
    assert store.sessions[0].interaction_count == 2
    assert client.deletes == []
    assert store.pending == []


@pytest.mark.asyncio
async def test_follow_up_pins_stored_model_and_catalog_after_role_change() -> None:
    store = FakeStore(
        sessions=[
            Session(
                "video_old",
                "https://youtu.be/abcdefghijk",
                "abcdefghijk",
                "remote-old",
                model="old-upstream",
                catalog_model="old-catalog",
            )
        ]
    )
    client = FakeClient()
    service = VideoUnderstandingService(client=client, get_store=lambda: store)

    analysis = await service.ask(
        conversation_id=1,
        actor_user_id="user",
        guild_id="guild",
        session="video_old",
        question="Follow up",
        config=_config(model="new-upstream", catalog_model="new-catalog"),
    )

    assert client.asks[0]["model"] == "old-upstream"
    assert analysis.catalog_model == "old-catalog"


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrent", [False, True])
async def test_follow_up_save_failures_keep_stored_catalog_model(concurrent: bool) -> None:
    class FailingStore(FakeStore):
        async def advance_session(self, **kwargs: Any) -> bool:
            if concurrent:
                return False
            raise RuntimeError("database unavailable")

    store = FailingStore(
        sessions=[
            Session(
                "video_old",
                "https://youtu.be/abcdefghijk",
                "abcdefghijk",
                "remote-old",
                model="old-upstream",
                catalog_model="old-catalog",
            )
        ]
    )
    service = VideoUnderstandingService(client=FakeClient(), get_store=lambda: store)

    with pytest.raises(VideoSessionError) as error:
        await service.ask(
            conversation_id=1,
            actor_user_id="user",
            guild_id="guild",
            session="video_old",
            question="Follow up",
            config=_config(model="new-upstream", catalog_model="new-catalog"),
        )

    assert error.value.result is not None
    assert error.value.catalog_model == "old-catalog"


@pytest.mark.asyncio
async def test_malformed_follow_up_keeps_stored_catalog_model() -> None:
    class CleanupFailureStore(FakeStore):
        async def complete_deletion(self, interaction_id: str) -> None:
            raise RuntimeError("cleanup database unavailable")

    malformed = VideoInteractionError(
        "malformed",
        interaction_id="remote-malformed",
        model="old-upstream",
        usage=VideoUsage(input_tokens=10),
    )
    store = CleanupFailureStore(
        sessions=[
            Session(
                "video_old",
                "https://youtu.be/abcdefghijk",
                "abcdefghijk",
                "remote-old",
                model="old-upstream",
                catalog_model="old-catalog",
            )
        ]
    )
    service = VideoUnderstandingService(
        client=FakeClient(ask_error=malformed),
        get_store=lambda: store,
    )

    with pytest.raises(VideoInteractionError) as error:
        await service.ask(
            conversation_id=1,
            actor_user_id="user",
            guild_id="guild",
            session="video_old",
            question="Follow up",
            config=_config(model="new-upstream", catalog_model="new-catalog"),
        )

    assert error.value.catalog_model == "old-catalog"


@pytest.mark.asyncio
async def test_cancelled_malformed_follow_up_cleanup_keeps_usage_attribution() -> None:
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()

    class BlockingCleanupStore(FakeStore):
        async def enqueue_deletion(self, **kwargs: Any) -> None:
            cleanup_started.set()
            await release_cleanup.wait()
            await super().enqueue_deletion(**kwargs)

    malformed = VideoInteractionError(
        "malformed",
        interaction_id="remote-malformed",
        model="old-upstream",
        usage=VideoUsage(input_tokens=10),
    )
    store = BlockingCleanupStore(
        sessions=[
            Session(
                "video_old",
                "https://youtu.be/abcdefghijk",
                "abcdefghijk",
                "remote-old",
                model="old-upstream",
                catalog_model="old-catalog",
            )
        ]
    )
    service = VideoUnderstandingService(
        client=FakeClient(ask_error=malformed),
        get_store=lambda: store,
    )
    task = asyncio.create_task(
        service.ask(
            conversation_id=1,
            actor_user_id="user",
            guild_id="guild",
            session="video_old",
            question="Follow up",
            config=_config(model="new-upstream", catalog_model="new-catalog"),
        )
    )
    await cleanup_started.wait()

    task.cancel()
    release_cleanup.set()
    with pytest.raises(VideoInteractionCancelled) as cancellation:
        await task

    assert cancellation.value.error.usage == VideoUsage(input_tokens=10)
    assert cancellation.value.error.catalog_model == "old-catalog"


@pytest.mark.asyncio
async def test_cancelled_failed_follow_up_save_cleans_provider_result() -> None:
    saving = asyncio.Event()
    release_save = asyncio.Event()

    class BlockingStore(FakeStore):
        async def advance_session(self, **kwargs: Any) -> bool:
            saving.set()
            await release_save.wait()
            raise RuntimeError("database unavailable")

    store = BlockingStore(
        sessions=[
            Session(
                "video_old",
                "https://youtu.be/abcdefghijk",
                "abcdefghijk",
                "remote-old",
                model="old-upstream",
                catalog_model="old-catalog",
            )
        ]
    )
    client = FakeClient()
    service = VideoUnderstandingService(client=client, get_store=lambda: store)
    task = asyncio.create_task(
        service.ask(
            conversation_id=1,
            actor_user_id="user",
            guild_id="guild",
            session="video_old",
            question="Follow up",
            config=_config(model="new-upstream", catalog_model="new-catalog"),
        )
    )
    await saving.wait()

    task.cancel()
    await asyncio.sleep(0)
    release_save.set()
    with pytest.raises(VideoResultCancelled) as error:
        await task

    assert error.value.catalog_model == "old-catalog"
    assert store.sessions[0].latest_interaction_id == "remote-old"
    assert client.deletes == ["remote-ask"]


@pytest.mark.asyncio
async def test_uploaded_video_is_reserved_and_persisted_before_follow_up() -> None:
    async def source() -> Any:
        yield b"video"

    store = FakeStore()
    client = FakeClient()
    service = VideoUnderstandingService(client=client, get_store=lambda: store)

    started = await service.start_uploaded(
        conversation_id=1,
        actor_user_id="user",
        guild_id="guild",
        source=UploadedVideoSource(
            kind="attachment",
            display_name="clip.mp4",
            locator="clip.mp4",
            mime_type="video/mp4",
            byte_size=5,
            bytes=source(),
        ),
        question="What happens?",
        config=_config(),
    )

    assert started.source_kind == "attachment"
    assert started.source_display_name == "clip.mp4"
    assert not started.youtube_url
    assert len(store.reserved_files) == 1
    assert store.sessions[0].source_byte_size == 5
    assert client.starts[0]["file_uri"].startswith(
        "https://generativelanguage.googleapis.com/v1beta/files/"
    )


@pytest.mark.asyncio
async def test_uploaded_video_failure_releases_reserved_provider_file() -> None:
    async def source() -> Any:
        yield b"video"

    store = FakeStore()
    client = FakeClient(
        upload_error=VideoInteractionError(
            "upload failed",
            file_name="files/provider-name",
        )
    )
    service = VideoUnderstandingService(client=client, get_store=lambda: store)

    with pytest.raises(VideoInteractionError, match="upload failed"):
        await service.start_uploaded(
            conversation_id=1,
            actor_user_id="user",
            guild_id="guild",
            source=UploadedVideoSource(
                kind="workspace",
                display_name="clip.mp4",
                locator="imports/clip.mp4",
                mime_type="video/mp4",
                byte_size=5,
                bytes=source(),
            ),
            question="What happens?",
            config=_config(),
        )

    assert not store.reserved_files
    assert not store.pending_files
    assert len(client.file_deletes) == 1
    assert client.file_deletes[0].startswith("files/kv-")


@pytest.mark.asyncio
async def test_uploaded_session_persistence_cancellation_cleans_remote_resources() -> None:
    persisting = asyncio.Event()
    release_persistence = asyncio.Event()

    class BlockingStore(FakeStore):
        async def create_uploaded_session(self, **kwargs: Any) -> None:
            persisting.set()
            await release_persistence.wait()
            raise RuntimeError("database unavailable")

    async def source() -> Any:
        yield b"video"

    store = BlockingStore()
    client = FakeClient(fail_delete=True)
    service = VideoUnderstandingService(client=client, get_store=lambda: store)
    task = asyncio.create_task(
        service.start_uploaded(
            conversation_id=1,
            actor_user_id="user",
            guild_id="guild",
            source=UploadedVideoSource(
                kind="attachment",
                display_name="clip.mp4",
                locator="clip.mp4",
                mime_type="video/mp4",
                byte_size=5,
                bytes=source(),
            ),
            question="What happens?",
            config=_config(),
        )
    )
    await persisting.wait()

    task.cancel()
    await asyncio.sleep(0)
    release_persistence.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert [item.interaction_id for item in store.pending] == ["remote-file-start"]
    assert client.deletes == ["remote-file-start"]
    assert not store.reserved_files
    assert len(client.file_deletes) == 1


@pytest.mark.asyncio
async def test_cancellation_after_upload_releases_only_unclaimed_provider_file() -> None:
    analyzing = asyncio.Event()
    release_analysis = asyncio.Event()

    class BlockingClient(FakeClient):
        async def start_from_file(self, **kwargs: Any) -> VideoInteractionResult:
            analyzing.set()
            await release_analysis.wait()
            raise VideoInteractionError("provider call did not complete")

    async def source() -> Any:
        yield b"video"

    store = FakeStore()
    client = BlockingClient()
    service = VideoUnderstandingService(client=client, get_store=lambda: store)
    task = asyncio.create_task(
        service.start_uploaded(
            conversation_id=1,
            actor_user_id="user",
            guild_id="guild",
            source=UploadedVideoSource(
                kind="attachment",
                display_name="clip.mp4",
                locator="clip.mp4",
                mime_type="video/mp4",
                byte_size=5,
                bytes=source(),
            ),
            question="What happens?",
            config=_config(),
        )
    )
    await analyzing.wait()

    task.cancel()
    await asyncio.sleep(0)
    release_analysis.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not store.reserved_files
    assert not store.pending_files
    assert len(client.file_deletes) == 1
    assert client.file_deletes[0].startswith("files/kv-")
    assert not client.deletes
    assert not store.sessions


@pytest.mark.asyncio
async def test_cancelled_upload_does_not_delete_file_when_reservation_is_already_claimed() -> None:
    analyzing = asyncio.Event()
    release_analysis = asyncio.Event()

    class ClaimedStore(FakeStore):
        async def release_provider_file(self, file_name: str, actor_user_id: str) -> bool:
            return False

    class BlockingClient(FakeClient):
        async def start_from_file(self, **kwargs: Any) -> VideoInteractionResult:
            analyzing.set()
            await release_analysis.wait()
            raise VideoInteractionError("provider call did not complete")

    async def source() -> Any:
        yield b"video"

    store = ClaimedStore()
    client = BlockingClient()
    service = VideoUnderstandingService(client=client, get_store=lambda: store)
    task = asyncio.create_task(
        service.start_uploaded(
            conversation_id=1,
            actor_user_id="user",
            guild_id="guild",
            source=UploadedVideoSource(
                kind="workspace",
                display_name="clip.mp4",
                locator="imports/clip.mp4",
                mime_type="video/mp4",
                byte_size=5,
                bytes=source(),
            ),
            question="What happens?",
            config=_config(),
        )
    )
    await analyzing.wait()

    task.cancel()
    await asyncio.sleep(0)
    release_analysis.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert store.reserved_files
    assert not store.pending_files
    assert not client.file_deletes


@pytest.mark.asyncio
async def test_ask_requires_unambiguous_active_session() -> None:
    store = FakeStore(
        sessions=[
            Session("video_1", "https://youtu.be/abcdefghijk", "abcdefghijk", "r1"),
            Session("video_2", "https://youtu.be/lmnopqrstuv", "lmnopqrstuv", "r2"),
        ]
    )
    service = VideoUnderstandingService(  # type: ignore[arg-type]
        client=FakeClient(),
        get_store=lambda: store,
    )

    with pytest.raises(VideoSessionError, match="Several video sessions"):
        await service.ask(
            conversation_id=1,
            actor_user_id="user",
            guild_id="guild",
            session=None,
            question="Follow up",
            config=_config(),
        )


@pytest.mark.asyncio
async def test_malformed_completed_interaction_is_durably_queued_for_cleanup() -> None:
    store = FakeStore()
    client = FakeClient(
        fail_delete=True,
        start_error=VideoInteractionError(
            "malformed",
            interaction_id="remote-malformed",
            model="gemini-3.7-flash",
            usage=VideoUsage(input_tokens=10),
        ),
    )
    service = VideoUnderstandingService(client=client, get_store=lambda: store)

    with pytest.raises(VideoInteractionError):
        await service.start(
            conversation_id=1,
            actor_user_id="user",
            guild_id="guild",
            youtube_url="https://www.youtube.com/watch?v=abcdefghijk",
            youtube_video_id="abcdefghijk",
            question="Question",
            config=_config(),
        )

    assert [item.interaction_id for item in store.pending] == ["remote-malformed"]
    assert client.deletes == ["remote-malformed"]


@pytest.mark.asyncio
async def test_privacy_cleanup_retries_provider_deletion() -> None:
    store = FakeStore(pending=[Deletion("remote-1")])
    client = FakeClient(fail_delete=True)
    service = VideoUnderstandingService(  # type: ignore[arg-type]
        client=client,
        get_store=lambda: store,
    )

    assert await service.delete_user_data("user") == (1, True)
    assert store.deleted_users == ["user"]
    assert store.pending

    client.fail_delete = False
    assert await service.delete_user_data("user") == (1, False)
    assert not store.pending


@pytest.mark.asyncio
async def test_privacy_provider_cleanup_attempt_is_bounded() -> None:
    store = FakeStore(pending=[Deletion(f"remote-{index}") for index in range(6)])
    client = FakeClient()
    service = VideoUnderstandingService(client=client, get_store=lambda: store)

    assert await service.delete_user_data("user") == (1, True)
    assert client.deletes == ["remote-0", "remote-1", "remote-2", "remote-3"]
    assert [item.interaction_id for item in store.pending] == ["remote-4", "remote-5"]


@pytest.mark.asyncio
async def test_deletion_backoff_defers_provider_retry() -> None:
    store = FakeStore(pending=[Deletion("remote-1", retry_at=99_999_999_999)])
    client = FakeClient()
    service = VideoUnderstandingService(client=client, get_store=lambda: store)

    removed, pending = await service.sweep(now=1)

    assert removed == 0
    assert pending is False
    assert client.deletes == []
    assert store.pending


@pytest.mark.asyncio
async def test_keyless_privacy_cleanup_keeps_provider_deletion_queued() -> None:
    store = FakeStore(pending=[Deletion("remote-1")])
    service = VideoUnderstandingService(client=None, get_store=lambda: store)

    assert await service.delete_user_data("user") == (1, True)
    assert store.deleted_users == ["user"]
    assert [item.interaction_id for item in store.pending] == ["remote-1"]
