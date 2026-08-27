"""zip and extract_archive."""

from __future__ import annotations

import io
import json
import tarfile
import zipfile
from pathlib import Path

import pytest

from tools.workspace import (
    WorkspaceToolConfig,
)

from tests.workspace_tool_helpers import (
    WS,
    _make_ctx,
    _make_repo_tar,
    _register,
    _requires_symlinks,
)


def test_workspace_tool_config_has_import_and_zip_limits() -> None:
    cfg = WorkspaceToolConfig()
    assert cfg.max_file_bytes == 50 * 1024 * 1024
    assert cfg.max_import_bytes == 25 * 1024 * 1024
    assert cfg.max_zip_entries == 10_000
    assert cfg.max_attachments == 5
    assert cfg.max_pdf_pages == 500


@pytest.mark.asyncio
async def test_zip_round_trip(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path, WorkspaceToolConfig(max_attachments=3))
    ctx = _make_ctx()
    root = mgr.user_files_dir(WS)
    (root / "a.txt").write_text("alpha", encoding="utf-8")
    (root / "sub").mkdir()
    (root / "sub" / "b.txt").write_text("beta", encoding="utf-8")

    result = await reg.dispatch("zip", {"paths": ["a.txt", "sub"], "output": "out.zip"}, ctx)

    body = json.loads(result)
    assert body["path"] == "out.zip"
    assert body["entry_count"] == 2
    assert body["attached"] is False
    assert "queue_file" in body["attachment_hint"]
    assert ctx.output_files == []
    queued = json.loads(await reg.dispatch("queue_file", {"path": "out.zip"}, ctx))
    assert queued["queued"] is True
    with zipfile.ZipFile(root / "out.zip") as zf:
        assert sorted(zf.namelist()) == ["a.txt", "sub/b.txt"]
        assert zf.read("sub/b.txt") == b"beta"


@pytest.mark.asyncio
async def test_zip_empty_paths(tmp_path: Path) -> None:
    reg, _mgr = _register(tmp_path)
    ctx = _make_ctx()
    result = await reg.dispatch("zip", {"paths": [], "output": "out.zip"}, ctx)
    assert json.loads(result)["error"] == "paths must be a non-empty array of strings"


@pytest.mark.asyncio
async def test_zip_requires_zip_extension(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path)
    ctx = _make_ctx()
    (mgr.user_files_dir(WS) / "a.txt").write_text("x", encoding="utf-8")
    result = await reg.dispatch("zip", {"paths": ["a.txt"], "output": "out.tar"}, ctx)
    assert json.loads(result)["error"] == "output must end in .zip"


@pytest.mark.asyncio
async def test_zip_missing_path(tmp_path: Path) -> None:
    reg, _mgr = _register(tmp_path)
    ctx = _make_ctx()
    result = await reg.dispatch("zip", {"paths": ["nope.txt"], "output": "out.zip"}, ctx)
    assert json.loads(result)["error"] == "path not found: nope.txt"


@pytest.mark.asyncio
async def test_zip_output_collision(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path)
    ctx = _make_ctx()
    root = mgr.user_files_dir(WS)
    (root / "a.txt").write_text("x", encoding="utf-8")
    (root / "out.zip").write_bytes(b"existing")
    result = await reg.dispatch("zip", {"paths": ["a.txt"], "output": "out.zip"}, ctx)
    assert "already exists" in json.loads(result)["error"]


@pytest.mark.asyncio
async def test_zip_entry_cap(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path, WorkspaceToolConfig(max_zip_entries=2, max_attachments=3))
    ctx = _make_ctx()
    root = mgr.user_files_dir(WS)
    for i in range(3):
        (root / f"f{i}.txt").write_text("x", encoding="utf-8")
    result = await reg.dispatch(
        "zip", {"paths": ["f0.txt", "f1.txt", "f2.txt"], "output": "out.zip"}, ctx
    )
    assert "too many files" in json.loads(result)["error"]


@pytest.mark.asyncio
async def test_zip_entry_cap_applies_to_directory(tmp_path: Path) -> None:
    # Guards against the DoS path: the cap must fire during traversal of a
    # directory, not only after the whole tree is materialized.
    reg, mgr = _register(tmp_path, WorkspaceToolConfig(max_zip_entries=2, max_attachments=3))
    ctx = _make_ctx()
    d = mgr.user_files_dir(WS) / "many"
    d.mkdir()
    for i in range(3):
        (d / f"f{i}.txt").write_text("x", encoding="utf-8")
    result = await reg.dispatch("zip", {"paths": ["many"], "output": "out.zip"}, ctx)
    assert "too many files" in json.loads(result)["error"]


@pytest.mark.asyncio
async def test_zip_quota_exceeded_message(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path, WorkspaceToolConfig(max_user_bytes=1, max_attachments=3))
    ctx = _make_ctx()
    (mgr.user_files_dir(WS) / "a.txt").write_text("hello world", encoding="utf-8")
    result = await reg.dispatch("zip", {"paths": ["a.txt"], "output": "out.zip"}, ctx)
    assert "zip would exceed your workspace quota" in json.loads(result)["error"]


@pytest.mark.asyncio
async def test_extract_archive_happy_path(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path)
    ctx = _make_ctx()
    ctx.activated_tools.add("extract_archive")
    files = mgr.user_files_dir(WS)
    _make_repo_tar(files / "repo-main.tar.gz")

    result = await reg.dispatch("extract_archive", {"path": "repo-main.tar.gz"}, ctx)
    payload = json.loads(result)

    assert payload["dest"] == "repo-main"
    assert payload["entries"] == 2
    assert payload["stripped_top_level"] == "repo-abc"
    assert (files / "repo-main" / "README.md").read_bytes() == b"hello"
    assert (files / "repo-main" / "src" / "app.py").read_bytes() == b"print(1)"
    assert ctx.output_files == []


@pytest.mark.asyncio
async def test_extract_archive_dest_collision(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path)
    ctx = _make_ctx()
    ctx.activated_tools.add("extract_archive")
    files = mgr.user_files_dir(WS)
    _make_repo_tar(files / "repo-main.tar.gz")
    (files / "repo-main").mkdir()

    result = await reg.dispatch("extract_archive", {"path": "repo-main.tar.gz"}, ctx)
    assert "already exists" in json.loads(result)["error"]


@pytest.mark.asyncio
async def test_extract_archive_unsupported_ext(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path)
    ctx = _make_ctx()
    ctx.activated_tools.add("extract_archive")
    (mgr.user_files_dir(WS) / "data.rar").write_bytes(b"x")
    result = await reg.dispatch("extract_archive", {"path": "data.rar"}, ctx)
    assert "error" in json.loads(result)


@pytest.mark.asyncio
async def test_extract_archive_partial_cleanup(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path)
    ctx = _make_ctx()
    ctx.activated_tools.add("extract_archive")
    files = mgr.user_files_dir(WS)
    # Good file is written first, then a hardlink member is refused mid-extraction
    # (after dest is created and ok.txt is written), so the handler must remove the
    # partial dest. A traversal member would be caught pre-mkdir and would not test
    # cleanup at all.
    with tarfile.open(files / "bad.tar.gz", "w:gz") as tf:
        good = tarfile.TarInfo("ok.txt")
        good.size = 2
        tf.addfile(good, io.BytesIO(b"ok"))
        link = tarfile.TarInfo("hard")
        link.type = tarfile.LNKTYPE
        link.linkname = "ok.txt"
        tf.addfile(link)

    result = await reg.dispatch("extract_archive", {"path": "bad.tar.gz"}, ctx)
    assert "error" in json.loads(result)
    assert not (files / "bad").exists()


@pytest.mark.asyncio
@_requires_symlinks
async def test_multi_edit_rejects_symlink_target(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path)
    ctx = _make_ctx()
    root = mgr.user_files_dir(WS)
    real = root / "real.txt"
    real.write_text("keep", encoding="utf-8")
    (root / "link.txt").symlink_to(real)

    result = await reg.dispatch(
        "multi_edit",
        {"path": "link.txt", "edits": [{"old_string": "keep", "new_string": "x"}]},
        ctx,
    )
    assert "error" in json.loads(result)
    assert real.read_text(encoding="utf-8") == "keep"
