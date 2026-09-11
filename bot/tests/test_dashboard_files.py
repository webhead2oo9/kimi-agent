from __future__ import annotations

import os
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp import web

from app.dashboard_files import DashboardFiles, read_regular
from storage.dashboard import DashboardStore
from storage.dashboard_branches import DashboardBranches
from storage.db import Database
from tests.helpers import make_settings
from tests.test_dashboard_store import saved_turn
from tools.workspace.common import UserLocks
from workspace import WorkspaceManager, workspace_owner_key


@pytest_asyncio.fixture(params=[False, True], ids=["absolute-workspace", "relative-workspace"])
async def files(tmp_path, monkeypatch, request):
    monkeypatch.chdir(tmp_path)
    db = Database(tmp_path / "files.db")
    await db.connect()
    service = DashboardFiles(
        store=DashboardStore(db),
        workspace=WorkspaceManager(Path("work") if request.param else tmp_path / "work"),
        locks=UserLocks(),
        settings=make_settings(),
    )
    yield service
    await db.close()


async def chat(files, user="1"):
    return await files.store.create(
        user_id=user, guild_id="2", channel_id="3", parent_channel_id="3", channel_name="general"
    )


@pytest.mark.asyncio
async def test_attachments_are_private_bounded_and_inert(files):
    own, other = await chat(files), await chat(files, "9")
    record = await files.save(own, "page.html", b"<script>alert(1)</script>", kind="upload")
    assert (await files.preview(record))["kind"] == "text"
    assert "path" not in record.public()
    with pytest.raises(web.HTTPNotFound):
        await files.attachments(other, [record.id])
    with pytest.raises(web.HTTPBadRequest):
        await files.attachments(own, [record.id, record.id])
    attachments, _ = await files.attachments(own, [record.id])
    assert await attachments[0].read() == b"<script>alert(1)</script>"
    with pytest.raises(web.HTTPRequestEntityTooLarge):
        await files.save(own, "large", b"x" * (files.upload_limit + 1), kind="upload")


@pytest.mark.asyncio
async def test_workspace_snapshots_are_immutable_and_chat_deletion_keeps_shared_files(files):
    own = await chat(files)
    path = files.workspace.user_files_dir(workspace_owner_key("1", "2")) / "notes.txt"
    path.write_text("first")
    saved = await files.snapshot_workspace(own, "notes.txt")
    path.write_text("changed")
    assert await files.payload(saved) == b"first"
    assert (await files.capture_outputs(own, ("/etc/passwd",)))[0].payload is None
    with pytest.raises(ValueError):
        await files.snapshot_workspace(own, "../outside")
    await files.delete_chat(own)
    with pytest.raises(web.HTTPGone):
        await files.payload(saved)
    assert path.read_text() == "changed"


@pytest.mark.asyncio
async def test_output_snapshot_keeps_content_and_rejects_linked_or_foreign_sources(files):
    own, other = await chat(files), await chat(files, "9")
    directory = files.workspace.generated_job_dir(own.key, "delivery-test")
    source = directory / "answer.txt"
    source.write_text("delivered content")
    outputs = await files.capture_outputs(own, (str(source),))
    turn, _ = await files.store.accept(own, request_id="output", text="file", files=[])
    async with files.output_copies(own, outputs) as (public, records):
        await files.store.finish_turn(own.id, turn, "completed", {"files": public}, files=records)
    assert public[0]["filename"] == "answer.txt"
    record = await files.store.file(public[0]["id"], user_id=own.user_id, guild_id=own.guild_id)
    assert await files.payload(record) == b"delivered content"
    assert (await files.preview(record))["text"] == "delivered content"
    assert (await files.capture_outputs(other, (str(source),)))[0].payload is None
    linked = directory / "link.txt"
    linked.symlink_to(source.absolute())
    assert (await files.capture_outputs(own, (str(linked),)))[0].payload is None
    os.link(source, directory / "hardlink.txt")
    assert (await files.capture_outputs(own, (str(source),)))[0].payload is None


def test_read_regular_rejects_symlinks_hardlinks_directories_and_oversize(tmp_path):
    original = tmp_path / "a"
    original.write_bytes(b"secret")
    assert read_regular(original, 6) == b"secret"
    with pytest.raises(ValueError):
        read_regular(original, 5)
    link = tmp_path / "link"
    link.symlink_to(original)
    with pytest.raises(OSError):
        read_regular(link, 6)
    with pytest.raises((OSError, ValueError)):
        read_regular(tmp_path, 100)
    os.link(original, tmp_path / "hardlink")
    with pytest.raises(ValueError):
        read_regular(original, 6)


@pytest.mark.asyncio
async def test_failed_metadata_write_cleans_private_snapshot(files, monkeypatch):
    from unittest.mock import AsyncMock

    own = await chat(files)
    monkeypatch.setattr(
        files.store, "add_file", AsyncMock(side_effect=RuntimeError("database failed"))
    )
    with pytest.raises(RuntimeError, match="database failed"):
        await files.save(own, "private.txt", b"private", kind="upload")
    root = files.workspace.allowed_output_roots(context_key=files.context(own.id, "upload"))[0]
    assert list(root.iterdir()) == []


@pytest.mark.asyncio
async def test_cancelled_upload_finishes_snapshot_and_metadata_together(files, monkeypatch):
    import asyncio

    own = await chat(files)
    original = files.store.add_file
    writing, release = asyncio.Event(), asyncio.Event()

    async def slow(*args, **kwargs):
        writing.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(files.store, "add_file", slow)
    task = asyncio.create_task(files.save(own, "private.txt", b"private", kind="upload"))
    await writing.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    records = await files.store.files(own)
    assert len(records) == 1
    assert await files.payload(records[0]) == b"private"


@pytest.mark.asyncio
async def test_snapshot_finishes_when_maintenance_queues_behind_existing_workspace_lease(files):
    import asyncio

    own = await chat(files)
    key = workspace_owner_key(own.user_id, own.guild_id)
    source = files.workspace.user_files_dir(key) / "result.txt"
    source.write_text("safe output")
    entered = asyncio.Event()

    async def sweep():
        async with files.locks.maintenance():
            entered.set()

    async with asyncio.timeout(2):
        async with files.locks.activity(key):
            maintenance = asyncio.create_task(sweep())
            # Queue the maintenance writer before taking the nested snapshot mutex.
            await asyncio.sleep(0)
            assert files.locks._maintenance_waiters == 1
            saved = await files.capture_outputs(own, (str(source),), workspace_guard_held=True)
            assert saved[0].filename == "result.txt"
            assert saved[0].payload == b"safe output"
            assert not entered.is_set()
        await maintenance
    assert entered.is_set()


@pytest.mark.asyncio
async def test_branch_and_returned_attachments_survive_source_deletion(files):
    parent = await chat(files)
    original = await files.save(parent, "notes.txt", b"Original notes", kind="output")
    selected = await saved_turn(files.store, parent, files=[original.public()])
    async with files.branch_copies(parent) as copy_file:
        branches = DashboardBranches(files.store, copy_file=copy_file)
        branch = await branches.fork(parent, event_id=selected.id, request_id="fork")
        retry = await branches.fork(parent, event_id=selected.id, request_id="fork")
    assert branch == retry
    copies = await files.store.files(branch)
    assert len(copies) == 1  # A repeated card and HTTP retry reuse the same copy.
    assert copies[0].id != original.id
    await files.delete_chat(parent)
    assert await files.payload(copies[0]) == b"Original notes"
    answer = await saved_turn(files.store, branch, answer="**Result**", files=[copies[0].public()])
    async with files.branch_copies(branch) as copy_file:
        branches = DashboardBranches(files.store, copy_file=copy_file)
        await branches.return_result(branch, parent, event_id=answer.id)
        await branches.return_result(branch, parent, event_id=answer.id)
    returned = (await files.store.events(parent.id))[-1].payload["files"][0]
    result = await files.store.file(returned["id"], user_id="1", guild_id="2")
    assert result.dashboard_id == parent.id and result.id != copies[0].id
    await files.delete_chat(branch)
    await files.store.delete(branch)
    assert await files.payload(result) == b"Original notes"
    assert len(await files.store.files(parent)) == 2


@pytest.mark.asyncio
async def test_branch_quota_failure_rolls_back_history_and_copied_files(files, monkeypatch):
    parent = await chat(files)
    originals = [
        await files.save(parent, name, b"ok", kind="output") for name in ("a.txt", "b.txt")
    ]
    selected = await saved_turn(
        files.store, parent, files=[record.public() for record in originals]
    )
    monkeypatch.setattr(files.settings, "workspace_tool_max_user_bytes", 6)
    before = set(files.workspace._base_dir.rglob("*.txt"))
    with pytest.raises(web.HTTPConflict, match="quota"):
        async with files.branch_copies(parent) as copy_file:
            await DashboardBranches(files.store, copy_file=copy_file).fork(
                parent, event_id=selected.id, request_id="quota"
            )
    assert await files.store.list_chats(user_id="1", guild_id="2") == [
        await files.store.get(parent.id, user_id="1", guild_id="2")
    ]
    assert set(files.workspace._base_dir.rglob("*.txt")) == before
    assert all(event.kind != "branch_created" for event in await files.store.events(parent.id))


@pytest.mark.asyncio
async def test_branch_copies_expired_and_foreign_files_as_expired_cards(files):
    parent, foreign = await chat(files), await chat(files, "9")
    private = await files.save(foreign, "private.txt", b"secret", kind="output")
    expired = await files.save(parent, "old.txt", b"expired", kind="output")
    await files.delete_chat(parent)
    selected = await saved_turn(files.store, parent, files=[private.public(), expired.public()])
    async with files.branch_copies(parent) as copy_file:
        branch = await DashboardBranches(files.store, copy_file=copy_file).fork(
            parent, event_id=selected.id, request_id="expired"
        )
    assert await files.store.files(branch) == []
    assert (await files.store.events(branch.id))[-1].payload["files"] == [
        {"filename": "private.txt", "expired": True},
        {"filename": "old.txt", "expired": True},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("committed", [False, True])
async def test_restart_reconciles_quarantined_chat_files(files, committed):
    own = await chat(files)
    record = await files.save(own, "secret.txt", b"private", kind="upload")
    root = files.workspace.allowed_output_roots(context_key=files.context(own.id, "upload"))[0]
    quarantine = root.with_name(root.name + ".deleting")
    root.rename(quarantine)
    if committed:
        await files.store.delete(own)
    await files.recover_deletions()
    assert not quarantine.exists()
    if committed:
        assert not root.exists()
    else:
        assert await files.payload(record) == b"private"


@pytest.mark.asyncio
async def test_chat_deletion_refuses_a_symlink_to_another_chats_snapshots(files):
    own, other = await chat(files), await chat(files, "9")
    record = await files.save(other, "private.txt", b"other member", kind="upload")
    root = files.workspace.allowed_output_roots(context_key=files.context(own.id, "upload"))[0]
    target = files.workspace.allowed_output_roots(context_key=files.context(other.id, "upload"))[0]
    root.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="snapshot"):
        await files.delete_conversation(own)
    assert await files.payload(record) == b"other member"
    assert await files.store.get(own.id, user_id="1", guild_id="2") is not None


@pytest.mark.asyncio
async def test_output_publication_counts_staged_bytes_and_rechecks_current_quota(
    files, monkeypatch
):
    own = await chat(files)
    directory = files.workspace.user_files_dir(workspace_owner_key("1", "2"))
    paths = [directory / name for name in ("a.txt", "b.txt")]
    for path in paths:
        path.write_bytes(b"result")
    outputs = await files.capture_outputs(own, tuple(str(path) for path in paths))
    monkeypatch.setattr(files.settings, "workspace_tool_max_user_bytes", 14)
    await files.save(own, "existing.txt", b"old", kind="upload")
    turn, _ = await files.store.accept(own, request_id="quota", text="files", files=[])
    async with files.output_copies(own, outputs) as (public, records):
        assert len(records) == 1
        assert public[1] == {"filename": "b.txt", "unavailable": True}
        await files.store.finish_turn(own.id, turn, "completed", {"files": public}, files=records)
    assert len(await files.store.files(own)) == 2


@pytest.mark.asyncio
async def test_output_capture_bounds_total_memory_and_file_count(files, monkeypatch):
    own = await chat(files)
    source = files.workspace.user_files_dir(workspace_owner_key("1", "2")) / "result.txt"
    source.write_bytes(b"result")
    monkeypatch.setattr(files.settings, "workspace_tool_max_user_bytes", 12)
    outputs = await files.capture_outputs(own, (str(source),) * 11)
    assert len(outputs) == 10
    assert sum(len(output.payload or b"") for output in outputs) == 12
    assert sum(output.payload is not None for output in outputs) == 2
    assert await files.store.files(own) == []
