"""Direct tests for ``tools/workspace/files.py:apply_multi_edit_to_file``.

The rest of the workspace file tools are exercised through registry dispatch in
``test_workspace_tools.py``. This function is worth reaching directly because it
is the one synchronous read-modify-write in the package: it runs off the event
loop under the per-user lock, and its contract is that *any* rejected hunk
leaves the file byte-identical. A partial write here is silent data loss, so the
abort paths are asserted against file contents, not just return values.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from workspace import WorkspaceKey, WorkspaceManager
from tools.workspace.config import WorkspaceToolConfig
from tools.workspace.files import apply_multi_edit_to_file

USER = WorkspaceKey("user__guild")


@pytest.fixture
def workspace(tmp_path: Path) -> WorkspaceManager:
    manager = WorkspaceManager(tmp_path / "workspaces", max_size_bytes=10_000_000)
    manager.user_files_dir(USER).mkdir(parents=True, exist_ok=True)
    return manager


def _write(workspace: WorkspaceManager, name: str, text: str) -> Path:
    path = workspace.user_files_dir(USER) / name
    path.write_text(text, encoding="utf-8")
    return path


def _apply(
    workspace: WorkspaceManager,
    path: Path,
    parsed: list[tuple[str, str, bool]],
    *,
    config: WorkspaceToolConfig | None = None,
) -> dict[str, object]:
    return apply_multi_edit_to_file(
        path,
        path.name,
        parsed,
        config or WorkspaceToolConfig(),
        workspace_manager=workspace,
        workspace_key=USER,
    )


def test_edits_apply_in_order_each_seeing_the_previous_result(
    workspace: WorkspaceManager,
) -> None:
    path = _write(workspace, "a.txt", "one two three")

    result = _apply(
        workspace,
        path,
        [("one", "1", False), ("1 two", "1 2", False), ("three", "3", False)],
    )

    assert result["replacements"] == 3
    assert result["edits"] == 3
    assert path.read_text(encoding="utf-8") == "1 2 3"


def test_replace_all_counts_every_occurrence(workspace: WorkspaceManager) -> None:
    path = _write(workspace, "a.txt", "x x x")

    result = _apply(workspace, path, [("x", "y", True)])

    assert result["replacements"] == 3
    assert path.read_text(encoding="utf-8") == "y y y"


def test_missing_hunk_aborts_without_writing_earlier_edits(
    workspace: WorkspaceManager,
) -> None:
    """The whole call is atomic: edit 1 must not survive edit 2's failure."""
    path = _write(workspace, "a.txt", "keep me")

    result = _apply(workspace, path, [("keep", "kept", False), ("absent", "x", False)])

    assert "edit 2" in str(result["error"])
    assert path.read_text(encoding="utf-8") == "keep me"


def test_ambiguous_hunk_is_refused_unless_replace_all(
    workspace: WorkspaceManager,
) -> None:
    path = _write(workspace, "a.txt", "dup dup")

    result = _apply(workspace, path, [("dup", "x", False)])

    assert "found 2 times" in str(result["error"])
    assert path.read_text(encoding="utf-8") == "dup dup"


def test_projected_growth_is_rejected_before_the_buffer_is_built(
    workspace: WorkspaceManager,
) -> None:
    """Each edit's projected size is bounded, so chained replace_all cannot
    amplify the working buffer past the limit before the final size check."""
    path = _write(workspace, "a.txt", "aaaa")
    config = WorkspaceToolConfig(max_file_bytes=16)

    result = _apply(workspace, path, [("a", "bbbbb", True)], config=config)

    assert "would exceed" in str(result["error"])
    assert path.read_text(encoding="utf-8") == "aaaa"


def test_shrinking_edit_within_the_limit_still_applies(
    workspace: WorkspaceManager,
) -> None:
    path = _write(workspace, "a.txt", "aaaabbbb")
    config = WorkspaceToolConfig(max_file_bytes=16)

    result = _apply(workspace, path, [("aaaa", "c", False)], config=config)

    assert result["size_bytes"] == 5
    assert path.read_text(encoding="utf-8") == "cbbbb"


def test_oversized_file_is_refused_before_reading(workspace: WorkspaceManager) -> None:
    path = _write(workspace, "a.txt", "x" * 100)

    result = _apply(
        workspace, path, [("x", "y", False)], config=WorkspaceToolConfig(max_file_bytes=10)
    )

    assert "edit limit" in str(result["error"])
    assert path.read_text(encoding="utf-8") == "x" * 100


def test_binary_content_is_refused(workspace: WorkspaceManager) -> None:
    path = workspace.user_files_dir(USER) / "a.bin"
    path.write_bytes(b"text\x00more")

    result = _apply(workspace, path, [("text", "x", False)])

    assert "not a text file" in str(result["error"])
    assert path.read_bytes() == b"text\x00more"


def test_invalid_utf8_is_refused(workspace: WorkspaceManager) -> None:
    path = workspace.user_files_dir(USER) / "a.txt"
    path.write_bytes(b"\xff\xfe not utf8")

    result = _apply(workspace, path, [("not", "x", False)])

    assert "not a text file" in str(result["error"])


def test_symlink_target_is_refused_at_write_time(
    workspace: WorkspaceManager, tmp_path: Path
) -> None:
    """TOCTOU guard: the is_symlink re-check happens here, immediately before the
    read, so a path swapped after resolution is still refused."""
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = workspace.user_files_dir(USER) / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError, NotImplementedError:
        pytest.skip("symlink creation is not permitted in this environment")

    result = _apply(workspace, link, [("secret", "leaked", False)])

    assert result == {"error": "path is not a file"}
    assert outside.read_text(encoding="utf-8") == "secret"


def test_missing_file_is_refused(workspace: WorkspaceManager) -> None:
    result = _apply(workspace, workspace.user_files_dir(USER) / "nope.txt", [("a", "b", False)])

    assert result == {"error": "path is not a file"}


def test_exceeding_the_user_quota_leaves_the_file_untouched(
    workspace: WorkspaceManager,
) -> None:
    path = _write(workspace, "a.txt", "small")

    result = _apply(
        workspace,
        path,
        [("small", "much longer replacement", False)],
        config=WorkspaceToolConfig(max_user_bytes=8),
    )

    assert "error" in result
    assert path.read_text(encoding="utf-8") == "small"


def test_empty_edit_list_rewrites_identical_content(workspace: WorkspaceManager) -> None:
    """No hunks is a degenerate but valid call; it must not corrupt the file."""
    path = _write(workspace, "a.txt", "unchanged")

    result = _apply(workspace, path, [])

    assert result["edits"] == 0
    assert result["replacements"] == 0
    assert path.read_text(encoding="utf-8") == "unchanged"
