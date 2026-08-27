from __future__ import annotations

from pathlib import Path

import pytest

from workspace import WorkspaceManager
from tools.output_queue import (
    AttachmentLimitError,
    enqueue_context_generated_file,
    enqueue_output_file,
    enqueue_workspace_file,
    queued_file_paths,
    requeue_moved_output,
    unqueue_output_file,
)
from tools.registry import MessageContext
from trust.tiers import TrustTier


def _ctx() -> MessageContext:
    return MessageContext(
        user_id="user123",
        user_name="test",
        guild_id="g1",
        channel_id="c1",
        thread_id=None,
        trust_tier=TrustTier.MEMBER,
        context_key="g1:c1:main",
    )


def test_enqueue_workspace_file_adds_file_and_allowed_root(tmp_path: Path) -> None:
    manager = WorkspaceManager(base_dir=tmp_path)
    ctx = _ctx()
    saved = manager.user_files_dir(ctx.workspace_key) / "notes" / "result.txt"
    saved.parent.mkdir(parents=True, exist_ok=True)
    saved.write_text("ready", encoding="utf-8")

    result = enqueue_workspace_file(
        ctx,
        manager,
        saved,
        max_attachments=3,
    )

    assert result.added is True
    assert result.path == saved.resolve()
    assert result.root == manager.user_files_dir(ctx.workspace_key).resolve()
    assert ctx.output_files == [str(saved.resolve())]
    assert ctx.allowed_file_roots == [str(manager.user_files_dir(ctx.workspace_key).resolve())]
    assert queued_file_paths(ctx, manager, ctx.workspace_key) == ["notes/result.txt"]


def test_enqueue_output_file_is_idempotent_and_restores_missing_root(
    tmp_path: Path,
) -> None:
    ctx = _ctx()
    root = tmp_path / "root"
    root.mkdir()
    output = root / "report.txt"
    output.write_text("done", encoding="utf-8")
    ctx.output_files.append(str(output.resolve()))

    result = enqueue_output_file(ctx, output, root, max_attachments=1)

    assert result.added is False
    assert ctx.output_files == [str(output.resolve())]
    assert ctx.allowed_file_roots == [str(root.resolve())]


def test_attachment_description_tracks_move_and_unqueue(tmp_path: Path) -> None:
    ctx = _ctx()
    root = tmp_path / "root"
    root.mkdir()
    output = root / "chart.png"
    moved = root / "renamed.png"
    output.write_bytes(b"png")

    enqueue_output_file(ctx, output, root, description="An increasing line chart.")
    assert ctx.output_file_descriptions == {str(output.resolve()): "An increasing line chart."}

    assert requeue_moved_output(ctx, output.resolve(), moved.resolve()) == 1
    assert ctx.output_file_descriptions == {str(moved.resolve()): "An increasing line chart."}

    unqueue_output_file(ctx, str(moved.resolve()))
    assert ctx.output_file_descriptions == {}


def test_enqueue_output_file_rejects_files_outside_root(tmp_path: Path) -> None:
    ctx = _ctx()
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("nope", encoding="utf-8")

    with pytest.raises(ValueError, match="outside allowed output root"):
        enqueue_output_file(ctx, outside, root, max_attachments=3)

    assert ctx.output_files == []
    assert ctx.allowed_file_roots == []


def test_enqueue_output_file_raises_when_attachment_cap_is_reached(
    tmp_path: Path,
) -> None:
    ctx = _ctx()
    root = tmp_path / "root"
    root.mkdir()
    first = root / "first.txt"
    second = root / "second.txt"
    first.write_text("one", encoding="utf-8")
    second.write_text("two", encoding="utf-8")

    enqueue_output_file(ctx, first, root, max_attachments=1)

    with pytest.raises(AttachmentLimitError, match="attachment limit reached"):
        enqueue_output_file(ctx, second, root, max_attachments=1)

    assert ctx.output_files == [str(first.resolve())]
    assert ctx.allowed_file_roots == [str(root.resolve())]


def test_enqueue_context_generated_file_scopes_to_current_context(
    tmp_path: Path,
) -> None:
    manager = WorkspaceManager(base_dir=tmp_path)
    ctx = _ctx()
    generated = manager.generated_job_dir(ctx.context_key, "job-1") / "output-1.png"
    generated.write_bytes(b"png")
    relative_path = manager.relative_generated_file_path(generated)

    result = enqueue_context_generated_file(
        ctx,
        manager,
        relative_path,
        max_attachments=3,
    )

    assert result.added is True
    assert ctx.output_files == [str(generated.resolve())]
    assert ctx.allowed_file_roots == [
        str(manager.allowed_output_roots(context_key=ctx.context_key)[0])
    ]
    assert queued_file_paths(ctx, manager, ctx.workspace_key) == [relative_path]


def test_enqueue_context_generated_file_requires_context_key(tmp_path: Path) -> None:
    manager = WorkspaceManager(base_dir=tmp_path)
    ctx = _ctx()
    ctx.context_key = ""
    generated = manager.generated_job_dir("g1:c1:main", "job-1") / "output-1.png"
    generated.write_bytes(b"png")
    relative_path = manager.relative_generated_file_path(generated)

    with pytest.raises(ValueError, match="conversation context"):
        enqueue_context_generated_file(
            ctx,
            manager,
            relative_path,
            max_attachments=3,
        )

    assert ctx.output_files == []
    assert ctx.allowed_file_roots == []


def test_enqueue_output_file_rejects_basename_colliding_with_pending_embed(
    tmp_path: Path,
) -> None:
    from tools.embeds import EmbedAttachment

    ctx = _ctx()
    root_a = tmp_path / "a"
    root_a.mkdir()
    embed_file = root_a / "chart.png"
    embed_file.write_text("x", encoding="utf-8")
    ctx.embed_attachment = EmbedAttachment(
        path=str(embed_file.resolve()),
        root=str(root_a.resolve()),
        filename="chart.png",
    )

    root_b = tmp_path / "b"
    root_b.mkdir()
    other = root_b / "chart.png"
    other.write_text("y", encoding="utf-8")

    with pytest.raises(ValueError, match="already attached|rename"):
        enqueue_output_file(ctx, other, root_b)
    assert ctx.output_files == []


def test_enqueue_output_file_allows_same_file_as_pending_embed(tmp_path: Path) -> None:
    from tools.embeds import EmbedAttachment

    ctx = _ctx()
    root = tmp_path / "r"
    root.mkdir()
    f = root / "chart.png"
    f.write_text("x", encoding="utf-8")
    ctx.embed_attachment = EmbedAttachment(
        path=str(f.resolve()), root=str(root.resolve()), filename="chart.png"
    )

    result = enqueue_output_file(ctx, f, root)

    assert result.added is True
    assert ctx.output_files == [str(f.resolve())]
