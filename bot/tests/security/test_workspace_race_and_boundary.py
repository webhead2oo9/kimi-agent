"""Composition-level regressions for staging, maintenance, and deletion gates."""

from __future__ import annotations

import asyncio
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import discord_adapter.lifecycle as bot_lifecycle
from agent.context import ConversationContext
from agent.core import ConversationRunResult
from agent.turn import (
    TurnExecutionConfig,
    TurnRequest,
    execute_turn,
)
from workspace import WorkspaceKey, WorkspaceManager, workspace_owner_key
from utils.privacy_barrier import UserPrivacyBarrier
from commands.privacy_cmd import run_privacy_deletion
from discord_adapter.io import _validated_output_files
from moderation.types import Direction, ModerationDecision
from providers.types import ProviderCapability
from storage.conversations import UserDataDeletion
from tools.workspace.common import UserLocks
from trust.tiers import TrustTier

from tests.helpers import make_turn_dependencies
from tests.security.test_workspace_isolation_adversarial import ATTACKER


async def _hold_activity(
    locks: UserLocks,
    workspace_key: WorkspaceKey,
    entered: asyncio.Event,
    release: asyncio.Event,
) -> None:
    async with locks.activity(workspace_key):
        entered.set()
        await release.wait()


@pytest.mark.asyncio
async def test_pending_outputs_are_staged_under_lease_before_moderation_and_delivery(
    tmp_path: Path,
) -> None:
    manager = WorkspaceManager(tmp_path / "workspaces")
    locks = UserLocks()
    workspace_key = workspace_owner_key("111", "g1")
    source = manager.user_files_dir(workspace_key) / "report.txt"
    source.write_text("SAFE-STAGED-BYTES", encoding="utf-8")
    context = ConversationContext(key="g1:c1:main")
    context.pending_output_files.append(str(source))
    context.pending_allowed_file_roots.append(str(source.parent.resolve()))
    run_finished = asyncio.Event()

    async def run_conversation(**_kwargs: Any) -> ConversationRunResult:
        run_finished.set()
        return ConversationRunResult(text="attachment ready")

    class RecordingModeration:
        enabled = True
        output_exempt_tier = None

        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def check(self, **kwargs: Any) -> ModerationDecision:
            self.calls.append(kwargs)
            source.write_text("MUTATED-AFTER-STAGING", encoding="utf-8")
            return ModerationDecision(blocked=False, matched_categories=[])

        def refusal_for(self, direction: Direction, *, error: bool = False) -> str:
            _ = direction
            return "blocked"

    moderation = RecordingModeration()
    turn = TurnRequest(
        content="attach it",
        context=context,
        trust_tier=TrustTier.MEMBER,
        user_id="111",
        user_name="Alice",
        guild_id="g1",
        channel_id="c1",
        thread_id=None,
        channel_name="general",
    )
    dependencies = make_turn_dependencies(
        workspace_dir=tmp_path / "workspaces",
        # Deliberately inert: execute_turn must not reach the context manager on
        # this path, so anything it touched would raise instead of passing.
        context_manager=cast(Any, object()),
        provider=cast(
            Any,
            SimpleNamespace(
                provider_key="test",
                model="test",
                capabilities={ProviderCapability.TEXT},
            ),
        ),
        workspace_manager=manager,
        workspace_locks=locks,
        run_conversation=cast(Any, run_conversation),
        moderation_service=cast(Any, moderation),
    )

    holder_entered = asyncio.Event()
    release_holder = asyncio.Event()
    holder = asyncio.create_task(
        _hold_activity(locks, workspace_key, holder_entered, release_holder)
    )
    await holder_entered.wait()
    execution = asyncio.create_task(
        execute_turn(
            turn,
            dependencies=dependencies,
            config=TurnExecutionConfig(max_iterations=2, max_tokens=128),
        )
    )
    await run_finished.wait()
    await asyncio.sleep(0)
    assert not execution.done()
    assert moderation.calls == []

    release_holder.set()
    result = await execution
    await holder

    assert "SAFE-STAGED-BYTES" in str(moderation.calls[0]["text"])
    assert "MUTATED-AFTER-STAGING" not in str(moderation.calls[0]["text"])
    staged = Path(result.output_files[0])
    assert staged != source
    assert staged.parent.name.startswith("delivery-")
    assert staged.read_text(encoding="utf-8") == "SAFE-STAGED-BYTES"
    assert (staged.parent / ".owner-user-id").read_text(encoding="utf-8") == "111"
    assert _validated_output_files(
        list(result.output_files),
        list(result.allowed_file_roots),
    ) == [staged.resolve()]
    assert source.read_text(encoding="utf-8") == "MUTATED-AFTER-STAGING"


@pytest.mark.asyncio
async def test_sweep_maintenance_waits_for_active_workspace_activity(
    tmp_path: Path,
) -> None:
    class RecordingWorkspace(WorkspaceManager):
        def __init__(self, base_dir: Path) -> None:
            super().__init__(base_dir, file_ttl=1)
            self.sweep_started = asyncio.Event()
            self.sweep_finished = asyncio.Event()

        async def sweep_expired(
            self, *, excluded_workspace_keys: frozenset[str] = frozenset()
        ) -> int:
            self.sweep_started.set()
            removed = await super().sweep_expired(excluded_workspace_keys=excluded_workspace_keys)
            self.sweep_finished.set()
            return removed

    manager = RecordingWorkspace(tmp_path / "workspaces")
    stale = manager.user_files_dir(ATTACKER) / "stale.txt"
    stale.write_text("stale", encoding="utf-8")
    old = time.time() - 60
    stale.touch()
    os.utime(stale, (old, old))
    locks = UserLocks()

    async with locks.activity(ATTACKER):
        sweeper = asyncio.create_task(
            bot_lifecycle.workspace_sweeper(
                manager,
                sweep_interval=0,
                workspace_locks=locks,
            )
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not manager.sweep_started.is_set()
        assert stale.exists()

    await asyncio.wait_for(manager.sweep_finished.wait(), timeout=2)
    assert not stale.exists()
    sweeper.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sweeper


@pytest.mark.asyncio
async def test_sweep_skips_long_lived_durable_writer_workspace(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path / "workspaces", file_ttl=1)
    active_key = WorkspaceKey("active__guild")
    other_key = WorkspaceKey("other__guild")
    active_file = manager.user_files_dir(active_key) / "stale.txt"
    other_file = manager.user_files_dir(other_key) / "stale.txt"
    active_file.write_text("active", encoding="utf-8")
    other_file.write_text("other", encoding="utf-8")
    old = time.time() - 60
    os.utime(active_file, (old, old))
    os.utime(other_file, (old, old))
    locks = UserLocks()

    async with locks.writer(active_key):
        async with locks.maintenance():
            removed = await manager.sweep_expired(excluded_workspace_keys=await locks.writer_keys())

    assert removed == 1
    assert active_file.exists()
    assert not other_file.exists()


@pytest.mark.asyncio
async def test_detached_writer_reference_keeps_ordinary_activity_blocked() -> None:
    locks = UserLocks()
    key = WorkspaceKey("user__guild")
    reference = locks.writer_reference(key)
    entered = asyncio.Event()

    async with locks.writer(key):
        await reference.__aenter__()

    async def ordinary_activity() -> None:
        async with locks.activity(key):
            entered.set()

    ordinary = asyncio.create_task(ordinary_activity())
    await asyncio.sleep(0)
    assert not entered.is_set()

    await reference.__aexit__(None, None, None)
    await asyncio.wait_for(entered.wait(), timeout=1)
    await ordinary


@pytest.mark.asyncio
async def test_privacy_workspace_maintenance_waits_for_active_activity(
    tmp_path: Path,
) -> None:
    class RecordingWorkspace(WorkspaceManager):
        def __init__(self, base_dir: Path) -> None:
            super().__init__(base_dir)
            self.delete_called = threading.Event()

        def delete_owner_dirs(self, user_id: str) -> int:
            self.delete_called.set()
            return super().delete_owner_dirs(user_id)

    class Conversations:
        def __init__(self) -> None:
            self.deleted = asyncio.Event()

        async def delete_user_data(self, user_id: str) -> UserDataDeletion:
            _ = user_id
            self.deleted.set()
            return UserDataDeletion(conversations_deleted=0, messages_scrubbed=0)

    class Preferences:
        async def set_memory_enabled(self, user_id: str, enabled: bool) -> bool:
            _ = (user_id, enabled)
            return True

        async def clear_persona(self, user_id: str) -> None:
            _ = user_id

    manager = RecordingWorkspace(tmp_path / "workspaces")
    target = manager.user_files_dir(workspace_owner_key("111", "g1")) / "private.txt"
    target.write_text("private", encoding="utf-8")
    locks = UserLocks()
    conversations = Conversations()

    async with locks.activity(ATTACKER):
        deletion = asyncio.create_task(
            run_privacy_deletion(
                scope="all",
                user_id="111",
                conversation_store=cast(Any, conversations),
                preference_store=cast(Any, Preferences()),
                memory_client=None,
                auto_retain_watermarks=None,
                workspace_manager=manager,
                workspace_locks=locks,
            )
        )
        await conversations.deleted.wait()
        await asyncio.sleep(0)
        assert not manager.delete_called.is_set()
        assert target.exists()

    outcome = await deletion
    assert outcome.ok is True
    assert manager.delete_called.is_set()
    assert not target.exists()


def test_owner_deletion_removes_owned_jobs_without_prefix_bleed(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path / "workspaces")
    mine_g1 = manager.user_files_dir(workspace_owner_key("111", "g1")) / "a.txt"
    mine_g2 = manager.user_files_dir(workspace_owner_key("111", "g2")) / "b.txt"
    prefix_other = manager.user_files_dir(workspace_owner_key("1111", "g1")) / "keep.txt"
    mine_g1.write_text("a", encoding="utf-8")
    mine_g2.write_text("b", encoding="utf-8")
    prefix_other.write_text("keep", encoding="utf-8")
    owned = manager.generated_job_dir(
        "g1:c1:shared",
        "delivery-owned",
        owner_user_id="111",
    )
    other = manager.generated_job_dir(
        "g1:c1:shared",
        "delivery-other",
        owner_user_id="999",
    )
    unowned = manager.generated_job_dir("g1:c1:shared", "unowned-job")
    (owned / "private.txt").write_text("private", encoding="utf-8")
    (other / "keep.txt").write_text("other", encoding="utf-8")
    (unowned / "keep.txt").write_text("unowned", encoding="utf-8")

    removed = manager.delete_owner_dirs("111")

    assert removed == 2
    assert not mine_g1.exists()
    assert not mine_g2.exists()
    assert prefix_other.read_text(encoding="utf-8") == "keep"
    assert not owned.exists()
    assert (other / "keep.txt").read_text(encoding="utf-8") == "other"
    assert (unowned / "keep.txt").read_text(encoding="utf-8") == "unowned"


@pytest.mark.asyncio
async def test_privacy_barrier_waits_for_guarded_workspace_write(tmp_path: Path) -> None:
    barrier = UserPrivacyBarrier()
    manager = WorkspaceManager(tmp_path / "workspaces")
    target = manager.user_files_dir(ATTACKER) / "late.txt"
    writer_entered = asyncio.Event()
    release_writer = asyncio.Event()

    async def writer() -> None:
        async with barrier.activity(WorkspaceKey("111")):
            writer_entered.set()
            await release_writer.wait()
            target.write_text("late write", encoding="utf-8")

    async def delete() -> int:
        async with barrier.deletion("111"):
            return manager.delete_owner_dirs("111")

    writer_task = asyncio.create_task(writer())
    await writer_entered.wait()
    delete_task = asyncio.create_task(delete())
    await asyncio.sleep(0)
    assert not delete_task.done()

    release_writer.set()
    removed = await delete_task
    await writer_task
    assert removed == 1
    assert not target.exists()
