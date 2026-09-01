from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import pytest
import pytest_asyncio

from agent.turn import TurnDependencies, TurnPreparationInput, TurnResult
from app.cancellation import ActiveOperationRegistry
from app.foreground_turn import (
    DeliveredReply,
    ForegroundActivityReporter,
    ForegroundTurnAdapter,
    ForegroundTurnInvocation,
    ForegroundTurnRunner,
    TurnConversationSpec,
    TurnDeliveryReceipt,
    TurnSurfaceOutcome,
)
from storage.conversations import ChannelMessageRecord, ConversationStore
from storage.db import Database
from tests.helpers import make_settings, make_turn_dependencies
from tools.workspace.common import UserLocks
from trust.tiers import TrustTier
from utils.privacy_barrier import UserPrivacyBarrier
from workspace import WorkspaceKey


class RecordingConversationStore(ConversationStore):
    def __init__(self, database: Database, events: list[str]) -> None:
        super().__init__(database)
        self.events = events

    async def touch(self, conversation_id: int) -> bool:
        self.events.append("conversation:touch")
        return await super().touch(conversation_id)

    async def get_or_create(self, key: str, channel_name: str = "", **kwargs: Any) -> int:
        self.events.append("conversation:create")
        return await super().get_or_create(key, channel_name, **kwargs)

    async def save_channel_messages(
        self,
        conversation_id: int,
        records: list[ChannelMessageRecord],
        *,
        context_channel_id: str | None = None,
    ) -> int | None:
        self.events.extend(f"persist:{record.role}" for record in records)
        return await super().save_channel_messages(
            conversation_id,
            records,
            context_channel_id=context_channel_id,
        )


class FakeReporter:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.finish_count = 0

    @property
    def committed_message_id(self) -> int | None:
        return None

    async def __call__(self, _update: object) -> None:
        self.events.append("reporter:update")

    async def finish(self) -> None:
        self.finish_count += 1
        self.events.append("reporter:finish")


class FakeAdapter:
    def __init__(
        self,
        events: list[str],
        *,
        receipt: TurnDeliveryReceipt | None = None,
        activity_must_finish_before_delivery: bool = False,
        deliver_error: Exception | None = None,
    ) -> None:
        self.events = events
        self.receipt = receipt or TurnDeliveryReceipt(
            replies=(DeliveredReply("assistant-1", "answer", 20.0),),
            context_channel_id="channel-1",
        )
        self._activity_must_finish_before_delivery = activity_must_finish_before_delivery
        self.deliver_error = deliver_error
        self.reporter = FakeReporter(events)
        self.outcomes: list[TurnSurfaceOutcome] = []
        self.delivery_started = asyncio.Event()

    @property
    def activity_must_finish_before_delivery(self) -> bool:
        return self._activity_must_finish_before_delivery

    def make_activity_reporter(
        self,
        *,
        on_committed_message: object,
    ) -> ForegroundActivityReporter:
        _ = on_committed_message
        self.events.append("adapter:make_reporter")
        return self.reporter

    @contextmanager
    def bind_turn_source(self, _source: TurnPreparationInput) -> Iterator[None]:
        self.events.append("adapter:bind_enter")
        try:
            yield
        finally:
            self.events.append("adapter:bind_exit")

    async def deliver(
        self,
        _result: TurnResult,
        *,
        conversation_id: int,
    ) -> TurnDeliveryReceipt:
        assert conversation_id > 0
        self.events.append("adapter:deliver")
        self.delivery_started.set()
        if self.deliver_error is not None:
            raise self.deliver_error
        return self.receipt

    async def finish(self, outcome: TurnSurfaceOutcome) -> None:
        self.outcomes.append(outcome)
        self.events.append(f"adapter:finish:{outcome.kind}")


class FakeDependencyFactory:
    def __init__(self, events: list[str], workspace_dir: Path, locks: UserLocks) -> None:
        self.events = events
        self.workspace_dir = workspace_dir
        self.locks = locks

    async def build(self, _source: TurnPreparationInput, **kwargs: Any) -> TurnDependencies:
        self.events.append("dependencies:build")
        return make_turn_dependencies(
            workspace_dir=self.workspace_dir,
            workspace_locks=self.locks,
            persist_prepared_user_message=kwargs["persist_prepared_user_message"],
            activity_reporter=kwargs["activity_reporter"],
        )


class PreparedTurnStub:
    content = "hello"
    input_parts: tuple[()] = ()


@pytest_asyncio.fixture
async def foreground_database(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(tmp_path / "foreground.db")
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


async def _collect_reply_context(*_args: Any, **_kwargs: Any) -> None:
    return None


def _strip_mention(content: str, *, bot_user: object) -> str:
    _ = bot_user
    return content.strip()


def _source(*, workspace_key: WorkspaceKey | None = None) -> TurnPreparationInput:
    return TurnPreparationInput(
        raw_content="hello",
        source_message=object(),
        bot_user=object(),
        guild_id="guild-1",
        channel_id="channel-1",
        thread_id=None,
        channel_name="general",
        user_id="user-1",
        user_name="User One",
        trust_tier=TrustTier.MEMBER,
        conversation_key="root-1",
        trigger_discord_message_id="user-message-1",
        workspace_key=workspace_key,
    )


def _invocation(
    *,
    existing_conversation_id: int | None = None,
    workspace_key: WorkspaceKey | None = None,
) -> ForegroundTurnInvocation:
    return ForegroundTurnInvocation(
        conversation=TurnConversationSpec(
            key="root-1",
            channel_name="general",
            guild_id="guild-1",
            channel_id="channel-1",
            thread_id=None,
            root_discord_message_id="user-message-1",
            existing_conversation_id=existing_conversation_id,
        ),
        source=_source(workspace_key=workspace_key),
        prepared_user_discord_message_id="user-message-1",
        prepared_user_source_created_at=10.0,
        prepared_user_context_channel_id="channel-1",
        collect_reply_context=_collect_reply_context,
        strip_mention=_strip_mention,
        stop_event=asyncio.Event(),
        timeout_seconds=30.0,
    )


def _runner(
    store: ConversationStore,
    factory: FakeDependencyFactory,
    locks: UserLocks,
    handle_turn_hook: object,
) -> ForegroundTurnRunner:
    return ForegroundTurnRunner(
        settings=make_settings(),
        conversation_store=store,
        dependency_factory=cast(Any, factory),
        active_operations=ActiveOperationRegistry(),
        privacy_barrier=UserPrivacyBarrier(),
        workspace_locks=locks,
        handle_turn_hook=cast(Any, handle_turn_hook),
    )


async def _transcript_rows(database: Database) -> list[tuple[str, str | None]]:
    async with database.conn.execute("SELECT role, content FROM messages ORDER BY id") as cursor:
        return [(str(row["role"]), row["content"]) for row in await cursor.fetchall()]


def _successful_handle_turn(
    events: list[str],
    *,
    result: TurnResult | None = None,
    database: Database | None = None,
):
    async def run(
        source: TurnPreparationInput,
        *,
        dependencies: TurnDependencies,
        **_kwargs: Any,
    ) -> TurnResult:
        events.append("handle_turn:enter")
        await dependencies.persist_prepared_user_message(
            source,
            cast(Any, PreparedTurnStub()),
        )
        if database is not None:
            assert await _transcript_rows(database) == [("user", "hello")]
        events.append("provider:run")
        return result or TurnResult(response_text="answer")

    return run


@pytest.mark.asyncio
async def test_successful_text_turn_call_order(
    foreground_database: Database, tmp_path: Path
) -> None:
    events: list[str] = []
    store = RecordingConversationStore(foreground_database, events)
    conversation_id = await store.get_or_create(
        "root-1",
        "general",
        guild_id="guild-1",
        channel_id="channel-1",
        root_discord_message_id="user-message-1",
    )
    events.clear()
    locks = UserLocks()
    adapter = FakeAdapter(events)
    runner = _runner(
        store,
        FakeDependencyFactory(events, tmp_path, locks),
        locks,
        _successful_handle_turn(events),
    )

    result = await runner.run(
        _invocation(existing_conversation_id=conversation_id),
        adapter=adapter,
    )

    assert result == TurnResult(response_text="answer")
    assert events == [
        "conversation:touch",
        "adapter:make_reporter",
        "dependencies:build",
        "adapter:bind_enter",
        "handle_turn:enter",
        "persist:user",
        "provider:run",
        "adapter:bind_exit",
        "adapter:deliver",
        "persist:assistant",
        "adapter:finish:delivered",
        "reporter:finish",
    ]


@pytest.mark.asyncio
async def test_user_persists_before_provider_and_assistant_after_delivery(
    foreground_database: Database,
    tmp_path: Path,
) -> None:
    events: list[str] = []
    store = RecordingConversationStore(foreground_database, events)
    locks = UserLocks()

    class InspectingAdapter(FakeAdapter):
        async def deliver(
            self,
            result: TurnResult,
            *,
            conversation_id: int,
        ) -> TurnDeliveryReceipt:
            assert await _transcript_rows(foreground_database) == [("user", "hello")]
            return await super().deliver(result, conversation_id=conversation_id)

    adapter = InspectingAdapter(events)
    runner = _runner(
        store,
        FakeDependencyFactory(events, tmp_path, locks),
        locks,
        _successful_handle_turn(events, database=foreground_database),
    )

    await runner.run(_invocation(), adapter=adapter)

    assert await _transcript_rows(foreground_database) == [
        ("user", "hello"),
        ("assistant", "answer"),
    ]


@pytest.mark.asyncio
async def test_delivery_failure_propagates_without_assistant_persistence(
    foreground_database: Database,
    tmp_path: Path,
) -> None:
    events: list[str] = []
    store = RecordingConversationStore(foreground_database, events)
    locks = UserLocks()
    adapter = FakeAdapter(
        events,
        receipt=TurnDeliveryReceipt(context_channel_id="channel-1", delivery_failed=True),
    )
    runner = _runner(
        store,
        FakeDependencyFactory(events, tmp_path, locks),
        locks,
        _successful_handle_turn(events),
    )

    result = await runner.run(_invocation(), adapter=adapter)

    assert result is not None and result.delivery_failed
    assert await _transcript_rows(foreground_database) == [("user", "hello")]
    assert adapter.outcomes[-1].kind == "delivery_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["handle_turn", "deliver"])
async def test_reporter_finishes_when_turn_or_delivery_raises(
    foreground_database: Database,
    tmp_path: Path,
    failure: str,
) -> None:
    events: list[str] = []
    store = RecordingConversationStore(foreground_database, events)
    locks = UserLocks()

    async def failing_handle(*_args: Any, **_kwargs: Any) -> TurnResult:
        events.append("handle_turn:enter")
        raise RuntimeError("turn failed")

    adapter = FakeAdapter(
        events,
        deliver_error=RuntimeError("delivery failed") if failure == "deliver" else None,
    )
    handle = failing_handle if failure == "handle_turn" else _successful_handle_turn(events)
    runner = _runner(store, FakeDependencyFactory(events, tmp_path, locks), locks, handle)

    with pytest.raises(RuntimeError):
        await runner.run(_invocation(), adapter=adapter)

    assert adapter.reporter.finish_count == 1
    assert adapter.outcomes[-1].kind == "failed"


@pytest.mark.asyncio
async def test_interaction_style_reporter_finishes_before_delivery(
    foreground_database: Database,
    tmp_path: Path,
) -> None:
    events: list[str] = []
    store = RecordingConversationStore(foreground_database, events)
    locks = UserLocks()
    adapter = FakeAdapter(events, activity_must_finish_before_delivery=True)
    runner = _runner(
        store,
        FakeDependencyFactory(events, tmp_path, locks),
        locks,
        _successful_handle_turn(events),
    )

    await runner.run(_invocation(), adapter=adapter)

    assert events.index("reporter:finish") < events.index("adapter:deliver")
    assert adapter.reporter.finish_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("has_workspace_key", "output_files", "waits_for_writer"),
    [
        (True, ("artifact.txt",), True),
        (True, (), False),
        (False, ("artifact.txt",), False),
    ],
)
async def test_delivery_workspace_guard_requires_key_and_files(
    foreground_database: Database,
    tmp_path: Path,
    has_workspace_key: bool,
    output_files: tuple[str, ...],
    waits_for_writer: bool,
) -> None:
    events: list[str] = []
    store = RecordingConversationStore(foreground_database, events)
    locks = UserLocks()
    workspace_key = WorkspaceKey("user-1__guild-1")
    result = TurnResult(
        response_text="answer",
        workspace_key=workspace_key if has_workspace_key else None,
        output_files=output_files,
    )
    adapter = FakeAdapter(events)
    runner = _runner(
        store,
        FakeDependencyFactory(events, tmp_path, locks),
        locks,
        _successful_handle_turn(events, result=result),
    )

    async with locks.writer(workspace_key):
        task = asyncio.create_task(
            runner.run(
                _invocation(workspace_key=workspace_key if has_workspace_key else None),
                adapter=cast(ForegroundTurnAdapter, adapter),
            )
        )
        if waits_for_writer:
            deadline = asyncio.get_running_loop().time() + 0.5
            while "adapter:bind_exit" not in events:
                if asyncio.get_running_loop().time() > deadline:
                    raise AssertionError("turn did not reach the delivery boundary")
                await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not adapter.delivery_started.is_set()
            assert not task.done()
        else:
            await asyncio.wait_for(task, timeout=0.5)
            assert adapter.delivery_started.is_set()

    if waits_for_writer:
        await asyncio.wait_for(task, timeout=0.5)
        assert adapter.delivery_started.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        TurnResult(
            response_text="blocked",
            blocked_by_moderation=True,
            termination_reason="moderation_blocked",
        ),
        TurnResult(response_text="attachment failed", termination_reason="attachment_error"),
    ],
    ids=["moderation", "attachment-error"],
)
async def test_excluded_turn_results_do_not_persist_assistant(
    foreground_database: Database,
    tmp_path: Path,
    result: TurnResult,
) -> None:
    events: list[str] = []
    store = RecordingConversationStore(foreground_database, events)
    locks = UserLocks()
    adapter = FakeAdapter(events)
    runner = _runner(
        store,
        FakeDependencyFactory(events, tmp_path, locks),
        locks,
        _successful_handle_turn(events, result=result),
    )

    await runner.run(_invocation(), adapter=adapter)

    assert await _transcript_rows(foreground_database) == [("user", "hello")]
