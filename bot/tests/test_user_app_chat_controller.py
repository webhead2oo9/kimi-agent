from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import discord
import pytest

from agent.turn import TurnResult
from app.admission import TurnAdmissionController
from app.cancellation import ActiveOperationRegistry
from app.root_locks import RootLockPool
from app.turn_entry import TurnEntryHooks
from app.user_app_chat import (
    UserAppChatConfig,
    UserAppChatController,
    UserAppChatRequest,
)
from storage.conversations import OWNER_ONLY, ConversationStore
from storage.db import Database
from trust.tiers import TrustTier
from utils.privacy_barrier import UserPrivacyBarrier


class _Interaction:
    def __init__(self, interaction_id: int = 1) -> None:
        self.id = interaction_id
        self.user = SimpleNamespace(id=42, display_name="Alice")
        self.channel = SimpleNamespace(guild=None)
        self.channel_id = 99
        self.guild = None
        self.guild_id = None
        self.created_at = datetime.now(UTC)
        self.edits: list[str] = []

    async def edit_original_response(self, **kwargs: object) -> None:
        self.edits.append(str(kwargs.get("content", "")))


class _Access:
    def __init__(self) -> None:
        self.tier: TrustTier | None = TrustTier.STAFF

    def resolve(self, _user_id: str) -> TrustTier | None:
        return self.tier


class _Blocked:
    def __init__(self) -> None:
        self.value = False

    async def __call__(self, _user_id: str) -> bool:
        return self.value


class _Runner:
    def __init__(
        self,
        run: Callable[[object, object], Awaitable[TurnResult | None]],
    ) -> None:
        self._run = run
        self.calls = 0

    async def run(self, invocation: object, *, adapter: object) -> TurnResult | None:
        self.calls += 1
        return await self._run(invocation, adapter)


class _Shutdown:
    def __init__(self) -> None:
        self.closed = False


class _Consent:
    async def prompt_if_needed(self, *_args: object, **_kwargs: object) -> bool:
        return False


class _UnusedConversationStore:
    async def delete_owner_conversation(self, _key: str, _user_id: str) -> bool:
        raise AssertionError("conversation persistence was not expected")


class _ActivityBarrier:
    def __init__(self, *, pause_before_entry: bool = False) -> None:
        self.pause_before_entry = pause_before_entry
        self.entering = asyncio.Event()
        self.release = asyncio.Event()
        self.active = False

    @asynccontextmanager
    async def activity(self, _user_id: str) -> AsyncIterator[None]:
        self.entering.set()
        if self.pause_before_entry:
            await self.release.wait()
        self.active = True
        try:
            yield
        finally:
            self.active = False


class _Canceller:
    def __init__(self) -> None:
        self.callback: Callable[..., Awaitable[bool]] | None = None

    async def __call__(self, **kwargs: str) -> bool:
        if self.callback is None:
            return True
        return await self.callback(**kwargs)


def _controller(
    *,
    runner: _Runner,
    active_operations: ActiveOperationRegistry | None = None,
    barrier: object | None = None,
    admission: TurnAdmissionController | None = None,
    root_locks: RootLockPool | None = None,
    access: _Access | None = None,
    blocked: _Blocked | None = None,
    shutdown: _Shutdown | None = None,
    canceller: _Canceller | None = None,
    conversation_store: object | None = None,
    timeout_seconds: float = 1.0,
) -> UserAppChatController:
    return UserAppChatController(
        config=UserAppChatConfig(timeout_seconds=timeout_seconds, dm_enabled=True),
        bot=cast(Any, SimpleNamespace(user=object())),
        access=cast(Any, access or _Access()),
        user_blocked=blocked or _Blocked(),
        consent=cast(Any, _Consent()),
        conversation_store=cast(
            Any,
            conversation_store or _UnusedConversationStore(),
        ),
        active_operations=active_operations or ActiveOperationRegistry(),
        privacy_barrier=cast(Any, barrier or UserPrivacyBarrier()),
        turn_admission=admission or TurnAdmissionController(max_active=2, max_active_per_user=2),
        root_locks=root_locks or RootLockPool(),
        turn_runner=cast(Any, runner),
        shutdown=shutdown or _Shutdown(),
        cancel_personal_work=canceller or _Canceller(),
        turn_entry_hooks=TurnEntryHooks(),
    )


async def _run(
    controller: UserAppChatController,
    interaction: _Interaction,
    *,
    request: UserAppChatRequest | None = None,
) -> TurnResult | None:
    return await controller.run(
        cast(discord.Interaction, interaction),
        message="hello",
        attachment=None,
        public=False,
        request=request or controller.capture_request("42"),
    )


@pytest.mark.asyncio
async def test_request_is_registered_before_first_await_and_privacy_covers_runner() -> None:
    active_operations = ActiveOperationRegistry()
    barrier = _ActivityBarrier(pause_before_entry=True)

    async def run_turn(_invocation: object, _adapter: object) -> TurnResult:
        assert barrier.active is True
        return TurnResult(response_text="done")

    controller = _controller(
        runner=_Runner(run_turn),
        active_operations=active_operations,
        barrier=barrier,
    )
    task = asyncio.create_task(_run(controller, _Interaction()))

    await barrier.entering.wait()
    assert active_operations.has_active_for_user("42") is True
    barrier.release.set()
    result = await task

    assert result == TurnResult(response_text="done")
    assert barrier.active is False
    assert active_operations.has_active_for_user("42") is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("gate", "expected"),
    [
        ("access", "You no longer have access to this app's personal chat."),
        ("blocked", "You can't use personal chat right now."),
    ],
)
async def test_access_and_block_are_rechecked_under_the_root_lock(
    gate: str,
    expected: str,
) -> None:
    access = _Access()
    blocked = _Blocked()
    roots = RootLockPool()

    async def should_not_run(_invocation: object, _adapter: object) -> None:
        raise AssertionError("the turn ran after its access gate changed")

    runner = _Runner(should_not_run)
    controller = _controller(
        runner=runner,
        root_locks=roots,
        access=access,
        blocked=blocked,
    )
    interaction = _Interaction()
    task: asyncio.Task[TurnResult | None] | None = None

    async with roots.hold("userchat:42"):
        task = asyncio.create_task(_run(controller, interaction))
        deadline = asyncio.get_running_loop().time() + 0.5
        while roots.snapshot().refcounts.get("userchat:42") != 2:
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("personal chat did not queue for its root lock")
            await asyncio.sleep(0)
        if gate == "access":
            access.tier = None
        else:
            blocked.value = True

    assert task is not None
    assert await task is None
    assert runner.calls == 0
    assert interaction.edits == [expected]


@pytest.mark.asyncio
async def test_timeout_envelope_includes_the_root_lock_wait() -> None:
    roots = RootLockPool()

    async def should_not_run(_invocation: object, _adapter: object) -> None:
        raise AssertionError("the turn ran after its deadline")

    controller = _controller(
        runner=_Runner(should_not_run),
        root_locks=roots,
        timeout_seconds=0.01,
    )
    interaction = _Interaction()

    # Contention must come from a different task, as in production: the root
    # pool is reentrant for the owning task, so holding inline would no-op.
    held = asyncio.Event()
    release = asyncio.Event()

    async def holder() -> None:
        async with roots.hold("userchat:42"):
            held.set()
            await release.wait()

    holder_task = asyncio.create_task(holder())
    await held.wait()
    try:
        assert await _run(controller, interaction) is None
    finally:
        release.set()
        await holder_task

    assert interaction.edits == ["That personal chat turn timed out. Run `/chat` again to retry."]


@pytest.mark.asyncio
async def test_shutting_down_admission_is_silent() -> None:
    admission = TurnAdmissionController(max_active=1, max_active_per_user=1)
    await admission.close()

    async def should_not_run(_invocation: object, _adapter: object) -> None:
        raise AssertionError("the turn ran during shutdown admission")

    runner = _Runner(should_not_run)
    controller = _controller(runner=runner, admission=admission)
    interaction = _Interaction()

    assert await _run(controller, interaction) is None
    assert runner.calls == 0
    assert interaction.edits == []


@pytest.mark.asyncio
async def test_shutdown_cancellation_propagates_without_interaction_edit() -> None:
    entered = asyncio.Event()
    shutdown = _Shutdown()

    async def wait_forever(_invocation: object, _adapter: object) -> TurnResult:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    controller = _controller(runner=_Runner(wait_forever), shutdown=shutdown)
    interaction = _Interaction()
    task = asyncio.create_task(_run(controller, interaction))
    await entered.wait()

    shutdown.closed = True
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert interaction.edits == []


@pytest.mark.asyncio
async def test_reset_drains_running_turn_and_invalidates_retained_request(tmp_path: Path) -> None:
    database = Database(tmp_path / "chat-controller.db")
    await database.connect()
    store = ConversationStore(database)
    await store.get_or_create(
        "userchat:42",
        "Personal chat",
        owner_user_id="42",
        access_scope=OWNER_ONLY,
    )
    active_operations = ActiveOperationRegistry()
    entered = asyncio.Event()

    async def wait_forever(_invocation: object, _adapter: object) -> TurnResult:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    runner = _Runner(wait_forever)
    canceller = _Canceller()
    controller = _controller(
        runner=runner,
        active_operations=active_operations,
        canceller=canceller,
        conversation_store=store,
    )

    async def cancel(**kwargs: str) -> bool:
        controller.invalidate_requests(kwargs["user_id"])
        _count, clean = await active_operations.cancel(
            user_id=kwargs["user_id"],
            root_key=kwargs["root_key"],
            channel_id=kwargs["channel_id"],
            all_operations=False,
            wait_seconds=0.5,
        )
        return clean

    canceller.callback = cancel
    retained = controller.capture_request("42")
    running_interaction = _Interaction(1)
    task = asyncio.create_task(_run(controller, running_interaction, request=retained))
    await entered.wait()

    try:
        reset_result = await controller.reset(cast(discord.Interaction, _Interaction(2)))
        await task
        stale_interaction = _Interaction(3)
        assert await _run(controller, stale_interaction, request=retained) is None

        assert reset_result == (
            "Your personal chat thread was cleared. Memory and workspace files were kept."
        )
        assert running_interaction.edits == ["Stopped."]
        assert (
            "expired because your personal thread was reset or deleted"
            in (stale_interaction.edits[-1])
        )
        assert runner.calls == 1
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await database.close()
