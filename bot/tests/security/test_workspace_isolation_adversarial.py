"""Workspace isolation regressions at the activity and path boundaries.

These tests exercise the production lease instead of forcing path swaps inside
protected functions. Symlink cases are intentionally Linux/POSIX-only; every
workspace and outside target is rooted in pytest's disposable ``tmp_path``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from pathlib import Path

import pytest

from workspace import WorkspaceManager, workspace_owner_key
from tools.registry import MessageContext, ToolRegistry
from tools.workspace import WorkspaceToolConfig, init_workspace_tools
from trust.tiers import TrustTier

ATTACKER = workspace_owner_key("111", "g1")
VICTIM = workspace_owner_key("333", "g1")
SAFE_TEXT = "SAFE-WORKSPACE-BYTES"
VICTIM_SECRET = "VICTIM-SECRET-7101"
TINY_PNG = b"\x89PNG\r\n\x1a\n" + b"safe-image-bytes"


@pytest.fixture
def posix_symlink_tmp_path(tmp_path: Path) -> Path:
    if os.name == "nt":
        pytest.skip("POSIX symlink regressions run on Linux")
    target = tmp_path / ".symlink-probe-target"
    target.mkdir()
    probe = tmp_path / ".symlink-probe"
    try:
        probe.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    probe.unlink()
    return tmp_path


def make_context(*, guild_id: str | None = "g1") -> MessageContext:
    return MessageContext(
        user_id="111",
        user_name="attacker",
        guild_id=guild_id,
        channel_id="c1",
        thread_id=None,
        trust_tier=TrustTier.MEMBER,
        context_key="g1:c1:main",
    )


def register_workspace(
    tmp_path: Path,
    *,
    manager: WorkspaceManager | None = None,
    config: WorkspaceToolConfig | None = None,
):
    registry = ToolRegistry()
    workspace_manager = manager or WorkspaceManager(tmp_path / "workspaces")
    locks = init_workspace_tools(
        registry,
        workspace_manager,
        config=config or WorkspaceToolConfig(),
    )
    return registry, workspace_manager, locks


@pytest.mark.asyncio
async def test_shared_activity_lease_blocks_read_grep_and_view_image(
    tmp_path: Path,
) -> None:
    registry, manager, locks = register_workspace(tmp_path)
    context = make_context()
    context.images_supported = True
    root = manager.user_files_dir(ATTACKER)
    (root / "notes.txt").write_text(SAFE_TEXT, encoding="utf-8")
    (root / "safe.png").write_bytes(TINY_PNG)

    holder_entered = asyncio.Event()
    release_holder = asyncio.Event()

    async def hold_workspace() -> None:
        async with locks.activity(context.workspace_key):
            holder_entered.set()
            await release_holder.wait()

    holder = asyncio.create_task(hold_workspace())
    await holder_entered.wait()
    read_task = asyncio.create_task(registry.dispatch("read_file", {"path": "notes.txt"}, context))
    grep_task = asyncio.create_task(
        registry.dispatch(
            "grep_workspace",
            {"pattern": SAFE_TEXT, "path": "notes.txt"},
            context,
        )
    )
    view_task = asyncio.create_task(registry.dispatch("view_image", {"path": "safe.png"}, context))

    await asyncio.sleep(0)
    assert not read_task.done()
    assert not grep_task.done()
    assert not view_task.done()

    release_holder.set()
    read_result, grep_result, view_result = await asyncio.gather(
        read_task,
        grep_task,
        view_task,
    )
    await holder

    assert SAFE_TEXT in read_result
    assert SAFE_TEXT in grep_result
    assert json.loads(view_result)["viewing"] is True
    expected_image = base64.b64encode(TINY_PNG).decode("ascii")
    assert any(expected_image in (part.image_url or "") for part in context.pending_view_images)


def test_workspace_paths_reject_traversal_and_isolate_guilds(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path / "workspaces")
    guild_one = workspace_owner_key("111", "g1")
    guild_two = workspace_owner_key("111", "g2")
    secret = manager.user_files_dir(guild_one) / "secret.txt"
    secret.write_text(SAFE_TEXT, encoding="utf-8")

    with pytest.raises(ValueError, match="traversal"):
        manager.resolve_user_file_path(guild_two, "../escape.txt")
    with pytest.raises(ValueError, match="relative"):
        manager.resolve_user_file_path(guild_two, str(secret.resolve()))

    assert manager.user_files_dir(guild_one) != manager.user_files_dir(guild_two)
    assert not (manager.user_files_dir(guild_two) / "secret.txt").exists()


@pytest.mark.asyncio
async def test_workspace_quota_rejects_projected_overflow(tmp_path: Path) -> None:
    registry, manager, _locks = register_workspace(
        tmp_path,
        config=WorkspaceToolConfig(max_file_bytes=1024, max_user_bytes=8),
    )
    context = make_context()
    existing = manager.user_files_dir(ATTACKER) / "existing.txt"
    existing.write_bytes(b"123456")

    result = await registry.dispatch(
        "write_file",
        {"path": "overflow.txt", "content": "abcd", "attach": False},
        context,
    )

    assert "quota" in str(json.loads(result).get("error", "")).lower()
    assert existing.read_bytes() == b"123456"
    assert not (existing.parent / "overflow.txt").exists()


@pytest.mark.asyncio
async def test_static_and_dangling_symlinks_fail_closed(
    posix_symlink_tmp_path: Path,
) -> None:
    tmp_path = posix_symlink_tmp_path
    registry, manager, _locks = register_workspace(tmp_path)
    context = make_context()
    victim_root = manager.user_files_dir(VICTIM)
    (victim_root / "secret.txt").write_text(VICTIM_SECRET, encoding="utf-8")
    attacker_root = manager.user_files_dir(ATTACKER)
    static_link = attacker_root / "crossed"
    static_link.symlink_to(victim_root, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        manager.resolve_user_file_path(
            ATTACKER,
            "crossed/secret.txt",
            must_exist=True,
        )
    read_result = await registry.dispatch(
        "read_file",
        {"path": "crossed/secret.txt"},
        context,
    )
    assert VICTIM_SECRET not in read_result

    static_link.unlink()
    dangling = attacker_root / "dangling"
    dangling.symlink_to(tmp_path / "missing-outside", target_is_directory=True)
    with pytest.raises(ValueError, match="traversal"):
        manager.resolve_user_file_path(
            ATTACKER,
            "dangling/file.txt",
            must_exist=False,
        )


@pytest.mark.asyncio
async def test_rglob_consumers_do_not_recurse_through_symlinked_directories(
    posix_symlink_tmp_path: Path,
) -> None:
    tmp_path = posix_symlink_tmp_path
    manager = WorkspaceManager(
        tmp_path / "workspaces",
        file_ttl=60,
        max_size_bytes=10**9,
    )
    registry, manager, _locks = register_workspace(tmp_path, manager=manager)
    context = make_context()
    root = manager.user_files_dir(ATTACKER)
    docs = root / "docs"
    docs.mkdir()
    decoy = docs / "decoy.txt"
    decoy.write_text("attacker decoy", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text(VICTIM_SECRET, encoding="utf-8")
    old = time.time() - 600
    os.utime(secret, (old, old))
    (docs / "crossed").symlink_to(outside, target_is_directory=True)

    walked = {path.relative_to(root).as_posix() for path in root.rglob("*")}
    assert "docs/crossed" in walked
    assert "docs/crossed/secret.txt" not in walked
    assert manager.user_files_size(ATTACKER) == decoy.stat().st_size
    # Non-recursion through the symlinked directory is proven by the rglob walk
    # above: "docs/crossed" lists, "docs/crossed/secret.txt" does not.

    manager._sweep_expired_sync()
    assert secret.exists(), "sweeper crossed a symlinked workspace directory"

    zip_result = await registry.dispatch(
        "zip",
        {"paths": ["docs"], "output": "out.zip"},
        context,
    )
    assert "symlink" in str(json.loads(zip_result).get("error", "")).lower()
    assert not (root / "out.zip").exists()
