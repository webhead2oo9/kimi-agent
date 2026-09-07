"""Current uploads become reusable, isolated workspace files after moderation."""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import replace
from pathlib import Path

import pytest

from agent.attachments import AttachmentRef, CollectedImage
from agent.context import ConversationContext
from agent.turn import TurnRequest
from app.chat_attachments import stage_chat_attachments
from providers.types import ContentPart
from tests.helpers import VALID_PNG_BYTES
from tests.test_image_gen_tool import TOOL_NAME, _args, _context, _registered
from tools.registry import BudgetName
from tools.workspace.common import UserLocks
from tools.workspace.config import WorkspaceToolConfig
from tools.workspace.files import FileToolDeps, _import_attachment
from trust.tiers import TrustTier
from workspace import WorkspaceManager, workspace_owner_key


def _turn(tmp_path: Path, *, filename: str = "image.png") -> TurnRequest:
    part = ContentPart.from_image_url(
        url="data:image/png;base64," + base64.b64encode(VALID_PNG_BYTES).decode(),
        media_type="image/png",
    )
    return TurnRequest(
        content="Edit my attached image",
        context=ConversationContext(key="guild:channel:root"),
        trust_tier=TrustTier.REGULAR,
        user_id="user-1",
        user_name="Regular",
        guild_id="guild-1",
        channel_id="channel-1",
        thread_id=None,
        channel_name="general",
        trigger_discord_message_id="123456",
        input_parts=(part,),
        current_images=(CollectedImage(part, "hash", tmp_path / "temp.png", filename),),
    )


def _deps(tmp_path: Path, **limits: int) -> FileToolDeps:
    return FileToolDeps(
        WorkspaceManager(tmp_path / "workspaces"), WorkspaceToolConfig(**limits), UserLocks()
    )


@pytest.mark.asyncio
async def test_staged_image_can_be_selected_directly_for_edit_and_import(tmp_path: Path) -> None:
    registry, service, manager = _registered(tmp_path)
    deps = replace(_deps(tmp_path), workspace_manager=manager)
    staged = await stage_chat_attachments(_turn(tmp_path), deps=deps)
    ctx = _context()
    ctx.attachments = list(staged.attachments)

    assert staged.attachments[0].workspace_path == "chat-attachments/123456/123456-1.png"
    assert staged.attachments[0].cached_payload == VALID_PNG_BYTES
    assert staged.attachments[0].workspace_path in (staged.input_parts[-1].text or "")
    imported = json.loads(await _import_attachment(deps, {"filename": "image.png"}, ctx))
    assert imported["already_saved"] is True
    copied = json.loads(
        await _import_attachment(deps, {"filename": "image.png", "dest": "imports/edit.png"}, ctx)
    )
    assert manager.resolve_user_file_path(ctx.workspace_key, copied["path"]).read_bytes() == (
        VALID_PNG_BYTES
    )
    result = json.loads(
        await registry.dispatch(TOOL_NAME, _args(reference_attachments=["image.png"]), ctx)
    )
    assert result["operation"] == "edit"
    assert not service.generate_requests
    assert base64.b64decode(service.edit_requests[0].images[0].data_url.split(",", 1)[1]) == (
        VALID_PNG_BYTES
    )


@pytest.mark.asyncio
async def test_named_uploads_reuse_identical_bytes_but_never_overwrite_edits(
    tmp_path: Path,
) -> None:
    deps = _deps(tmp_path)
    turn = _turn(tmp_path, filename="my-drawing.png")
    staged = await stage_chat_attachments(turn, deps=deps)
    again = await stage_chat_attachments(turn, deps=deps)
    path = staged.attachments[0].workspace_path
    assert path == "chat-attachments/123456/my-drawing.png"
    assert again.attachments[0].workspace_path == path
    key = workspace_owner_key(turn.user_id, turn.guild_id)
    original = deps.workspace_manager.resolve_user_file_path(key, path)
    original.write_bytes(b"user edited this")
    newer = await stage_chat_attachments(turn, deps=deps)
    assert newer.attachments[0].workspace_path != path
    assert original.read_bytes() == b"user edited this"


@pytest.mark.asyncio
async def test_staging_quota_failure_does_not_claim_a_saved_reference(tmp_path: Path) -> None:
    deps = _deps(tmp_path, max_user_bytes=1)
    staged = await stage_chat_attachments(_turn(tmp_path), deps=deps)
    assert staged.attachments[0].workspace_path == ""
    assert "Not saved" in (staged.input_parts[-1].text or "")
    assert not list(tmp_path.rglob("*.png"))


@pytest.mark.asyncio
async def test_only_current_valid_images_and_admitted_files_are_staged(tmp_path: Path) -> None:
    deps = _deps(tmp_path)
    turn = _turn(tmp_path)
    text = AttachmentRef("notes.txt", 5, "text/plain", None, cached_payload=b"notes")
    blocked = replace(text, filename="blocked.txt", unavailable_reason="moderation unavailable")
    video = replace(text, filename="movie.mp4", video_stream_url="https://example.invalid/video")
    staged = await stage_chat_attachments(
        replace(turn, current_images=(), attachments=(text, blocked, video)), deps=deps
    )
    assert [a.workspace_path for a in staged.attachments] == [
        "chat-attachments/123456/notes.txt",
        "",
        "",
    ]
    # An image in vision/reply context alone is not an upload by this user.
    empty = await stage_chat_attachments(replace(turn, current_images=()), deps=deps)
    assert not empty.attachments
    bad = replace(
        turn.current_images[0],
        part=ContentPart.from_image_url(
            url="data:image/png;base64,bm90IGFuIGltYWdl", media_type="image/png"
        ),
    )
    invalid = await stage_chat_attachments(replace(turn, current_images=(bad,)), deps=deps)
    assert not invalid.attachments


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["missing", "ambiguous", "unsaved", "duplicate", "over_limit"])
async def test_bad_attachment_selection_never_generates_without_reference(
    tmp_path: Path, case: str
) -> None:
    registry, service, manager = _registered(tmp_path)
    staged = await stage_chat_attachments(
        _turn(tmp_path), deps=replace(_deps(tmp_path), workspace_manager=manager)
    )
    ctx = _context(tool_config={"max_reference_images": 1} if case == "over_limit" else None)
    attachment = staged.attachments[0]
    ctx.attachments = list(staged.attachments)
    args = _args(reference_attachments=["image.png"])
    if case == "missing":
        args["reference_attachments"] = ["unknown.png"]
    elif case == "ambiguous":
        ctx.attachments = [attachment, attachment]
    elif case == "unsaved":
        ctx.attachments = [replace(attachment, workspace_path="")]
    else:
        args["reference_paths"] = [attachment.workspace_path]
    result = json.loads(await registry.dispatch(TOOL_NAME, args, ctx))
    assert "error" in result
    assert not service.edit_requests
    assert not service.generate_requests
    assert ctx.budget_used(BudgetName.IMAGE_GEN_CALLS) == 0


@pytest.mark.asyncio
async def test_busy_workspace_does_not_block_chat_indefinitely(tmp_path: Path) -> None:
    deps = _deps(tmp_path)
    turn = _turn(tmp_path)
    key = workspace_owner_key(turn.user_id, turn.guild_id)
    async with deps.locks.writer(key):
        async with asyncio.timeout(5):
            staged = await stage_chat_attachments(turn, deps=deps)
    assert not staged.attachments[0].workspace_path
    assert "workspace busy" in (staged.input_parts[-1].text or "")
    # The cancelled waiter must not leave a lock or maintenance lease behind.
    async with asyncio.timeout(5):
        retried = await stage_chat_attachments(turn, deps=deps)
    assert retried.attachments[0].workspace_path


@pytest.mark.asyncio
async def test_staging_isolated_and_rejects_symlink_destinations(tmp_path: Path) -> None:
    deps = _deps(tmp_path)
    turn = _turn(tmp_path)
    first = await stage_chat_attachments(turn, deps=deps)
    other = replace(turn, user_id="other-user")
    key = workspace_owner_key(other.user_id, other.guild_id)
    deps.workspace_manager.ensure(key)
    directory = deps.workspace_manager.resolve_user_file_path(key, "chat-attachments")
    outside = tmp_path / "outside"
    outside.mkdir()
    directory.symlink_to(outside, target_is_directory=True)
    rejected = await stage_chat_attachments(other, deps=deps)
    assert not rejected.attachments[0].workspace_path
    assert not list(outside.iterdir())
    original_key = workspace_owner_key(turn.user_id, turn.guild_id)
    assert (
        deps.workspace_manager.resolve_user_file_path(
            original_key, first.attachments[0].workspace_path
        ).read_bytes()
        == VALID_PNG_BYTES
    )
