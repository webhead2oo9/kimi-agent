"""Exercises tools/code_exec.py: the run_code tool surface as the model sees
it, including permission checks and workspace-artifact staging. Sits above
sandbox/runner.py; a change to the tool's argument handling should not
require touching the sandbox command itself.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import struct
import sys
import tempfile
import tracemalloc
from datetime import UTC, datetime
from pathlib import Path

import pytest

import app.tools as app_tools
import workspace.manager as workspace
from config.settings import Settings
import tools.code_exec as code_exec
from workspace.manager import WorkspaceKey, WorkspaceManager, workspace_owner_key
from sandbox.netns_lease import NetnsLease, NetnsLeasePoisonedError
from sandbox.runner import (
    SandboxConfig,
    SandboxResult,
    SandboxTeardownError,
    sandbox_available,
)
from storage.usage import UsageMarker
from tools.code_exec import MAX_AUTO_ATTACH_CHANGED_FILES, init_code_exec_tool
from tools.registry import MessageContext, ToolRegistry
from tools.workspace.common import UserLocks
from trust.tiers import TrustTier

_REAL = sandbox_available(SandboxConfig())
_requires_sandbox = pytest.mark.skipif(not _REAL, reason="bwrap/prlimit not available")


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
_requires_posix = pytest.mark.skipif(sys.platform == "win32", reason="POSIX execute-bit semantics")
_requires_dir_fd = pytest.mark.skipif(
    not os.supports_dir_fd,
    reason="owned-tree removal is fd-relative (dir_fd/O_DIRECTORY), POSIX only",
)

OWNER = "owner1"
# Workspaces are keyed per (user, guild); _ctx runs as OWNER in guild "g1", so
# the on-disk workspace lives under this composite owner key.
WS = workspace_owner_key(OWNER, "g1")


def _register(
    tmp_path: Path,
    *,
    max_auto_attachments: int = 5,
    max_auto_attachment_bytes: int = 8 * 1024 * 1024,
) -> tuple[ToolRegistry, WorkspaceManager]:
    reg = ToolRegistry(owner_user_id=OWNER)
    mgr = WorkspaceManager(base_dir=tmp_path)
    init_code_exec_tool(
        reg,
        mgr,
        SandboxConfig(),
        locks=UserLocks(),
        max_auto_attachments=max_auto_attachments,
        max_auto_attachment_bytes=max_auto_attachment_bytes,
    )
    return reg, mgr


def _ctx(user_id: str = OWNER, tier: TrustTier = TrustTier.STAFF) -> MessageContext:
    return MessageContext(
        user_id=user_id,
        user_name="t",
        guild_id="g1",
        channel_id="c1",
        thread_id=None,
        trust_tier=tier,
    )


@pytest.mark.asyncio
async def test_run_code_is_member_tier(tmp_path: Path) -> None:
    reg, _mgr = _register(tmp_path)

    member_schemas = reg.get_tool_schemas(TrustTier.MEMBER, set(), "someuser")
    member_names = [schema["name"] for schema in member_schemas]
    assert "run_code" in member_names

    result = await reg.dispatch("run_code", {}, _ctx("someuser", TrustTier.MEMBER))
    assert json.loads(result) == {"error": "pass at least one of code, path, or pip_install"}


@pytest.mark.asyncio
async def test_run_code_requires_path_or_code(tmp_path: Path) -> None:
    reg, _ = _register(tmp_path)
    result = await reg.dispatch("run_code", {}, _ctx())
    assert json.loads(result) == {"error": "pass at least one of code, path, or pip_install"}


@pytest.mark.asyncio
@_requires_symlinks
async def test_run_code_rejects_symlink(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path)
    root = mgr.user_files_dir(WS)
    (root / "real.py").write_text("print(1)", encoding="utf-8")
    (root / "link.py").symlink_to(root / "real.py")
    result = await reg.dispatch("run_code", {"path": "link.py"}, _ctx())
    assert "error" in json.loads(result)


@pytest.mark.asyncio
async def test_run_code_rejects_oversize_stdin(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path)
    (mgr.user_files_dir(WS) / "x.py").write_text("print(1)", encoding="utf-8")
    result = await reg.dispatch("run_code", {"path": "x.py", "stdin": "a" * 200_001}, _ctx())
    assert "stdin exceeds" in json.loads(result)["error"]


@pytest.mark.asyncio
@_requires_posix
async def test_run_code_auto_direct_sets_execute_bit_and_passes_argv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reg, mgr = _register(tmp_path)
    root = mgr.user_files_dir(WS)
    script = root / "tool"
    script.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    script.chmod(0o600)

    async def fake_run(
        config: SandboxConfig,
        workspace_dir: Path,
        file_path: Path,
        *,
        stdin: str | None = None,
        mode: str = "direct",
        argv: tuple[str, ...] = (),
    ) -> SandboxResult:
        del config, workspace_dir
        assert file_path == script
        assert stdin == "input"
        assert mode == "direct"
        assert argv == ("one", "two")
        assert script.stat().st_mode & stat.S_IXUSR
        return SandboxResult(
            exit_code=0,
            stdout="ok\n",
            stderr="",
            timed_out=False,
            duration_ms=3,
        )

    monkeypatch.setattr("tools.code_exec.run_workspace_file_in_sandbox", fake_run)

    result = await reg.dispatch(
        "run_code",
        {"path": "tool", "argv": ["one", "two"], "stdin": "input"},
        _ctx(),
    )

    body = json.loads(result)
    assert body["path"] == "tool"
    assert body["mode"] == "direct"
    assert body["exit_code"] == 0
    assert body["stdout"] == "ok\n"


@pytest.mark.asyncio
async def test_run_code_auto_shell_for_sh_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reg, mgr = _register(tmp_path)
    root = mgr.user_files_dir(WS)
    script = root / "hello.sh"
    script.write_text("echo hi", encoding="utf-8")

    async def fake_run(
        config: SandboxConfig,
        workspace_dir: Path,
        file_path: Path,
        *,
        stdin: str | None = None,
        mode: str = "direct",
        argv: tuple[str, ...] = (),
    ) -> SandboxResult:
        del config, workspace_dir, file_path, stdin, argv
        assert mode == "shell"
        assert not (script.stat().st_mode & stat.S_IXUSR)
        return SandboxResult(
            exit_code=0,
            stdout="hi\n",
            stderr="",
            timed_out=False,
            duration_ms=2,
        )

    monkeypatch.setattr("tools.code_exec.run_workspace_file_in_sandbox", fake_run)

    result = await reg.dispatch("run_code", {"path": "hello.sh"}, _ctx())

    body = json.loads(result)
    assert body["mode"] == "shell"
    assert body["stdout"] == "hi\n"


@pytest.mark.asyncio
async def test_run_code_rejects_bad_argv(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path)
    (mgr.user_files_dir(WS) / "x.py").write_text("print(1)", encoding="utf-8")

    result = await reg.dispatch("run_code", {"path": "x.py", "argv": ["ok", 1]}, _ctx())

    assert json.loads(result) == {"error": "argv must be an array of strings"}


@pytest.mark.asyncio
async def test_run_code_reports_and_queues_created_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reg, mgr = _register(tmp_path)
    root = mgr.user_files_dir(WS)
    (root / "make_plot.py").write_text("print('make plot')", encoding="utf-8")

    async def fake_run(
        config: SandboxConfig,
        workspace_dir: Path,
        script_path: Path,
        *,
        stdin: str | None = None,
        argv: tuple[str, ...] = (),
    ) -> SandboxResult:
        assert argv == ()
        assert script_path.name == "make_plot.py"
        assert stdin is None
        (workspace_dir / "plot.png").write_bytes(b"png")
        return SandboxResult(
            exit_code=0,
            stdout="done\n",
            stderr="",
            timed_out=False,
            duration_ms=7,
        )

    monkeypatch.setattr("tools.code_exec.run_python_in_sandbox", fake_run)
    ctx = _ctx()

    result = await reg.dispatch("run_code", {"path": "make_plot.py"}, ctx)

    body = json.loads(result)
    assert "changed_file_count" in body, body
    assert body["changed_file_count"] == 1
    assert body["changed_files_truncated"] is False
    assert body["changed_files"] == [
        {
            "path": "plot.png",
            "status": "created",
            "size_bytes": 3,
            "queued": True,
        }
    ]
    assert body["attached_files"] == ["plot.png"]
    assert ctx.output_files == [str((root / "plot.png").resolve())]


def test_workspace_snapshot_prunes_environment_descendants(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, mgr = _register(tmp_path)
    root = mgr.user_files_dir(WS)
    env = root / ".venv"
    env.mkdir()
    (env / "hidden").mkdir()
    (env / "hidden" / "package.py").write_text("pass", encoding="utf-8")
    ordinary = root / "src"
    ordinary.mkdir()
    (ordinary / "main.py").write_text("pass", encoding="utf-8")
    visited: list[Path] = []
    real_scandir = os.scandir

    def tracking_scandir(path: str | os.PathLike[str]):
        visited.append(Path(path))
        return real_scandir(path)

    monkeypatch.setattr(code_exec.os, "scandir", tracking_scandir)

    snapshot, complete = code_exec._snapshot_workspace(
        mgr,
        WS,
        max_workspace_files=100,
        max_env_roots=100,
    )

    assert complete is True
    assert snapshot[".venv"].kind == "env_root"
    assert "src/main.py" in snapshot
    assert env not in visited
    assert not any(path.is_relative_to(env) for path in visited)


def test_workspace_snapshot_bounds_ordinary_entries_without_unsafe_cleanup(
    tmp_path: Path,
) -> None:
    _, mgr = _register(tmp_path)
    root = mgr.user_files_dir(WS)
    for index in range(20):
        (root / f"{index}.txt").touch()

    snapshot, complete = code_exec._snapshot_workspace(
        mgr,
        WS,
        max_workspace_files=10,
        max_env_roots=10,
    )
    cleanup = code_exec._cleanup_quota_created_entries(
        mgr,
        WS,
        snapshot,
        remove_new_ordinary=complete,
    )

    assert complete is False
    assert len(snapshot) <= 10
    assert cleanup.removed_entries == 0
    assert len(list(root.iterdir())) == 20


@_requires_dir_fd
def test_mode_000_snapshot_error_preserves_data_but_allows_env_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, mgr = _register(tmp_path)
    root = mgr.user_files_dir(WS)
    protected = root / "protected"
    protected.mkdir()
    preexisting = protected / "keep.txt"
    preexisting.write_text("keep", encoding="utf-8")
    protected.chmod(0)
    env = root / ".venv"
    env.mkdir()
    (env / "package.py").write_text("regenerable", encoding="utf-8")
    real_scandir = os.scandir

    def fail_protected_scan(path: str | os.PathLike[str]):
        if Path(path) == protected:
            raise PermissionError("simulated mode-000 directory")
        return real_scandir(path)

    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(code_exec.os, "scandir", fail_protected_scan)
            before, complete = code_exec._snapshot_workspace(
                mgr,
                WS,
                max_workspace_files=100,
                max_env_roots=100,
            )
    finally:
        protected.chmod(0o700)

    assert complete is False
    created_after_snapshot = root / "created-after-snapshot.txt"
    created_after_snapshot.write_text("new", encoding="utf-8")

    cleanup = code_exec._cleanup_quota_created_entries(
        mgr,
        WS,
        before,
        remove_preexisting_envs=True,
        remove_new_ordinary=complete,
    )

    assert preexisting.read_text(encoding="utf-8") == "keep"
    assert created_after_snapshot.read_text(encoding="utf-8") == "new"
    assert not env.exists()
    assert cleanup.removed_env_dirs == 1


def test_workspace_snapshot_stat_error_marks_snapshot_incomplete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, mgr = _register(tmp_path)
    root = mgr.user_files_dir(WS)
    preexisting = root / "keep.txt"
    preexisting.write_text("keep", encoding="utf-8")
    real_scandir = os.scandir

    class FailingStatEntry:
        name = preexisting.name
        path = os.fspath(preexisting)

        def stat(self, *, follow_symlinks: bool = True) -> os.stat_result:
            del follow_symlinks
            raise PermissionError("simulated stat failure")

    class FailingStatScan:
        def __enter__(self) -> FailingStatScan:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def __iter__(self):
            return iter((FailingStatEntry(),))

    def fail_root_stat(path: str | os.PathLike[str]):
        if Path(path) == root:
            return FailingStatScan()
        return real_scandir(path)

    monkeypatch.setattr(code_exec.os, "scandir", fail_root_stat)

    snapshot, complete = code_exec._snapshot_workspace(
        mgr,
        WS,
        max_workspace_files=100,
        max_env_roots=100,
    )

    assert snapshot == {}
    assert complete is False


@_requires_dir_fd
def test_incomplete_snapshot_env_cleanup_does_not_walk_ordinary_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, mgr = _register(tmp_path)
    root = mgr.user_files_dir(WS)
    ordinary = root / "ordinary" / "deep"
    ordinary.mkdir(parents=True)
    (ordinary / "keep.txt").write_text("keep", encoding="utf-8")
    env = root / ".venv"
    env.mkdir()
    (env / "package.py").write_text("pass", encoding="utf-8")
    before, complete = code_exec._snapshot_workspace(
        mgr,
        WS,
        max_workspace_files=100,
        max_env_roots=100,
    )
    assert complete is True
    real_scandir = os.scandir

    def reject_root_walk(path):
        if isinstance(path, (str, os.PathLike)) and Path(path) == root:
            raise AssertionError("ordinary root traversal is not bounded")
        return real_scandir(path)

    monkeypatch.setattr(code_exec.os, "scandir", reject_root_walk)

    cleanup = code_exec._cleanup_quota_created_entries(
        mgr,
        WS,
        before,
        remove_preexisting_envs=True,
        remove_new_ordinary=False,
    )

    assert cleanup.removed_env_dirs == 1
    assert not env.exists()
    assert (ordinary / "keep.txt").exists()


@_requires_posix
def test_owned_directory_open_rejects_real_directory_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    target = parent / "target"
    target.mkdir()
    (target / "original.txt").write_text("original", encoding="utf-8")
    replacement = parent / "replacement"
    replacement.mkdir()
    (replacement / "keep.txt").write_text("replacement", encoding="utf-8")
    moved_original = parent / "moved-original"
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    real_open = workspace.os.open
    substituted = False

    def substitute_on_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal substituted
        if path == target.name and dir_fd == parent_fd and not substituted:
            substituted = True
            os.rename(
                target.name,
                moved_original.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            os.rename(
                replacement.name,
                target.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
        return real_open(path, flags, mode, dir_fd=dir_fd)

    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(workspace.os, "open", substitute_on_open)
            with pytest.raises(OSError, match="identity changed while opening"):
                workspace.open_owned_directory_at(parent_fd, target.name)
    finally:
        os.close(parent_fd)

    assert substituted is True
    assert (target / "keep.txt").read_text(encoding="utf-8") == "replacement"
    assert (moved_original / "original.txt").read_text(encoding="utf-8") == "original"


@_requires_posix
def test_owned_tree_child_rmdir_rejects_real_directory_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    child = root / "child"
    child.mkdir(parents=True)
    payload = child / "payload.txt"
    payload.write_text("removed before the substitution", encoding="utf-8")
    payload_size = payload.lstat().st_size
    replacement = tmp_path / "replacement-child"
    replacement.mkdir()
    (replacement / "keep.txt").write_text("replacement", encoding="utf-8")
    moved_original = tmp_path / "moved-original-child"
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    real_verify = workspace._verify_owned_directory_at
    substituted = False

    def substitute_before_verify(
        directory_fd: int,
        name: str,
        expected_stat: os.stat_result,
    ) -> None:
        nonlocal substituted
        if name == child.name and not substituted:
            substituted = True
            os.rename(name, moved_original, src_dir_fd=directory_fd)
            os.rename(replacement, name, dst_dir_fd=directory_fd)
        real_verify(directory_fd, name, expected_stat)

    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(
                workspace,
                "_verify_owned_directory_at",
                substitute_before_verify,
            )
            with pytest.raises(workspace.OwnedTreeRemovalError) as caught:
                workspace.remove_owned_tree_at(parent_fd, root.name)
    finally:
        os.close(parent_fd)

    assert substituted is True
    assert caught.value.removal == workspace.OwnedTreeRemoval(1, payload_size)
    assert (root / child.name / "keep.txt").read_text(encoding="utf-8") == "replacement"
    assert moved_original.is_dir()


@_requires_posix
def test_owned_tree_root_rmdir_rejects_real_directory_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    payload = root / "payload.txt"
    payload.write_text("removed before the substitution", encoding="utf-8")
    payload_size = payload.lstat().st_size
    replacement = tmp_path / "replacement-root"
    replacement.mkdir()
    (replacement / "keep.txt").write_text("replacement", encoding="utf-8")
    moved_original = tmp_path / "moved-original-root"
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    real_verify = workspace._verify_owned_directory_at
    substituted = False

    def substitute_before_verify(
        directory_fd: int,
        name: str,
        expected_stat: os.stat_result,
    ) -> None:
        nonlocal substituted
        if name == root.name and not substituted:
            substituted = True
            os.rename(
                name,
                moved_original.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            os.rename(
                replacement.name,
                name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
        real_verify(directory_fd, name, expected_stat)

    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(
                workspace,
                "_verify_owned_directory_at",
                substitute_before_verify,
            )
            with pytest.raises(workspace.OwnedTreeRemovalError) as caught:
                workspace.remove_owned_tree_at(parent_fd, root.name)
    finally:
        os.close(parent_fd)

    assert substituted is True
    assert caught.value.removal == workspace.OwnedTreeRemoval(1, payload_size)
    assert (root / "keep.txt").read_text(encoding="utf-8") == "replacement"
    assert moved_original.is_dir()


@_requires_posix
def test_quota_cleanup_accounts_only_entries_removed_before_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, mgr = _register(tmp_path)
    root = mgr.user_files_dir(WS)
    replacement = root / "preexisting-replacement"
    replacement.mkdir()
    (replacement / "keep.txt").write_text("replacement", encoding="utf-8")
    before, complete = code_exec._snapshot_workspace(
        mgr,
        WS,
        max_workspace_files=100,
        max_env_roots=100,
    )
    assert complete is True
    created = root / "created"
    created.mkdir()
    payload = created / "payload.txt"
    payload.write_text("removed before the substitution", encoding="utf-8")
    payload_size = payload.lstat().st_size
    moved_original = tmp_path / "moved-created"
    real_verify = workspace._verify_owned_directory_at
    substituted = False

    def substitute_before_verify(
        directory_fd: int,
        name: str,
        expected_stat: os.stat_result,
    ) -> None:
        nonlocal substituted
        if name == created.name and not substituted:
            substituted = True
            os.rename(name, moved_original, src_dir_fd=directory_fd)
            os.rename(replacement, name, dst_dir_fd=directory_fd)
        real_verify(directory_fd, name, expected_stat)

    with monkeypatch.context() as scoped:
        scoped.setattr(
            workspace,
            "_verify_owned_directory_at",
            substitute_before_verify,
        )
        cleanup = code_exec._cleanup_quota_created_entries(mgr, WS, before)

    assert substituted is True
    assert cleanup.removed_entries == 1
    assert cleanup.removed_bytes == payload_size
    assert (created / "keep.txt").read_text(encoding="utf-8") == "replacement"
    assert moved_original.is_dir()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX PATH_MAX permits this depth")
def test_quota_cleanup_handles_attacker_controlled_depth(tmp_path: Path) -> None:
    _, mgr = _register(tmp_path)
    root = mgr.user_files_dir(WS)
    before, complete = code_exec._snapshot_workspace(
        mgr,
        WS,
        max_workspace_files=10_000,
        max_env_roots=100,
    )
    assert complete is True
    top = root / "d"
    top.mkdir()
    directory_fd = os.open(top, os.O_RDONLY | os.O_DIRECTORY)
    depth = 2_300
    deepest_path_length = len(os.fsencode(top)) + (depth - 1) * 2 + len(b"/payload")
    assert deepest_path_length > os.pathconf(root, "PC_PATH_MAX")
    try:
        for _ in range(depth - 1):
            os.mkdir("d", dir_fd=directory_fd)
            child_fd = os.open(
                "d",
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            os.close(directory_fd)
            directory_fd = child_fd
        payload_fd = os.open(
            "payload",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=directory_fd,
        )
        os.close(payload_fd)
    finally:
        os.close(directory_fd)

    tracemalloc.start()
    try:
        cleanup = code_exec._cleanup_quota_created_entries(mgr, WS, before)
        _, peak_traced_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert not top.exists()
    assert cleanup.removed_entries == depth + 1
    # A pending full-path tuple at every depth retains at least this many pointer
    # slots. Keep a generous linear per-frame allowance while proving the traversal
    # did not retain that quadratic representation.
    quadratic_path_tuple_floor = struct.calcsize("P") * depth * (depth - 1) // 2
    linear_memory_budget = min(depth * 4_096, quadratic_path_tuple_floor // 2)
    assert peak_traced_bytes < linear_memory_budget


@_requires_dir_fd
@pytest.mark.asyncio
async def test_quota_violation_prunes_only_paths_created_by_the_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reg, mgr = _register(tmp_path)
    root = mgr.user_files_dir(WS)
    (root / "quota.py").write_text("pass", encoding="utf-8")
    existing = root / "keep.txt"
    existing.write_text("before", encoding="utf-8")
    protected_nested_env = root / ".pio" / ".venv"
    protected_nested_env.mkdir(parents=True)
    (protected_nested_env / "keep.bin").write_bytes(b"before")
    expected_removed_bytes = 0

    async def fake_run(
        config: SandboxConfig,
        workspace_dir: Path,
        script_path: Path,
        *,
        stdin: str | None = None,
        argv: tuple[str, ...] = (),
    ) -> SandboxResult:
        nonlocal expected_removed_bytes
        del config, script_path, stdin, argv
        existing.write_text("modified", encoding="utf-8")
        oversize = workspace_dir / "oversize.bin"
        oversize.write_bytes(b"x" * 1024)
        new_env = workspace_dir / ".venv"
        new_env.mkdir()
        package = new_env / "package.bin"
        package.write_bytes(b"x" * 1024)
        (protected_nested_env / "new.bin").write_bytes(b"x" * 1024)
        # Cleanup statistics count an environment root but deliberately avoid a
        # second traversal of its descendants before the tree remover runs.
        expected_removed_bytes = sum(path.lstat().st_size for path in (oversize, new_env))
        return SandboxResult(
            exit_code=1,
            stdout="",
            stderr="Execution stopped because the workspace quota was exceeded.",
            timed_out=False,
            duration_ms=1,
            quota_exceeded=True,
        )

    monkeypatch.setattr("tools.code_exec.run_python_in_sandbox", fake_run)

    body = json.loads(await reg.dispatch("run_code", {"path": "quota.py"}, _ctx()))

    assert not (root / "oversize.bin").exists()
    assert not (root / ".venv").exists()
    assert existing.read_text(encoding="utf-8") == "modified"
    assert (protected_nested_env / "keep.bin").read_bytes() == b"before"
    assert (protected_nested_env / "new.bin").exists()
    assert body["quota_exceeded"] is True
    assert body["quota_cleanup"]["removed_entries"] == 2
    assert body["quota_cleanup"]["removed_bytes"] == expected_removed_bytes
    assert body["quota_cleanup"]["removed_env_dirs"] == 1
    assert body["quota_cleanup"]["retained_preexisting_changes"] == 1
    assert [item["path"] for item in body["changed_files"]] == ["keep.txt"]


@_requires_dir_fd
@pytest.mark.asyncio
async def test_environment_quota_violation_removes_preexisting_env_trees(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reg, mgr = _register(tmp_path)
    root = mgr.user_files_dir(WS)
    (root / "quota.py").write_text("pass", encoding="utf-8")
    existing = root / "keep.txt"
    existing.write_text("before", encoding="utf-8")
    for env_name in (".venv", ".pio"):
        env = root / env_name
        env.mkdir()
        (env / "empty").touch()

    async def fake_run(
        config: SandboxConfig,
        workspace_dir: Path,
        script_path: Path,
        *,
        stdin: str | None = None,
        argv: tuple[str, ...] = (),
    ) -> SandboxResult:
        del config, workspace_dir, script_path, stdin, argv
        existing.write_text("modified", encoding="utf-8")
        return SandboxResult(
            exit_code=1,
            stdout="",
            stderr="Environment entry quota exceeded.",
            timed_out=False,
            duration_ms=1,
            quota_exceeded=True,
            environment_quota_exceeded=True,
        )

    monkeypatch.setattr("tools.code_exec.run_python_in_sandbox", fake_run)

    body = json.loads(await reg.dispatch("run_code", {"path": "quota.py"}, _ctx()))

    assert not (root / ".venv").exists()
    assert not (root / ".pio").exists()
    assert existing.read_text(encoding="utf-8") == "modified"
    assert body["quota_cleanup"]["removed_env_dirs"] == 2
    assert "regenerable .venv/.pio trees were removed" in body["quota_cleanup"]["note"]


@pytest.mark.asyncio
async def test_queued_run_snapshots_workspace_only_after_semaphore_acquisition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reg, mgr = _register(tmp_path)
    root = mgr.user_files_dir(WS)
    (root / "first.py").write_text("pass", encoding="utf-8")
    (root / "second.py").write_text("pass", encoding="utf-8")
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    async def fake_run(
        config: SandboxConfig,
        workspace_dir: Path,
        script_path: Path,
        *,
        stdin: str | None = None,
        argv: tuple[str, ...] = (),
    ) -> SandboxResult:
        del config, stdin, argv
        if script_path.name == "first.py":
            first_started.set()
            await release_first.wait()
        (workspace_dir / f"{script_path.stem}.out").write_text("done", encoding="utf-8")
        return SandboxResult(0, "", "", False, 1)

    monkeypatch.setattr("tools.code_exec.run_python_in_sandbox", fake_run)

    first = asyncio.create_task(reg.dispatch("run_code", {"path": "first.py"}, _ctx()))
    await first_started.wait()
    second = asyncio.create_task(reg.dispatch("run_code", {"path": "second.py"}, _ctx()))
    await asyncio.sleep(0)
    release_first.set()

    first_result, second_result = await asyncio.gather(first, second)

    assert [row["path"] for row in json.loads(first_result)["changed_files"]] == ["first.out"]
    assert [row["path"] for row in json.loads(second_result)["changed_files"]] == ["second.out"]


@pytest.mark.asyncio
async def test_run_code_bulk_change_skips_auto_attach(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reg, mgr = _register(tmp_path)
    root = mgr.user_files_dir(WS)
    (root / "extract.py").write_text("print('extract')", encoding="utf-8")
    count = MAX_AUTO_ATTACH_CHANGED_FILES + 1

    async def fake_run(
        config: SandboxConfig,
        workspace_dir: Path,
        script_path: Path,
        *,
        stdin: str | None = None,
        argv: tuple[str, ...] = (),
    ) -> SandboxResult:
        del config, script_path, stdin, argv
        for i in range(count):
            (workspace_dir / f"file{i:02d}.txt").write_text("x", encoding="utf-8")
        return SandboxResult(0, "done\n", "", False, 7)

    monkeypatch.setattr("tools.code_exec.run_python_in_sandbox", fake_run)
    ctx = _ctx()

    result = await reg.dispatch("run_code", {"path": "extract.py"}, ctx)

    body = json.loads(result)
    assert body["changed_file_count"] == count
    assert body["attached_files"] == []
    assert ctx.output_files == []
    assert f"{count} files changed" in body["auto_attach"]
    assert "queue_file" in body["auto_attach"]
    assert all(item["queued"] is False for item in body["changed_files"])


@pytest.mark.asyncio
async def test_run_code_at_bulk_threshold_still_auto_attaches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reg, mgr = _register(tmp_path)
    root = mgr.user_files_dir(WS)
    (root / "make.py").write_text("print('make')", encoding="utf-8")

    async def fake_run(
        config: SandboxConfig,
        workspace_dir: Path,
        script_path: Path,
        *,
        stdin: str | None = None,
        argv: tuple[str, ...] = (),
    ) -> SandboxResult:
        del config, script_path, stdin, argv
        for i in range(MAX_AUTO_ATTACH_CHANGED_FILES):
            (workspace_dir / f"file{i:02d}.txt").write_text("x", encoding="utf-8")
        return SandboxResult(0, "done\n", "", False, 7)

    monkeypatch.setattr("tools.code_exec.run_python_in_sandbox", fake_run)
    ctx = _ctx()

    result = await reg.dispatch("run_code", {"path": "make.py"}, ctx)

    body = json.loads(result)
    assert body["changed_file_count"] == MAX_AUTO_ATTACH_CHANGED_FILES
    assert "auto_attach" not in body
    assert len(body["attached_files"]) == 5
    assert len(ctx.output_files) == 5


@pytest.mark.asyncio
async def test_run_code_skips_unreasonable_artifacts_without_consuming_attach_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reg, mgr = _register(
        tmp_path,
        max_auto_attachments=1,
        max_auto_attachment_bytes=4,
    )
    root = mgr.user_files_dir(WS)
    (root / "make_files.py").write_text("print('make files')", encoding="utf-8")

    async def fake_run(
        config: SandboxConfig,
        workspace_dir: Path,
        script_path: Path,
        *,
        stdin: str | None = None,
        argv: tuple[str, ...] = (),
    ) -> SandboxResult:
        del config, script_path, stdin, argv
        (workspace_dir / "__pycache__").mkdir()
        (workspace_dir / "__pycache__" / "make_files.cpython-311.pyc").write_bytes(b"pyc")
        (workspace_dir / "big.bin").write_bytes(b"12345")
        (workspace_dir / "small.txt").write_text("ok", encoding="utf-8")
        return SandboxResult(
            exit_code=0,
            stdout="done\n",
            stderr="",
            timed_out=False,
            duration_ms=7,
        )

    monkeypatch.setattr("tools.code_exec.run_python_in_sandbox", fake_run)
    ctx = _ctx()

    result = await reg.dispatch("run_code", {"path": "make_files.py"}, ctx)

    body = json.loads(result)
    assert body["changed_file_count"] == 3
    assert body["changed_files_truncated"] is False
    by_path = {item["path"]: item for item in body["changed_files"]}
    assert by_path["__pycache__/make_files.cpython-311.pyc"]["queue_skip_reason"] == (
        "python_cache"
    )
    assert by_path["big.bin"]["queue_skip_reason"] == "too_large"
    assert by_path["small.txt"]["queued"] is True
    assert body["attached_files"] == ["small.txt"]
    assert ctx.output_files == [str((root / "small.txt").resolve())]


@_requires_sandbox
@pytest.mark.asyncio
async def test_run_code_executes_and_reads_stdin(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path)
    (mgr.user_files_dir(WS) / "echo.py").write_text(
        "import sys; data = sys.stdin.read().strip(); print('got', data)",
        encoding="utf-8",
    )
    result = await reg.dispatch("run_code", {"path": "echo.py", "stdin": "ping"}, _ctx())
    body = json.loads(result)
    assert body["exit_code"] == 0
    assert body["timed_out"] is False
    assert "got ping" in body["stdout"]
    assert body["path"] == "echo.py"


@pytest.mark.asyncio
async def test_code_exec_holds_workspace_lock_during_run(tmp_path: Path, monkeypatch) -> None:
    """The sandbox run must hold the per-workspace lock so a script cannot swap a
    symlink into a concurrent write_file's path (resolve->write TOCTOU). Stubs the
    sandbox, so it runs without bwrap."""
    import tools.code_exec as code_exec_mod

    reg = ToolRegistry(owner_user_id=OWNER)
    mgr = WorkspaceManager(base_dir=tmp_path)
    locks = UserLocks()
    init_code_exec_tool(reg, mgr, SandboxConfig(), locks=locks)

    ws_dir = mgr.user_files_dir(WS)
    ws_dir.mkdir(parents=True, exist_ok=True)
    (ws_dir / "x.py").write_text("print('hi')\n", encoding="utf-8")

    observed: dict[str, bool] = {}

    async def fake_run(*args, **kwargs) -> SandboxResult:
        observed["locked_during_run"] = locks.for_user(WS).locked()
        return SandboxResult(exit_code=0, stdout="hi", stderr="", timed_out=False, duration_ms=1)

    monkeypatch.setattr(code_exec_mod, "run_python_in_sandbox", fake_run)

    result = json.loads(await reg.dispatch("run_code", {"path": "x.py"}, _ctx()))
    assert result["exit_code"] == 0
    assert observed["locked_during_run"] is True  # held across the untrusted run
    assert not locks.for_user(WS).locked()  # and released afterwards


@pytest.mark.asyncio
async def test_code_exec_run_serializes_against_workspace_writes(
    tmp_path: Path, monkeypatch
) -> None:
    """A file write on the same workspace cannot proceed while a script is running:
    the shared per-workspace lock makes them mutually exclusive."""
    import tools.code_exec as code_exec_mod
    from tools.workspace.registration import init_workspace_tools

    reg = ToolRegistry(owner_user_id=OWNER)
    mgr = WorkspaceManager(base_dir=tmp_path)
    locks = init_workspace_tools(reg, mgr)
    init_code_exec_tool(reg, mgr, SandboxConfig(), locks=locks)

    ws_dir = mgr.user_files_dir(WS)
    ws_dir.mkdir(parents=True, exist_ok=True)
    (ws_dir / "x.py").write_text("print('hi')\n", encoding="utf-8")

    order: list[str] = []
    release = asyncio.Event()

    async def fake_run(*args, **kwargs) -> SandboxResult:
        order.append("run-start")
        await release.wait()  # hold the lock until the test lets go
        order.append("run-end")
        return SandboxResult(exit_code=0, stdout="", stderr="", timed_out=False, duration_ms=1)

    monkeypatch.setattr(code_exec_mod, "run_python_in_sandbox", fake_run)

    run_task = asyncio.create_task(reg.dispatch("run_code", {"path": "x.py"}, _ctx()))
    await asyncio.sleep(0.05)  # let the run start and take the lock

    async def do_write() -> None:
        order.append("write-start")
        await reg.dispatch("write_file", {"path": "out.txt", "content": "hi"}, _ctx())
        order.append("write-done")

    write_task = asyncio.create_task(do_write())
    await asyncio.sleep(0.05)  # write should be blocked on the lock, not done
    assert order == ["run-start", "write-start"]  # write is waiting

    release.set()
    await asyncio.gather(run_task, write_task)
    # The write only completes after the run releases the lock.
    assert order == ["run-start", "write-start", "run-end", "write-done"]


@pytest.mark.asyncio
async def test_code_exec_run_serializes_against_workspace_reads(
    tmp_path: Path, monkeypatch
) -> None:
    """A second rooted turn cannot read while sandbox code can swap symlinks."""
    import tools.code_exec as code_exec_mod
    from tools.workspace.registration import init_workspace_tools

    reg = ToolRegistry(owner_user_id=OWNER)
    mgr = WorkspaceManager(base_dir=tmp_path)
    locks = init_workspace_tools(reg, mgr)
    init_code_exec_tool(reg, mgr, SandboxConfig(), locks=locks)
    ws_dir = mgr.user_files_dir(WS)
    ws_dir.mkdir(parents=True, exist_ok=True)
    (ws_dir / "x.py").write_text("print('hi')\n", encoding="utf-8")
    (ws_dir / "note.txt").write_text("safe", encoding="utf-8")
    release = asyncio.Event()

    async def fake_run(*args, **kwargs) -> SandboxResult:
        await release.wait()
        return SandboxResult(exit_code=0, stdout="", stderr="", timed_out=False, duration_ms=1)

    monkeypatch.setattr(code_exec_mod, "run_python_in_sandbox", fake_run)
    run_task = asyncio.create_task(reg.dispatch("run_code", {"path": "x.py"}, _ctx()))
    await asyncio.sleep(0.05)
    read_task = asyncio.create_task(reg.dispatch("read_file", {"path": "note.txt"}, _ctx()))
    await asyncio.sleep(0.05)
    assert not read_task.done()

    release.set()
    await run_task
    assert "safe" in await read_task


@pytest.mark.asyncio
async def test_run_code_inline_code_uses_temp_file_and_cleans_up(
    tmp_path: Path, monkeypatch
) -> None:
    """Inline code runs from a transient workspace file that exists (with the
    code) during the run, is removed afterwards, and never reaches the
    changed-files/attachment rail."""
    import tools.code_exec as code_exec_mod

    reg, mgr = _register(tmp_path)
    observed: dict[str, object] = {}

    async def fake_run(config, workspace_dir, script, *, stdin=None, argv=()) -> SandboxResult:
        del argv
        observed["script"] = script
        observed["existed"] = script.exists()
        observed["content"] = script.read_text(encoding="utf-8")
        return SandboxResult(exit_code=0, stdout="ok", stderr="", timed_out=False, duration_ms=1)

    monkeypatch.setattr(code_exec_mod, "run_python_in_sandbox", fake_run)

    result = json.loads(await reg.dispatch("run_code", {"code": "print('ok')"}, _ctx()))

    assert result["exit_code"] == 0
    assert result["path"] == "<inline code>"
    assert result["mode"] == "python"
    script = observed["script"]
    assert isinstance(script, Path)
    assert observed["existed"] is True
    assert observed["content"] == "print('ok')"
    assert script.name.startswith(".inline-") and script.suffix == ".py"
    assert script.parent == mgr.user_files_dir(WS)
    assert not script.exists()  # cleaned up after the run
    assert result["changed_files"] == []  # temp never reported or attached
    assert result["attached_files"] == []
    assert "note" not in result


@pytest.mark.asyncio
async def test_run_code_inline_code_temp_removed_even_on_sandbox_error(
    tmp_path: Path, monkeypatch
) -> None:
    import tools.code_exec as code_exec_mod

    reg, mgr = _register(tmp_path)

    async def fake_run(config, workspace_dir, script, *, stdin=None, argv=()) -> SandboxResult:
        del config, workspace_dir, script, stdin, argv
        raise RuntimeError("sandbox exploded")

    monkeypatch.setattr(code_exec_mod, "run_python_in_sandbox", fake_run)

    result = json.loads(await reg.dispatch("run_code", {"code": "print(1)"}, _ctx()))

    assert "error" in result
    leftovers = list(mgr.user_files_dir(WS).glob(".inline-*.py"))
    assert leftovers == []


@pytest.mark.asyncio
async def test_run_code_rejects_code_and_path_together(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path)
    ws_dir = mgr.user_files_dir(WS)
    ws_dir.mkdir(parents=True, exist_ok=True)
    (ws_dir / "x.py").write_text("print(1)\n", encoding="utf-8")

    result = await reg.dispatch("run_code", {"code": "print(2)", "path": "x.py"}, _ctx())

    assert json.loads(result) == {"error": "pass either code or path, not both"}


@pytest.mark.asyncio
async def test_run_code_rejects_oversized_or_blank_code(tmp_path: Path) -> None:
    from tools.code_exec import MAX_INLINE_CODE_BYTES

    reg, _ = _register(tmp_path)

    big = await reg.dispatch("run_code", {"code": "#" * (MAX_INLINE_CODE_BYTES + 1)}, _ctx())
    assert "byte limit" in json.loads(big)["error"]

    blank = await reg.dispatch("run_code", {"code": "   "}, _ctx())
    assert "error" in json.loads(blank)


@_requires_sandbox
@pytest.mark.asyncio
async def test_run_code_inline_code_executes_in_real_sandbox(tmp_path: Path) -> None:
    reg, _ = _register(tmp_path)

    result = json.loads(
        await reg.dispatch(
            "run_code",
            {"code": "import sys\nprint('inline ok'); sys.exit(0)\n"},
            _ctx(),
        )
    )

    assert result["exit_code"] == 0
    assert result["timed_out"] is False
    assert "inline ok" in result["stdout"]
    assert result["path"] == "<inline code>"


# --- Networked runs (run_code) ---------------------------------------

import tools.code_exec as code_exec_mod  # noqa: E402


def _register_net(
    tmp_path: Path,
    *,
    weekly_limit: int = 0,
    netns_lease: NetnsLease | None = None,
) -> tuple[ToolRegistry, WorkspaceManager]:
    reg = ToolRegistry(owner_user_id=OWNER)
    mgr = WorkspaceManager(base_dir=tmp_path)
    net_cfg = SandboxConfig(
        network_mode="netns",
        netns_helper_bin="/usr/local/sbin/code-exec-netns",
        env_dir_names=(".venv", ".pio"),
        max_env_bytes=1_000_000,
    )
    init_code_exec_tool(
        reg,
        mgr,
        net_cfg,
        locks=UserLocks(),
        network_weekly_limit=weekly_limit,
        netns_lease=netns_lease,
    )
    return reg, mgr


def _make_healthy_venv(mgr: WorkspaceManager, ws_key: WorkspaceKey) -> None:
    venv = mgr.user_files_dir(ws_key) / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    (venv / "bin" / "python3").write_text("#!/bin/sh\n", encoding="utf-8")


class _NetworkUsageStore:
    def __init__(self, events: list[UsageMarker]) -> None:
        self.events = events
        self.markers: list[dict[str, object]] = []

    async def usage_markers(self, *args, **kwargs) -> list[UsageMarker]:
        del args, kwargs
        return list(self.events)

    async def record_usage_marker(self, **kwargs) -> None:
        self.markers.append(kwargs)


@pytest.mark.asyncio
async def test_networked_run_code_is_member_tier(tmp_path: Path) -> None:
    reg, _ = _register_net(tmp_path)
    member_names = [s["name"] for s in reg.get_tool_schemas(TrustTier.MEMBER, set(), "u")]
    assert "run_code" in member_names


@pytest.mark.asyncio
async def test_run_code_none_mode_rejects_pip_install(tmp_path: Path) -> None:
    reg, _ = _register(tmp_path)  # no network config
    names = [s["name"] for s in reg.get_tool_schemas(TrustTier.REGULAR, set(), "u")]
    assert "run_code" in names
    result = await reg.dispatch(
        "run_code",
        {"pip_install": ["requests"]},
        _ctx(tier=TrustTier.REGULAR),
    )
    assert "requires host or netns" in json.loads(result)["error"]


def test_app_wiring_registers_run_code_after_successful_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        code_exec_enabled=True,
        code_exec_network_mode="none",
    )
    registry = ToolRegistry(owner_user_id=OWNER)
    manager = WorkspaceManager(base_dir=tmp_path)
    probed: list[SandboxConfig] = []

    def accept_probe(config: SandboxConfig) -> bool:
        probed.append(config)
        return True

    monkeypatch.setattr(app_tools, "sandbox_available", accept_probe)

    app_tools._register_code_exec(settings, registry, manager, UserLocks())

    assert registry.is_registered("run_code")
    assert probed[0].workspace_probe_root == str(Path(settings.workspace_dir).resolve())


def test_app_wiring_does_not_register_run_code_after_failed_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        code_exec_enabled=True,
        code_exec_network_mode="host",
    )
    registry = ToolRegistry(owner_user_id=OWNER)
    manager = WorkspaceManager(base_dir=tmp_path)
    monkeypatch.setattr(app_tools, "sandbox_available", lambda _config: False)

    app_tools._register_code_exec(settings, registry, manager, UserLocks())

    assert not registry.is_registered("run_code")


@pytest.mark.asyncio
async def test_run_code_fails_closed_when_quota_store_is_unavailable(
    tmp_path: Path,
) -> None:
    reg, _ = _register_net(tmp_path, weekly_limit=100)

    result = json.loads(
        await reg.dispatch(
            "run_code",
            {"code": "print(1)"},
            _ctx(tier=TrustTier.REGULAR),
        )
    )

    assert "usage accounting is not ready" in result["error"]


@pytest.mark.asyncio
async def test_run_code_enforces_finite_weekly_limit(tmp_path: Path) -> None:
    reg, _ = _register_net(tmp_path, weekly_limit=2)
    usage = _NetworkUsageStore(
        [
            UsageMarker(created_at=datetime.now(UTC), unit_count=1),
            UsageMarker(created_at=datetime.now(UTC), unit_count=1),
        ]
    )
    ctx = _ctx(tier=TrustTier.REGULAR)
    ctx.usage_store = usage  # type: ignore[assignment]

    result = json.loads(await reg.dispatch("run_code", {"code": "print(1)"}, ctx))

    assert "used 2 of 2" in result["error"]
    assert usage.markers == []


@pytest.mark.asyncio
async def test_run_code_rejects_bad_pip_specs(tmp_path: Path) -> None:
    reg, _ = _register_net(tmp_path)
    for bad in (["-rreq.txt"], ["http://evil/x.whl"], ["../local"], ["a b"]):
        result = await reg.dispatch("run_code", {"pip_install": bad}, _ctx(tier=TrustTier.REGULAR))
        assert "error" in json.loads(result), bad


@pytest.mark.asyncio
async def test_run_code_requires_some_work(tmp_path: Path) -> None:
    reg, _ = _register_net(tmp_path)
    result = await reg.dispatch("run_code", {}, _ctx(tier=TrustTier.REGULAR))
    assert "error" in json.loads(result)


@pytest.mark.asyncio
async def test_run_code_uses_workspace_venv_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reg, mgr = _register_net(tmp_path)
    _make_healthy_venv(mgr, WS)
    seen: dict[str, object] = {}

    async def fake_run(config, workspace_dir, script_path, *, stdin=None, argv=()):
        del workspace_dir, script_path, stdin, argv
        seen["python_bin_override"] = config.python_bin_override
        seen["network_egress"] = config.network_mode
        return SandboxResult(exit_code=0, stdout="ok", stderr="", timed_out=False, duration_ms=1)

    monkeypatch.setattr(code_exec_mod, "run_python_in_sandbox", fake_run)
    result = json.loads(
        await reg.dispatch("run_code", {"code": "print(1)"}, _ctx(tier=TrustTier.REGULAR))
    )
    assert seen["python_bin_override"] == "/work/.venv/bin/python3"
    assert seen["network_egress"] == "netns"
    assert result["interpreter"] == "workspace_venv"
    assert result["network"] is True


@pytest.mark.asyncio
async def test_run_code_waits_for_shared_netns_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    netns_lease = NetnsLease()
    await netns_lease.acquire()
    reg, _mgr = _register_net(tmp_path, netns_lease=netns_lease)
    started = asyncio.Event()

    async def fake_run(config, workspace_dir, script_path, *, stdin=None, argv=()):
        del config, workspace_dir, script_path, stdin, argv
        started.set()
        return SandboxResult(
            exit_code=0,
            stdout="ok",
            stderr="",
            timed_out=False,
            duration_ms=1,
        )

    monkeypatch.setattr(code_exec_mod, "run_python_in_sandbox", fake_run)
    ctx = _ctx(tier=TrustTier.REGULAR)
    run = asyncio.create_task(
        reg.dispatch(
            "run_code",
            {"code": "print(1)"},
            ctx,
        )
    )
    await asyncio.sleep(0)

    assert not started.is_set()
    assert not run.done()
    await netns_lease.release()
    result = json.loads(await asyncio.wait_for(run, timeout=1.0))
    assert result["exit_code"] == 0
    assert started.is_set()


@pytest.mark.asyncio
async def test_unconfirmed_network_teardown_poisons_shared_netns_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    netns_lease = NetnsLease()
    reg, _mgr = _register_net(tmp_path, netns_lease=netns_lease)

    async def unsafe_teardown(*args: object, **kwargs: object) -> SandboxResult:
        del args, kwargs
        raise SandboxTeardownError("network unit remained active")

    monkeypatch.setattr(code_exec_mod, "run_python_in_sandbox", unsafe_teardown)

    result = json.loads(
        await reg.dispatch(
            "run_code",
            {"code": "print(1)"},
            _ctx(tier=TrustTier.REGULAR),
        )
    )

    assert "network unit remained active" in result["error"]
    assert netns_lease.poisoned
    assert not netns_lease.locked()
    with pytest.raises(NetnsLeasePoisonedError, match="unavailable until restart"):
        await netns_lease.acquire()


@pytest.mark.asyncio
async def test_plain_run_code_reuses_healthy_venv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A non-network run_code auto-uses a venv an earlier networked run created.
    reg, mgr = _register(tmp_path)
    _make_healthy_venv(mgr, WS)
    seen: dict[str, object] = {}

    async def fake_run(config, workspace_dir, script_path, *, stdin=None, argv=()):
        del workspace_dir, script_path, stdin, argv
        seen["override"] = config.python_bin_override
        return SandboxResult(exit_code=0, stdout="", stderr="", timed_out=False, duration_ms=1)

    monkeypatch.setattr(code_exec_mod, "run_python_in_sandbox", fake_run)
    await reg.dispatch("run_code", {"code": "print(1)"}, _ctx())
    assert seen["override"] == "/work/.venv/bin/python3"


def test_changed_files_excludes_env_dirs(tmp_path: Path) -> None:
    mgr = WorkspaceManager(base_dir=tmp_path)
    root = mgr.user_files_dir(WS)
    (root / ".venv" / "bin").mkdir(parents=True)
    before, complete = code_exec_mod._snapshot_workspace(
        mgr,
        WS,
        max_workspace_files=100,
        max_env_roots=100,
    )
    assert complete is True
    (root / "out.txt").write_text("hi", encoding="utf-8")
    (root / ".venv" / "bin" / "pip").write_text("x", encoding="utf-8")
    changed = code_exec_mod._changed_workspace_files(
        mgr,
        WS,
        before,
        max_workspace_files=100,
        max_env_roots=100,
    )
    paths = {c["path"] for c in changed}
    assert "out.txt" in paths
    assert not any(".venv" in str(p) for p in paths)


def test_artifact_skip_reason_env_dir(tmp_path: Path) -> None:
    reason = code_exec_mod._artifact_skip_reason(
        ".venv/bin/pip", tmp_path / "x", 10, 8 * 1024 * 1024
    )
    assert reason == "env_dir"


@pytest.mark.asyncio
async def test_run_code_inline_shell_one_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # code + mode=shell runs the string as a shell script in one call (no separate
    # write_file), e.g. `git clone ...`, `pio run ...`.
    reg, _mgr = _register_net(tmp_path)
    seen: dict[str, object] = {}

    async def fake_run(config, workspace_dir, file_path, *, stdin=None, mode="direct", argv=()):
        del workspace_dir, stdin
        seen["mode"] = mode
        seen["suffix"] = Path(file_path).suffix
        seen["network"] = config.network_mode
        seen["argv"] = tuple(argv)
        return SandboxResult(exit_code=0, stdout="ok", stderr="", timed_out=False, duration_ms=1)

    monkeypatch.setattr(code_exec_mod, "run_workspace_file_in_sandbox", fake_run)
    result = json.loads(
        await reg.dispatch(
            "run_code",
            {
                "code": "git clone https://github.com/x/y y",
                "mode": "shell",
                "argv": ["one", "two"],
            },
            _ctx(tier=TrustTier.REGULAR),
        )
    )
    assert seen["mode"] == "shell"  # ran as a shell script
    assert seen["suffix"] == ".sh"  # transient file got a .sh suffix
    assert seen["network"] == "netns"
    assert seen["argv"] == ("one", "two")
    assert result["mode"] == "shell"


@pytest.mark.asyncio
async def test_run_code_inline_code_defaults_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reg, _mgr = _register_net(tmp_path)
    seen: dict[str, object] = {}

    async def fake_run(config, workspace_dir, file_path, *, stdin=None, argv=()):
        del config, workspace_dir, stdin
        seen["suffix"] = Path(file_path).suffix
        seen["argv"] = tuple(argv)
        return SandboxResult(exit_code=0, stdout="", stderr="", timed_out=False, duration_ms=1)

    monkeypatch.setattr(code_exec_mod, "run_python_in_sandbox", fake_run)
    await reg.dispatch(
        "run_code",
        {"code": "print(1)", "argv": ["one", "two"]},
        _ctx(tier=TrustTier.REGULAR),
    )
    assert seen["suffix"] == ".py"  # defaults to Python (no mode)
    assert seen["argv"] == ("one", "two")


@pytest.mark.asyncio
async def test_run_code_inline_code_rejects_bad_argv(tmp_path: Path) -> None:
    reg, _mgr = _register_net(tmp_path)

    result = await reg.dispatch(
        "run_code",
        {"code": "print(1)", "argv": ["ok", 1]},
        _ctx(tier=TrustTier.REGULAR),
    )

    assert json.loads(result) == {"error": "argv must be an array of strings"}


@pytest.mark.asyncio
async def test_offline_inline_code_forwards_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reg, _mgr = _register(tmp_path)
    seen: dict[str, object] = {}

    async def fake_run(config, workspace_dir, file_path, *, stdin=None, argv=()):
        del config, workspace_dir, file_path, stdin
        seen["argv"] = tuple(argv)
        return SandboxResult(exit_code=0, stdout="", stderr="", timed_out=False, duration_ms=1)

    monkeypatch.setattr(code_exec_mod, "run_python_in_sandbox", fake_run)
    await reg.dispatch(
        "run_code",
        {"code": "print(1)", "argv": ["offline", "argument"]},
        _ctx(tier=TrustTier.REGULAR),
    )

    assert seen["argv"] == ("offline", "argument")
