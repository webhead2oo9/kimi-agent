from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import discord
import pytest

from app.work_cancellation import (
    CancellationSummary,
    WorkCancellationCoordinator,
    WorkScope,
)
from app.cancellation import ActiveOperationRegistry
from tools.registry import USER_APP_SCOPE_CHANNEL_ID
from trust.tiers import TrustTier


class _ConsentGate:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    async def invalidate_user(self, user_id: str) -> None:
        self._events.append(f"consent:{user_id}")


class _PersonalRequests:
    def __init__(self, events: list[str]) -> None:
        self._events = events
        self.dm_tier: TrustTier | None = None

    def invalidate_requests(self, user_id: str) -> None:
        self._events.append(f"personal:{user_id}")

    def classify_dm(self, _message: discord.Message) -> TrustTier | None:
        return self.dm_tier


class _ActiveOperations:
    def __init__(
        self,
        events: list[str],
        results: list[tuple[int, bool]] | None = None,
    ) -> None:
        self._events = events
        self._results = list(results or [(0, True)])
        self.calls: list[dict[str, object]] = []

    async def cancel(self, **kwargs: object) -> tuple[int, bool]:
        self._events.append("foreground")
        self.calls.append(kwargs)
        return self._results.pop(0)


class _CodingTasks:
    def __init__(
        self,
        events: list[str],
        results: list[tuple[list[str], bool]] | None = None,
        *,
        running: bool = True,
    ) -> None:
        self._events = events
        self._results = list(results or [([], True)])
        self.running = running
        self.calls: list[dict[str, object]] = []
        self.store = SimpleNamespace()

    async def cancel_for_scope(self, **kwargs: object) -> tuple[list[str], bool]:
        self._events.append("coding")
        self.calls.append(kwargs)
        return self._results.pop(0)


class _Gateway:
    def __init__(self) -> None:
        self.reactions: list[tuple[object, str]] = []

    async def add_status_reaction(self, message: object, emoji: str) -> None:
        self.reactions.append((message, emoji))


class _TrustResolver:
    def resolve(
        self,
        _member: object,
        _user_id: str,
        _guild_id: str | None,
    ) -> TrustTier:
        return TrustTier.MEMBER


def _coordinator(
    *,
    events: list[str],
    active: object | None = None,
    coding: _CodingTasks | None = None,
    personal: _PersonalRequests | None = None,
    consent: _ConsentGate | None = None,
) -> WorkCancellationCoordinator:
    async def resolve(_message: discord.Message, *, allow_new_root: bool) -> None:
        assert allow_new_root is False

    async def send_response(
        _channel: discord.abc.Messageable,
        _content: str,
        *,
        reference: discord.Message | None = None,
    ) -> object:
        assert reference is not None
        return object()

    def strip_invocation(content: str, *, bot_user: object | None) -> str:
        assert bot_user is not None
        return content

    return WorkCancellationCoordinator(
        bot=cast(Any, SimpleNamespace(user=object())),
        consent_gate=cast(Any, consent),
        personal_requests=cast(Any, personal or _PersonalRequests(events)),
        active_operations=cast(Any, active or _ActiveOperations(events)),
        coding_tasks=cast(Any, coding or _CodingTasks(events)),
        trust_resolver=cast(Any, _TrustResolver()),
        discord_gateway=cast(Any, _Gateway()),
        conversation_resolver=resolve,
        response_sender=send_response,
        strip_message_invocation=strip_invocation,
        cleanup_wait_seconds=0.25,
        global_staff_ids=frozenset(),
    )


@pytest.mark.asyncio
async def test_reset_cancellation_preserves_invalidation_and_drain_order() -> None:
    events: list[str] = []
    coordinator = _coordinator(
        events=events,
        consent=_ConsentGate(events),
        personal=_PersonalRequests(events),
    )

    summary = await coordinator.cancel_for_reset(
        user_id="42",
        scope=WorkScope(channel_id=USER_APP_SCOPE_CHANNEL_ID, root_key="userchat:42"),
    )

    assert events == ["consent:42", "personal:42", "foreground", "coding"]
    assert summary == CancellationSummary()


@pytest.mark.asyncio
async def test_overlapping_scopes_aggregate_once_per_coding_task() -> None:
    events: list[str] = []
    active = _ActiveOperations(events, [(1, True), (2, False)])
    coding = _CodingTasks(
        events,
        [(["task-a", "task-b"], True), (["task-b", "task-c"], False)],
    )
    coordinator = _coordinator(events=events, active=active, coding=coding)

    summary = await coordinator.cancel(
        user_id="42",
        scopes=(
            WorkScope(channel_id="userapp", root_key="userchat:42"),
            WorkScope(channel_id="555", root_key=None),
        ),
        all_work=False,
    )

    assert events == ["foreground", "coding", "foreground", "coding"]
    assert summary == CancellationSummary(
        foreground_count=3,
        foreground_clean=False,
        coding_task_ids=("task-a", "task-b", "task-c"),
        coding_clean=False,
    )
    assert active.calls == [
        {
            "user_id": "42",
            "root_key": "userchat:42",
            "channel_id": "userapp",
            "all_operations": False,
            "wait_seconds": 0.25,
        },
        {
            "user_id": "42",
            "root_key": None,
            "channel_id": "555",
            "all_operations": False,
            "wait_seconds": 0.25,
        },
    ]


@pytest.mark.asyncio
async def test_dual_installed_stop_cancels_personal_and_guild_scopes() -> None:
    events: list[str] = []
    active = _ActiveOperations(events, [(0, True), (0, True)])
    coordinator = _coordinator(
        events=events,
        active=active,
        coding=_CodingTasks(events, [([], True), ([], True)]),
    )
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=42),
        channel_id=555,
        guild_id=777,
        is_user_integration=lambda: True,
        is_guild_integration=lambda: True,
    )

    result = await coordinator.handle_stop_interaction(
        cast(discord.Interaction, interaction),
        False,
        None,
    )

    assert result == "I couldn't find active work to stop here."
    assert [(call["channel_id"], call["root_key"]) for call in active.calls] == [
        (USER_APP_SCOPE_CHANNEL_ID, "userchat:42"),
        ("555", None),
    ]


@pytest.mark.asyncio
async def test_stop_message_targets_the_shared_personal_scope_for_a_dm() -> None:
    events: list[str] = []
    active = _ActiveOperations(events)
    personal = _PersonalRequests(events)
    personal.dm_tier = TrustTier.STAFF
    coordinator = _coordinator(
        events=events,
        active=active,
        personal=personal,
        coding=_CodingTasks(events, running=False),
    )
    message = SimpleNamespace(
        author=SimpleNamespace(id=42),
        channel=SimpleNamespace(id=555),
        content="stop",
    )

    await coordinator.handle_stop_message(cast(discord.Message, message))

    assert [(call["channel_id"], call["root_key"]) for call in active.calls] == [
        (USER_APP_SCOPE_CHANNEL_ID, "userchat:42")
    ]


def test_cancellation_summary_reports_all_work_and_cleanup_state() -> None:
    summary = CancellationSummary(
        foreground_count=2,
        foreground_clean=True,
        coding_task_ids=("123456789", "abcdefghi"),
        coding_clean=False,
    )

    assert summary.total == 4
    assert summary.clean is False
    assert summary.describe() == (
        "Stopped 2 active response(s) and coding task(s) `12345678`, `abcdefgh`. "
        "Cleanup is still finishing in the background. Partial file changes were kept."
    )


@pytest.mark.asyncio
async def test_root_scope_reaches_a_provisional_turn_before_root_binding() -> None:
    events: list[str] = []
    active = ActiveOperationRegistry()
    coordinator = _coordinator(
        events=events,
        active=active,
        coding=_CodingTasks(events, running=False),
    )
    registered = asyncio.Event()

    async def turn() -> None:
        try:
            with active.register_provisional(user_id="42", channel_id="555"):
                registered.set()
                await asyncio.Event().wait()
        except asyncio.CancelledError:
            return

    task = asyncio.create_task(turn())
    await registered.wait()

    summary = await coordinator.cancel(
        user_id="42",
        scopes=(WorkScope(channel_id="555", root_key="resolved-root"),),
        all_work=False,
    )
    await task

    assert summary.foreground_count == 1
    assert summary.foreground_clean is True
    assert task.done()


@pytest.mark.asyncio
async def test_root_scope_does_not_cancel_a_different_bound_provisional_turn() -> None:
    events: list[str] = []
    active = ActiveOperationRegistry()
    coordinator = _coordinator(
        events=events,
        active=active,
        coding=_CodingTasks(events, running=False),
    )
    registered = {"root-1": asyncio.Event(), "root-2": asyncio.Event()}

    async def turn(root_key: str) -> None:
        try:
            with active.register_provisional(user_id="42", channel_id="555"):
                active.bind_current_provisional(root_key)
                registered[root_key].set()
                await asyncio.Event().wait()
        except asyncio.CancelledError:
            return

    first = asyncio.create_task(turn("root-1"))
    second = asyncio.create_task(turn("root-2"))
    await asyncio.gather(*(event.wait() for event in registered.values()))

    summary = await coordinator.cancel(
        user_id="42",
        scopes=(WorkScope(channel_id="555", root_key="root-1"),),
        all_work=False,
    )
    await first

    try:
        assert summary.foreground_count == 1
        assert first.done()
        assert second.done() is False
    finally:
        second.cancel()
        await asyncio.gather(second, return_exceptions=True)
