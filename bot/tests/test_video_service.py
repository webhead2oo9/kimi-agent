from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from video_understanding.client import (
    VideoInteractionError,
    VideoInteractionResult,
    VideoUsage,
)
from video_understanding.service import (
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
    interaction_count: int = 1
    expires_at: float = 99999999999


@dataclass
class Deletion:
    interaction_id: str
    retry_at: float = 0


@dataclass
class FakeStore:
    sessions: list[Session] = field(default_factory=list)
    pending: list[Deletion] = field(default_factory=list)
    created: list[dict[str, Any]] = field(default_factory=list)
    advances: list[dict[str, Any]] = field(default_factory=list)
    deleted_users: list[str] = field(default_factory=list)

    async def create_session(self, **kwargs: Any) -> None:
        self.created.append(kwargs)
        self.sessions.append(
            Session(
                handle=kwargs["handle"],
                youtube_url=kwargs["youtube_url"],
                youtube_video_id=kwargs["youtube_video_id"],
                latest_interaction_id=kwargs["interaction_id"],
            )
        )

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

    async def complete_deletion(self, interaction_id: str) -> None:
        self.pending = [item for item in self.pending if item.interaction_id != interaction_id]

    async def fail_deletion(
        self,
        interaction_id: str,
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

    async def start(self, **kwargs: Any) -> VideoInteractionResult:
        self.starts.append(kwargs)
        if self.start_error is not None:
            raise self.start_error
        return _result("remote-start")

    async def ask(self, **kwargs: Any) -> VideoInteractionResult:
        self.asks.append(kwargs)
        return _result("remote-ask")

    async def delete(self, interaction_id: str) -> None:
        self.deletes.append(interaction_id)
        if self.fail_delete:
            raise VideoInteractionError("unavailable")

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


def _config() -> VideoSessionConfig:
    return VideoSessionConfig(
        model="gemini-3.7-flash",
        thinking_level="low",
        max_output_tokens=4096,
        max_session_interactions=5,
        session_ttl_minutes=60,
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
