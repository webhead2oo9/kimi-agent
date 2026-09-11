from __future__ import annotations

import asyncio
import base64
import threading
from pathlib import Path

import pytest

from agent.attachments import AttachmentRef
from app.modules import ModuleManager
from config.settings import Settings
from bram_agent_module_api import ModulePermissions, ModuleSpec, ModuleToolContext
from bram_agent_module_api.files import FileAccessError
from modules.files import ModuleToolFiles
from modules.testing import build_test_runtime
from providers.types import ContentPart
from tools.registry import MessageContext, ToolRegistry
from trust.tiers import TrustTier
from tools.workspace.common import UserLocks
from workspace import WorkspaceManager


def context(**kwargs) -> MessageContext:
    return MessageContext(
        user_id="1",
        user_name="Alice",
        guild_id=None if kwargs.get("personal_chat") else "10",
        channel_id="20",
        thread_id=None,
        trust_tier=TrustTier.MEMBER,
        **kwargs,
    )


def save(manager: WorkspaceManager, ctx: MessageContext, name: str, data: bytes) -> None:
    manager.ensure(ctx.workspace_key)
    path = manager.resolve_user_file_path(ctx.workspace_key, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


@pytest.mark.asyncio
async def test_read_admitted_staged_attachment_and_saved_file(tmp_path: Path) -> None:
    ctx = context(
        attachments=[
            AttachmentRef("original.png", 8, "image/png", None, workspace_path="saved.png")
        ]
    )
    manager = WorkspaceManager(tmp_path)
    save(manager, ctx, "saved.png", b"\x89PNG\r\n\x1a\n")
    files = ModuleToolFiles(ctx, manager, UserLocks())
    attachment = files.attachments[0]
    result = await files.read_attachment(attachment.id, max_bytes=8)
    assert result.filename == "original.png"
    assert result.data == b"\x89PNG\r\n\x1a\n"
    assert attachment.workspace_path == "saved.png"
    assert (await files.read_workspace("saved.png", max_bytes=8)).data == result.data


@pytest.mark.asyncio
async def test_unavailable_never_falls_back_to_remote_or_cached_payload(tmp_path: Path) -> None:
    ctx = context(
        attachments=[
            AttachmentRef(
                "blocked.png",
                3,
                "image/png",
                None,
                cached_payload=b"raw",
                unavailable_reason="moderation",
                workspace_path="saved.png",
            ),
            AttachmentRef("unstaged.png", 3, "image/png", None, cached_payload=b"raw"),
        ]
    )
    files = ModuleToolFiles(ctx, WorkspaceManager(tmp_path), UserLocks())
    for entry in files.attachments:
        assert entry.unavailable_reason
        with pytest.raises(FileAccessError) as error:
            await files.read_attachment(entry.id, max_bytes=3)
        assert error.value.code == "unavailable"


@pytest.mark.asyncio
async def test_only_admitted_reply_images_are_exposed(tmp_path: Path) -> None:
    raw = b"\x89PNG\r\n\x1a\n"
    part = ContentPart.from_image_url(
        url="data:image/png;base64," + base64.b64encode(raw).decode(), media_type="image/png"
    )
    ctx = context(
        reply_image_parts=[part],
        edit_target_image=ContentPart.from_image_url(
            url="https://example.com/other.png", media_type="image/png"
        ),
    )
    files = ModuleToolFiles(ctx, WorkspaceManager(tmp_path), UserLocks())
    assert len(files.attachments) == 1
    entry = files.attachments[0]
    assert entry.source == "reply" and entry.size == len(raw)
    assert (await files.read_attachment(entry.id, max_bytes=len(raw))).data == raw


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["../other/private.txt", "/etc/passwd", "link.txt", "directory"])
async def test_workspace_boundary_and_symlinks(tmp_path: Path, path: str) -> None:
    ctx = context()
    manager = WorkspaceManager(tmp_path / "workspaces")
    save(manager, ctx, "own.txt", b"own")
    root = manager.user_files_dir(ctx.workspace_key)
    other = tmp_path / "private.txt"
    other.write_bytes(b"secret")
    (root / "link.txt").symlink_to(other)
    (root / "directory").mkdir()
    files = ModuleToolFiles(ctx, manager, UserLocks())
    with pytest.raises(FileAccessError) as error:
        await files.read_workspace(path, max_bytes=100)
    assert str(tmp_path) not in str(error.value)
    assert "secret" not in str(error.value)


@pytest.mark.asyncio
async def test_scoped_personal_workspace_and_budget(tmp_path: Path) -> None:
    ctx = context()
    personal = context(personal_chat=True)
    manager = WorkspaceManager(tmp_path)
    save(manager, ctx, "same.txt", b"guild")
    save(manager, personal, "same.txt", b"private")
    files = ModuleToolFiles(personal, manager, UserLocks(), max_bytes=7)
    assert (await files.read_workspace("same.txt", max_bytes=7)).data == b"private"
    with pytest.raises(FileAccessError) as error:
        await files.read_workspace("same.txt", max_bytes=7)
    assert error.value.code == "budget_exhausted"


@pytest.mark.asyncio
async def test_read_bounds_and_expiration(tmp_path: Path) -> None:
    ctx = context()
    manager = WorkspaceManager(tmp_path)
    save(manager, ctx, "a.txt", b"12345")
    files = ModuleToolFiles(ctx, manager, UserLocks())
    for limit in (0, -1, True):
        with pytest.raises(FileAccessError) as error:
            await files.read_workspace("a.txt", max_bytes=limit)
        assert error.value.code == "invalid_limit"
    with pytest.raises(FileAccessError) as error:
        await files.read_workspace("a.txt", max_bytes=4)
    assert error.value.code == "too_large"
    files.close()
    with pytest.raises(FileAccessError) as error:
        await files.read_workspace("a.txt", max_bytes=5)
    assert error.value.code == "expired"


@pytest.mark.asyncio
async def test_module_declaration_dispatch_and_lifetime(tmp_path: Path) -> None:
    seen: list[ModuleToolContext] = []

    class Module:
        scoped_migrations = ()

        async def start(self, ctx):
            pass

        async def close(self):
            pass

    async def handler(args, ctx):
        seen.append(ctx)
        assert ctx.files is not None
        assert (await ctx.files.read_workspace("file.txt", max_bytes=10)).data == b"hello"
        return "ok"

    def create(ctx):
        ctx.registry.register(
            "file_test",
            "Read a file",
            {"type": "object"},
            handler,
            guild_only=False,
            untrusted=False,
        )
        return Module()

    spec = ModuleSpec(
        "file_test",
        "1",
        create,
        api_version=2,
        permissions=ModulePermissions(tool_files=True),
        requires_capabilities=("tools.files.v1",),
    )
    runtime = await build_test_runtime(tmp_path, [spec.name], installed={spec.name: spec})
    ctx = context(personal_chat=True)
    save(WorkspaceManager(tmp_path / "workspaces"), ctx, "file.txt", b"hello")
    try:
        assert await runtime.registry.dispatch("file_test", {}, ctx) == "ok"
        assert seen[0].guild_id is None
        assert seen[0].files is not None
        with pytest.raises(FileAccessError) as error:
            await seen[0].files.read_workspace("file.txt", max_bytes=10)
        assert error.value.code == "expired"
    finally:
        await runtime.close()


def test_permission_fails_without_host_reader(tmp_path: Path) -> None:
    def never_create(ctx):
        raise AssertionError("create must not run")

    spec = ModuleSpec(
        "file_test",
        "1",
        never_create,
        api_version=2,
        permissions=ModulePermissions(tool_files=True),
    )
    with pytest.raises(RuntimeError, match="file access is unavailable"):
        ModuleManager.load(
            [spec.name],
            core_settings=Settings(_env_file=None, config_dir=str(tmp_path)),  # type: ignore[call-arg]
            registry=ToolRegistry(),
            installed={spec.name: spec},
        )


@pytest.mark.asyncio
async def test_cancelled_worker_retains_workspace_lease(tmp_path: Path) -> None:
    ctx = context()
    locks = UserLocks()
    files = ModuleToolFiles(ctx, WorkspaceManager(tmp_path), locks)
    entered, release = threading.Event(), threading.Event()

    def read(limit):
        entered.set()
        release.wait(5)
        from bram_agent_module_api import ToolFile

        return ToolFile("a.txt", "text/plain", b"raw")

    task = asyncio.create_task(files._read(read, 10))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert locks.for_user(ctx.workspace_key).locked()
        assert not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not locks.for_user(ctx.workspace_key).locked()


@pytest.mark.asyncio
async def test_cannot_read_another_users_saved_file(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path)
    alice = context()
    bob = context()
    bob.user_id = "2"
    save(manager, bob, "private.txt", b"bob secret")
    files = ModuleToolFiles(alice, manager, UserLocks())
    with pytest.raises(FileAccessError):
        await files.read_workspace("private.txt", max_bytes=100)


@pytest.mark.asyncio
async def test_attachment_ids_cannot_be_reused_in_a_later_invocation(tmp_path: Path) -> None:
    ctx = context(
        attachments=[AttachmentRef("a.txt", 3, "text/plain", None, workspace_path="a.txt")]
    )
    manager = WorkspaceManager(tmp_path)
    save(manager, ctx, "a.txt", b"raw")
    first = ModuleToolFiles(ctx, manager, UserLocks())
    identifier = first.attachments[0].id
    first.close()
    second = ModuleToolFiles(ctx, manager, UserLocks())
    with pytest.raises(FileAccessError) as error:
        await second.read_attachment(identifier, max_bytes=3)
    assert error.value.code == "unknown_attachment"


@pytest.mark.asyncio
async def test_reader_expires_on_failed_dispatch(tmp_path: Path) -> None:
    seen = []

    class Module:
        scoped_migrations = ()

        async def start(self, ctx):
            pass

        async def close(self):
            pass

    async def handler(args, ctx):
        seen.append(ctx.files)
        raise RuntimeError("handler failed")

    def create(ctx):
        ctx.registry.register(
            "fail_file", "Fail after obtaining file access", {"type": "object"}, handler
        )
        return Module()

    spec = ModuleSpec(
        "fail_file", "1", create, api_version=2, permissions=ModulePermissions(tool_files=True)
    )
    runtime = await build_test_runtime(tmp_path, [spec.name], installed={spec.name: spec})
    try:
        await runtime.registry.dispatch("fail_file", {}, context())
        with pytest.raises(FileAccessError) as error:
            await seen[0].read_workspace("a.txt", max_bytes=10)
        assert error.value.code == "expired"
    finally:
        await runtime.close()
