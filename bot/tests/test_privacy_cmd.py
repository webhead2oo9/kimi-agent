"""Exercises commands/privacy_cmd.py's deletion workflow: confirmation,
durable in-flight deletion state, and activity draining across
transcripts, memory, and workspace files for one user.
"""

from __future__ import annotations

import asyncio
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from workspace import manager as workspace_module
from workspace import WorkspaceKey, WorkspaceManager, workspace_owner_key
from utils.privacy_barrier import PrivacyDeletionPendingError, UserPrivacyBarrier
from commands import privacy_cmd as privacy_cmd_module
from commands.privacy_cmd import (
    drain_confirmed_privacy_deletions,
    register_privacy_command,
    run_privacy_deletion,
)
from memory.mutations import user_memory_mutation
from storage.conversations import UserDataDeletion
from storage.db import Database
from storage.memory_banks import UserMemoryBankStateStore
from storage.privacy import PrivacyDeletionRequest, PrivacyDeletionRequestStore
from tools.workspace.common import UserLocks


class _UnusedWorkspace:
    """Workspace deps for deletion tests that do not assert on the workspace wipe.

    run_privacy_deletion requires these even for scope="memory", where they go
    unused, so that a scope="all" run can never silently skip the wipe. Tests
    that DO assert on the wipe build their own manager over tmp_path.
    """

    def __init__(self) -> None:
        self._base: Path | None = None

    @property
    def manager(self) -> WorkspaceManager:
        if self._base is None:
            self._base = Path(tempfile.mkdtemp(prefix="privacy-tests-"))
        return WorkspaceManager(self._base)

    @property
    def locks(self) -> UserLocks:
        return UserLocks()


_UNUSED_WORKSPACE = _UnusedWorkspace()


class _FakeConversationStore:
    def __init__(self, deletion: UserDataDeletion | None = None) -> None:
        self._deletion = deletion or UserDataDeletion(
            conversations_deleted=2,
            messages_scrubbed=3,
            coding_tasks_deleted=2,
        )
        self.delete_calls: list[str] = []

    async def delete_user_data(self, user_id: str) -> UserDataDeletion:
        self.delete_calls.append(user_id)
        return self._deletion


class _FakePreferenceStore:
    def __init__(self) -> None:
        self.disabled: list[str] = []
        self.persona_cleared: list[str] = []

    async def set_memory_enabled(self, user_id: str, enabled: bool) -> bool:
        if not enabled:
            self.disabled.append(user_id)
        return True

    async def clear_persona(self, user_id: str) -> None:
        self.persona_cleared.append(user_id)


class _FakeMemoryClient:
    def __init__(self, deleted: bool = True) -> None:
        self._deleted = deleted
        self.deleted_banks: list[str] = []

    async def delete_bank_strict(self, bank_id: str) -> bool:
        self.deleted_banks.append(bank_id)
        return self._deleted


class _FakeAutoRetain:
    def __init__(self) -> None:
        self.fast_forwarded: list[str] = []

    async def fast_forward_user(self, user_id: str) -> int:
        self.fast_forwarded.append(user_id)
        return 0


class _FakeBrowserService:
    def __init__(self, removed: int = 1) -> None:
        self._removed = removed
        self.delete_calls: list[str] = []

    async def delete_user_data(self, user_id: str) -> int:
        self.delete_calls.append(user_id)
        return self._removed


class _FakeVideoService:
    def __init__(self, removed: int = 1, *, provider_cleanup_pending: bool = False) -> None:
        self._removed = removed
        self._provider_cleanup_pending = provider_cleanup_pending
        self.delete_calls: list[str] = []

    async def delete_user_data(self, user_id: str) -> tuple[int, bool]:
        self.delete_calls.append(user_id)
        return self._removed, self._provider_cleanup_pending


def _privacy_view(*, is_available: Any) -> privacy_cmd_module._PrivacyView:
    return privacy_cmd_module._PrivacyView(
        author_id=42,
        conversation_store=cast(Any, _FakeConversationStore()),
        preference_store=cast(Any, _FakePreferenceStore()),
        memory_client=None,
        auto_retain_watermarks=None,
        workspace_manager=_UNUSED_WORKSPACE.manager,
        workspace_locks=_UNUSED_WORKSPACE.locks,
        is_available=is_available,
    )


@pytest.mark.asyncio
async def test_run_privacy_deletion_all_deletes_transcripts_and_memory() -> None:
    store = _FakeConversationStore()
    prefs = _FakePreferenceStore()
    memory = _FakeMemoryClient(deleted=True)
    retain = _FakeAutoRetain()
    browser = _FakeBrowserService()
    video = _FakeVideoService(removed=2)

    outcome = await run_privacy_deletion(
        workspace_manager=_UNUSED_WORKSPACE.manager,
        workspace_locks=_UNUSED_WORKSPACE.locks,
        scope="all",
        user_id="42",
        conversation_store=cast(Any, store),
        preference_store=cast(Any, prefs),
        memory_client=cast(Any, memory),
        auto_retain_watermarks=cast(Any, retain),
        browser_data_store=browser,
        video_data_store=video,
    )

    assert outcome.ok is True
    assert store.delete_calls == ["42"]
    # The summary is what the user is shown as proof of what was deleted, so
    # assert the whole thing: one line per data category, in order. A category
    # silently dropping out of the report is the failure this catches.
    assert outcome.lines == [
        (
            "Deleted **2** conversation(s) you started and scrubbed **3** of your "
            "message(s) from shared conversations."
        ),
        "Deleted **2** coding task record(s).",
        "Wiped your workspace files across **0** community workspace(s).",
        "Wiped **1** persistent browser profile(s).",
        "Deleted **2** stored video session(s).",
        "Long-term memory wiped and future memory disabled.",
    ]
    assert memory.deleted_banks == ["user:42"]
    assert prefs.disabled == ["42"]
    assert retain.fast_forwarded == ["42"]
    assert browser.delete_calls == ["42"]
    assert video.delete_calls == ["42"]


@pytest.mark.asyncio
async def test_provider_video_cleanup_queue_does_not_keep_privacy_barrier_pending(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        barrier = UserPrivacyBarrier()
        requests = PrivacyDeletionRequestStore(db)
        video = _FakeVideoService(removed=2, provider_cleanup_pending=True)

        outcome = await run_privacy_deletion(
            workspace_manager=_UNUSED_WORKSPACE.manager,
            workspace_locks=_UNUSED_WORKSPACE.locks,
            scope="all",
            user_id="42",
            conversation_store=cast(Any, _FakeConversationStore()),
            preference_store=cast(Any, _FakePreferenceStore()),
            memory_client=None,
            auto_retain_watermarks=None,
            privacy_barrier=barrier,
            deletion_request_store=requests,
            video_data_store=video,
        )

        assert outcome.ok is True
        assert outcome.durable_request_completed is True
        assert await requests.list_pending() == []
        async with barrier.activity(WorkspaceKey("42")):
            pass
        assert outcome.lines[-2:] == [
            (
                "Gemini video data deletion remains durably queued and will retry when "
                "provider access is available."
            ),
            "No long-term memory backend is configured, so there was none to wipe.",
        ]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_complete_deletion_drains_old_activity_then_allows_new_activity() -> None:
    barrier = UserPrivacyBarrier()
    old_activity_entered = asyncio.Event()
    release_old_activity = asyncio.Event()

    class _Conversations(_FakeConversationStore):
        messages: list[str] = []
        delete_calls: list[str] = []

        async def delete_user_data(self, user_id: str) -> UserDataDeletion:
            self.delete_calls.append(user_id)
            self.messages.clear()
            return UserDataDeletion(conversations_deleted=1, messages_scrubbed=0)

    conversations = _Conversations()

    async def old_turn() -> None:
        async with barrier.activity(WorkspaceKey("42")):
            conversations.messages.append("created by already-started turn")
            old_activity_entered.set()
            await release_old_activity.wait()

    old = asyncio.create_task(old_turn())
    await old_activity_entered.wait()
    deleting = asyncio.create_task(
        run_privacy_deletion(
            workspace_manager=_UNUSED_WORKSPACE.manager,
            workspace_locks=_UNUSED_WORKSPACE.locks,
            scope="all",
            user_id="42",
            conversation_store=cast(Any, conversations),
            preference_store=cast(Any, _FakePreferenceStore()),
            memory_client=None,
            auto_retain_watermarks=None,
            privacy_barrier=barrier,
        )
    )
    await asyncio.sleep(0)
    assert conversations.delete_calls == []

    release_old_activity.set()
    outcome = await deleting
    await old
    assert outcome.ok is True
    assert conversations.messages == []

    # Deletion is a barrier, not a permanent tombstone. A genuinely later user
    # interaction can create new state under a fresh lease.
    async with barrier.activity(WorkspaceKey("42")):
        conversations.messages.append("new interaction")
    assert conversations.messages == ["new interaction"]


@pytest.mark.asyncio
async def test_cancelled_callback_finishes_confirmed_delete_under_barrier() -> None:
    barrier = UserPrivacyBarrier()
    transcript_delete_entered = asyncio.Event()
    release_transcript_delete = asyncio.Event()
    later_activity_entered = asyncio.Event()

    class _SlowConversations(_FakeConversationStore):
        async def delete_user_data(self, user_id: str) -> UserDataDeletion:
            transcript_delete_entered.set()
            await release_transcript_delete.wait()
            return await super().delete_user_data(user_id)

    deleting = asyncio.create_task(
        run_privacy_deletion(
            workspace_manager=_UNUSED_WORKSPACE.manager,
            workspace_locks=_UNUSED_WORKSPACE.locks,
            scope="all",
            user_id="42",
            conversation_store=cast(Any, _SlowConversations()),
            preference_store=cast(Any, _FakePreferenceStore()),
            memory_client=None,
            auto_retain_watermarks=None,
            privacy_barrier=barrier,
        )
    )
    await transcript_delete_entered.wait()
    deleting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await deleting

    async def later_activity() -> None:
        async with barrier.activity(WorkspaceKey("42")):
            later_activity_entered.set()

    later = asyncio.create_task(later_activity())
    await asyncio.sleep(0)
    assert not later_activity_entered.is_set()

    release_transcript_delete.set()
    await later_activity_entered.wait()
    await later


@pytest.mark.asyncio
async def test_drain_confirmed_privacy_deletions_waits_for_active_delete() -> None:
    barrier = UserPrivacyBarrier()
    transcript_delete_entered = asyncio.Event()
    release_transcript_delete = asyncio.Event()

    class _SlowConversations(_FakeConversationStore):
        async def delete_user_data(self, user_id: str) -> UserDataDeletion:
            transcript_delete_entered.set()
            await release_transcript_delete.wait()
            return await super().delete_user_data(user_id)

    deleting = asyncio.create_task(
        run_privacy_deletion(
            workspace_manager=_UNUSED_WORKSPACE.manager,
            workspace_locks=_UNUSED_WORKSPACE.locks,
            scope="all",
            user_id="42",
            conversation_store=cast(Any, _SlowConversations()),
            preference_store=cast(Any, _FakePreferenceStore()),
            memory_client=None,
            auto_retain_watermarks=None,
            privacy_barrier=barrier,
        )
    )
    await transcript_delete_entered.wait()

    draining = asyncio.create_task(drain_confirmed_privacy_deletions())
    await asyncio.sleep(0)
    assert not draining.done()

    release_transcript_delete.set()
    outcome = await deleting
    await draining
    assert outcome.ok is True


@pytest.mark.asyncio
async def test_confirmation_workflow_is_tracked_while_authorization_is_in_flight() -> None:
    authorization_started = asyncio.Event()
    release_authorization = asyncio.Event()
    events: list[str] = []
    available = True

    class _SlowRequestStore:
        async def request(self, **kwargs: object) -> PrivacyDeletionRequest:
            events.append("authorization-start")
            authorization_started.set()
            await release_authorization.wait()
            events.append("authorization-commit")
            return PrivacyDeletionRequest(
                user_id="42",
                scope="memory",
                generation=1,
                request_token="request-token",
                memory_backend_required=False,
                requested_at=1.0,
                updated_at=1.0,
            )

        async def complete(self, request: PrivacyDeletionRequest) -> bool:
            events.append("request-complete")
            return True

    class _InteractionResponse:
        async def defer(self) -> None:
            events.append("discord-defer")

        async def edit_message(self, **kwargs: object) -> None:
            raise AssertionError("authorization unexpectedly failed")

    async def edit_original_response(**kwargs: object) -> None:
        events.append("discord-result")

    view = privacy_cmd_module._DeleteConfirmView(
        author_id=42,
        scope="memory",
        workspace_manager=_UNUSED_WORKSPACE.manager,
        workspace_locks=_UNUSED_WORKSPACE.locks,
        conversation_store=cast(Any, _FakeConversationStore()),
        preference_store=cast(Any, _FakePreferenceStore()),
        memory_client=None,
        auto_retain_watermarks=None,
        deletion_request_store=cast(Any, _SlowRequestStore()),
        privacy_barrier=UserPrivacyBarrier(),
        is_available=lambda: available,
    )
    interaction = cast(
        Any,
        SimpleNamespace(
            user=SimpleNamespace(id=42),
            response=_InteractionResponse(),
            edit_original_response=edit_original_response,
        ),
    )
    button = cast(
        Any,
        next(child for child in view.children if getattr(child, "label", None) == "Yes, delete"),
    )

    callback = asyncio.create_task(button.callback(interaction))
    await authorization_started.wait()
    available = False
    draining = asyncio.create_task(drain_confirmed_privacy_deletions())
    await asyncio.sleep(0)

    assert not draining.done()
    assert "discord-defer" not in events

    release_authorization.set()
    await callback
    await draining

    assert events.index("authorization-commit") < events.index("discord-defer")
    assert events[-2:] == ["request-complete", "discord-result"]


@pytest.mark.asyncio
@pytest.mark.parametrize("label", ["Yes, delete", "Cancel"])
async def test_confirmation_rechecks_readiness_when_click_waits_to_claim(
    monkeypatch: pytest.MonkeyPatch,
    label: str,
) -> None:
    available = True
    deletion_calls = 0

    async def deletion(**_kwargs: object) -> privacy_cmd_module.PrivacyDeletionOutcome:
        nonlocal deletion_calls
        deletion_calls += 1
        return privacy_cmd_module.PrivacyDeletionOutcome(ok=True, lines=["Deleted."])

    monkeypatch.setattr(privacy_cmd_module, "run_privacy_deletion", deletion)

    class _InteractionResponse:
        def __init__(self) -> None:
            self.sent: list[tuple[object, dict[str, object]]] = []
            self.edited: list[dict[str, object]] = []

        async def send_message(self, content: object = None, **kwargs: object) -> None:
            self.sent.append((content, kwargs))

        async def edit_message(self, **kwargs: object) -> None:
            self.edited.append(kwargs)

    view = privacy_cmd_module._DeleteConfirmView(
        author_id=42,
        scope="memory",
        workspace_manager=_UNUSED_WORKSPACE.manager,
        workspace_locks=_UNUSED_WORKSPACE.locks,
        conversation_store=cast(Any, _FakeConversationStore()),
        preference_store=cast(Any, _FakePreferenceStore()),
        memory_client=None,
        auto_retain_watermarks=None,
        is_available=lambda: available,
    )
    button = cast(
        Any,
        next(child for child in view.children if getattr(child, "label", None) == label),
    )
    response = _InteractionResponse()

    async def edit_original_response(**_kwargs: object) -> None:
        raise AssertionError("unavailable confirmation unexpectedly edited its response")

    interaction = cast(
        Any,
        SimpleNamespace(
            user=SimpleNamespace(id=42),
            response=response,
            edit_original_response=edit_original_response,
        ),
    )

    await view._decision_lock.acquire()
    clicking = asyncio.create_task(button.callback(interaction))
    await asyncio.sleep(0)
    available = False
    view._decision_lock.release()
    await clicking

    assert deletion_calls == 0
    assert view._resolved is False
    assert response.edited == []
    assert response.sent == [("This deletion prompt is no longer available.", {"ephemeral": True})]


@pytest.mark.asyncio
async def test_privacy_scope_selection_rechecks_readiness() -> None:
    class _InteractionResponse:
        def __init__(self) -> None:
            self.sent: list[tuple[object, dict[str, object]]] = []
            self.edited: list[dict[str, object]] = []

        async def send_message(self, content: object = None, **kwargs: object) -> None:
            self.sent.append((content, kwargs))

        async def edit_message(self, **kwargs: object) -> None:
            self.edited.append(kwargs)

    response = _InteractionResponse()
    interaction = cast(
        Any,
        SimpleNamespace(user=SimpleNamespace(id=42), response=response),
    )
    view = _privacy_view(is_available=lambda: False)
    button = cast(
        Any,
        next(child for child in view.children if getattr(child, "label", None) == "Delete memory"),
    )

    await button.callback(interaction)

    assert view._resolved is False
    assert response.edited == []
    assert response.sent == [("This privacy prompt is no longer available.", {"ephemeral": True})]


@pytest.mark.asyncio
async def test_privacy_scope_selection_is_single_use() -> None:
    class _InteractionResponse:
        def __init__(self) -> None:
            self.sent: list[tuple[object, dict[str, object]]] = []
            self.edited: list[dict[str, object]] = []

        async def send_message(self, content: object = None, **kwargs: object) -> None:
            self.sent.append((content, kwargs))

        async def edit_message(self, **kwargs: object) -> None:
            self.edited.append(kwargs)

    view = _privacy_view(is_available=lambda: True)
    buttons = {
        cast(Any, child).label: cast(Any, child)
        for child in view.children
        if getattr(child, "label", None) in {"Delete memory", "Delete my data"}
    }
    responses = [_InteractionResponse(), _InteractionResponse()]
    interactions = [
        cast(Any, SimpleNamespace(user=SimpleNamespace(id=42), response=response))
        for response in responses
    ]

    await buttons["Delete memory"].callback(interactions[0])
    await buttons["Delete my data"].callback(interactions[1])

    assert len(responses[0].edited) == 1
    assert responses[1].edited == []
    assert responses[1].sent == [
        ("This privacy choice has already been handled.", {"ephemeral": True})
    ]


@pytest.mark.asyncio
async def test_confirmation_atomically_claims_one_concurrent_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deletion_started = asyncio.Event()
    release_deletion = asyncio.Event()
    deletion_calls = 0

    async def slow_deletion(**_kwargs: object) -> privacy_cmd_module.PrivacyDeletionOutcome:
        nonlocal deletion_calls
        deletion_calls += 1
        deletion_started.set()
        await release_deletion.wait()
        return privacy_cmd_module.PrivacyDeletionOutcome(ok=True, lines=["Data deleted."])

    monkeypatch.setattr(privacy_cmd_module, "run_privacy_deletion", slow_deletion)

    class _InteractionResponse:
        def __init__(self) -> None:
            self.sent: list[tuple[object, dict[str, object]]] = []
            self.edited: list[dict[str, object]] = []

        async def defer(self) -> None:
            return None

        async def edit_message(self, **kwargs: object) -> None:
            self.edited.append(kwargs)

        async def send_message(self, content: object = None, **kwargs: object) -> None:
            self.sent.append((content, kwargs))

    async def edit_original_response(**_kwargs: object) -> None:
        return None

    view = privacy_cmd_module._DeleteConfirmView(
        author_id=42,
        scope="memory",
        workspace_manager=_UNUSED_WORKSPACE.manager,
        workspace_locks=_UNUSED_WORKSPACE.locks,
        conversation_store=cast(Any, _FakeConversationStore()),
        preference_store=cast(Any, _FakePreferenceStore()),
        memory_client=None,
        auto_retain_watermarks=None,
    )
    confirm_button = cast(
        Any,
        next(child for child in view.children if getattr(child, "label", None) == "Yes, delete"),
    )
    cancel_button = cast(
        Any,
        next(child for child in view.children if getattr(child, "label", None) == "Cancel"),
    )
    confirm_response = _InteractionResponse()
    cancel_response = _InteractionResponse()
    confirm_interaction = cast(
        Any,
        SimpleNamespace(
            user=SimpleNamespace(id=42),
            response=confirm_response,
            edit_original_response=edit_original_response,
        ),
    )
    cancel_interaction = cast(
        Any,
        SimpleNamespace(
            user=SimpleNamespace(id=42),
            response=cancel_response,
            edit_original_response=edit_original_response,
        ),
    )

    confirming = asyncio.create_task(confirm_button.callback(confirm_interaction))
    await deletion_started.wait()
    await cancel_button.callback(cancel_interaction)
    release_deletion.set()
    await confirming

    assert deletion_calls == 1
    assert cancel_response.edited == []
    assert cancel_response.sent == [
        ("This deletion choice has already been handled.", {"ephemeral": True})
    ]


@pytest.mark.asyncio
async def test_durable_deletion_success_removes_request_and_releases_activity(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        requests = PrivacyDeletionRequestStore(db)
        barrier = UserPrivacyBarrier()

        outcome = await run_privacy_deletion(
            workspace_manager=_UNUSED_WORKSPACE.manager,
            workspace_locks=_UNUSED_WORKSPACE.locks,
            scope="all",
            user_id="42",
            conversation_store=cast(Any, _FakeConversationStore()),
            preference_store=cast(Any, _FakePreferenceStore()),
            memory_client=None,
            auto_retain_watermarks=None,
            privacy_barrier=barrier,
            deletion_request_store=requests,
        )

        assert outcome.ok is True
        assert await requests.list_pending() == []
        async with barrier.activity(WorkspaceKey("42")):
            pass
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_newer_durable_generation_is_not_reported_as_completed() -> None:
    request = PrivacyDeletionRequest(
        user_id="42",
        scope="memory",
        generation=1,
        request_token="old-generation",
        memory_backend_required=False,
        requested_at=1.0,
        updated_at=1.0,
    )

    class _ReplacedRequestStore:
        async def request(self, **kwargs: object) -> PrivacyDeletionRequest:
            return request

        async def complete(self, completed: PrivacyDeletionRequest) -> bool:
            assert completed is request
            return False

    barrier = UserPrivacyBarrier()
    outcome = await run_privacy_deletion(
        workspace_manager=_UNUSED_WORKSPACE.manager,
        workspace_locks=_UNUSED_WORKSPACE.locks,
        scope="memory",
        user_id="42",
        conversation_store=cast(Any, _FakeConversationStore()),
        preference_store=cast(Any, _FakePreferenceStore()),
        memory_client=None,
        auto_retain_watermarks=None,
        privacy_barrier=barrier,
        deletion_request_store=cast(Any, _ReplacedRequestStore()),
    )

    assert outcome.ok is True
    assert outcome.durable_request_completed is False
    assert any("newer deletion request is still pending" in line for line in outcome.lines)
    with pytest.raises(PrivacyDeletionPendingError):
        async with barrier.activity(WorkspaceKey("42")):
            pass


@pytest.mark.asyncio
async def test_partial_durable_deletion_keeps_request_and_blocks_new_activity(
    tmp_path: Path,
) -> None:
    class _FailingConversations(_FakeConversationStore):
        async def delete_user_data(self, user_id: str) -> UserDataDeletion:
            self.delete_calls.append(user_id)
            raise RuntimeError("database unavailable")

    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        requests = PrivacyDeletionRequestStore(db)
        barrier = UserPrivacyBarrier()
        conversations = _FailingConversations()

        outcome = await run_privacy_deletion(
            workspace_manager=_UNUSED_WORKSPACE.manager,
            workspace_locks=_UNUSED_WORKSPACE.locks,
            scope="all",
            user_id="42",
            conversation_store=cast(Any, conversations),
            preference_store=cast(Any, _FakePreferenceStore()),
            memory_client=None,
            auto_retain_watermarks=None,
            privacy_barrier=barrier,
            deletion_request_store=requests,
        )

        assert outcome.ok is False
        assert conversations.delete_calls == ["42"]
        assert [request.user_id for request in await requests.list_pending()] == ["42"]
        with pytest.raises(PrivacyDeletionPendingError):
            async with barrier.activity(WorkspaceKey("42")):
                pass
        async with barrier.activity(WorkspaceKey("99")):
            pass
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_durable_delete_drains_inflight_memory_write_before_destructive_steps(
    tmp_path: Path,
) -> None:
    stale_write_entered = asyncio.Event()
    release_stale_write = asyncio.Event()
    request_persisted = asyncio.Event()
    events: list[str] = []

    async def stale_memory_write() -> None:
        async with user_memory_mutation("42"):
            stale_write_entered.set()
            await release_stale_write.wait()
            events.append("retain-finished")

    class _OrderingConversations(_FakeConversationStore):
        async def delete_user_data(self, user_id: str) -> UserDataDeletion:
            events.append("transcript-delete")
            return await super().delete_user_data(user_id)

    db = Database(tmp_path / "bot.db")
    await db.connect()
    stale_write = asyncio.create_task(stale_memory_write())
    await stale_write_entered.wait()
    try:
        requests = PrivacyDeletionRequestStore(db)

        class _NotifyingRequests:
            async def request(self, **kwargs: Any) -> PrivacyDeletionRequest:
                request = await requests.request(**kwargs)
                request_persisted.set()
                return request

            async def complete(self, request: PrivacyDeletionRequest) -> bool:
                return await requests.complete(request)

        deleting = asyncio.create_task(
            run_privacy_deletion(
                workspace_manager=_UNUSED_WORKSPACE.manager,
                workspace_locks=_UNUSED_WORKSPACE.locks,
                scope="all",
                user_id="42",
                conversation_store=cast(Any, _OrderingConversations()),
                preference_store=cast(Any, _FakePreferenceStore()),
                memory_client=None,
                auto_retain_watermarks=None,
                privacy_barrier=UserPrivacyBarrier(),
                deletion_request_store=cast(Any, _NotifyingRequests()),
            )
        )

        await request_persisted.wait()
        await asyncio.sleep(0)
        assert events == []

        release_stale_write.set()
        outcome = await deleting
        await stale_write

        assert outcome.ok is True
        assert events == ["retain-finished", "transcript-delete"]
    finally:
        release_stale_write.set()
        await stale_write
        await db.close()


@pytest.mark.asyncio
async def test_required_memory_backend_missing_on_retry_stays_pending(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        requests = PrivacyDeletionRequestStore(db)
        request = await requests.request(
            user_id="42",
            scope="memory",
            memory_backend_required=True,
        )
        barrier = UserPrivacyBarrier()

        outcome = await run_privacy_deletion(
            workspace_manager=_UNUSED_WORKSPACE.manager,
            workspace_locks=_UNUSED_WORKSPACE.locks,
            scope=request.scope,
            user_id=request.user_id,
            conversation_store=cast(Any, _FakeConversationStore()),
            preference_store=cast(Any, _FakePreferenceStore()),
            memory_client=None,
            auto_retain_watermarks=None,
            privacy_barrier=barrier,
            deletion_request_store=requests,
            pending_request=request,
        )

        assert outcome.ok is False
        assert await requests.list_pending() == [request]
        assert any("backend" in line and "unavailable" in line for line in outcome.lines)
        with pytest.raises(PrivacyDeletionPendingError):
            async with barrier.activity(WorkspaceKey("42")):
                pass
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_prior_bank_marker_requires_backend_even_when_currently_unconfigured(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        requests = PrivacyDeletionRequestStore(db)
        bank_states = UserMemoryBankStateStore(db)
        await bank_states.mark_may_exist("42")
        barrier = UserPrivacyBarrier()

        first = await run_privacy_deletion(
            workspace_manager=_UNUSED_WORKSPACE.manager,
            workspace_locks=_UNUSED_WORKSPACE.locks,
            scope="memory",
            user_id="42",
            conversation_store=cast(Any, _FakeConversationStore()),
            preference_store=cast(Any, _FakePreferenceStore()),
            memory_client=None,
            auto_retain_watermarks=None,
            privacy_barrier=barrier,
            deletion_request_store=requests,
            memory_bank_state_store=bank_states,
        )

        assert first.ok is False
        pending = await requests.list_pending()
        assert len(pending) == 1
        assert pending[0].memory_backend_required is True
        assert await bank_states.may_exist("42") is True

        memory = _FakeMemoryClient(deleted=True)
        second = await run_privacy_deletion(
            workspace_manager=_UNUSED_WORKSPACE.manager,
            workspace_locks=_UNUSED_WORKSPACE.locks,
            scope=pending[0].scope,
            user_id="42",
            conversation_store=cast(Any, _FakeConversationStore()),
            preference_store=cast(Any, _FakePreferenceStore()),
            memory_client=cast(Any, memory),
            auto_retain_watermarks=None,
            privacy_barrier=barrier,
            deletion_request_store=requests,
            pending_request=pending[0],
            memory_bank_state_store=bank_states,
        )

        assert second.ok is True
        assert memory.deleted_banks == ["user:42"]
        assert await bank_states.may_exist("42") is False
        assert await requests.list_pending() == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_full_delete_drains_affected_conversation_turns_before_transcript_wipe() -> None:
    turn_entered = asyncio.Event()
    release_turn = asyncio.Event()
    conversations = _FakeConversationStore()

    @asynccontextmanager
    async def lock_affected_turns(user_id: str):
        assert user_id == "42"
        turn_entered.set()
        await release_turn.wait()
        yield

    deleting = asyncio.create_task(
        run_privacy_deletion(
            workspace_manager=_UNUSED_WORKSPACE.manager,
            workspace_locks=_UNUSED_WORKSPACE.locks,
            scope="all",
            user_id="42",
            conversation_store=cast(Any, conversations),
            preference_store=cast(Any, _FakePreferenceStore()),
            memory_client=None,
            auto_retain_watermarks=None,
            conversation_turn_lock=lock_affected_turns,
        )
    )

    await turn_entered.wait()
    await asyncio.sleep(0)
    assert conversations.delete_calls == []

    release_turn.set()
    outcome = await deleting
    assert outcome.ok is True
    assert conversations.delete_calls == ["42"]


@pytest.mark.asyncio
async def test_newer_all_request_survives_memory_worker_completion(
    tmp_path: Path,
) -> None:
    first_memory_delete_entered = asyncio.Event()
    release_first_memory_delete = asyncio.Event()

    class _BlockingMemory:
        def __init__(self) -> None:
            self.calls = 0

        async def delete_bank_strict(self, bank_id: str) -> bool:
            self.calls += 1
            if self.calls == 1:
                first_memory_delete_entered.set()
                await release_first_memory_delete.wait()
            return True

    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        requests = PrivacyDeletionRequestStore(db)
        barrier = UserPrivacyBarrier()
        memory = _BlockingMemory()
        conversations = _FakeConversationStore()
        preferences = _FakePreferenceStore()

        memory_only = asyncio.create_task(
            run_privacy_deletion(
                workspace_manager=_UNUSED_WORKSPACE.manager,
                workspace_locks=_UNUSED_WORKSPACE.locks,
                scope="memory",
                user_id="42",
                conversation_store=cast(Any, conversations),
                preference_store=cast(Any, preferences),
                memory_client=cast(Any, memory),
                auto_retain_watermarks=None,
                privacy_barrier=barrier,
                deletion_request_store=requests,
            )
        )
        await first_memory_delete_entered.wait()
        delete_all = asyncio.create_task(
            run_privacy_deletion(
                workspace_manager=_UNUSED_WORKSPACE.manager,
                workspace_locks=_UNUSED_WORKSPACE.locks,
                scope="all",
                user_id="42",
                conversation_store=cast(Any, conversations),
                preference_store=cast(Any, preferences),
                memory_client=cast(Any, memory),
                auto_retain_watermarks=None,
                privacy_barrier=barrier,
                deletion_request_store=requests,
            )
        )

        while True:
            pending = await requests.list_pending()
            if pending and pending[0].generation == 2:
                break
            await asyncio.sleep(0)
        assert pending[0].scope == "all"

        release_first_memory_delete.set()
        first_outcome, all_outcome = await asyncio.gather(memory_only, delete_all)

        assert first_outcome.ok is True
        assert all_outcome.ok is True
        assert conversations.delete_calls == ["42"]
        assert memory.calls == 2
        assert await requests.list_pending() == []
        async with barrier.activity(WorkspaceKey("42")):
            pass
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_durable_authorization_failure_does_not_delete_any_state() -> None:
    class _FailingRequests:
        async def request(self, **kwargs: object) -> PrivacyDeletionRequest:
            raise RuntimeError("SQLite unavailable")

    conversations = _FakeConversationStore()
    preferences = _FakePreferenceStore()
    barrier = UserPrivacyBarrier()

    outcome = await run_privacy_deletion(
        workspace_manager=_UNUSED_WORKSPACE.manager,
        workspace_locks=_UNUSED_WORKSPACE.locks,
        scope="all",
        user_id="42",
        conversation_store=cast(Any, conversations),
        preference_store=cast(Any, preferences),
        memory_client=None,
        auto_retain_watermarks=None,
        privacy_barrier=barrier,
        deletion_request_store=cast(Any, _FailingRequests()),
    )

    assert outcome.ok is False
    assert "nothing was deleted" in outcome.lines[0]
    assert conversations.delete_calls == []
    assert preferences.disabled == []
    async with barrier.activity(WorkspaceKey("42")):
        pass


@pytest.mark.asyncio
async def test_run_privacy_deletion_all_wipes_workspace_dirs(tmp_path: Path) -> None:
    store = _FakeConversationStore()
    prefs = _FakePreferenceStore()
    memory = _FakeMemoryClient(deleted=True)
    retain = _FakeAutoRetain()
    workspace = WorkspaceManager(base_dir=tmp_path)
    # The user has files in two guilds; another user's workspace must survive.
    (workspace.user_files_dir(workspace_owner_key("42", "g1")) / "a.txt").write_text(
        "a", encoding="utf-8"
    )
    (workspace.user_files_dir(workspace_owner_key("42", "g2")) / "b.txt").write_text(
        "b", encoding="utf-8"
    )
    keep = workspace.user_files_dir(workspace_owner_key("99", "g1"))
    (keep / "keep.txt").write_text("keep", encoding="utf-8")

    outcome = await run_privacy_deletion(
        workspace_locks=_UNUSED_WORKSPACE.locks,
        scope="all",
        user_id="42",
        conversation_store=cast(Any, store),
        preference_store=cast(Any, prefs),
        memory_client=cast(Any, memory),
        auto_retain_watermarks=cast(Any, retain),
        workspace_manager=workspace,
    )

    assert outcome.ok is True
    assert not (tmp_path / workspace_owner_key("42", "g1")).exists()
    assert not (tmp_path / workspace_owner_key("42", "g2")).exists()
    assert (keep / "keep.txt").exists()
    assert "Wiped your workspace files across **2** community workspace(s)." in outcome.lines


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_kind", ["workspace", "generated"])
async def test_run_privacy_deletion_reports_partial_workspace_wipe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_kind: str,
) -> None:
    workspace = WorkspaceManager(base_dir=tmp_path)
    first_root = tmp_path / workspace_owner_key("42", "g1")
    second_root = tmp_path / workspace_owner_key("42", "g2")
    (workspace.user_files_dir(WorkspaceKey(first_root.name)) / "a.txt").write_text(
        "a", encoding="utf-8"
    )
    (workspace.user_files_dir(WorkspaceKey(second_root.name)) / "b.txt").write_text(
        "b", encoding="utf-8"
    )
    generated = workspace.generated_job_dir(
        "g1:c1:main",
        "owned-job",
        owner_user_id="42",
    )
    (generated / "private.txt").write_text("private", encoding="utf-8")
    other = workspace.generated_job_dir(
        "g1:c1:main",
        "other-job",
        owner_user_id="99",
    )
    (other / "keep.txt").write_text("keep", encoding="utf-8")

    failed_path = first_root if failed_kind == "workspace" else generated
    real_rmtree = workspace_module.shutil.rmtree

    def fail_one_owned_path(path: str | Path, *args: Any, **kwargs: Any) -> None:
        if Path(path) == failed_path:
            raise OSError("simulated deletion failure")
        real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(workspace_module.shutil, "rmtree", fail_one_owned_path)

    outcome = await run_privacy_deletion(
        workspace_locks=_UNUSED_WORKSPACE.locks,
        scope="all",
        user_id="42",
        conversation_store=cast(Any, _FakeConversationStore()),
        preference_store=cast(Any, _FakePreferenceStore()),
        memory_client=None,
        auto_retain_watermarks=None,
        workspace_manager=workspace,
    )

    assert outcome.ok is False
    assert failed_path.exists()
    assert other.exists()
    assert any("workspace files could not be wiped" in line for line in outcome.lines)
    if failed_kind == "workspace":
        assert not second_root.exists()
        assert not generated.exists()
    else:
        assert not first_root.exists()
        assert not second_root.exists()


@pytest.mark.asyncio
async def test_run_privacy_deletion_memory_scope_leaves_transcripts() -> None:
    store = _FakeConversationStore()
    prefs = _FakePreferenceStore()
    memory = _FakeMemoryClient(deleted=True)

    outcome = await run_privacy_deletion(
        workspace_manager=_UNUSED_WORKSPACE.manager,
        workspace_locks=_UNUSED_WORKSPACE.locks,
        scope="memory",
        user_id="7",
        conversation_store=cast(Any, store),
        preference_store=cast(Any, prefs),
        memory_client=cast(Any, memory),
        auto_retain_watermarks=None,
    )

    assert outcome.ok is True
    # The transcript store is never touched for the memory-only scope.
    assert store.delete_calls == []
    assert memory.deleted_banks == ["user:7"]
    assert prefs.disabled == ["7"]
    # The memory-only scope reports neither transcript nor workspace status.
    assert outcome.lines == ["Long-term memory wiped and future memory disabled."]


@pytest.mark.asyncio
async def test_run_privacy_deletion_reports_partial_when_bank_delete_fails() -> None:
    store = _FakeConversationStore()
    prefs = _FakePreferenceStore()
    memory = _FakeMemoryClient(deleted=False)

    outcome = await run_privacy_deletion(
        workspace_manager=_UNUSED_WORKSPACE.manager,
        workspace_locks=_UNUSED_WORKSPACE.locks,
        scope="memory",
        user_id="9",
        conversation_store=cast(Any, store),
        preference_store=cast(Any, prefs),
        memory_client=cast(Any, memory),
        auto_retain_watermarks=None,
    )

    assert outcome.ok is False
    assert prefs.disabled == ["9"]  # opt-out still applied


@pytest.mark.asyncio
async def test_run_privacy_deletion_no_backend_is_ok() -> None:
    store = _FakeConversationStore()
    prefs = _FakePreferenceStore()

    outcome = await run_privacy_deletion(
        workspace_manager=_UNUSED_WORKSPACE.manager,
        workspace_locks=_UNUSED_WORKSPACE.locks,
        scope="memory",
        user_id="3",
        conversation_store=cast(Any, store),
        preference_store=cast(Any, prefs),
        memory_client=None,
        auto_retain_watermarks=None,
    )

    assert outcome.ok is True
    assert prefs.disabled == ["3"]


class _Tree:
    def __init__(self) -> None:
        self.commands: dict[str, Any] = {}

    def add_command(self, command: Any, *, override: bool = False) -> None:
        assert override is True
        self.commands[command.name] = command.callback


class _Response:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_message(self, **kwargs: Any) -> None:
        self.sent.append(kwargs)


@pytest.mark.asyncio
async def test_privacy_command_replies_ephemerally_with_embed_and_buttons() -> None:
    tree = _Tree()
    bot = SimpleNamespace(tree=tree)
    store = _FakeConversationStore()
    prefs = _FakePreferenceStore()

    register_privacy_command(
        cast(Any, bot),
        cast(Any, store),
        cast(Any, prefs),
        workspace_manager=_UNUSED_WORKSPACE.manager,
        workspace_locks=_UNUSED_WORKSPACE.locks,
        memory_client=None,
        auto_retain_watermarks=None,
        retention_days=30,
        bot_name="Kimi",
    )

    response = _Response()
    interaction = SimpleNamespace(user=SimpleNamespace(id=123), response=response)
    await tree.commands["privacy"](interaction)

    assert len(response.sent) == 1
    sent = response.sent[0]
    assert sent["ephemeral"] is True
    assert sent["embed"].title == "Kimi: Privacy in brief"
    description = sent["embed"].description or ""
    assert "Long-term memory is enabled by default" in description
    assert "conversation history" in description
    assert "recent public channel messages" in description
    assert "Exa or Brave" in description
    assert "cannot erase Discord messages" in description
    assert "community knowledge" in description
    # Two delete buttons ride the TL;DR.
    labels = {item.label for item in sent["view"].children}
    assert {"Delete memory", "Delete my data"} <= labels


def test_privacy_confirmation_plainly_states_scope_and_limits() -> None:
    all_prompt = privacy_cmd_module._CONFIRM_PROMPTS["all"]
    assert "including messages other people added there" in all_prompt
    assert "conversations someone else started" in all_prompt
    assert "does **not** delete Discord messages" in all_prompt
    assert "community knowledge" in all_prompt

    memory_prompt = privacy_cmd_module._CONFIRM_PROMPTS["memory"]
    assert "personal long-term memory and persona" in memory_prompt
    assert "Community knowledge and skills are also untouched" in memory_prompt


@pytest.mark.asyncio
async def test_drain_failure_blocks_deletion_and_arms_the_barrier() -> None:
    """A failed work drain must delete nothing and keep activity paused."""
    from utils.privacy_barrier import UserPrivacyBarrier

    events: list[str] = []
    barrier = UserPrivacyBarrier()
    conversations = _FakeConversationStore()

    class _RequestStore:
        async def request(self, **kwargs: object) -> PrivacyDeletionRequest:
            return PrivacyDeletionRequest(
                user_id="42",
                scope="all",
                generation=1,
                request_token="request-token",
                memory_backend_required=False,
                requested_at=1.0,
                updated_at=1.0,
            )

        async def complete(self, request: PrivacyDeletionRequest) -> bool:
            events.append("request-complete")
            return True

    async def failing_drain(user_id: str) -> None:
        raise RuntimeError("drain exploded")

    class _InteractionResponse:
        async def defer(self) -> None:
            events.append("discord-defer")

        async def edit_message(self, **kwargs: object) -> None:
            raise AssertionError("authorization unexpectedly failed")

    edits: list[object] = []

    async def edit_original_response(**kwargs: object) -> None:
        edits.append(kwargs.get("embed"))

    view = privacy_cmd_module._DeleteConfirmView(
        author_id=42,
        scope="all",
        workspace_manager=_UNUSED_WORKSPACE.manager,
        workspace_locks=_UNUSED_WORKSPACE.locks,
        conversation_store=cast(Any, conversations),
        preference_store=cast(Any, _FakePreferenceStore()),
        memory_client=None,
        auto_retain_watermarks=None,
        deletion_request_store=cast(Any, _RequestStore()),
        privacy_barrier=barrier,
        cancel_user_work=failing_drain,
        is_available=lambda: True,
    )
    interaction = cast(
        Any,
        SimpleNamespace(
            user=SimpleNamespace(id=42),
            response=_InteractionResponse(),
            edit_original_response=edit_original_response,
        ),
    )
    button = cast(
        Any,
        next(child for child in view.children if getattr(child, "label", None) == "Yes, delete"),
    )
    await button.callback(interaction)

    assert conversations.delete_calls == []
    assert "request-complete" not in events
    assert len(edits) == 1
    assert barrier._states["42"].pending_deletion is True
