"""queue_file: attaching and removing reply attachments."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.embeds import EmbedAttachment
from tools.output_queue import enqueue_output_file
from tools.workspace import (
    WorkspaceToolConfig,
)

from tests.workspace_tool_helpers import (
    WS,
    _make_ctx,
    _register,
    _requires_symlinks,
)


@pytest.mark.asyncio
async def test_queue_file_attaches_existing_user_workspace_file(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path, WorkspaceToolConfig(max_attachments=3))
    ctx = _make_ctx()
    saved = mgr.user_files_dir(WS) / "notes" / "result.txt"
    saved.parent.mkdir(parents=True, exist_ok=True)
    saved.write_text("ready", encoding="utf-8")

    result = await reg.dispatch("queue_file", {"path": "notes/result.txt"}, ctx)

    assert json.loads(result) == {
        "path": "notes/result.txt",
        "queued": True,
        "queued_files": ["notes/result.txt"],
    }
    assert ctx.output_files == [str(saved.resolve())]
    assert ctx.allowed_file_roots == [str(mgr.user_files_dir(WS).resolve())]


@pytest.mark.asyncio
async def test_queue_file_attaches_existing_generated_file(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path, WorkspaceToolConfig(max_attachments=3))
    ctx = _make_ctx()
    generated = mgr.generated_job_dir(ctx.context_key, "job-1") / "output-1.png"
    generated.write_bytes(b"image")
    relative_path = mgr.relative_generated_file_path(generated)

    result = await reg.dispatch(
        "queue_file",
        {"path": relative_path},
        ctx,
    )

    assert json.loads(result) == {
        "path": relative_path,
        "queued": True,
        "queued_files": [relative_path],
    }
    assert ctx.output_files == [str(generated.resolve())]
    assert ctx.allowed_file_roots == [str(mgr.allowed_output_roots(context_key=ctx.context_key)[0])]


@pytest.mark.asyncio
async def test_queue_file_rejects_generated_file_without_context(
    tmp_path: Path,
) -> None:
    reg, mgr = _register(tmp_path, WorkspaceToolConfig(max_attachments=3))
    ctx = _make_ctx()
    generated = mgr.generated_job_dir(ctx.context_key, "job-1") / "output-1.png"
    generated.write_bytes(b"image")
    ctx.context_key = ""

    result = await reg.dispatch(
        "queue_file",
        {"path": mgr.relative_generated_file_path(generated)},
        ctx,
    )

    parsed = json.loads(result)
    assert "conversation context" in parsed["error"].lower()
    assert ctx.output_files == []
    assert ctx.allowed_file_roots == []


@pytest.mark.asyncio
async def test_queue_file_prefers_user_workspace_generated_named_file(
    tmp_path: Path,
) -> None:
    reg, mgr = _register(tmp_path, WorkspaceToolConfig(max_attachments=3))
    ctx = _make_ctx()
    saved = mgr.user_files_dir(WS) / "generated" / "note.txt"
    saved.parent.mkdir(parents=True, exist_ok=True)
    saved.write_text("user file", encoding="utf-8")

    result = await reg.dispatch("queue_file", {"path": "generated/note.txt"}, ctx)

    assert json.loads(result) == {
        "path": "generated/note.txt",
        "queued": True,
        "queued_files": ["generated/note.txt"],
    }
    assert ctx.output_files == [str(saved.resolve())]
    assert ctx.allowed_file_roots == [str(mgr.user_files_dir(WS).resolve())]


@pytest.mark.asyncio
async def test_queue_file_respects_attachment_cap(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path, WorkspaceToolConfig(max_attachments=1))
    ctx = _make_ctx()
    first = mgr.user_files_dir(WS) / "a.txt"
    second = mgr.user_files_dir(WS) / "b.txt"
    first.write_text("one", encoding="utf-8")
    second.write_text("two", encoding="utf-8")

    queued = await reg.dispatch("queue_file", {"path": "a.txt"}, ctx)
    capped = await reg.dispatch("queue_file", {"path": "b.txt"}, ctx)

    assert json.loads(queued)["queued"] is True
    parsed = json.loads(capped)
    assert parsed["path"] == "b.txt"
    assert parsed["queued"] is False
    assert parsed["queued_files"] == ["a.txt"]
    assert "attachment limit" in parsed["error"].lower()
    assert ctx.output_files == [str(first.resolve())]


@pytest.mark.asyncio
async def test_queue_file_is_idempotent_for_already_queued_file(
    tmp_path: Path,
) -> None:
    reg, mgr = _register(tmp_path, WorkspaceToolConfig(max_attachments=1))
    ctx = _make_ctx()
    saved = mgr.user_files_dir(WS) / "a.txt"
    saved.write_text("one", encoding="utf-8")

    first = await reg.dispatch("queue_file", {"path": "a.txt"}, ctx)
    second = await reg.dispatch("queue_file", {"path": "a.txt"}, ctx)

    assert json.loads(first)["queued"] is True
    assert json.loads(second) == {
        "path": "a.txt",
        "queued": True,
        "queued_files": ["a.txt"],
    }
    assert ctx.output_files == [str(saved.resolve())]


@pytest.mark.asyncio
async def test_queue_file_remove_frees_slot_for_new_attachment(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path, WorkspaceToolConfig(max_attachments=1))
    ctx = _make_ctx()
    first = mgr.user_files_dir(WS) / "junk.txt"
    second = mgr.user_files_dir(WS) / "deliverable.zip"
    first.write_text("junk", encoding="utf-8")
    second.write_bytes(b"zip")
    await reg.dispatch("queue_file", {"path": "junk.txt"}, ctx)

    capped = await reg.dispatch("queue_file", {"path": "deliverable.zip"}, ctx)
    removed = await reg.dispatch("queue_file", {"path": "junk.txt", "action": "remove"}, ctx)
    queued = await reg.dispatch("queue_file", {"path": "deliverable.zip"}, ctx)

    assert json.loads(capped)["queued"] is False
    assert json.loads(removed) == {
        "path": "junk.txt",
        "removed": True,
        "queued_files": [],
    }
    assert json.loads(queued)["queued"] is True
    assert ctx.output_files == [str(second.resolve())]


@pytest.mark.asyncio
async def test_queue_file_remove_bare_filename_requires_unique_match(
    tmp_path: Path,
) -> None:
    reg, mgr = _register(tmp_path, WorkspaceToolConfig(max_attachments=3))
    ctx = _make_ctx()
    for sub in ("sub1", "sub2"):
        path = mgr.user_files_dir(WS) / sub / "x.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(sub, encoding="utf-8")
        await reg.dispatch("queue_file", {"path": f"{sub}/x.txt"}, ctx)

    ambiguous = await reg.dispatch("queue_file", {"path": "x.txt", "action": "remove"}, ctx)
    ambiguous_payload = json.loads(ambiguous)
    by_path = await reg.dispatch("queue_file", {"path": "sub1/x.txt", "action": "remove"}, ctx)
    now_unique = await reg.dispatch("queue_file", {"path": "x.txt", "action": "remove"}, ctx)

    assert "more than one" in ambiguous_payload["error"]
    assert [match["path"] for match in ambiguous_payload["matches"]] == [
        "sub1/x.txt",
        "sub2/x.txt",
    ]
    assert len({match["remove_id"] for match in ambiguous_payload["matches"]}) == 2
    assert json.loads(by_path)["removed"] is True
    assert json.loads(now_unique) == {
        "path": "sub2/x.txt",
        "removed": True,
        "queued_files": [],
    }
    assert ctx.output_files == []


@pytest.mark.asyncio
async def test_queue_file_remove_handles_entry_deleted_from_disk(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path, WorkspaceToolConfig(max_attachments=1))
    ctx = _make_ctx()
    saved = mgr.user_files_dir(WS) / "gone.txt"
    saved.write_text("soon gone", encoding="utf-8")
    await reg.dispatch("queue_file", {"path": "gone.txt"}, ctx)
    saved.unlink()

    removed = await reg.dispatch("queue_file", {"path": "gone.txt", "action": "remove"}, ctx)

    assert json.loads(removed)["removed"] is True
    assert ctx.output_files == []


@pytest.mark.asyncio
async def test_queue_file_remove_generated_artifact(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path, WorkspaceToolConfig(max_attachments=1))
    ctx = _make_ctx()
    generated = mgr.generated_job_dir(ctx.context_key, "job-1") / "output-1.png"
    generated.write_bytes(b"image")
    relative_path = mgr.relative_generated_file_path(generated)
    await reg.dispatch("queue_file", {"path": relative_path}, ctx)

    removed = await reg.dispatch("queue_file", {"path": relative_path, "action": "remove"}, ctx)

    assert json.loads(removed) == {
        "path": relative_path,
        "removed": True,
        "queued_files": [],
    }
    assert ctx.output_files == []


@pytest.mark.asyncio
async def test_queue_file_remove_by_absolute_path_for_skill_output(
    tmp_path: Path,
) -> None:
    reg, mgr = _register(tmp_path, WorkspaceToolConfig(max_attachments=1))
    ctx = _make_ctx()
    job_dir = mgr.ensure(WS) / "jobs" / "job-xyz"
    job_dir.mkdir(parents=True, exist_ok=True)
    chart = job_dir / "matplotlib-chart.png"
    chart.write_bytes(b"png")
    ctx.output_files.append(str(chart.resolve()))

    removed = await reg.dispatch(
        "queue_file", {"path": str(chart.resolve()), "action": "remove"}, ctx
    )

    assert json.loads(removed)["removed"] is True
    assert ctx.output_files == []


@pytest.mark.asyncio
async def test_queue_file_remove_id_disambiguates_duplicate_skill_outputs(
    tmp_path: Path,
) -> None:
    reg, mgr = _register(tmp_path, WorkspaceToolConfig(max_attachments=2))
    ctx = _make_ctx()
    job_dir = mgr.ensure(WS) / "jobs" / "job-xyz"
    first = job_dir / "first" / "chart.png"
    second = job_dir / "second" / "chart.png"
    for output in (first, second):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"png")
    first_queued = enqueue_output_file(ctx, first, job_dir, max_attachments=2)
    second_queued = enqueue_output_file(ctx, second, job_dir, max_attachments=2)

    ambiguous = json.loads(
        await reg.dispatch("queue_file", {"path": "chart.png", "action": "remove"}, ctx)
    )
    removed = json.loads(
        await reg.dispatch(
            "queue_file",
            {"path": second_queued.remove_id, "action": "remove"},
            ctx,
        )
    )

    assert first_queued.remove_id != second_queued.remove_id
    assert {match["remove_id"] for match in ambiguous["matches"]} == {
        first_queued.remove_id,
        second_queued.remove_id,
    }
    assert removed == {
        "path": "chart.png",
        "removed": True,
        "queued_files": ["chart.png"],
    }
    assert ctx.output_files == [str(first.resolve())]
    assert str(job_dir) not in json.dumps(ambiguous)


@pytest.mark.asyncio
async def test_queue_file_remove_unattached_file_reports_queue(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path, WorkspaceToolConfig(max_attachments=2))
    ctx = _make_ctx()
    queued = mgr.user_files_dir(WS) / "kept.txt"
    other = mgr.user_files_dir(WS) / "never-queued.txt"
    queued.write_text("kept", encoding="utf-8")
    other.write_text("other", encoding="utf-8")
    await reg.dispatch("queue_file", {"path": "kept.txt"}, ctx)

    result = await reg.dispatch("queue_file", {"path": "never-queued.txt", "action": "remove"}, ctx)

    assert json.loads(result) == {
        "path": "never-queued.txt",
        "removed": False,
        "queued_files": ["kept.txt"],
        "error": "file is not attached",
    }
    assert ctx.output_files == [str(queued.resolve())]


@pytest.mark.asyncio
async def test_queue_file_remove_refuses_pending_embed_image(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path, WorkspaceToolConfig(max_attachments=1))
    ctx = _make_ctx()
    image = mgr.user_files_dir(WS) / "img.png"
    image.write_bytes(b"png")
    await reg.dispatch("queue_file", {"path": "img.png"}, ctx)
    ctx.embed_attachment = EmbedAttachment(
        path=str(image.resolve()),
        root=str(mgr.user_files_dir(WS).resolve()),
        filename="img.png",
    )

    result = await reg.dispatch("queue_file", {"path": "img.png", "action": "remove"}, ctx)

    assert "pending embed" in json.loads(result)["error"]
    assert ctx.output_files == [str(image.resolve())]


@pytest.mark.asyncio
async def test_queue_file_rejects_unknown_action(tmp_path: Path) -> None:
    reg, _mgr = _register(tmp_path, WorkspaceToolConfig(max_attachments=1))
    ctx = _make_ctx()

    result = await reg.dispatch("queue_file", {"path": "a.txt", "action": "evict"}, ctx)

    assert json.loads(result) == {"error": "action must be 'add' or 'remove'"}


@pytest.mark.asyncio
@_requires_symlinks
async def test_queue_file_noop_when_skill_output_already_attached(
    tmp_path: Path,
) -> None:
    # Skill outputs live beside files/ and may already be attached. Re-queueing
    # the reported path is an idempotent success even though ordinary file tools
    # cannot address that path.
    reg, mgr = _register(tmp_path, WorkspaceToolConfig(max_attachments=3))
    ctx = _make_ctx()
    job_dir = mgr.ensure(WS) / "jobs" / "job-xyz"
    job_dir.mkdir(parents=True, exist_ok=True)
    chart = job_dir / "matplotlib-chart.png"
    chart.write_bytes(b"png")
    ctx.output_files.append(str(chart.resolve()))

    by_abs = await reg.dispatch("queue_file", {"path": str(chart.resolve())}, ctx)
    by_name = await reg.dispatch("queue_file", {"path": "matplotlib-chart.png"}, ctx)

    for raw in (by_abs, by_name):
        parsed = json.loads(raw)
        assert parsed["queued"] is True
        assert parsed["already_attached"] is True
        assert parsed["path"] == "matplotlib-chart.png"
        assert "error" not in parsed

    # A structured/traversal path that merely collides on basename must NOT be
    # masked as already-attached; the real rejection has to surface.
    traversal = await reg.dispatch("queue_file", {"path": "../../etc/matplotlib-chart.png"}, ctx)
    parsed = json.loads(traversal)
    assert "error" in parsed
    assert "already_attached" not in parsed

    # A *different* absolute path that only collides on basename must be rejected,
    # not reported as already-attached (absolute requires an exact match).
    wrong_abs = await reg.dispatch(
        "queue_file", {"path": "/tmp/elsewhere/matplotlib-chart.png"}, ctx
    )
    parsed = json.loads(wrong_abs)
    assert "error" in parsed
    assert "already_attached" not in parsed

    # A bare name that resolves to a (live) symlink in files/ must surface the
    # symlink rejection, even though its basename collides with an attached file.
    target = mgr.user_files_dir(WS) / "real-target.png"
    target.write_bytes(b"png")
    link = mgr.user_files_dir(WS) / "matplotlib-chart.png"
    link.symlink_to(target)
    symlinked = await reg.dispatch("queue_file", {"path": "matplotlib-chart.png"}, ctx)
    parsed = json.loads(symlinked)
    assert "error" in parsed
    assert "already_attached" not in parsed

    # No duplicate attachment from any of the redundant queue_file calls.
    assert ctx.output_files == [str(chart.resolve())]


@pytest.mark.asyncio
async def test_queue_file_rejects_generated_file_from_other_context(
    tmp_path: Path,
) -> None:
    reg, mgr = _register(tmp_path, WorkspaceToolConfig(max_attachments=3))
    ctx = _make_ctx()
    generated = mgr.generated_job_dir("other:guild:main", "job-1") / "output-1.png"
    generated.write_bytes(b"image")

    result = await reg.dispatch(
        "queue_file",
        {"path": mgr.relative_generated_file_path(generated)},
        ctx,
    )

    parsed = json.loads(result)
    assert "error" in parsed
    assert "conversation context" in parsed["error"].lower()
    assert ctx.output_files == []
    assert ctx.allowed_file_roots == []


@pytest.mark.asyncio
@_requires_symlinks
async def test_queue_file_rejects_invalid_generated_paths_without_appending(
    tmp_path: Path,
) -> None:
    reg, mgr = _register(tmp_path, WorkspaceToolConfig(max_attachments=3))
    ctx = _make_ctx()
    generated_dir = mgr.generated_job_dir(ctx.context_key, "job-1")
    target = generated_dir / "target.png"
    target.write_bytes(b"image")
    symlink = generated_dir / "link.png"
    symlink.symlink_to(target)
    relative_dir = mgr.relative_generated_file_path(generated_dir)

    missing = await reg.dispatch(
        "queue_file",
        {"path": f"{relative_dir}/missing.png"},
        ctx,
    )
    directory = await reg.dispatch(
        "queue_file",
        {"path": relative_dir},
        ctx,
    )
    linked = await reg.dispatch(
        "queue_file",
        {"path": f"{relative_dir}/link.png"},
        ctx,
    )

    assert "error" in json.loads(missing)
    assert "not a file" in json.loads(directory)["error"].lower()
    assert "symlink" in json.loads(linked)["error"].lower()
    assert ctx.output_files == []
    assert ctx.allowed_file_roots == []
