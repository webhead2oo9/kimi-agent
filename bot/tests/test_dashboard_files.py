from __future__ import annotations

import os

import pytest
import pytest_asyncio
from aiohttp import web

from app.dashboard_files import DashboardFiles, read_regular
from storage.dashboard import DashboardStore
from storage.db import Database
from tests.helpers import make_settings
from tools.workspace.common import UserLocks
from workspace import WorkspaceManager, workspace_owner_key


@pytest_asyncio.fixture
async def files(tmp_path):
    db = Database(tmp_path / "files.db")
    await db.connect()
    service = DashboardFiles(
        store=DashboardStore(db),
        workspace=WorkspaceManager(tmp_path / "work"),
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
    assert (await files.snapshot(own, ("/etc/passwd",)))[0]["expired"]
    with pytest.raises(ValueError):
        await files.snapshot_workspace(own, "../outside")
    await files.delete_chat(own)
    with pytest.raises(web.HTTPGone):
        await files.payload(saved)
    assert path.read_text() == "changed"


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
            saved = await files.snapshot(own, (str(source),), workspace_guard_held=True)
            assert saved[0]["filename"] == "result.txt"
            assert not entered.is_set()
        await maintenance
    assert entered.is_set()
