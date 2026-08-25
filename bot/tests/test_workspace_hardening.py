"""Coverage for the agent-review workspace hardening: env-dir containment on
move/extract, attachment-rail bookkeeping, the entry cap, and the
self-correcting error surface."""

import json
import os
import tempfile
import zipfile
from pathlib import Path

import pytest

from workspace import WorkspaceManager, workspace_owner_key
from tools.downloads import FetchResult
from tools.registry import MessageContext, ToolRegistry
from tools.workspace import WorkspaceToolConfig, init_workspace_tools
from tools.workspace import fetch as workspace_fetch
from trust.tiers import TrustTier

WS = workspace_owner_key("user123", "g1")


def _can_symlink() -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "target"
        target.touch()
        try:
            os.symlink(target, Path(tmp) / "link")
        except OSError:
            return False
    return True


_requires_symlinks = pytest.mark.skipif(
    not _can_symlink(), reason="symlink creation unavailable on this host"
)


def _register(
    tmp_path: Path, config: WorkspaceToolConfig | None = None
) -> tuple[ToolRegistry, WorkspaceManager]:
    reg = ToolRegistry()
    mgr = WorkspaceManager(base_dir=tmp_path)
    init_workspace_tools(reg, mgr, config=config or WorkspaceToolConfig())
    return reg, mgr


def _make_ctx() -> MessageContext:
    return MessageContext(
        user_id="user123",
        user_name="User",
        guild_id="g1",
        channel_id="c1",
        thread_id=None,
        trust_tier=TrustTier.MEMBER,
        context_key="g1_c1_main",
    )


@pytest.mark.asyncio
async def test_move_file_refuses_env_dir_source_and_destination(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path)
    ctx = _make_ctx()
    root = mgr.user_files_dir(WS)
    (root / "big.bin").write_bytes(b"x" * 64)

    result = await reg.dispatch("move_file", {"path": "big.bin", "dest": ".venv/big.bin"}, ctx)
    error = json.loads(result)["error"]
    assert "environment directories" in error
    assert "quota" not in error  # never masked as a quota failure
    assert (root / "big.bin").exists()

    # Source side too: pulling files OUT of a reserved environment dir is refused.
    (root / ".venv").mkdir()
    (root / ".venv" / "pkg.py").write_text("x", encoding="utf-8")
    result = await reg.dispatch("move_file", {"path": ".venv/pkg.py", "dest": "pkg.py"}, ctx)
    assert "environment directories" in json.loads(result)["error"]


@pytest.mark.asyncio
async def test_extract_archive_refuses_env_dir_destination(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path)
    ctx = _make_ctx()
    ctx.activated_tools.add("extract_archive")  # searchable tool
    root = mgr.user_files_dir(WS)
    archive = root / "pkg.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("mod.py", "print(1)\n")

    result = await reg.dispatch("extract_archive", {"path": "pkg.zip", "dest": ".venv/pkg"}, ctx)
    assert "environment directories" in json.loads(result)["error"]


@pytest.mark.asyncio
async def test_edit_file_env_dir_refusal_is_not_reported_as_quota(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path)
    ctx = _make_ctx()
    root = mgr.user_files_dir(WS)
    (root / ".venv" / "lib").mkdir(parents=True)
    (root / ".venv" / "lib" / "mod.py").write_text("value = 1\n", encoding="utf-8")

    result = await reg.dispatch(
        "edit_file",
        {
            "path": ".venv/lib/mod.py",
            "old_string": "value = 1",
            "new_string": "value = 2",
        },
        ctx,
    )
    error = json.loads(result)["error"]
    assert "environment directories" in error
    assert "quota" not in error


@pytest.mark.asyncio
async def test_move_file_requeues_attached_files(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path)
    ctx = _make_ctx()
    await reg.dispatch("write_file", {"path": "report.md", "content": "done"}, ctx)
    assert len(ctx.output_files) == 1

    result = json.loads(
        await reg.dispatch("move_file", {"path": "report.md", "dest": "final/report.md"}, ctx)
    )
    assert result["attachments_updated"] == 1
    resolved = mgr.user_files_dir(WS).resolve() / "final" / "report.md"
    assert ctx.output_files == [str(resolved)]

    # Directory moves rewrite every queued entry underneath.
    moved = json.loads(await reg.dispatch("move_file", {"path": "final", "dest": "shipped"}, ctx))
    assert moved["attachments_updated"] == 1
    resolved = mgr.user_files_dir(WS).resolve() / "shipped" / "report.md"
    assert ctx.output_files == [str(resolved)]


@pytest.mark.asyncio
async def test_write_file_refuses_when_entry_cap_reached(tmp_path: Path) -> None:
    reg, _mgr = _register(tmp_path, WorkspaceToolConfig(max_workspace_entries=2))
    ctx = _make_ctx()
    for name in ("a.txt", "b.txt"):
        result = json.loads(await reg.dispatch("write_file", {"path": name, "content": "x"}, ctx))
        assert "error" not in result

    over = json.loads(await reg.dispatch("write_file", {"path": "c.txt", "content": "x"}, ctx))
    assert "too many files" in over["error"]

    # Existing files can still be edited: the cap bounds new entries only.
    edited = json.loads(
        await reg.dispatch(
            "edit_file", {"path": "a.txt", "old_string": "x", "new_string": "y"}, ctx
        )
    )
    assert "error" not in edited


@pytest.mark.asyncio
async def test_grep_literal_zero_match_hints_regex_mode(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path)
    ctx = _make_ctx()
    (mgr.user_files_dir(WS) / "log.txt").write_text("an error happened\n", encoding="utf-8")

    result = json.loads(await reg.dispatch("grep_workspace", {"pattern": "error|warning"}, ctx))
    assert result["count"] == 0
    assert "regex: true" in result.get("hint", "")

    # A plain literal miss gets no hint (nothing to correct).
    result = json.loads(await reg.dispatch("grep_workspace", {"pattern": "absent"}, ctx))
    assert "hint" not in result


@pytest.mark.asyncio
async def test_glob_globstar_prefix_matches_root_files(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path)
    ctx = _make_ctx()
    root = mgr.user_files_dir(WS)
    (root / "README.md").write_text("hi", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "guide.md").write_text("hi", encoding="utf-8")

    result = json.loads(await reg.dispatch("glob_workspace", {"pattern": "**/*.md"}, ctx))
    assert sorted(result["matches"]) == ["README.md", "docs/guide.md"]


@pytest.mark.asyncio
async def test_read_file_reports_offset_past_end(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path)
    ctx = _make_ctx()
    (mgr.user_files_dir(WS) / "short.txt").write_text("one\ntwo\n", encoding="utf-8")

    result = await reg.dispatch("read_file", {"path": "short.txt", "offset": 10}, ctx)
    error = json.loads(result)["error"]
    assert "past the end" in error
    assert "2 lines" in error


@pytest.mark.asyncio
async def test_malformed_args_get_corrective_errors(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path)
    ctx = _make_ctx()
    (mgr.user_files_dir(WS) / "f.txt").write_text("x\n", encoding="utf-8")

    result = await reg.dispatch("read_file", {"path": "f.txt", "offset": "abc"}, ctx)
    assert "offset must be an integer" in json.loads(result)["error"]

    result = await reg.dispatch(
        "write_file", {"path": "g.txt", "content": "x", "attach": "flase"}, ctx
    )
    assert "attach must be true or false" in json.loads(result)["error"]


@pytest.mark.asyncio
async def test_fetch_url_refuses_existing_explicit_filename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_fetch(url, destination, *, max_bytes, timeout_seconds, max_redirects):
        destination.write_text("fresh", encoding="utf-8")
        return FetchResult(size_bytes=5, content_type="text/plain")

    monkeypatch.setattr(workspace_fetch, "fetch_url_to_file", fake_fetch)
    reg, mgr = _register(tmp_path)
    ctx = _make_ctx()
    (mgr.user_files_dir(WS) / "data.csv").write_text("edited by user", encoding="utf-8")

    result = await reg.dispatch(
        "fetch_url", {"url": "https://example.com/d", "filename": "data.csv"}, ctx
    )
    assert "already exists" in json.loads(result)["error"]
    assert (mgr.user_files_dir(WS) / "data.csv").read_text(encoding="utf-8") == "edited by user"


@pytest.mark.asyncio
async def test_zip_accepts_workspace_root_and_skips_env_dirs(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path)
    ctx = _make_ctx()
    root = mgr.user_files_dir(WS)
    (root / "a.txt").write_text("a", encoding="utf-8")
    (root / "sub").mkdir()
    (root / "sub" / "b.txt").write_text("b", encoding="utf-8")
    (root / ".venv").mkdir()
    (root / ".venv" / "hidden.py").write_text("x", encoding="utf-8")

    result = json.loads(await reg.dispatch("zip", {"paths": ["."], "output": "all.zip"}, ctx))
    assert result["entry_count"] == 2  # env-dir contents stay invisible
    with zipfile.ZipFile(root / "all.zip") as zf:
        assert sorted(zf.namelist()) == ["a.txt", "sub/b.txt"]


@pytest.mark.asyncio
@_requires_symlinks
async def test_delete_file_unlinks_host_created_symlink(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path)
    ctx = _make_ctx()
    root = mgr.user_files_dir(WS)
    (root / "target.txt").write_text("t", encoding="utf-8")
    os.symlink(root / "target.txt", root / "link.txt")

    result = json.loads(await reg.dispatch("delete_file", {"path": "link.txt"}, ctx))
    assert result == {"path": "link.txt", "deleted": True, "was_symlink": True}
    assert not (root / "link.txt").exists()
    assert (root / "target.txt").exists()  # never follows the link
