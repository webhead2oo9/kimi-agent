from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import discord
import pytest

from agent.turn import TurnPreparationInput, TurnResult
from app import guild_turn_adapter
from app import runtime as app_runtime
from app.guild_turn_adapter import (
    CallbackDiscordResponseSender,
    GuildMessageTurnAdapter,
    GuildTurnCollaborators,
    GuildTurnDeliveryConfig,
)
from tests.helpers import StubProviderManager, make_settings
from tools.registry import TurnHandoff
from tools.threads import ThreadCloseRequest, ThreadRequest


class FakeChannel:
    def __init__(self, channel_id: int) -> None:
        self.id = channel_id
        self.name = f"channel-{channel_id}"


class FakeThread(FakeChannel):
    def __init__(self, thread_id: int, *, parent_id: int) -> None:
        super().__init__(thread_id)
        self.parent_id = parent_id


class FakeMessage:
    def __init__(self, channel: FakeChannel) -> None:
        self.id = 41
        self.channel = channel
        self.content = "question"
        self.created_at = datetime(2026, 2, 3, 4, 5, tzinfo=UTC)


class FakeSentMessage:
    def __init__(
        self,
        message_id: int,
        content: str,
        channel: FakeChannel,
    ) -> None:
        self.id = message_id
        self.content = content
        self.channel = channel
        self.created_at = datetime.fromtimestamp(message_id, tz=UTC)


class FakeGateway:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.bound: list[tuple[str, str, object]] = []
        self.unbound: list[object] = []

    def bind_turn_source(
        self,
        conversation_key: str,
        message_id: str,
        source_message: object,
    ) -> object:
        self.events.append("bind")
        binding = object()
        self.bound.append((conversation_key, message_id, source_message))
        return binding

    def unbind_turn_source(self, binding: object) -> None:
        self.events.append("unbind")
        self.unbound.append(binding)

    async def add_status_reaction(self, _message: object, _emoji: str) -> None:
        self.events.append("acknowledge")


class FakeThreads:
    def __init__(
        self,
        events: list[str],
        *,
        created_thread: FakeThread | None = None,
    ) -> None:
        self.events = events
        self.created_thread = created_thread
        self.created: list[tuple[object, ThreadRequest, int]] = []
        self.closed: list[tuple[object, ThreadCloseRequest]] = []
        self.pointers: list[tuple[object, object]] = []
        self.discarded: list[object] = []

    def thread_handoff_creation_allowed(self, _message: object) -> bool:
        return False

    async def create_handoff_thread(
        self,
        message: object,
        request: ThreadRequest,
        conversation_id: int,
    ) -> FakeThread | None:
        self.events.append("create-thread")
        self.created.append((message, request, conversation_id))
        return self.created_thread

    async def close_handoff_thread(
        self,
        channel: object,
        request: ThreadCloseRequest,
    ) -> None:
        self.events.append("close-thread")
        self.closed.append((channel, request))

    async def send_cross_channel_pointer(
        self,
        message: object,
        thread: object,
    ) -> None:
        self.pointers.append((message, thread))

    async def discard_cross_channel_thread(self, thread: object) -> None:
        self.discarded.append(thread)


class FakeThreadHandoff:
    def __init__(self, managed_ids: set[int] | None = None) -> None:
        self.managed_ids = managed_ids or set()
        self.pruned: list[int] = []

    def is_managed(self, thread_id: int) -> bool:
        return thread_id in self.managed_ids

    async def prune(self, thread_id: int) -> None:
        self.pruned.append(thread_id)


class FakeCodingTasks:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.prepared: list[tuple[str, str, str | None]] = []
        self.released: list[str] = []
        self.finalized: list[str] = []

    async def prepare_handoff(
        self,
        task_id: str,
        *,
        channel_id: str,
        thread_id: str | None,
    ) -> bool:
        self.events.append("prepare")
        self.prepared.append((task_id, channel_id, thread_id))
        return True

    async def release_handoff(self, task_id: str) -> bool:
        self.events.append("release")
        self.released.append(task_id)
        return True

    async def finalize_handoff(self, task_id: str) -> bool:
        self.events.append("finalize")
        self.finalized.append(task_id)
        return True

    async def failed_handoff_task(self, _task_id: str) -> None:
        return None

    async def delete_status_message(self, *_args: object, **_kwargs: object) -> None:
        return None

    def task_marker(self, task_id: str) -> str:
        return task_id


class FakeCollaborators:
    def __init__(
        self,
        channel: FakeChannel,
        *,
        created_thread: FakeThread | None = None,
        sent_contents: tuple[str, ...] = ("answer",),
    ) -> None:
        self.events: list[str] = []
        self.config = GuildTurnDeliveryConfig(
            thread_auto_handoff_enabled=False,
            thread_handoff_enabled=True,
            bot_name="Kimi",
        )
        self.bot_user = object()
        self.discord_gateway = FakeGateway(self.events)
        self.threads = FakeThreads(self.events, created_thread=created_thread)
        self.thread_handoff = FakeThreadHandoff()
        self.coding = FakeCodingTasks(self.events)
        self.sent_contents = sent_contents
        self.send_calls: list[tuple[object, str, dict[str, object]]] = []
        self.channel = channel

    def _strip_message_invocation(self, content: str, *, bot_user: object) -> str:
        _ = bot_user
        return content

    async def send(
        self,
        channel: object,
        content: str,
        **kwargs: object,
    ) -> list[FakeSentMessage]:
        self.events.append("send")
        self.send_calls.append((channel, content, kwargs))
        return [
            FakeSentMessage(700 + index, chunk, cast(FakeChannel, channel))
            for index, chunk in enumerate(self.sent_contents)
        ]

    def bundle(self) -> GuildTurnCollaborators:
        return GuildTurnCollaborators(
            config=self.config,
            gateway=cast(Any, self.discord_gateway),
            threads=cast(Any, self.threads),
            thread_handoff=cast(Any, self.thread_handoff),
            coding=cast(Any, self.coding),
            responses=cast(Any, self),
            bot_user=cast(Any, lambda: self.bot_user),
            strip_invocation=cast(Any, self._strip_message_invocation),
        )


def _adapter(
    app: FakeCollaborators,
    message: FakeMessage,
) -> GuildMessageTurnAdapter:
    return GuildMessageTurnAdapter(
        collaborators=app.bundle(),
        message=cast(discord.Message, message),
        context_channel_id=str(message.channel.id),
        personal_chat=False,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("in_thread", [False, True], ids=["channel", "thread"])
async def test_receipt_preserves_sent_chunks_and_actual_destination(
    monkeypatch: pytest.MonkeyPatch,
    in_thread: bool,
) -> None:
    channel = FakeChannel(100)
    thread = FakeThread(200, parent_id=100) if in_thread else None
    if thread is not None:
        monkeypatch.setattr(guild_turn_adapter.discord, "Thread", FakeThread)
    app = FakeCollaborators(
        channel,
        created_thread=thread,
        sent_contents=("first `(1/2)`", "second"),
    )
    message = FakeMessage(channel)
    request = ThreadRequest(name="focused") if in_thread else None

    receipt = await _adapter(app, message).deliver(
        TurnResult(response_text="answer", thread_request=request),
        conversation_id=9,
    )

    destination = thread or channel
    assert app.send_calls[0][0] is destination
    assert app.send_calls[0][2]["reference"] is (None if in_thread else message)
    assert "workspace_key" not in app.send_calls[0][2]
    assert receipt.context_channel_id == str(destination.id)
    assert [reply.discord_message_id for reply in receipt.replies] == ["700", "701"]
    assert [reply.content for reply in receipt.replies] == ["first", "second"]
    assert [reply.source_created_at for reply in receipt.replies] == [700.0, 701.0]
    assert receipt.delivery_failed is False
    if request is not None:
        assert app.threads.created == [(message, request, 9)]


@pytest.mark.asyncio
async def test_thread_and_coding_handoffs_keep_prepare_send_release_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(guild_turn_adapter.discord, "Thread", FakeThread)
    channel = FakeChannel(100)
    thread = FakeThread(200, parent_id=100)
    app = FakeCollaborators(channel, created_thread=thread)
    coding = FakeCodingTasks(app.events)
    app.coding = coding
    message = FakeMessage(channel)
    thread_request = ThreadRequest(name="coding")
    close_request = ThreadCloseRequest(thread_id=200)
    terminal_handoff = TurnHandoff(
        response_text="Coding task started.",
        reason="coding_task",
        task_id="task-123456",
    )
    result = TurnResult(
        response_text="Coding task started.",
        thread_request=thread_request,
        thread_close_request=close_request,
        terminal_handoff=terminal_handoff,
    )

    receipt = await _adapter(app, message).deliver(result, conversation_id=12)

    assert app.threads.created == [(message, thread_request, 12)]
    assert app.threads.closed == [(thread, close_request)]
    assert coding.prepared == [("task-123456", "100", "200")]
    assert coding.released == ["task-123456"]
    assert coding.finalized == []
    assert app.events.index("prepare") < app.events.index("send") < app.events.index("release")
    assert app.events.index("prepare") < app.events.index("acknowledge")
    assert receipt.delivered_result is None
    assert result.thread_request is thread_request
    assert result.thread_close_request is close_request
    assert result.terminal_handoff is terminal_handoff


@pytest.mark.asyncio
async def test_delivery_failure_is_explicit_when_no_message_was_sent() -> None:
    channel = FakeChannel(100)
    app = FakeCollaborators(channel, sent_contents=())
    receipt = await _adapter(app, FakeMessage(channel)).deliver(
        TurnResult(response_text="answer"),
        conversation_id=9,
    )

    assert receipt.replies == ()
    assert receipt.delivery_failed is True
    assert receipt.context_channel_id == "100"


def test_bind_turn_source_scopes_gateway_binding() -> None:
    channel = FakeChannel(100)
    app = FakeCollaborators(channel)
    message = FakeMessage(channel)
    source_message = object()
    source = cast(
        TurnPreparationInput,
        SimpleNamespace(
            conversation_key="root-key",
            trigger_discord_message_id="41",
            source_message=source_message,
        ),
    )

    with _adapter(app, message).bind_turn_source(source):
        app.events.append("inside")

    assert app.events == ["bind", "inside", "unbind"]
    assert app.discord_gateway.bound == [("root-key", "41", source_message)]
    assert len(app.discord_gateway.unbound) == 1


def test_guild_activity_finishes_after_delivery() -> None:
    app = FakeCollaborators(FakeChannel(100))
    assert _adapter(app, FakeMessage(app.channel)).activity_must_finish_before_delivery is False


@pytest.mark.asyncio
async def test_build_app_populates_guild_turn_collaborators_after_init(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        app_runtime,
        "build_provider_manager",
        lambda settings: StubProviderManager(settings),
    )
    app = app_runtime.build_app(
        make_settings(
            config_dir=str(tmp_path),
            database_path=str(tmp_path / "guild-turn.db"),
            model_api_key="test-key",
        )
    )

    try:
        await app.lifecycle.initialize()
        collaborators = app.guild_turn_collaborators()

        assert collaborators.config == GuildTurnDeliveryConfig(
            thread_auto_handoff_enabled=app.settings.thread_auto_handoff_enabled,
            thread_handoff_enabled=app.settings.thread_handoff_enabled,
            bot_name=app.settings.bot_name,
        )
        assert collaborators.gateway is app.discord_gateway
        assert collaborators.threads is app.threads
        assert collaborators.thread_handoff is app.thread_handoff
        assert collaborators.thread_handoff is not None
        assert collaborators.coding is app.lifecycle.resources.coding_tasks
        assert isinstance(collaborators.responses, CallbackDiscordResponseSender)
        assert callable(collaborators.bot_user)
        assert callable(collaborators.strip_invocation)
    finally:
        await app.close()
