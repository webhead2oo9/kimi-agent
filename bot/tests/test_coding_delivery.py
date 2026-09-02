from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import discord
import pytest

from app import coding_delivery as coding_delivery_module
from app.coding_delivery import (
    CodingDelivery,
    CodingDeliveryConfig,
    CodingTaskController,
    CodingTaskControllerState,
)
from app.root_locks import RootLockPool
from discord_adapter.io import (
    attachment_delivery_notice,
    chunk_message,
    prepare_attachment_delivery,
    suppress_link_previews,
)
from moderation.types import Direction
from storage.coding_tasks import CodingTask, CodingTaskStatus, CodingTaskStore
from storage.conversations import ConversationStore
from tests.helpers import make_settings
from tools.workspace.common import UserLocks
from trust.tiers import TrustTier
from utils.privacy_barrier import UserPrivacyBarrier


class AsyncCall:
    def __init__(self, return_value: Any = None) -> None:
        self.return_value = return_value
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        return self.return_value


class FakeBot:
    def __init__(self, *, user: object | None = None) -> None:
        self.user = user
        self.channels: dict[int, object] = {}

    def get_channel(self, channel_id: int) -> object | None:
        return self.channels.get(channel_id)

    async def fetch_channel(self, channel_id: int) -> object:
        return self.channels[channel_id]


class FakeCodingTaskStore:
    def __init__(self, task: CodingTask | None = None) -> None:
        self.task = task
        self.checkpoints: list[tuple[str, dict[str, Any]]] = []

    async def get_task(self, task_id: str) -> CodingTask | None:
        if self.task is not None:
            assert task_id == self.task.id
        return self.task

    async def set_checkpoint(self, task_id: str, checkpoint: dict[str, Any]) -> None:
        self.checkpoints.append((task_id, checkpoint))


class FakeConversationStore:
    def __init__(self) -> None:
        self.saved: list[tuple[int, list[Any], str]] = []

    async def save_channel_messages(
        self,
        conversation_id: int,
        records: list[Any],
        *,
        context_channel_id: str,
    ) -> None:
        self.saved.append((conversation_id, records, context_channel_id))


class FakeGateway:
    def __init__(self) -> None:
        self.reactions: list[tuple[object, str]] = []

    async def add_status_reaction(self, message: object, emoji: str) -> None:
        self.reactions.append((message, emoji))


class FakeThreads:
    def __init__(self) -> None:
        self.thread_handoff: object | None = None
        self.adopt = AsyncCall()
        self.create = AsyncCall()
        self.creation_allowed = False

    async def adopt_managed_handoff_thread(self, message: object) -> object | None:
        return await self.adopt(message)

    def thread_handoff_creation_allowed(self, message: object) -> bool:
        del message
        return self.creation_allowed

    async def create_handoff_thread(
        self,
        message: object,
        request: object,
        conversation_id: int | None,
        *,
        creator_user_id: str,
    ) -> object | None:
        return await self.create(
            message,
            request,
            conversation_id,
            creator_user_id=creator_user_id,
        )


def make_delivery(
    *,
    bot: object | None = None,
    store: object | None = None,
    conversation_store: object | None = None,
    gateway: object | None = None,
    threads: object | None = None,
    moderation_service: object | None = None,
    config: CodingDeliveryConfig | None = None,
) -> CodingDelivery:
    return CodingDelivery(
        bot=cast(Any, bot or FakeBot()),
        store=cast(CodingTaskStore, store or FakeCodingTaskStore()),
        conversation_store=cast(
            ConversationStore,
            conversation_store or FakeConversationStore(),
        ),
        discord_gateway=cast(Any, gateway or FakeGateway()),
        workspace_locks=UserLocks(),
        root_locks=RootLockPool(),
        threads=cast(Any, threads or FakeThreads()),
        moderation_service=cast(Any, moderation_service),
        config=config
        or CodingDeliveryConfig(
            thread_handoff_enabled=False,
            thread_auto_handoff_enabled=False,
            bot_name="Kimi",
        ),
        strip_message_invocation=lambda content, *, bot_user: content,
    )


def make_controller(*, coding_tasks_enabled: bool) -> CodingTaskController:
    async def user_blocked(_user_id: str) -> bool:
        return False

    return CodingTaskController(
        settings=make_settings(coding_tasks_enabled=coding_tasks_enabled),
        store=cast(CodingTaskStore, FakeCodingTaskStore()),
        usage_store=cast(Any, object()),
        provider_manager=cast(Any, SimpleNamespace(model_config=None)),
        source_registry=cast(Any, object()),
        tools=cast(Any, object()),
        llm_semaphore=asyncio.Semaphore(1),
        privacy_barrier=UserPrivacyBarrier(),
        user_blocked=user_blocked,
        delivery=make_delivery(),
    )


@pytest.mark.asyncio
async def test_disabled_controller_has_an_explicit_valid_state() -> None:
    controller = make_controller(coding_tasks_enabled=False)
    assert controller.state is CodingTaskControllerState.NOT_STARTED

    await controller.start()

    assert controller.state is CodingTaskControllerState.DISABLED
    assert controller.running is False


@pytest.mark.asyncio
async def test_enabled_controller_degrades_when_coding_role_is_unavailable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A missing coding role unregisters the surface; it must not abort startup."""

    controller = make_controller(coding_tasks_enabled=True)

    with caplog.at_level(logging.WARNING, logger="app.coding_delivery"):
        await controller.start()

    assert controller.state is CodingTaskControllerState.DISABLED
    assert controller.running is False
    assert "assigns no coding role" in caplog.text


def test_coding_delivery_text_uses_readable_short_task_reference() -> None:
    task_id = "3ff8bac7f9e24ed19a65d267c188d7ea"
    task = SimpleNamespace(
        id=task_id,
        status=CodingTaskStatus.QUEUED,
        objective="Review the entire workspace and produce a detailed report",
        display_summary="Review the workspace",
        milestone="",
        plan=[],
    )

    status_text = CodingDelivery.status_text(cast(CodingTask, task))
    result_text = CodingDelivery.result_delivery_text(
        task_id,
        "Implemented the requested change.",
    )

    assert "coding-status:" not in status_text
    assert "coding-result:" not in result_text
    assert "Coding task `3ff8bac7`: queued" in status_text
    assert "Review the workspace" in status_text
    assert "produce a detailed report" not in status_text
    assert result_text.startswith("**Coding result `3ff8bac7`**\n")
    assert (
        CodingDelivery.strip_delivery_marker(
            result_text,
            task_ref=task_id[:8],
        )
        == "Implemented the requested change."
    )


def test_coding_status_replaces_summary_with_worker_plan() -> None:
    task = SimpleNamespace(
        id="3ff8bac7f9e24ed19a65d267c188d7ea",
        status=CodingTaskStatus.RUNNING,
        objective="Raw durable objective",
        display_summary="Queued summary",
        milestone="Repository inspected",
        plan=[{"content": "Update the parser", "status": "in_progress"}],
    )

    status = CodingDelivery.status_text(cast(CodingTask, task))

    assert "Update the parser" in status
    assert "Repository inspected" in status
    assert "Raw durable objective" not in status
    assert "Queued summary" not in status


def test_coding_status_wire_text_suppresses_link_previews() -> None:
    status = "Working on https://example.com/repo"

    assert CodingDelivery.status_wire_text(status) == "Working on <https://example.com/repo>"


def test_strip_coding_delivery_marker_supports_legacy_messages() -> None:
    text = "-# coding-result:3ff8bac7f9e24ed19a65d267c188d7ea\nDone."

    assert CodingDelivery.strip_delivery_marker(text) == "Done."


def test_coding_result_delivery_uses_normal_discord_chunking_without_truncation() -> None:
    task_id = "3ff8bac7f9e24ed19a65d267c188d7ea"
    report = "start\n" + ("detail line\n" * 500) + "end-of-report"

    delivery_text = CodingDelivery.result_delivery_text(task_id, report)
    chunks = chunk_message(delivery_text)

    assert len(chunks) > 1
    assert "[Report truncated for delivery.]" not in delivery_text
    assert "end-of-report" in chunks[-1]


@pytest.mark.asyncio
async def test_coding_result_recovery_matches_link_suppressed_wire_chunks() -> None:
    bot_user = SimpleNamespace(id=99)
    expected_text = "first https://example.com\n" + ("detail\n" * 500) + "last"
    expected = chunk_message(suppress_link_previews(expected_text))

    class HistoryChannel:
        def __init__(self, contents: list[str]) -> None:
            self.messages = [
                SimpleNamespace(content=content, author=bot_user, id=index)
                for index, content in enumerate(contents, start=1)
            ]

        async def history(self, *, limit: int):
            del limit
            for message in reversed(self.messages):
                yield message

    delivery = make_delivery(bot=FakeBot(user=bot_user))
    complete = cast(discord.TextChannel | discord.Thread, HistoryChannel(expected))
    partial = cast(discord.TextChannel | discord.Thread, HistoryChannel(expected[:-1]))

    recovered = await delivery.find_result_delivery(
        complete,
        expected_text,
        legacy_marker="coding-result:legacy",
    )
    incomplete = await delivery.find_result_delivery(
        partial,
        expected_text,
        legacy_marker="coding-result:legacy",
    )

    assert [message.content for message in recovered] == expected
    assert incomplete == []


@pytest.mark.asyncio
async def test_coding_result_channel_keeps_originating_thread() -> None:
    fallback = cast(discord.TextChannel | discord.Thread, SimpleNamespace(id=22))
    task = cast(CodingTask, SimpleNamespace(thread_id="22"))

    result = await make_delivery().result_channel(task, fallback, "result")

    assert result is fallback


@pytest.mark.asyncio
async def test_coding_result_channel_adopts_foreground_handoff_thread(monkeypatch) -> None:
    class FakeTextChannel:
        async def fetch_message(self, message_id: int):
            assert message_id == 123
            return trigger

    class FakeThread:
        id = 20

    trigger = SimpleNamespace(content="build the CLI")
    fallback = FakeTextChannel()
    thread = FakeThread()
    threads = FakeThreads()
    threads.thread_handoff = object()
    threads.adopt.return_value = thread
    monkeypatch.setattr(coding_delivery_module.discord, "TextChannel", FakeTextChannel)
    monkeypatch.setattr(coding_delivery_module.discord, "Thread", FakeThread)
    task = cast(
        CodingTask,
        SimpleNamespace(
            id="3ff8bac7f9e24ed19a65d267c188d7ea",
            thread_id=None,
            checkpoint={},
            trigger_discord_message_id="123",
        ),
    )
    store = FakeCodingTaskStore(task)
    delivery = make_delivery(
        store=store,
        threads=threads,
        config=CodingDeliveryConfig(
            thread_auto_handoff_enabled=False,
            thread_handoff_enabled=True,
            bot_name="Kimi",
        ),
    )

    result = await delivery.result_channel(
        task,
        cast(discord.TextChannel | discord.Thread, fallback),
        "result",
    )

    assert result is thread
    assert threads.adopt.calls == [((trigger,), {})]
    assert store.checkpoints[0][1]["delivery"]["thread_id"] == "20"


@pytest.mark.asyncio
async def test_coding_result_channel_applies_forced_auto_thread_policy(monkeypatch) -> None:
    class FakeTextChannel:
        id = 10

        async def fetch_message(self, message_id: int):
            assert message_id == 123
            return trigger

    class FakeThread:
        id = 20

    trigger = SimpleNamespace(content="build the CLI")
    fallback = FakeTextChannel()
    thread = FakeThread()
    threads = FakeThreads()
    threads.thread_handoff = object()
    threads.creation_allowed = True
    threads.create.return_value = thread
    gateway = FakeGateway()
    bot = FakeBot(user=SimpleNamespace(id=99))
    monkeypatch.setattr(coding_delivery_module.discord, "TextChannel", FakeTextChannel)
    monkeypatch.setattr(coding_delivery_module.discord, "Thread", FakeThread)
    monkeypatch.setattr(
        coding_delivery_module,
        "load_channel_auto_thread",
        lambda _channel_id: SimpleNamespace(
            min_lines=None,
            min_chars=None,
            always=True,
        ),
    )
    task = cast(
        CodingTask,
        SimpleNamespace(
            id="3ff8bac7f9e24ed19a65d267c188d7ea",
            thread_id=None,
            checkpoint={},
            trigger_discord_message_id="123",
            channel_id="10",
            conversation_id=7,
            user_id="42",
        ),
    )
    store = FakeCodingTaskStore(task)
    delivery = make_delivery(
        bot=bot,
        store=store,
        threads=threads,
        gateway=gateway,
        config=CodingDeliveryConfig(
            thread_auto_handoff_enabled=True,
            thread_handoff_enabled=True,
            bot_name="Kimi",
        ),
    )

    result = await delivery.result_channel(
        task,
        cast(discord.TextChannel | discord.Thread, fallback),
        "short result",
    )

    assert result is thread
    assert len(threads.create.calls) == 1
    assert gateway.reactions == [(trigger, coding_delivery_module.THREAD_HANDOFF_REACTION)]


@pytest.mark.asyncio
async def test_delete_coding_status_uses_recorded_message() -> None:
    delete = AsyncCall()
    message = SimpleNamespace(delete=delete)
    fetch_message = AsyncCall(message)
    channel = SimpleNamespace(fetch_message=fetch_message)
    task = cast(
        CodingTask,
        SimpleNamespace(
            id="3ff8bac7f9e24ed19a65d267c188d7ea",
            status_discord_message_id="456",
        ),
    )

    await make_delivery().delete_status_message(
        cast(discord.TextChannel | discord.Thread, channel),
        task,
        "Coding task `3ff8bac7`",
    )

    assert fetch_message.calls == [((456,), {})]
    assert delete.calls == [((), {})]


@pytest.mark.asyncio
async def test_commit_final_delivery_preserves_durable_ordering() -> None:
    events: list[str] = []

    class EventStore(FakeCodingTaskStore):
        async def mark_delivered(self, task_id: str, message_id: str) -> None:
            assert task_id == "task-1"
            assert message_id == "123"
            events.append("mark-delivered")

    class EventConversationStore(FakeConversationStore):
        async def save_channel_messages(
            self,
            conversation_id: int,
            records: list[Any],
            *,
            context_channel_id: str,
        ) -> None:
            assert conversation_id == 7
            assert len(records) == 1
            assert context_channel_id == "20"
            events.append("persist")

    async def delete_status() -> None:
        events.append("delete-status")

    task = cast(CodingTask, SimpleNamespace(id="task-1", conversation_id=7))
    final_message = cast(
        discord.Message,
        SimpleNamespace(id=123, content="Done.", created_at=None),
    )
    status_message = cast(discord.Message, SimpleNamespace(delete=delete_status))
    delivery = make_delivery(
        store=EventStore(),
        conversation_store=EventConversationStore(),
    )

    await delivery._commit_final_delivery(
        task,
        [final_message],
        delivery_channel_id="20",
        status_channel=cast(discord.TextChannel | discord.Thread, SimpleNamespace()),
        status_marker="Coding task `task-1`",
        status_message=status_message,
    )

    assert events == ["persist", "mark-delivered", "delete-status"]


@pytest.mark.asyncio
async def test_coding_output_moderation_honors_exempt_task_tier() -> None:
    check = AsyncCall()
    service = SimpleNamespace(
        enabled=True,
        output_exempt_tier=TrustTier.REGULAR,
        check=check,
    )
    delivery = make_delivery(moderation_service=service)
    task = cast(
        CodingTask,
        SimpleNamespace(checkpoint={"trust_tier": TrustTier.STAFF.value}),
    )

    result = await delivery.moderate_text(task, "full coding report", status=False)

    assert result.text == "full coding report"
    assert result.blocked is False
    assert delivery.should_moderate_output(task) is False
    assert check.calls == []


@pytest.mark.asyncio
async def test_coding_output_moderation_uses_checkpoint_task_tier() -> None:
    check = AsyncCall(SimpleNamespace(blocked=False, error=False))
    service = SimpleNamespace(
        enabled=True,
        output_exempt_tier=TrustTier.REGULAR,
        check=check,
    )
    delivery = make_delivery(moderation_service=service)
    task = cast(
        CodingTask,
        SimpleNamespace(
            checkpoint={"trust_tier": TrustTier.MEMBER.value},
            user_id="42",
            channel_id="10",
            thread_id=None,
        ),
    )

    result = await delivery.moderate_text(task, "full coding report", status=False)

    assert result.text == "full coding report"
    assert result.blocked is False
    assert check.calls == [
        (
            (),
            {
                "text": "full coding report",
                "direction": Direction.OUTPUT,
                "user_id": "42",
                "channel_id": "10",
                "thread_id": None,
                "trust_tier": TrustTier.MEMBER.value,
            },
        )
    ]


@pytest.mark.asyncio
async def test_coding_output_moderation_marks_blocked_result() -> None:
    check = AsyncCall(SimpleNamespace(blocked=True, error=False))
    service = SimpleNamespace(
        enabled=True,
        output_exempt_tier=None,
        check=check,
        refusal_for=lambda _direction, *, error: f"refused:{error}",
    )
    delivery = make_delivery(moderation_service=service)
    task = cast(
        CodingTask,
        SimpleNamespace(
            checkpoint={"trust_tier": TrustTier.MEMBER.value},
            user_id="42",
            channel_id="10",
            thread_id=None,
        ),
    )

    result = await delivery.moderate_text(task, "blocked report", status=False)

    assert result.text == "refused:False"
    assert result.blocked is True


@pytest.mark.asyncio
async def test_durable_attachment_plan_freezes_limit_and_plain_notice(tmp_path: Path) -> None:
    output = tmp_path / "large.zip"
    output.write_bytes(b"12345")
    guild = SimpleNamespace(filesize_limit=4)
    channel = cast(
        discord.TextChannel | discord.Thread,
        SimpleNamespace(guild=guild),
    )

    class PlanStore(FakeCodingTaskStore):
        def __init__(self) -> None:
            super().__init__()
            self.plans: list[tuple[str, dict[str, Any]]] = []

        async def set_delivery_attachment_plan_if_absent(
            self,
            task_id: str,
            plan: dict[str, Any],
        ) -> dict[str, Any]:
            self.plans.append((task_id, plan))
            return plan

    class AttachmentGateway(FakeGateway):
        def prepare_attachment_delivery(self, target: object, **kwargs: Any):
            return prepare_attachment_delivery(cast(discord.abc.Messageable, target), **kwargs)

    store = PlanStore()
    delivery = make_delivery(store=store, gateway=AttachmentGateway())
    task = cast(CodingTask, SimpleNamespace(id="task-1", checkpoint={}))

    plan = await delivery.prepare_attachment_delivery(
        task,
        channel,
        output_files=[str(output)],
        allowed_roots=[str(tmp_path)],
    )

    assert plan.files == ()
    assert [item.filename for item in plan.omitted] == ["large.zip"]
    frozen = store.plans[0][1]
    notice = frozen["notice_text"]
    assert notice == attachment_delivery_notice(plan)
    assert "**" not in notice
    assert "`" not in notice

    guild.filesize_limit = 100
    recovered_task = cast(
        CodingTask,
        SimpleNamespace(id="task-1", checkpoint={"delivery": {"attachment_plan": frozen}}),
    )
    recovered = await delivery.prepare_attachment_delivery(
        recovered_task,
        channel,
        output_files=[str(output)],
        allowed_roots=[str(tmp_path)],
    )

    assert recovered.effective_limit_bytes == 4
    assert recovered.files == ()
    assert attachment_delivery_notice(recovered) == notice
    assert len(store.plans) == 1


@pytest.mark.asyncio
async def test_durable_delivery_notice_is_persisted_in_assistant_transcript() -> None:
    conversation_store = FakeConversationStore()
    delivery = make_delivery(conversation_store=conversation_store)
    task = cast(
        CodingTask,
        SimpleNamespace(
            id="3ff8bac7f9e24ed19a65d267c188d7ea",
            conversation_id=7,
        ),
    )
    notice = "Delivery notice: Discord did not attach large.zip because it exceeds the limit."
    message = cast(
        discord.Message,
        SimpleNamespace(
            id=123,
            content=f"**Coding result `3ff8bac7`**\n{notice}\n\nReport body.",
            created_at=None,
        ),
    )

    await delivery.persist_final_messages(
        task,
        [message],
        channel_id="10",
    )

    assert len(conversation_store.saved) == 1
    conversation_id, records, channel_id = conversation_store.saved[0]
    assert conversation_id == 7
    assert records[0].content == f"{notice}\n\nReport body."
    assert channel_id == "10"
