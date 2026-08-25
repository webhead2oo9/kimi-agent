"""read/write/edit/move/delete and the shared path fences."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.attachments import AttachmentRef
from tools.downloads import (
    FetchUrlError,
    validate_fetch_url,
)
from tools.workspace import (
    WorkspaceToolConfig,
)
from trust.tiers import TrustTier

from tests.workspace_tool_helpers import (
    WS,
    _FakeImport,
    _ctx_with_attachments,
    _make_ctx,
    _register,
    _requires_symlinks,
)


@pytest.mark.asyncio
async def test_workspace_tools_write_read_list_grep_and_delete(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path)
    ctx = _make_ctx()

    write_result = await reg.dispatch(
        "write_file",
        {"path": "notes/hello.txt", "content": "Hello\nWorld"},
        ctx,
    )
    parsed_write = json.loads(write_result)
    saved = mgr.user_files_dir(WS) / "notes" / "hello.txt"
    assert parsed_write == {
        "path": "notes/hello.txt",
        "size_bytes": len(b"Hello\nWorld"),
        "attached": True,
    }
    assert saved.read_text(encoding="utf-8") == "Hello\nWorld"
    assert ctx.output_files == [str(saved.resolve())]
    assert ctx.allowed_file_roots == [str(mgr.user_files_dir(WS).resolve())]

    read_result = await reg.dispatch("read_file", {"path": "notes/hello.txt"}, ctx)
    assert read_result == "notes/hello.txt: lines 1-2 of 2\n1: Hello\n2: World"

    list_result = await reg.dispatch("list_workspace", {"path": "notes"}, ctx)
    parsed_list = json.loads(list_result)
    assert parsed_list["entries"] == [
        {"name": "hello.txt", "type": "file", "size_bytes": len("Hello\nWorld")}
    ]

    grep_result = await reg.dispatch("grep_workspace", {"pattern": "hello"}, ctx)
    parsed_grep = json.loads(grep_result)
    assert parsed_grep["matches"] == [
        {"file": "notes/hello.txt", "line_number": 1, "text": "Hello"}
    ]

    delete_result = await reg.dispatch("delete_file", {"path": "notes/hello.txt"}, ctx)
    # The write auto-queued the file; deleting it must also drop the stale rail
    # entry (a dangling queued path fails the whole reply's file staging).
    assert json.loads(delete_result) == {
        "path": "notes/hello.txt",
        "deleted": True,
        "unattached": True,
    }
    assert not saved.exists()
    assert ctx.output_files == []


def test_heavier_workspace_tools_are_search_only(tmp_path: Path) -> None:
    reg, _mgr = _register(tmp_path)

    schema_names = {schema["name"] for schema in reg.get_tool_schemas(TrustTier.MEMBER)}
    catalog_names = {entry.name for entry in reg.catalog(TrustTier.MEMBER)}
    activated_schema_names = {
        schema["name"]
        for schema in reg.get_tool_schemas(
            TrustTier.MEMBER,
            activated={"extract_archive", "extract_document_text"},
        )
    }

    assert "extract_document_text" not in schema_names
    assert "extract_archive" not in schema_names
    assert "zip" in schema_names
    assert "extract_document_text" in catalog_names
    assert "extract_archive" in catalog_names
    assert "extract_document_text" in activated_schema_names
    assert "extract_archive" in activated_schema_names


@pytest.mark.asyncio
async def test_delete_file_removes_empty_directory_without_recursive(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path)
    ctx = _make_ctx()
    empty = mgr.user_files_dir(WS) / "empty"
    empty.mkdir(parents=True, exist_ok=True)

    result = await reg.dispatch("delete_file", {"path": "empty"}, ctx)

    assert json.loads(result) == {"path": "empty", "deleted": True}
    assert not empty.exists()


@pytest.mark.asyncio
async def test_delete_file_refuses_populated_directory_without_recursive(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path)
    ctx = _make_ctx()
    root = mgr.user_files_dir(WS)
    (root / "proj" / "src").mkdir(parents=True, exist_ok=True)
    (root / "proj" / "src" / "a.txt").write_text("a", encoding="utf-8")
    (root / "proj" / "b.txt").write_text("b", encoding="utf-8")

    result = await reg.dispatch("delete_file", {"path": "proj"}, ctx)

    assert "error" in json.loads(result)
    assert (root / "proj" / "src" / "a.txt").exists()


@pytest.mark.asyncio
async def test_delete_file_error_does_not_leak_absolute_paths(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path)
    ctx = _make_ctx()
    root = mgr.user_files_dir(WS)
    (root / "proj").mkdir(parents=True, exist_ok=True)
    (root / "proj" / "a.txt").write_text("a", encoding="utf-8")

    result = await reg.dispatch("delete_file", {"path": "proj"}, ctx)

    error = json.loads(result)["error"]
    assert str(root.resolve()) not in error
    assert "proj" in error


@pytest.mark.asyncio
async def test_write_file_to_directory_error_does_not_leak_absolute_paths(
    tmp_path: Path,
) -> None:
    reg, mgr = _register(tmp_path)
    ctx = _make_ctx()
    root = mgr.user_files_dir(WS)
    (root / "adir").mkdir(parents=True, exist_ok=True)

    result = await reg.dispatch("write_file", {"path": "adir", "content": "hi"}, ctx)

    # The write-time re-check refuses the directory target with a clean,
    # path-free error instead of a scrubbed IsADirectoryError.
    error = json.loads(result)["error"]
    assert str(root.resolve()) not in error
    assert error == "path is not a file"


@pytest.mark.asyncio
async def test_delete_file_recursive_removes_populated_directory(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path)
    ctx = _make_ctx()
    root = mgr.user_files_dir(WS)
    (root / "proj" / "src").mkdir(parents=True, exist_ok=True)
    (root / "proj" / "src" / "a.txt").write_text("a", encoding="utf-8")
    (root / "proj" / "b.txt").write_text("b", encoding="utf-8")

    result = await reg.dispatch("delete_file", {"path": "proj", "recursive": True}, ctx)

    assert json.loads(result) == {"path": "proj", "deleted": True}
    assert not (root / "proj").exists()


@pytest.mark.asyncio
async def test_delete_file_recursive_respects_entry_cap(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path, WorkspaceToolConfig(max_zip_entries=2))
    ctx = _make_ctx()
    root = mgr.user_files_dir(WS)
    (root / "proj").mkdir(parents=True, exist_ok=True)
    for name in ("a.txt", "b.txt", "c.txt"):
        (root / "proj" / name).write_text("x", encoding="utf-8")

    result = await reg.dispatch("delete_file", {"path": "proj", "recursive": True}, ctx)

    parsed = json.loads(result)
    assert "error" in parsed
    assert "limit is 2" in parsed["error"]
    assert (root / "proj" / "a.txt").exists()


@pytest.mark.asyncio
async def test_delete_file_recursive_stops_counting_after_entry_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reg, mgr = _register(tmp_path, WorkspaceToolConfig(max_zip_entries=2))
    ctx = _make_ctx()
    root = mgr.user_files_dir(WS)
    target = root / "proj"
    target.mkdir(parents=True, exist_ok=True)
    for name in ("a.txt", "b.txt", "c.txt", "d.txt"):
        (target / name).write_text("x", encoding="utf-8")

    original_rglob = Path.rglob
    yielded = 0

    def capped_rglob(self: Path, pattern: str):
        nonlocal yielded
        if self == target.resolve():
            for name in ("a.txt", "b.txt", "c.txt", "d.txt"):
                yielded += 1
                if yielded > 3:
                    raise AssertionError("recursive delete counted beyond cap")
                yield target / name
            return
        yield from original_rglob(self, pattern)

    monkeypatch.setattr(Path, "rglob", capped_rglob)

    result = await reg.dispatch("delete_file", {"path": "proj", "recursive": True}, ctx)

    parsed = json.loads(result)
    assert "limit is 2" in parsed["error"]
    assert yielded == 3
    assert (target / "a.txt").exists()


@pytest.mark.asyncio
async def test_workspace_tools_reject_traversal_and_job_access(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path)
    ctx = _make_ctx()
    job_file = mgr.create_job_dir(WS, job_id="job-abc") / "secret.txt"
    job_file.write_text("secret", encoding="utf-8")

    traversal = await reg.dispatch(
        "write_file",
        {"path": "../jobs/job-abc/secret.txt", "content": "replace"},
        ctx,
    )
    assert "traversal" in json.loads(traversal)["error"].lower()

    job_read = await reg.dispatch("read_file", {"path": "../jobs/job-abc/secret.txt"}, ctx)
    assert "traversal" in json.loads(job_read)["error"].lower()
    assert job_file.read_text(encoding="utf-8") == "secret"


@pytest.mark.asyncio
async def test_workspace_tools_enforce_file_and_user_quotas(tmp_path: Path) -> None:
    reg, mgr = _register(
        tmp_path,
        WorkspaceToolConfig(max_file_bytes=10, max_user_bytes=15),
    )
    ctx = _make_ctx()

    too_big = await reg.dispatch(
        "write_file",
        {"path": "too-big.txt", "content": "x" * 11},
        ctx,
    )
    assert "exceeds" in json.loads(too_big)["error"].lower()
    assert not (mgr.user_files_dir(WS) / "too-big.txt").exists()

    first = await reg.dispatch("write_file", {"path": "a.txt", "content": "x" * 10}, ctx)
    assert "error" not in json.loads(first)
    over_quota = await reg.dispatch("write_file", {"path": "b.txt", "content": "x" * 10}, ctx)
    assert "quota" in json.loads(over_quota)["error"].lower()
    assert not (mgr.user_files_dir(WS) / "b.txt").exists()


@pytest.mark.asyncio
async def test_read_file_negative_offset_reads_tail(tmp_path: Path) -> None:
    reg, _mgr = _register(tmp_path)
    ctx = _make_ctx()
    await reg.dispatch(
        "write_file",
        {"path": "log.txt", "content": "l1\nl2\nl3\nl4\nl5"},
        ctx,
    )

    result = await reg.dispatch("read_file", {"path": "log.txt", "offset": -2}, ctx)

    assert result == "log.txt: lines 4-5 of 5\n4: l4\n5: l5"


def test_workspace_tool_config_defaults_match_settings() -> None:
    """The no-settings defaults must equal the `workspace_tool_*` defaults.

    `app/tools.py` always builds this from Settings, so these defaults only
    apply to tests and direct construction. When they drift, the suite
    exercises limits production never uses, which is exactly what had
    happened to max_user_bytes, max_zip_entries and max_extract_total_bytes.
    """

    from config.settings import Settings

    cfg = WorkspaceToolConfig()
    settings_defaults = {
        name.removeprefix("workspace_tool_"): field.default
        for name, field in Settings.model_fields.items()
        if name.startswith("workspace_tool_")
    }
    # The dataclass spells this field differently from its settings key.
    aliases = {"max_entries": "max_workspace_entries"}

    unmatched = [key for key in settings_defaults if not hasattr(cfg, aliases.get(key, key))]
    assert not unmatched, (
        "workspace_tool_* settings with no WorkspaceToolConfig field (wire it "
        f"in app/tools.py, or add an alias here): {unmatched}"
    )

    mismatches = {
        key: (default, getattr(cfg, aliases.get(key, key)))
        for key, default in settings_defaults.items()
        if getattr(cfg, aliases.get(key, key)) != default
    }
    assert not mismatches, (
        "WorkspaceToolConfig defaults differ from their Settings defaults "
        f"(setting, dataclass): {mismatches}"
    )


@pytest.mark.asyncio
async def test_edit_file_unique_replace(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path, WorkspaceToolConfig(max_attachments=3))
    ctx = _make_ctx()
    f = mgr.user_files_dir(WS) / "tool.py"
    f.write_text("print('hello')\n", encoding="utf-8")

    result = await reg.dispatch(
        "edit_file",
        {"path": "tool.py", "old_string": "hello", "new_string": "world"},
        ctx,
    )

    body = json.loads(result)
    assert body["replacements"] == 1
    assert body["attached"] is True
    assert f.read_text(encoding="utf-8") == "print('world')\n"


@pytest.mark.asyncio
async def test_edit_file_empty_old_string_rejected(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path)
    ctx = _make_ctx()
    (mgr.user_files_dir(WS) / "a.txt").write_text("x", encoding="utf-8")
    result = await reg.dispatch(
        "edit_file", {"path": "a.txt", "old_string": "", "new_string": "y"}, ctx
    )
    assert json.loads(result) == {"error": "old_string must not be empty"}


@pytest.mark.asyncio
async def test_edit_file_multiple_matches_without_flag(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path)
    ctx = _make_ctx()
    (mgr.user_files_dir(WS) / "a.txt").write_text("a a a", encoding="utf-8")
    result = await reg.dispatch(
        "edit_file", {"path": "a.txt", "old_string": "a", "new_string": "b"}, ctx
    )
    assert "found 3 times" in json.loads(result)["error"]


@pytest.mark.asyncio
async def test_edit_file_replace_all(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path, WorkspaceToolConfig(max_attachments=3))
    ctx = _make_ctx()
    f = mgr.user_files_dir(WS) / "a.txt"
    f.write_text("a a a", encoding="utf-8")
    result = await reg.dispatch(
        "edit_file",
        {"path": "a.txt", "old_string": "a", "new_string": "b", "replace_all": True},
        ctx,
    )
    assert json.loads(result)["replacements"] == 3
    assert f.read_text(encoding="utf-8") == "b b b"


@pytest.mark.asyncio
async def test_edit_file_growth_guard_aborts_before_write(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path, WorkspaceToolConfig(max_file_bytes=20))
    ctx = _make_ctx()
    f = mgr.user_files_dir(WS) / "a.txt"
    f.write_text("aaaa", encoding="utf-8")

    result = await reg.dispatch(
        "edit_file",
        {"path": "a.txt", "old_string": "a", "new_string": "aaaaaa", "replace_all": True},
        ctx,
    )

    assert "edited file would exceed" in json.loads(result)["error"]
    assert f.read_text(encoding="utf-8") == "aaaa"


@pytest.mark.asyncio
async def test_edit_file_growth_guard_counts_utf8_bytes(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path, WorkspaceToolConfig(max_file_bytes=5))
    ctx = _make_ctx()
    f = mgr.user_files_dir(WS) / "a.txt"
    f.write_text("aaaaa", encoding="utf-8")

    result = await reg.dispatch(
        "edit_file",
        {"path": "a.txt", "old_string": "a", "new_string": "é", "replace_all": True},
        ctx,
    )

    assert "edited file would exceed" in json.loads(result)["error"]
    assert f.read_bytes() == b"aaaaa"


@pytest.mark.asyncio
async def test_edit_file_not_found_string(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path)
    ctx = _make_ctx()
    (mgr.user_files_dir(WS) / "a.txt").write_text("hello", encoding="utf-8")
    result = await reg.dispatch(
        "edit_file", {"path": "a.txt", "old_string": "zzz", "new_string": "y"}, ctx
    )
    assert json.loads(result)["error"] == "old_string not found in a.txt"


@pytest.mark.asyncio
async def test_edit_file_rejects_binary(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path)
    ctx = _make_ctx()
    (mgr.user_files_dir(WS) / "bin").write_bytes(b"\x00\x01\x02")
    result = await reg.dispatch(
        "edit_file", {"path": "bin", "old_string": "a", "new_string": "b"}, ctx
    )
    assert json.loads(result)["error"] == "bin is not a text file"


@pytest.mark.asyncio
async def test_edit_file_edits_file_larger_than_read_limit(tmp_path: Path) -> None:
    # max_read_bytes is small, but edit_file must read up to max_file_bytes.
    cfg = WorkspaceToolConfig(max_read_bytes=16, max_file_bytes=10_000, max_attachments=3)
    reg, mgr = _register(tmp_path, cfg)
    ctx = _make_ctx()
    f = mgr.user_files_dir(WS) / "big.txt"
    f.write_text("x" * 500 + "NEEDLE" + "y" * 500, encoding="utf-8")
    result = await reg.dispatch(
        "edit_file", {"path": "big.txt", "old_string": "NEEDLE", "new_string": "FOUND"}, ctx
    )
    assert json.loads(result)["replacements"] == 1
    assert "FOUND" in f.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_import_attachment_saves_by_filename(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path)
    src = _FakeImport(b"payload-bytes")
    ctx = _ctx_with_attachments(
        AttachmentRef(filename="data.bin", size=src.size, content_type=None, source=src)
    )

    result = await reg.dispatch("import_attachment", {"filename": "data.bin"}, ctx)

    body = json.loads(result)
    assert body["path"] == "imports/data.bin"
    assert body["size_bytes"] == len(b"payload-bytes")
    assert (mgr.user_files_dir(WS) / "imports" / "data.bin").read_bytes() == b"payload-bytes"
    # import does not auto-attach
    assert ctx.output_files == []


@pytest.mark.asyncio
async def test_import_attachment_unknown_filename_lists_available(tmp_path: Path) -> None:
    reg, _mgr = _register(tmp_path)
    ctx = _ctx_with_attachments(
        AttachmentRef(filename="a.txt", size=1, content_type=None, source=_FakeImport(b"a"))
    )
    result = await reg.dispatch("import_attachment", {"filename": "b.txt"}, ctx)
    assert (
        json.loads(result)["error"] == "no attachment named b.txt on this message; available: a.txt"
    )


@pytest.mark.asyncio
async def test_import_attachment_no_attachments(tmp_path: Path) -> None:
    reg, _mgr = _register(tmp_path)
    result = await reg.dispatch("import_attachment", {"filename": "a.txt"}, _make_ctx())
    assert json.loads(result)["error"] == "no files attached to this message"


@pytest.mark.asyncio
async def test_import_attachment_duplicate_filename_is_ambiguous(tmp_path: Path) -> None:
    reg, _mgr = _register(tmp_path)
    ctx = _ctx_with_attachments(
        AttachmentRef(filename="dup", size=1, content_type=None, source=_FakeImport(b"a")),
        AttachmentRef(filename="dup", size=1, content_type=None, source=_FakeImport(b"b")),
    )
    result = await reg.dispatch("import_attachment", {"filename": "dup"}, ctx)
    assert "multiple attachments named dup" in json.loads(result)["error"]


@pytest.mark.asyncio
async def test_import_attachment_size_precheck(tmp_path: Path) -> None:
    reg, _mgr = _register(tmp_path, WorkspaceToolConfig(max_import_bytes=4))
    src = _FakeImport(b"toolong", declared_size=7)
    ctx = _ctx_with_attachments(
        AttachmentRef(filename="big.bin", size=7, content_type=None, source=src)
    )
    result = await reg.dispatch("import_attachment", {"filename": "big.bin"}, ctx)
    assert "over the 4 byte import limit" in json.loads(result)["error"]


@pytest.mark.asyncio
async def test_import_attachment_empty_filename(tmp_path: Path) -> None:
    reg, _mgr = _register(tmp_path)
    result = await reg.dispatch("import_attachment", {"filename": ""}, _make_ctx())
    assert json.loads(result)["error"] == "filename must not be empty"


@pytest.mark.asyncio
async def test_import_attachment_explicit_dest_collision(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path)
    (mgr.user_files_dir(WS) / "taken.bin").write_bytes(b"existing")
    ctx = _ctx_with_attachments(
        AttachmentRef(filename="a.bin", size=1, content_type=None, source=_FakeImport(b"a"))
    )
    result = await reg.dispatch(
        "import_attachment", {"filename": "a.bin", "dest": "taken.bin"}, ctx
    )
    assert "already exists" in json.loads(result)["error"]


@pytest.mark.asyncio
async def test_import_attachment_quota_exceeded_message(tmp_path: Path) -> None:
    reg, _mgr = _register(tmp_path, WorkspaceToolConfig(max_user_bytes=1))
    ctx = _ctx_with_attachments(
        AttachmentRef(filename="a.bin", size=100, content_type=None, source=_FakeImport(b"x" * 100))
    )
    result = await reg.dispatch("import_attachment", {"filename": "a.bin"}, ctx)
    assert "importing a.bin would exceed your workspace quota" in json.loads(result)["error"]


def test_validate_url_rejects_userinfo() -> None:
    with pytest.raises(FetchUrlError):
        validate_fetch_url("https://user:pass@example.com/repo.tar.gz")
    with pytest.raises(FetchUrlError):
        validate_fetch_url("https://token@example.com/repo.tar.gz")


def test_validate_url_allows_plain_https() -> None:
    validate_fetch_url("https://codeload.github.com/o/r/tar.gz/refs/heads/main")


@pytest.mark.asyncio
async def test_multi_edit_applies_all_atomically(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path, WorkspaceToolConfig(max_attachments=3))
    ctx = _make_ctx()
    f = mgr.user_files_dir(WS) / "conf.txt"
    f.write_text("host=old\nport=1\n", encoding="utf-8")

    result = await reg.dispatch(
        "multi_edit",
        {
            "path": "conf.txt",
            "edits": [
                {"old_string": "host=old", "new_string": "host=new"},
                {"old_string": "port=1", "new_string": "port=2"},
            ],
        },
        ctx,
    )
    body = json.loads(result)
    assert body["edits"] == 2
    assert body["replacements"] == 2
    assert f.read_text(encoding="utf-8") == "host=new\nport=2\n"


@pytest.mark.asyncio
async def test_multi_edit_is_sequential(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path)
    ctx = _make_ctx()
    f = mgr.user_files_dir(WS) / "a.txt"
    f.write_text("alpha", encoding="utf-8")

    result = await reg.dispatch(
        "multi_edit",
        {
            "path": "a.txt",
            "edits": [
                {"old_string": "alpha", "new_string": "beta"},
                {"old_string": "beta", "new_string": "gamma"},
            ],
        },
        ctx,
    )
    assert json.loads(result)["replacements"] == 2
    assert f.read_text(encoding="utf-8") == "gamma"


@pytest.mark.asyncio
async def test_multi_edit_aborts_without_partial_write(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path)
    ctx = _make_ctx()
    f = mgr.user_files_dir(WS) / "a.txt"
    f.write_text("x=1\ny=2\n", encoding="utf-8")

    result = await reg.dispatch(
        "multi_edit",
        {
            "path": "a.txt",
            "edits": [
                {"old_string": "x=1", "new_string": "x=9"},
                {"old_string": "ZZZ", "new_string": "Q"},
            ],
        },
        ctx,
    )
    assert "edit 2: old_string not found" in json.loads(result)["error"]
    # The first (valid) edit must NOT have been written.
    assert f.read_text(encoding="utf-8") == "x=1\ny=2\n"


@pytest.mark.asyncio
async def test_multi_edit_ambiguous_hunk_aborts(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path)
    ctx = _make_ctx()
    f = mgr.user_files_dir(WS) / "a.txt"
    f.write_text("a a", encoding="utf-8")

    result = await reg.dispatch(
        "multi_edit",
        {"path": "a.txt", "edits": [{"old_string": "a", "new_string": "b"}]},
        ctx,
    )
    assert "edit 1: old_string found 2 times" in json.loads(result)["error"]
    assert f.read_text(encoding="utf-8") == "a a"


@pytest.mark.asyncio
async def test_multi_edit_replace_all_within_edit(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path)
    ctx = _make_ctx()
    f = mgr.user_files_dir(WS) / "a.txt"
    f.write_text("a a a", encoding="utf-8")

    result = await reg.dispatch(
        "multi_edit",
        {
            "path": "a.txt",
            "edits": [{"old_string": "a", "new_string": "b", "replace_all": True}],
        },
        ctx,
    )
    assert json.loads(result)["replacements"] == 3
    assert f.read_text(encoding="utf-8") == "b b b"


@pytest.mark.asyncio
async def test_multi_edit_rejects_empty_edits(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path)
    ctx = _make_ctx()
    (mgr.user_files_dir(WS) / "a.txt").write_text("x", encoding="utf-8")
    result = await reg.dispatch("multi_edit", {"path": "a.txt", "edits": []}, ctx)
    assert json.loads(result) == {"error": "edits must be a non-empty list"}


@pytest.mark.asyncio
async def test_multi_edit_exceeds_max_ops(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path, WorkspaceToolConfig(multi_edit_max_ops=1))
    ctx = _make_ctx()
    (mgr.user_files_dir(WS) / "a.txt").write_text("uv", encoding="utf-8")
    result = await reg.dispatch(
        "multi_edit",
        {
            "path": "a.txt",
            "edits": [
                {"old_string": "u", "new_string": "1"},
                {"old_string": "v", "new_string": "2"},
            ],
        },
        ctx,
    )
    assert "at most 1 edits" in json.loads(result)["error"]


@pytest.mark.asyncio
async def test_multi_edit_growth_guard_aborts_before_write(tmp_path: Path) -> None:
    # A replace_all whose result would exceed the file limit must be rejected
    # per-edit, before the growing buffer is ever allocated, with no write.
    reg, mgr = _register(tmp_path, WorkspaceToolConfig(max_file_bytes=20))
    ctx = _make_ctx()
    f = mgr.user_files_dir(WS) / "a.txt"
    f.write_text("aaaa", encoding="utf-8")

    result = await reg.dispatch(
        "multi_edit",
        {
            "path": "a.txt",
            "edits": [{"old_string": "a", "new_string": "aaaaaa", "replace_all": True}],
        },
        ctx,
    )
    assert "edit 1: result would exceed" in json.loads(result)["error"]
    assert f.read_text(encoding="utf-8") == "aaaa"


@pytest.mark.asyncio
async def test_multi_edit_growth_guard_counts_utf8_bytes(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path, WorkspaceToolConfig(max_file_bytes=5))
    ctx = _make_ctx()
    f = mgr.user_files_dir(WS) / "a.txt"
    f.write_text("aaaaa", encoding="utf-8")

    result = await reg.dispatch(
        "multi_edit",
        {
            "path": "a.txt",
            "edits": [
                {
                    "old_string": "a",
                    "new_string": "\u00e9",
                    "replace_all": True,
                }
            ],
        },
        ctx,
    )
    assert "edit 1: result would exceed" in json.loads(result)["error"]
    assert f.read_bytes() == b"aaaaa"


@pytest.mark.asyncio
async def test_write_file_attach_false_skips_queue_and_queue_file_recovers(
    tmp_path: Path,
) -> None:
    reg, mgr = _register(tmp_path)
    ctx = _make_ctx()

    result = json.loads(
        await reg.dispatch(
            "write_file",
            {"path": "scratch.py", "content": "print('x')", "attach": False},
            ctx,
        )
    )

    assert result["attached"] is False
    assert ctx.output_files == []
    saved = mgr.user_files_dir(WS) / "scratch.py"
    assert saved.read_text(encoding="utf-8") == "print('x')"

    # The opt-out is reversible: queue_file attaches the same file explicitly.
    queued = json.loads(await reg.dispatch("queue_file", {"path": "scratch.py"}, ctx))
    assert queued["queued"] is True
    assert ctx.output_files == [str(saved.resolve())]


@pytest.mark.asyncio
async def test_edit_and_multi_edit_attach_false_skip_queue(tmp_path: Path) -> None:
    reg, _mgr = _register(tmp_path)
    ctx = _make_ctx()
    await reg.dispatch(
        "write_file",
        {"path": "s.txt", "content": "a b c", "attach": False},
        ctx,
    )

    edited = json.loads(
        await reg.dispatch(
            "edit_file",
            {"path": "s.txt", "old_string": "a", "new_string": "x", "attach": False},
            ctx,
        )
    )
    assert edited["attached"] is False

    multi = json.loads(
        await reg.dispatch(
            "multi_edit",
            {
                "path": "s.txt",
                "edits": [{"old_string": "b", "new_string": "y"}],
                "attach": False,
            },
            ctx,
        )
    )
    assert multi["attached"] is False
    assert ctx.output_files == []


@pytest.mark.asyncio
async def test_move_file_renames_file_and_creates_parents(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path)
    ctx = _make_ctx()
    root = mgr.user_files_dir(WS)
    (root / "old.bin").write_bytes(b"\x00\x01binary")

    result = json.loads(
        await reg.dispatch("move_file", {"path": "old.bin", "dest": "renamed/new.bin"}, ctx)
    )

    assert result == {"from": "old.bin", "to": "renamed/new.bin", "moved": True}
    assert not (root / "old.bin").exists()
    assert (root / "renamed" / "new.bin").read_bytes() == b"\x00\x01binary"


@pytest.mark.asyncio
async def test_move_file_renames_directory(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path)
    ctx = _make_ctx()
    root = mgr.user_files_dir(WS)
    (root / "proj" / "sub").mkdir(parents=True)
    (root / "proj" / "sub" / "f.txt").write_text("hi", encoding="utf-8")

    result = json.loads(await reg.dispatch("move_file", {"path": "proj", "dest": "project"}, ctx))

    assert result["moved"] is True
    assert (root / "project" / "sub" / "f.txt").read_text(encoding="utf-8") == "hi"
    assert not (root / "proj").exists()


@pytest.mark.asyncio
@_requires_symlinks
async def test_move_file_refusals(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path)
    ctx = _make_ctx()
    root = mgr.user_files_dir(WS)
    (root / "a.txt").write_text("a", encoding="utf-8")
    (root / "b.txt").write_text("b", encoding="utf-8")
    (root / "d").mkdir()
    (root / "real.txt").write_text("real", encoding="utf-8")
    (root / "link.txt").symlink_to(root / "real.txt")

    missing = await reg.dispatch("move_file", {"path": "nope.txt", "dest": "x"}, ctx)
    assert "error" in json.loads(missing)

    taken = json.loads(await reg.dispatch("move_file", {"path": "a.txt", "dest": "b.txt"}, ctx))
    assert "already exists" in taken["error"]

    same = json.loads(await reg.dispatch("move_file", {"path": "a.txt", "dest": "a.txt"}, ctx))
    assert "error" in same

    into_itself = json.loads(
        await reg.dispatch("move_file", {"path": "d", "dest": "d/inside"}, ctx)
    )
    assert "error" in into_itself

    linked = json.loads(
        await reg.dispatch("move_file", {"path": "link.txt", "dest": "moved.txt"}, ctx)
    )
    assert "error" in linked

    escape = json.loads(
        await reg.dispatch("move_file", {"path": "a.txt", "dest": "../escape.txt"}, ctx)
    )
    assert "error" in escape

    # Nothing moved by the refused calls.
    assert (root / "a.txt").read_text(encoding="utf-8") == "a"
    assert (root / "d").is_dir()
    assert not (root / "moved.txt").exists()


@pytest.mark.asyncio
async def test_read_file_empty_file(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path)
    ctx = _make_ctx()
    (mgr.user_files_dir(WS) / "empty.txt").write_text("", encoding="utf-8")

    result = await reg.dispatch("read_file", {"path": "empty.txt"}, ctx)

    assert result == "empty.txt: empty file"


@pytest.mark.asyncio
async def test_attached_field_reports_rail_state_not_this_call(tmp_path: Path) -> None:
    reg, _mgr = _register(tmp_path)
    ctx = _make_ctx()

    first = json.loads(await reg.dispatch("write_file", {"path": "a.txt", "content": "one"}, ctx))
    assert first["attached"] is True

    # Re-writing an already-queued path adds nothing new to the queue, but the
    # file still rides the reply, so attached stays true.
    second = json.loads(await reg.dispatch("write_file", {"path": "a.txt", "content": "two"}, ctx))
    assert second["attached"] is True

    # attach=false skips adding but does not remove: still attached.
    third = json.loads(
        await reg.dispatch(
            "edit_file",
            {"path": "a.txt", "old_string": "two", "new_string": "three", "attach": False},
            ctx,
        )
    )
    assert third["attached"] is True
    assert len(ctx.output_files) == 1


@pytest.mark.asyncio
async def test_read_file_truncation_header_matches_shown_lines(tmp_path: Path) -> None:
    reg, _mgr = _register(tmp_path, WorkspaceToolConfig(max_text_chars=30))
    ctx = _make_ctx()
    content = "\n".join(f"line-{i}" for i in range(1, 21))
    await reg.dispatch("write_file", {"path": "big.txt", "content": content, "attach": False}, ctx)

    result = await reg.dispatch("read_file", {"path": "big.txt"}, ctx)

    header, _, body = result.partition("\n")
    assert body.endswith("[TRUNCATED]")
    shown_lines = body.rsplit("\n[TRUNCATED]", 1)[0].split("\n")
    # The header range equals what the truncated body actually shows.
    assert header == f"big.txt: lines 1-{len(shown_lines)} of 20"
    assert len(shown_lines) < 20


def test_file_handlers_are_callable_without_the_registration_wrapper(tmp_path: Path) -> None:
    """A handler is now a plain function over an explicit deps object.

    While these were closures inside register_file_tools, reaching one meant
    registering all of them and dispatching through the registry. This is the
    seam that change bought, so it is worth a test.
    """
    import asyncio

    from tools.workspace.config import WorkspaceToolConfig
    from tools.workspace.common import UserLocks
    from tools.workspace.files import FileToolDeps, _read_file
    from workspace import WorkspaceManager

    manager = WorkspaceManager(base_dir=tmp_path)
    deps = FileToolDeps(
        workspace_manager=manager,
        config=WorkspaceToolConfig(),
        locks=UserLocks(),
    )
    ctx = _make_ctx()
    target = manager.user_files_dir(WS) / "note.txt"
    target.write_text("alpha\nbeta\n", encoding="utf-8")

    result = asyncio.run(_read_file(deps, {"path": "note.txt"}, ctx))

    assert "1: alpha" in result
    assert "2: beta" in result
