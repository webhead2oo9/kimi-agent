from __future__ import annotations

import asyncio
from functools import partial
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from PIL import Image

from agent.attachments import AttachmentStore, collect_turn_attachments, collect_turn_images
from agent.context import ContextManager
from agent.core import ConversationRunResult
from app.admission import TurnAdmissionController
from app.cancellation import ActiveOperationRegistry
from app.chat_attachments import stage_chat_attachments
from app.dashboard_files import DashboardFiles
from app.dashboard_turn import DashboardTurns
from app.foreground_turn import ForegroundTurnRunner
from app.root_locks import RootLockPool
from app.turn_entry import TurnEntryHooks
from providers.types import ProviderCapability
from storage.dashboard import DashboardStore
from storage.db import Database
from tests.helpers import StubProvider, make_settings, make_turn_dependencies
from tests.test_turn_handler import BlockingModerationService, RecordingModerationService
from tools.registry import TurnOutbox
from tools.workspace.common import UserLocks
from tools.workspace.config import WorkspaceToolConfig
from tools.workspace.files import FileToolDeps
from trust.tiers import TrustTier
from utils.privacy_barrier import UserPrivacyBarrier
from workspace import WorkspaceManager, workspace_owner_key


@pytest_asyncio.fixture(params=[False, True], ids=["absolute-workspace", "relative-workspace"])
async def surface(tmp_path, monkeypatch, request):
    monkeypatch.chdir(tmp_path)
    db = Database(tmp_path / "turn.db")
    await db.connect()
    store = DashboardStore(db)
    settings = make_settings()
    locks, privacy, operations = UserLocks(), UserPrivacyBarrier(), ActiveOperationRegistry()
    workspace = WorkspaceManager(Path("work") if request.param else tmp_path / "work")
    files = DashboardFiles(store=store, workspace=workspace, locks=locks, settings=settings)
    member = SimpleNamespace(
        id=1, display_name="Charlie", guild=SimpleNamespace(id=2, name="Test guild")
    )
    access = SimpleNamespace(
        bot=SimpleNamespace(user=None),
        resolve=AsyncMock(
            return_value=SimpleNamespace(
                member=member,
                channel=SimpleNamespace(
                    id=3,
                    history=MagicMock(
                        side_effect=AssertionError("Activity must not read ambient Discord images")
                    ),
                ),
                tier=TrustTier.MEMBER,
            )
        ),
        consent_required=AsyncMock(return_value=False),
    )
    executed = []
    moderation = RecordingModerationService()
    release = asyncio.Event()
    started = asyncio.Event()
    release.set()

    async def run(*, request):
        executed.append(request)
        started.set()
        await release.wait()
        output = workspace.user_files_dir(workspace_owner_key("1", "2")) / "answer.txt"
        output.write_text("result")
        request.context.pending_outbox = TurnOutbox(
            output_files=(str(output),), allowed_file_roots=(str(output.parent),)
        )
        return ConversationRunResult(
            text="Here is your file.", outbox=request.context.pending_outbox
        )

    async def build(source, **kwargs):
        assert source.guild_id == "2" and not source.personal_chat
        assert source.workspace_key == workspace_owner_key("1", "2")
        assert "move_to_thread" in kwargs["extra_blocked_tools"]
        assert "discord_text_search" not in kwargs["extra_blocked_tools"]
        assert kwargs["command_template"] == "dashboard"
        return make_turn_dependencies(
            context_manager=ContextManager(store.conversations),
            workspace_dir=tmp_path / "work",
            workspace_manager=workspace,
            workspace_locks=locks,
            provider=StubProvider(
                capabilities={ProviderCapability.TEXT, ProviderCapability.IMAGE_INPUT}
            ),
            attachment_store=AttachmentStore(tmp_path / "attachments", max_bytes=1024 * 1024),
            collect_turn_attachments=collect_turn_attachments,
            collect_turn_images=collect_turn_images,
            moderation_service=moderation,
            run_conversation=run,
            stage_chat_attachments=partial(
                stage_chat_attachments, deps=FileToolDeps(workspace, WorkspaceToolConfig(), locks)
            ),
            persist_prepared_user_message=kwargs["persist_prepared_user_message"],
            activity_reporter=kwargs["activity_reporter"],
        )

    factory = SimpleNamespace(build=build)
    runner = ForegroundTurnRunner(
        settings=settings,
        conversation_store=store.conversations,
        dependency_factory=factory,
        active_operations=operations,
        privacy_barrier=privacy,
        workspace_locks=locks,
    )
    turns = DashboardTurns(
        store=store,
        files=files,
        access=access,
        runner=runner,
        gateway=MagicMock(),
        coding=SimpleNamespace(running=False),
        preview=AsyncMock(),
        operations=operations,
        privacy=privacy,
        admission=TurnAdmissionController(max_active=3, max_active_per_user=1),
        roots=RootLockPool(),
        hooks=TurnEntryHooks(),
        settings=settings,
    )
    chat = await store.create(
        user_id="1", guild_id="2", channel_id="3", parent_channel_id="3", channel_name="general"
    )
    yield SimpleNamespace(
        turns=turns,
        store=store,
        files=files,
        chat=chat,
        executed=executed,
        moderation=moderation,
        access=access,
        release=release,
        started=started,
        workspace=workspace,
        db=db,
    )
    await turns.close()
    await db.close()


async def settled(surface):
    async with asyncio.timeout(5):
        while surface.turns._tasks:
            await asyncio.gather(*list(surface.turns._tasks), return_exceptions=True)


@pytest.mark.asyncio
async def test_shared_turn_pipeline_moderates_images_stages_files_and_persists_private_result(
    surface,
):
    buffer = BytesIO()
    Image.new("RGB", (8, 8), "blue").save(buffer, format="PNG")
    picture = await surface.files.save(
        surface.chat, "picture.png", buffer.getvalue(), kind="upload"
    )
    notes = await surface.files.save(surface.chat, "notes.txt", b"the notes", kind="upload")
    turn = await surface.turns.submit(
        surface.chat, request_id="req", text="Read these", file_ids=[picture.id, notes.id]
    )
    await settled(surface)
    events = await surface.store.events(surface.chat.id)
    assert events[-1].payload["status"] == "completed"
    assert events[-1].payload["text"] == "Here is your file."
    assert events[-1].payload["files"][0]["filename"] == "answer.txt"
    record = await surface.store.file(
        events[-1].payload["files"][0]["id"], user_id="1", guild_id="2"
    )
    assert await surface.files.payload(record) == b"result"
    assert len(surface.executed) == 1
    request = surface.executed[0]
    assert request.command_template == "dashboard"
    assert request.trigger_discord_message_id == ""
    assert request.context.key == surface.chat.key
    assert len(request.attachments) == 2
    assert all(attachment.workspace_path for attachment in request.attachments)
    assert any(call.get("images") or call.get("image_urls") for call in surface.moderation.calls)
    async with surface.db.conn.execute(
        "SELECT discord_message_id,source_id FROM messages ORDER BY id"
    ) as cursor:
        rows = await cursor.fetchall()
    assert [tuple(row) for row in rows] == [
        (None, f"dashboard:{turn}:user"),
        (None, f"dashboard:{turn}:assistant"),
    ]
    assert (
        await surface.turns.submit(
            surface.chat, request_id="req", text="Read these", file_ids=[picture.id]
        )
        == turn
    )
    await settled(surface)
    assert len(surface.executed) == 1


@pytest.mark.asyncio
async def test_stop_drains_running_turn_and_releases_shared_admission(surface):
    surface.release.clear()
    await surface.turns.submit(surface.chat, request_id="req", text="hello", file_ids=[])
    async with asyncio.timeout(3):
        await surface.started.wait()
    assert await surface.turns.stop(surface.chat)
    await settled(surface)
    assert (await surface.store.events(surface.chat.id))[-1].payload["status"] == "cancelled"
    surface.release.set()
    await surface.turns.submit(surface.chat, request_id="next", text="continue", file_ids=[])
    await settled(surface)
    assert (await surface.store.events(surface.chat.id))[-1].payload["status"] == "completed"


@pytest.mark.asyncio
async def test_blocked_upload_never_reaches_model_workspace(surface):
    surface.moderation.check = BlockingModerationService().check
    upload = await surface.files.save(surface.chat, "notes.txt", b"reject me", kind="upload")
    await surface.turns.submit(surface.chat, request_id="req", text="hello", file_ids=[upload.id])
    await settled(surface)
    assert surface.executed == []
    assert not list(
        surface.workspace.user_files_dir(workspace_owner_key("1", "2")).rglob("notes.txt")
    )
    assert (await surface.store.events(surface.chat.id))[-1].payload["text"] == "moderated refusal"


@pytest.mark.asyncio
async def test_continued_dashboard_uses_saved_history_without_discord_image_lookback(surface):
    for index in range(2):
        await surface.turns.submit(
            surface.chat, request_id=str(index), text="continue", file_ids=[]
        )
        await settled(surface)
    assert len(surface.executed) == 2
    assert surface.executed[-1].context.messages
    surface.access.resolve.return_value.channel.history.assert_not_called()


@pytest.mark.asyncio
async def test_delete_drains_coding_finalizer_before_locking_chat_and_fences_new_turns(surface):
    from aiohttp import web

    started, release = asyncio.Event(), asyncio.Event()

    async def finalize():
        started.set()
        await release.wait()
        async with surface.turns.roots.hold(surface.chat.key):
            await surface.store.event(
                surface.chat.id, "coding_task", {"id": "code", "status": "cancelled"}
            )

    async def cancel(_ids):
        await asyncio.create_task(finalize())
        return 1, True

    surface.turns.coding = SimpleNamespace(running=True, cancel_for_conversations=cancel)
    deletion = asyncio.create_task(surface.turns.delete(surface.chat))
    await started.wait()
    with pytest.raises(web.HTTPGone):
        await surface.turns.submit(
            surface.chat, request_id="race", text="start more work", file_ids=[]
        )
    release.set()
    async with asyncio.timeout(2):
        await deletion
    assert await surface.store.get(surface.chat.id, user_id="1", guild_id="2") is None
    assert not surface.turns._deleting
