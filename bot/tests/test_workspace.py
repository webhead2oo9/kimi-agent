"""Exercises workspace/manager.py's job-directory contract: id validation,
traversal rejection, and idempotent creation. This is the filesystem layer
a sandboxed process runs inside, not the process itself; see
test_sandbox_runner.py for that.
"""

import os
import stat
import tempfile
import uuid
from pathlib import Path

import pytest

from workspace import (
    MAX_PATH_DEPTH,
    WorkspaceKey,
    WorkspaceManager,
    path_contains_symlink,
    workspace_owner_key,
)


def test_ensure_creates_directory() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        mgr = WorkspaceManager(base_dir=Path(tmp))
        path = mgr.ensure(WorkspaceKey("user123"))
        assert path.exists()
        assert path.is_dir()
        assert path.name == "user123"


def test_ensure_idempotent() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        mgr = WorkspaceManager(base_dir=Path(tmp))
        path1 = mgr.ensure(WorkspaceKey("user123"))
        (path1 / "file.txt").write_text("test", encoding="utf-8")
        path2 = mgr.ensure(WorkspaceKey("user123"))
        assert path1 == path2
        assert (path2 / "file.txt").exists()


def test_create_job_dir_accepts_safe_explicit_id() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        mgr = WorkspaceManager(base_dir=Path(tmp))
        job_dir = mgr.create_job_dir(WorkspaceKey("user123"), job_id="job-abc_v1.2")
        assert job_dir == Path(tmp) / "user123" / "jobs" / "job-abc_v1.2"
        assert job_dir.exists()
        assert job_dir.is_dir()


def test_create_job_dir_defaults_to_uuid() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        mgr = WorkspaceManager(base_dir=Path(tmp))

        job_dir = mgr.create_job_dir(WorkspaceKey("user123"))

        assert job_dir.parent == Path(tmp) / "user123" / "jobs"
        assert uuid.UUID(job_dir.name).hex == job_dir.name
        assert job_dir.is_dir()


@pytest.mark.parametrize("job_id", [".", "..", "../escape", r"..\escape"])
def test_create_job_dir_rejects_traversal_ids(tmp_path: Path, job_id: str) -> None:
    base_dir = tmp_path / "workspace"
    mgr = WorkspaceManager(base_dir=base_dir)

    with pytest.raises(ValueError, match="safe path segment"):
        mgr.create_job_dir(WorkspaceKey("user123"), job_id=job_id)

    assert not base_dir.exists()


@pytest.mark.parametrize("job_id", ["/absolute/job", r"C:\absolute\job", r"\\host\share\job"])
def test_create_job_dir_rejects_absolute_ids(tmp_path: Path, job_id: str) -> None:
    base_dir = tmp_path / "workspace"
    mgr = WorkspaceManager(base_dir=base_dir)

    with pytest.raises(ValueError, match="safe path segment"):
        mgr.create_job_dir(WorkspaceKey("user123"), job_id=job_id)

    assert not base_dir.exists()


@pytest.mark.parametrize("job_id", ["nested/job", r"nested\job"])
def test_create_job_dir_rejects_separator_ids(tmp_path: Path, job_id: str) -> None:
    base_dir = tmp_path / "workspace"
    mgr = WorkspaceManager(base_dir=base_dir)

    with pytest.raises(ValueError, match="safe path segment"):
        mgr.create_job_dir(WorkspaceKey("user123"), job_id=job_id)

    assert not base_dir.exists()


@pytest.mark.parametrize(
    "job_id",
    ["", " ", " job-abc", "job-abc ", "_job", "job_", ".job", "job.", "job:name"],
)
def test_create_job_dir_rejects_empty_stripped_or_rewritten_ids(
    tmp_path: Path, job_id: str
) -> None:
    base_dir = tmp_path / "workspace"
    mgr = WorkspaceManager(base_dir=base_dir)

    with pytest.raises(ValueError, match="safe path segment"):
        mgr.create_job_dir(WorkspaceKey("user123"), job_id=job_id)

    assert not base_dir.exists()


def test_create_job_dir_rejects_explicit_id_collision(tmp_path: Path) -> None:
    mgr = WorkspaceManager(base_dir=tmp_path)
    original = mgr.create_job_dir(WorkspaceKey("user123"), job_id="job-abc")

    with pytest.raises(FileExistsError):
        mgr.create_job_dir(WorkspaceKey("user123"), job_id="job-abc")

    assert original.is_dir()
    assert list(original.iterdir()) == []


def test_user_files_dir_is_separate_from_job_dirs() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        mgr = WorkspaceManager(base_dir=Path(tmp))
        files_dir = mgr.user_files_dir(WorkspaceKey("user123"))
        job_dir = mgr.create_job_dir(WorkspaceKey("user123"), job_id="job-abc")

        assert files_dir == Path(tmp) / "user123" / "files"
        assert files_dir.exists()
        assert job_dir == Path(tmp) / "user123" / "jobs" / "job-abc"
        assert job_dir.exists()


def test_resolve_user_file_path_rejects_traversal_and_absolute_paths() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        mgr = WorkspaceManager(base_dir=Path(tmp))

        resolved = mgr.resolve_user_file_path(WorkspaceKey("user123"), "notes/today.txt")
        assert resolved == Path(tmp).resolve() / "user123" / "files" / "notes" / "today.txt"

        with pytest.raises(ValueError, match="relative"):
            mgr.resolve_user_file_path(WorkspaceKey("user123"), "/etc/passwd")
        with pytest.raises(ValueError, match="traversal"):
            mgr.resolve_user_file_path(WorkspaceKey("user123"), "../jobs/job-abc/secret.txt")
        with pytest.raises(ValueError, match="traversal"):
            mgr.resolve_user_file_path(WorkspaceKey("user123"), "notes/../secret.txt")


def test_resolve_user_file_path_enforces_depth_cap() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        mgr = WorkspaceManager(base_dir=Path(tmp))

        at_cap = "/".join(["d"] * (MAX_PATH_DEPTH - 1) + ["f.txt"])
        resolved = mgr.resolve_user_file_path(WorkspaceKey("user123"), at_cap)
        assert resolved.is_relative_to(Path(tmp).resolve() / "user123" / "files")

        over_cap = "/".join(["d"] * MAX_PATH_DEPTH + ["f.txt"])
        with pytest.raises(ValueError, match="deeply nested"):
            mgr.resolve_user_file_path(WorkspaceKey("user123"), over_cap)


def test_resolve_user_file_path_rejects_symlink_escape() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        mgr = WorkspaceManager(base_dir=Path(tmp))
        files_dir = mgr.user_files_dir(WorkspaceKey("user123"))
        outside = Path(tmp) / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        link = files_dir / "link.txt"
        try:
            link.symlink_to(outside)
        except NotImplementedError, OSError:
            pytest.skip("symlinks are not available on this filesystem")

        with pytest.raises(ValueError, match="symlink"):
            mgr.resolve_user_file_path(WorkspaceKey("user123"), "link.txt", must_exist=True)


def test_path_contains_symlink_is_available_as_shared_helper(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except NotImplementedError, OSError:
        pytest.skip("symlinks are not available on this filesystem")

    assert path_contains_symlink(root, link / "image.png") is True
    assert path_contains_symlink(root, root / "plain" / "image.png") is False


@pytest.mark.asyncio
async def test_sweep_removes_old_files() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        mgr = WorkspaceManager(base_dir=Path(tmp), file_ttl=1)
        path = mgr.ensure(WorkspaceKey("user123"))
        old_file = path / "jobs" / "old-job" / "old.txt"
        old_file.parent.mkdir(parents=True)
        old_file.write_text("old", encoding="utf-8")
        os.utime(old_file, (0, 0))

        new_file = path / "jobs" / "new-job" / "new.txt"
        new_file.parent.mkdir(parents=True)
        new_file.write_text("new", encoding="utf-8")

        removed = await mgr.sweep_expired()
        assert removed >= 1
        assert not old_file.exists()
        assert new_file.exists()


@pytest.mark.asyncio
async def test_sweep_removes_old_user_visible_files_and_jobs() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        mgr = WorkspaceManager(base_dir=Path(tmp), file_ttl=1, max_size_bytes=1000)
        path = mgr.ensure(WorkspaceKey("user123"))
        old_visible = mgr.user_files_dir(WorkspaceKey("user123")) / "old.txt"
        old_visible.write_text("x" * 100, encoding="utf-8")
        os.utime(old_visible, (0, 0))
        new_visible = mgr.user_files_dir(WorkspaceKey("user123")) / "new.txt"
        new_visible.write_text("new", encoding="utf-8")

        old_job_file = path / "jobs" / "old-job" / "old.txt"
        old_job_file.parent.mkdir(parents=True)
        old_job_file.write_text("old", encoding="utf-8")
        os.utime(old_job_file, (0, 0))

        removed = await mgr.sweep_expired()

        assert removed == 2
        assert not old_visible.exists()
        assert new_visible.exists()
        assert not old_job_file.exists()


@pytest.mark.asyncio
async def test_sweep_prunes_oversized() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        mgr = WorkspaceManager(base_dir=Path(tmp), file_ttl=86400, max_size_bytes=100)
        path = mgr.ensure(WorkspaceKey("user123"))

        old_file = path / "jobs" / "old-job" / "old.txt"
        old_file.parent.mkdir(parents=True)
        old_file.write_text("x" * 80, encoding="utf-8")
        os.utime(old_file, (1, 1))

        new_file = path / "jobs" / "new-job" / "new.txt"
        new_file.parent.mkdir(parents=True)
        new_file.write_text("x" * 80, encoding="utf-8")

        removed = await mgr.sweep_expired()
        assert removed >= 1
        assert not old_file.exists()
        assert new_file.exists()


@pytest.mark.asyncio
async def test_sweep_tolerates_files_that_disappear_during_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        mgr = WorkspaceManager(base_dir=Path(tmp), file_ttl=1)
        path = mgr.ensure(WorkspaceKey("user123"))
        old_file = path / "jobs" / "old-job" / "old.txt"
        old_file.parent.mkdir(parents=True)
        old_file.write_text("old", encoding="utf-8")
        os.utime(old_file, (0, 0))

        original_stat = Path.stat
        deleted = False

        def flaky_stat(self: Path, *, follow_symlinks: bool = True):
            nonlocal deleted
            if self == old_file and not deleted:
                deleted = True
                old_file.unlink()
            return original_stat(self, follow_symlinks=follow_symlinks)

        monkeypatch.setattr(Path, "stat", flaky_stat)

        removed = await mgr.sweep_expired()
        assert removed == 0


def test_generated_job_dir_sanitizes_context_key(tmp_path: Path) -> None:
    manager = WorkspaceManager(base_dir=tmp_path)

    job_dir = manager.generated_job_dir("guild:channel/thread", "job-1")

    assert job_dir == tmp_path / "generated" / "guild_channel_thread" / "job-1"
    assert job_dir.exists()


def test_new_workspace_directories_are_private_on_posix(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("POSIX permission bits are not portable to this platform")

    base_dir = tmp_path / "workspace"
    manager = WorkspaceManager(base_dir=base_dir)
    files_dir = manager.user_files_dir(WorkspaceKey("owner"))
    job_dir = manager.create_job_dir(WorkspaceKey("owner"), job_id="job-1")
    generated_job_dir = manager.generated_job_dir("guild:channel", "generated-job")

    expected_private_dirs = (
        base_dir,
        base_dir / "owner",
        files_dir,
        base_dir / "owner" / "jobs",
        job_dir,
        base_dir / "generated",
        base_dir / "generated" / "guild_channel",
        generated_job_dir,
    )
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o700 for path in expected_private_dirs)


def test_private_creation_does_not_chmod_existing_workspace_dirs(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("POSIX permission bits are not portable to this platform")

    base_dir = tmp_path / "workspace"
    base_dir.mkdir(mode=0o750)
    # mkdir's requested mode is filtered through the caller's umask. Normalize
    # the fixtures so this test measures preservation of an existing mode.
    base_dir.chmod(0o750)
    owner_dir = base_dir / "owner"
    owner_dir.mkdir(mode=0o750)
    owner_dir.chmod(0o750)
    manager = WorkspaceManager(base_dir=base_dir)

    files_dir = manager.user_files_dir(WorkspaceKey("owner"))

    assert stat.S_IMODE(base_dir.stat().st_mode) == 0o750
    assert stat.S_IMODE(owner_dir.stat().st_mode) == 0o750
    assert stat.S_IMODE(files_dir.stat().st_mode) == 0o700


def test_generated_owner_marker_preserves_exact_owner_and_private_mode(tmp_path: Path) -> None:
    manager = WorkspaceManager(base_dir=tmp_path)
    job_dir = manager.generated_job_dir("guild:channel", "job-1", owner_user_id="00123")
    marker = job_dir / ".owner-user-id"

    assert marker.read_text(encoding="utf-8") == "00123"
    if os.name == "posix":
        assert stat.S_IMODE(marker.stat().st_mode) == 0o600


def test_allowed_output_roots_sanitizes_generated_context_key(tmp_path: Path) -> None:
    manager = WorkspaceManager(base_dir=tmp_path)

    roots = manager.allowed_output_roots(context_key="guild:channel/thread")
    job_dir = manager.generated_job_dir("guild:channel/thread", "job-1")

    assert roots == [(tmp_path / "generated" / "guild_channel_thread").resolve()]
    assert job_dir.parent.resolve() in roots


def test_allowed_output_roots_keeps_traversal_context_under_generated(tmp_path: Path) -> None:
    manager = WorkspaceManager(base_dir=tmp_path)

    roots = manager.allowed_output_roots(context_key="../user123/files")

    generated_root = (tmp_path / "generated").resolve()
    assert roots == [generated_root / "user123_files"]
    assert all(root.is_relative_to(generated_root) for root in roots)


def test_generated_relative_and_resolve_round_trip(tmp_path: Path) -> None:
    manager = WorkspaceManager(base_dir=tmp_path)
    job_dir = manager.generated_job_dir("guild:channel", "job-1")
    output = job_dir / "output-1.png"
    output.write_bytes(b"png")

    relative = manager.relative_generated_file_path(output)

    assert relative == "generated/guild_channel/job-1/output-1.png"
    assert manager.resolve_generated_file_path(relative, must_exist=True) == output


def test_context_generated_file_resolve_returns_context_metadata(tmp_path: Path) -> None:
    manager = WorkspaceManager(base_dir=tmp_path)
    job_dir = manager.generated_job_dir("guild:channel", "job-1")
    output = job_dir / "output-1.png"
    output.write_bytes(b"png")
    relative = manager.relative_generated_file_path(output)

    resolved = manager.resolve_context_generated_file(
        relative,
        context_key="guild:channel",
        must_exist=True,
    )

    assert resolved.path == output
    assert resolved.relative_path == relative
    assert resolved.root == manager.allowed_output_roots(context_key="guild:channel")[0]


def test_context_generated_file_rejects_other_context(tmp_path: Path) -> None:
    manager = WorkspaceManager(base_dir=tmp_path)
    output = manager.generated_job_dir("other:guild", "job-1") / "output-1.png"
    output.write_bytes(b"png")
    relative = manager.relative_generated_file_path(output)

    with pytest.raises(ValueError, match="conversation context"):
        manager.resolve_context_generated_file(
            relative,
            context_key="guild:channel",
            must_exist=True,
        )


def test_context_generated_file_rejects_symlinked_segments(tmp_path: Path) -> None:
    manager = WorkspaceManager(base_dir=tmp_path)
    context_root = manager.allowed_output_roots(context_key="guild:channel")[0]
    real_dir = context_root / "real"
    real_dir.mkdir(parents=True)
    output = real_dir / "output-1.png"
    output.write_bytes(b"png")
    link = context_root / "link"
    try:
        link.symlink_to(real_dir, target_is_directory=True)
    except NotImplementedError, OSError:
        pytest.skip("symlinks are not available on this filesystem")

    with pytest.raises(ValueError, match="symlinks"):
        manager.resolve_context_generated_file(
            "generated/guild_channel/link/output-1.png",
            context_key="guild:channel",
            must_exist=True,
        )


def test_generated_resolve_rejects_non_generated_path(tmp_path: Path) -> None:
    manager = WorkspaceManager(base_dir=tmp_path)

    try:
        manager.resolve_generated_file_path("files/output.png")
    except ValueError as exc:
        assert "generated" in str(exc).lower()
    else:
        raise AssertionError("non-generated paths must be rejected")


@pytest.mark.parametrize(
    "generated_path",
    [
        "generated/./output.png",
        "generated//output.png",
        "generated/../output.png",
    ],
)
def test_generated_resolve_rejects_invalid_raw_segments(
    tmp_path: Path, generated_path: str
) -> None:
    manager = WorkspaceManager(base_dir=tmp_path)

    with pytest.raises(ValueError, match="traversal"):
        manager.resolve_generated_file_path(generated_path)


def test_workspace_owner_key_includes_guild() -> None:
    assert workspace_owner_key("123", "456") == "123__456"


def test_workspace_owner_key_no_guild_collapses_to_dm() -> None:
    assert workspace_owner_key("123", None) == "123__dm"
    assert workspace_owner_key("123", "") == "123__dm"


def test_workspace_owner_key_sanitizes_synthetic_guild() -> None:
    # Defensive: a synthetic guild id with a colon must not leak into the path.
    assert workspace_owner_key("123", "userapp:123") == "123__userapp_123"


def test_workspace_owner_key_isolates_guilds_for_one_user() -> None:
    assert workspace_owner_key("123", "g1") != workspace_owner_key("123", "g2")


def test_workspace_files_isolated_per_guild(tmp_path: Path) -> None:
    manager = WorkspaceManager(base_dir=tmp_path)
    key_a = workspace_owner_key("123", "g1")
    key_b = workspace_owner_key("123", "g2")

    (manager.user_files_dir(key_a) / "secret.txt").write_text("a", encoding="utf-8")

    assert not (manager.user_files_dir(key_b) / "secret.txt").exists()
    assert manager.user_files_dir(key_a) != manager.user_files_dir(key_b)


def test_delete_owner_dirs_removes_all_guilds_for_user_only(tmp_path: Path) -> None:
    manager = WorkspaceManager(base_dir=tmp_path)
    # Target user's files in two guilds.
    (manager.user_files_dir(workspace_owner_key("123", "g1")) / "a.txt").write_text(
        "a", encoding="utf-8"
    )
    (manager.user_files_dir(workspace_owner_key("123", "g2")) / "b.txt").write_text(
        "b", encoding="utf-8"
    )
    # An unrelated user, and an unowned generated job.
    other = manager.user_files_dir(workspace_owner_key("999", "g1"))
    (other / "keep.txt").write_text("keep", encoding="utf-8")
    generated = manager.generated_job_dir("g1:c1:main", "job-1")
    (generated / "img.png").write_bytes(b"png")

    removed = manager.delete_owner_dirs("123")

    assert removed == 2
    assert not (tmp_path / workspace_owner_key("123", "g1")).exists()
    assert not (tmp_path / workspace_owner_key("123", "g2")).exists()
    assert (other / "keep.txt").exists()
    assert (generated / "img.png").exists()


def test_delete_owner_dirs_removes_only_owned_generated_jobs(tmp_path: Path) -> None:
    manager = WorkspaceManager(base_dir=tmp_path)
    owned = manager.generated_job_dir("g1:c1:shared", "job-owned", owner_user_id="123")
    other = manager.generated_job_dir("g1:c1:shared", "job-other", owner_user_id="999")
    (owned / "prompt.json").write_text("private prompt", encoding="utf-8")
    (other / "image.png").write_bytes(b"other")

    manager.delete_owner_dirs("123")

    assert not owned.exists()
    assert (other / "image.png").exists()


def test_delete_owner_dirs_ignores_unreadable_unconfirmed_owner_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = WorkspaceManager(base_dir=tmp_path)
    other = manager.generated_job_dir(
        "g1:c1:shared",
        "job-other",
        owner_user_id="999",
    )
    marker = other / ".owner-user-id"
    real_read_text = Path.read_text

    def fail_other_marker(
        path: Path,
        encoding: str | None = None,
        errors: str | None = None,
    ) -> str:
        if path == marker:
            raise OSError("simulated unreadable marker")
        return real_read_text(path, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", fail_other_marker)

    assert manager.delete_owner_dirs("123") == 0
    assert other.exists()


def test_delete_owner_dirs_reports_owned_non_directory_path(tmp_path: Path) -> None:
    manager = WorkspaceManager(base_dir=tmp_path)
    malformed = tmp_path / workspace_owner_key("123", "g1")
    malformed.write_text("not a workspace directory", encoding="utf-8")

    with pytest.raises(OSError, match="owned workspace path"):
        manager.delete_owner_dirs("123")

    assert malformed.exists()


def test_delete_owner_dirs_no_prefix_bleed_between_users(tmp_path: Path) -> None:
    # "12" must not match "123__..." despite the shared "12" prefix; the "__"
    # delimiter is what keeps numeric ids distinct.
    manager = WorkspaceManager(base_dir=tmp_path)
    (manager.user_files_dir(workspace_owner_key("12", "g1")) / "x.txt").write_text(
        "x", encoding="utf-8"
    )
    (manager.user_files_dir(workspace_owner_key("123", "g1")) / "y.txt").write_text(
        "y", encoding="utf-8"
    )

    removed = manager.delete_owner_dirs("12")

    assert removed == 1
    assert not (tmp_path / workspace_owner_key("12", "g1")).exists()
    assert (tmp_path / workspace_owner_key("123", "g1")).exists()


@pytest.mark.asyncio
async def test_sweep_removes_expired_venv_as_whole_unit() -> None:
    import time as _time

    from workspace import ENV_DIR_NAMES

    assert ".venv" in ENV_DIR_NAMES
    with tempfile.TemporaryDirectory() as tmp:
        mgr = WorkspaceManager(base_dir=Path(tmp), file_ttl=100, env_max_bytes=10_000_000)
        files = mgr.user_files_dir(WorkspaceKey("u1"))
        venv = files / ".venv" / "bin"
        venv.mkdir(parents=True)
        (venv / "python3").write_text("x", encoding="utf-8")
        (files / "keep.txt").write_text("y", encoding="utf-8")
        # Age the whole venv past the TTL; leave keep.txt fresh.
        old = _time.time() - 500
        for p in (files / ".venv").rglob("*"):
            os.utime(p, (old, old))
        os.utime(files / ".venv" / "bin" / "python3", (old, old))

        await mgr.sweep_expired()
        assert not (files / ".venv").exists()  # removed as a whole unit
        assert (files / "keep.txt").exists()  # fresh doc survives


@pytest.mark.asyncio
async def test_sweep_prunes_venv_over_env_allowance_not_docs() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        # Doc cap tiny, env allowance tiny: a big venv must be pruned wholesale
        # without touching documents.
        mgr = WorkspaceManager(
            base_dir=Path(tmp),
            file_ttl=10_000_000,
            max_size_bytes=10_000_000,
            env_max_bytes=100,
        )
        files = mgr.user_files_dir(WorkspaceKey("u1"))
        (files / ".venv").mkdir(parents=True)
        (files / ".venv" / "big").write_bytes(b"z" * 5000)
        (files / "notes.txt").write_text("hi", encoding="utf-8")
        await mgr.sweep_expired()
        assert not (files / ".venv").exists()
        assert (files / "notes.txt").exists()


@pytest.mark.asyncio
async def test_sweep_prunes_env_over_entry_allowance() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        mgr = WorkspaceManager(
            base_dir=Path(tmp),
            file_ttl=10_000_000,
            env_max_bytes=10_000_000,
            env_max_files=10,
        )
        files = mgr.user_files_dir(WorkspaceKey("u1"))
        env = files / ".venv"
        env.mkdir()
        for index in range(20):
            (env / str(index)).touch()
        (files / "notes.txt").write_text("hi", encoding="utf-8")

        await mgr.sweep_expired()

        assert not env.exists()
        assert (files / "notes.txt").exists()


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="mode-000 traversal requires a non-root POSIX user",
)
@pytest.mark.asyncio
async def test_sweep_removes_unreadable_env_tree() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        mgr = WorkspaceManager(
            base_dir=Path(tmp),
            file_ttl=10_000_000,
            env_max_bytes=10_000_000,
            env_max_files=100,
        )
        files = mgr.user_files_dir(WorkspaceKey("u1"))
        env = files / ".venv"
        locked = env / "locked"
        locked.mkdir(parents=True)
        for index in range(20):
            (locked / str(index)).touch()
        locked.chmod(0)

        try:
            removed = await mgr.sweep_expired()
        finally:
            if locked.exists():
                locked.chmod(0o700)

        assert removed == 1
        assert not env.exists()


def test_user_files_size_excludes_env_dirs() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        mgr = WorkspaceManager(base_dir=Path(tmp))
        files = mgr.user_files_dir(WorkspaceKey("u1"))
        (files / ".venv").mkdir(parents=True)
        (files / ".venv" / "big").write_bytes(b"z" * 5000)
        (files / "doc.txt").write_bytes(b"x" * 42)
        assert mgr.user_files_size(WorkspaceKey("u1")) == 42


def test_ensure_quota_rejects_writes_into_env_dirs() -> None:
    # Reserved environment directories are excluded from user_files_size, so
    # write tools must reject paths beneath them.
    import pytest as _pytest

    from tools.workspace.common import ensure_quota

    with tempfile.TemporaryDirectory() as tmp:
        mgr = WorkspaceManager(base_dir=Path(tmp))
        files = mgr.user_files_dir(WorkspaceKey("u1"))
        # A normal doc path is fine.
        ensure_quota(
            mgr,
            WorkspaceKey("u1"),
            new_size=10,
            destination=files / "notes.txt",
            temp_path=None,
            max_user_bytes=1_000_000,
        )
        # Anything under an env dir is rejected outright.
        for bad in (".venv/x", ".pio/build/y", "proj/.venv/z"):
            with _pytest.raises(ValueError, match="environment director"):
                ensure_quota(
                    mgr,
                    WorkspaceKey("u1"),
                    new_size=10,
                    destination=(files / bad),
                    temp_path=None,
                    max_user_bytes=1_000_000,
                )
