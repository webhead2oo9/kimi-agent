from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shutil
import stat
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import NewType

# The composite `<user_id>__<guild_id>` owner key, distinct from the raw Discord
# user id. They are both strings of digits-and-underscores, and passing one where
# the other belongs is either an isolation bypass (a raw id resolving a workspace)
# or a silent no-op (a composite key reaching delete_owner_dirs), so the two are
# kept apart in the type system. Build one with `workspace_owner_key`.
WorkspaceKey = NewType("WorkspaceKey", str)

log = logging.getLogger(__name__)
GENERATED_SEGMENT_RE = re.compile(r"[^A-Za-z0-9_.-]+")
PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)

# Sanity bound on path segment count, not the containment boundary (that is the
# absolute/.. rejects, the symlink check, and the is_relative_to re-check). Set
# generously so real source trees (e.g. src/main/java/<pkg>/... nested under an
# extracted-archive prefix) resolve; see docs/workspace.md.
MAX_PATH_DEPTH = 40

# Directory names holding regenerable dependency trees (per-user venvs, PlatformIO
# state) created by sandboxed code execution. Write tools still refuse these
# names; the exec runner alone creates them under separate accounting so a large
# dependency tree cannot block a small user document.
# See docs/workspace.md.
ENV_DIR_NAMES = frozenset({".venv", ".pio", ".pio-core"})
_OWNED_DIRECTORY_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)


def _has_env_dir_part(rel_parts: tuple[str, ...]) -> bool:
    return any(part in ENV_DIR_NAMES for part in rel_parts)


@dataclass(frozen=True)
class OwnedTreeRemoval:
    entries: int = 0
    bytes: int = 0


class OwnedTreeRemovalError(OSError):
    """A failed owned-tree removal with counts for work already completed."""

    def __init__(self, message: str, removal: OwnedTreeRemoval) -> None:
        super().__init__(message)
        self.removal = removal


@dataclass(frozen=True, slots=True)
class _OwnedTreeFrame:
    """One fixed-size traversal frame; names are never accumulated into paths."""

    name: str | None
    directory_stat: os.stat_result


def _validate_dir_fd_name(name: str) -> None:
    if not name or name in {".", ".."} or "/" in name:
        raise ValueError("fd-relative paths must contain exactly one safe segment")


def _same_file_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _verify_owned_directory_at(
    parent_fd: int,
    name: str,
    expected_stat: os.stat_result,
) -> None:
    """Fail if ``name`` no longer identifies the directory that was opened."""
    try:
        current_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise OSError(f"Owned directory identity changed during removal: {name}") from exc
    if not stat.S_ISDIR(current_stat.st_mode) or not _same_file_identity(
        current_stat,
        expected_stat,
    ):
        raise OSError(f"Owned directory identity changed during removal: {name}")


def open_owned_directory_at(parent_fd: int, name: str) -> int:
    """Open one owned child directory and verify its no-follow identity."""
    _validate_dir_fd_name(name)
    path_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISDIR(path_stat.st_mode):
        raise NotADirectoryError(name)
    try:
        if os.name == "posix" and stat.S_IMODE(path_stat.st_mode) & 0o700 != 0o700:
            os.chmod(
                name,
                stat.S_IMODE(path_stat.st_mode) | 0o700,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        directory_fd = os.open(name, _OWNED_DIRECTORY_OPEN_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError as exc:
        raise OSError(f"Owned directory identity changed while opening: {name}") from exc
    try:
        opened_stat = os.fstat(directory_fd)
        if not stat.S_ISDIR(opened_stat.st_mode):
            raise NotADirectoryError(name)
        if not _same_file_identity(opened_stat, path_stat):
            raise OSError(f"Owned directory identity changed while opening: {name}")
        if os.name == "posix" and stat.S_IMODE(opened_stat.st_mode) & 0o700 != 0o700:
            os.fchmod(directory_fd, stat.S_IMODE(opened_stat.st_mode) | 0o700)
    except BaseException:
        os.close(directory_fd)
        raise
    return directory_fd


def open_owned_directory_path_at(root_fd: int, parts: Sequence[str]) -> int:
    """Open an owned descendant through one-component, no-follow operations."""
    directory_fd = os.dup(root_fd)
    try:
        for part in parts:
            child_fd = open_owned_directory_at(directory_fd, part)
            os.close(directory_fd)
            directory_fd = child_fd
    except BaseException:
        os.close(directory_fd)
        raise
    return directory_fd


def _open_verified_owned_parent(
    directory_fd: int,
    expected_parent_stat: os.stat_result,
) -> int:
    """Open ``..`` without symlink traversal and verify it is the saved parent."""
    parent_fd = os.open("..", _OWNED_DIRECTORY_OPEN_FLAGS, dir_fd=directory_fd)
    try:
        parent_stat = os.fstat(parent_fd)
        if not _same_file_identity(parent_stat, expected_parent_stat):
            raise OSError("Owned-tree parent changed during removal")
    except BaseException:
        os.close(parent_fd)
        raise
    return parent_fd


def remove_owned_tree_at(parent_fd: int, name: str) -> OwnedTreeRemoval:
    """Remove one owned child tree using bounded fd-relative traversal."""
    _validate_dir_fd_name(name)
    directory_fd = open_owned_directory_at(parent_fd, name)
    root_stat = os.fstat(directory_fd)
    frames = [_OwnedTreeFrame(name=None, directory_stat=root_stat)]
    removed_entries = 0
    removed_bytes = 0
    try:
        while frames:
            try:
                with os.scandir(directory_fd) as iterator:
                    child_directory: str | None = None
                    for entry in iterator:
                        try:
                            child_stat = os.stat(
                                entry.name,
                                dir_fd=directory_fd,
                                follow_symlinks=False,
                            )
                        except FileNotFoundError:
                            continue
                        except OSError as exc:
                            raise OSError(
                                f"Failed to inspect owned tree entry: {entry.name}"
                            ) from exc

                        if stat.S_ISDIR(child_stat.st_mode):
                            child_directory = entry.name
                            break
                        try:
                            os.unlink(entry.name, dir_fd=directory_fd)
                        except FileNotFoundError:
                            continue
                        except OSError as exc:
                            raise OSError(
                                f"Failed to remove owned tree entry: {entry.name}"
                            ) from exc
                        removed_entries += 1
                        removed_bytes += child_stat.st_size
            except OSError as exc:
                raise OSError(f"Failed to inspect owned tree entry: {name}") from exc

            if child_directory is None:
                if len(frames) == 1:
                    break
                finished = frames[-1]
                ascended_parent_fd = _open_verified_owned_parent(
                    directory_fd,
                    frames[-2].directory_stat,
                )
                child_fd = directory_fd
                directory_fd = ascended_parent_fd
                os.close(child_fd)
                frames.pop()
                assert finished.name is not None
                _verify_owned_directory_at(
                    directory_fd,
                    finished.name,
                    finished.directory_stat,
                )
                try:
                    os.rmdir(finished.name, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    raise OSError(f"Failed to remove owned tree entry: {finished.name}") from exc
                else:
                    removed_entries += 1
                    removed_bytes += finished.directory_stat.st_size
                continue

            child_name = child_directory
            child_fd = -1
            try:
                child_fd = open_owned_directory_at(directory_fd, child_name)
                child_frame = _OwnedTreeFrame(
                    name=child_name,
                    directory_stat=os.fstat(child_fd),
                )
                frames.append(child_frame)
            except FileNotFoundError:
                if child_fd >= 0:
                    os.close(child_fd)
                continue
            except BaseException:
                if child_fd >= 0:
                    os.close(child_fd)
                raise
            parent_work_fd = directory_fd
            directory_fd = child_fd
            child_fd = -1
            os.close(parent_work_fd)

        _verify_owned_directory_at(parent_fd, name, root_stat)
        try:
            os.rmdir(name, dir_fd=parent_fd)
        except FileNotFoundError:
            return OwnedTreeRemoval(removed_entries, removed_bytes)
        except OSError as exc:
            raise OSError(f"Failed to remove owned tree entry: {name}") from exc
        removed_entries += 1
        removed_bytes += root_stat.st_size
        return OwnedTreeRemoval(removed_entries, removed_bytes)
    except OSError as exc:
        removal = OwnedTreeRemoval(removed_entries, removed_bytes)
        raise OwnedTreeRemovalError(str(exc), removal) from exc
    finally:
        if directory_fd >= 0:
            os.close(directory_fd)


def _remove_owned_tree_by_path(root: Path) -> None:
    """Portable fallback for platforms without fd-relative directory APIs."""
    try:
        root_stat = root.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(root_stat.st_mode):
        raise NotADirectoryError(root)

    pending: list[tuple[Path, bool]] = [(root, False)]
    first_error: OSError | None = None
    while pending:
        path, visited = pending.pop()
        if visited:
            try:
                path.rmdir()
            except FileNotFoundError:
                continue
            except OSError as exc:
                first_error = first_error or exc
            continue

        try:
            with os.scandir(path) as iterator:
                entries = list(iterator)
        except FileNotFoundError:
            continue
        except OSError as exc:
            first_error = first_error or exc
            continue

        pending.append((path, True))
        for entry in entries:
            child = Path(entry.path)
            try:
                child_stat = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            except OSError as exc:
                first_error = first_error or exc
                continue
            if stat.S_ISDIR(child_stat.st_mode):
                pending.append((child, False))
                continue
            try:
                child.unlink()
            except FileNotFoundError:
                continue
            except OSError as exc:
                first_error = first_error or exc

    if root.exists():
        if first_error is not None:
            raise OSError(f"Failed to remove owned tree: {root}") from first_error
        raise OSError(f"Failed to remove owned tree: {root}")


def remove_owned_tree(root: Path) -> None:
    """Remove an owned tree, including mode-000 and over-PATH_MAX descendants."""
    if os.name != "posix":
        _remove_owned_tree_by_path(root)
        return
    absolute_root = Path(os.path.abspath(root))
    if not absolute_root.name:
        raise OSError("Refusing to remove a filesystem root")
    try:
        parent_fd = os.open(absolute_root.parent, _OWNED_DIRECTORY_OPEN_FLAGS)
    except FileNotFoundError:
        return
    try:
        try:
            remove_owned_tree_at(parent_fd, absolute_root.name)
        except FileNotFoundError:
            return
    finally:
        os.close(parent_fd)


def _mkdir_private(path: Path, *, parents: bool = False, exist_ok: bool = True) -> None:
    """Create one private directory without changing the mode of an existing tree."""
    try:
        path.mkdir(mode=PRIVATE_DIRECTORY_MODE, parents=parents, exist_ok=False)
    except FileExistsError:
        if not exist_ok or not path.is_dir():
            raise
    else:
        if os.name == "posix":
            path.chmod(PRIVATE_DIRECTORY_MODE)


def _write_private_text(path: Path, value: str) -> None:
    """Exclusively create a private marker, including under a restrictive umask."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, PRIVATE_FILE_MODE)
    try:
        if os.name == "posix":
            os.fchmod(fd, PRIVATE_FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as marker:
            fd = -1
            marker.write(value)
    finally:
        if fd >= 0:
            os.close(fd)


def _validate_explicit_job_id(job_id: str) -> str:
    if not isinstance(job_id, str):
        raise ValueError("job_id must be exactly one safe path segment")
    if (
        not job_id
        or job_id in {".", ".."}
        or job_id != job_id.strip()
        or "/" in job_id
        or "\\" in job_id
        or Path(job_id).is_absolute()
        or PurePosixPath(job_id).is_absolute()
        or PureWindowsPath(job_id).is_absolute()
        or job_id.partition(".")[0].upper() in WINDOWS_RESERVED_NAMES
        or safe_generated_segment(job_id) != job_id
    ):
        raise ValueError("job_id must be exactly one safe path segment")
    return job_id


def workspace_owner_key(user_id: str, guild_id: str | None) -> WorkspaceKey:
    """Composite owner key for the per-(user, guild) file workspace.

    The workspace is keyed by *community*, not just user: a user's files in one
    guild are isolated from their files in another. ``WorkspaceManager`` treats
    this as an opaque, single-level directory segment, so the key stays flat
    (``<user>__<guild>``); the sweeper, quota, and resolver never parse it. DMs
    (no guild) collapse to a shared ``<user>__dm``
    namespace. Both halves are sanitized so synthetic ids (e.g. ``userapp:123``)
    never escape the path.
    """
    guild_token = safe_generated_segment(guild_id) if guild_id else "dm"
    return WorkspaceKey(f"{safe_generated_segment(user_id)}__{guild_token}")


class WorkspacePathSymlinkError(ValueError):
    """A workspace path includes a symlink component.

    Subclasses ValueError so every existing `except ValueError` handler keeps
    working; delete_file catches this precisely to offer safe unlink of a
    host-created link instead of a dead-end error.
    """


def path_contains_symlink(root: Path, candidate: Path) -> bool:
    current = candidate
    to_check: list[Path] = []
    while True:
        to_check.append(current)
        if current == root:
            break
        parent = current.parent
        if parent == current:
            break
        current = parent

    for path in reversed(to_check):
        try:
            if path.exists() and path.is_symlink():
                return True
        except OSError:
            return True
    return False


@dataclass(frozen=True)
class _FileRecord:
    path: Path
    mtime: float
    size: int


@dataclass(frozen=True)
class ResolvedGeneratedFile:
    path: Path
    relative_path: str
    root: Path


class WorkspaceManager:
    def __init__(
        self,
        base_dir: Path,
        file_ttl: int = 86400,
        max_size_bytes: int = 100 * 1024 * 1024,
        env_max_bytes: int = 0,
        env_max_files: int = 0,
    ) -> None:
        self._base_dir = base_dir
        self._file_ttl = file_ttl
        self._max_size_bytes = max_size_bytes
        # Per-owner byte/entry allowances for regenerable env dirs (.venv/.pio);
        # 0 leaves that dimension to age out on the TTL only.
        self._env_max_bytes = env_max_bytes
        self._env_max_files = env_max_files

    def ensure(self, workspace_key: WorkspaceKey) -> Path:
        _mkdir_private(self._base_dir, parents=True)
        path = self._base_dir / workspace_key
        _mkdir_private(path)
        return path

    def user_files_dir(self, workspace_key: WorkspaceKey) -> Path:
        path = self.ensure(workspace_key) / "files"
        _mkdir_private(path)
        return path

    def resolve_user_file_path(
        self,
        workspace_key: WorkspaceKey,
        user_path: str,
        *,
        allow_root: bool = False,
        must_exist: bool = False,
    ) -> Path:
        root = self.user_files_dir(workspace_key).resolve()
        raw = str(user_path or "").strip()
        if raw in {"", "."}:
            if not allow_root:
                raise ValueError("Path must be relative to your workspace")
            return root

        requested = Path(raw)
        if (
            requested.is_absolute()
            or PurePosixPath(raw).is_absolute()
            or PureWindowsPath(raw).is_absolute()
        ):
            raise ValueError("Path must be relative to your workspace")

        parts = requested.parts
        if any(part in {"", ".", ".."} for part in parts):
            raise ValueError("Path traversal is not allowed")
        if len(parts) > MAX_PATH_DEPTH:
            raise ValueError("Path is too deeply nested")

        candidate = root / requested
        if path_contains_symlink(root, candidate):
            raise WorkspacePathSymlinkError("Workspace paths may not include symlinks")

        if must_exist and not candidate.exists():
            raise FileNotFoundError(f"Workspace file not found: {raw}")

        resolved = candidate.resolve(strict=False)
        if not resolved.is_relative_to(root):
            raise ValueError("Path traversal is not allowed")
        return resolved

    def resolve_user_symlink_entry(self, workspace_key: WorkspaceKey, user_path: str) -> Path:
        """Resolve a path whose FINAL component is a symlink, for unlink only.

        A host-side process can leave symlinks a user then cannot delete, because
        resolve_user_file_path rejects any symlink component. Parents get the
        full normalization/containment treatment here; the returned path is the
        link itself (never followed), so the only safe operation on it is
        lstat/unlink.
        """
        root = self.user_files_dir(workspace_key).resolve()
        raw = str(user_path or "").strip()
        requested = Path(raw)
        if (
            not raw
            or requested.is_absolute()
            or PurePosixPath(raw).is_absolute()
            or PureWindowsPath(raw).is_absolute()
        ):
            raise ValueError("Path must be relative to your workspace")
        parts = requested.parts
        if any(part in {"", ".", ".."} for part in parts):
            raise ValueError("Path traversal is not allowed")
        if len(parts) > MAX_PATH_DEPTH:
            raise ValueError("Path is too deeply nested")
        candidate = root / requested
        if path_contains_symlink(root, candidate.parent):
            raise WorkspacePathSymlinkError("Workspace paths may not include symlinks")
        parent = candidate.parent.resolve(strict=False)
        if not parent.is_relative_to(root):
            raise ValueError("Path traversal is not allowed")
        final = parent / candidate.name
        if not final.is_symlink():
            raise FileNotFoundError(f"Workspace file not found: {raw}")
        return final

    def relative_user_file_path(self, workspace_key: WorkspaceKey, path: Path) -> str:
        root = self.user_files_dir(workspace_key).resolve()
        resolved = path.resolve(strict=False)
        if not resolved.is_relative_to(root):
            raise ValueError("Path is outside your workspace")
        return resolved.relative_to(root).as_posix()

    def user_files_size(self, workspace_key: WorkspaceKey) -> int:
        """Total document bytes, excluding regenerable env dirs (.venv/.pio/...).

        This backs the write-tool doc quota, so a large per-user venv/toolchain
        tree must not count against it. The env dirs have their own sweep-time
        allowances (see _sweep_expired_sync).
        """
        root = self.user_files_dir(workspace_key)
        total = 0
        for path in root.rglob("*"):
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                if _has_env_dir_part(path.relative_to(root).parts):
                    continue
                total += path.stat().st_size
            except OSError:
                continue
        return total

    def allowed_output_roots(
        self,
        workspace_key: WorkspaceKey | None = None,
        *,
        context_key: str | None = None,
    ) -> list[Path]:
        roots: list[Path] = []
        if workspace_key:
            roots.append(self.user_files_dir(workspace_key).resolve())
        if context_key:
            generated_root = (self._base_dir / "generated").resolve()
            generated_context_root = (
                generated_root / safe_generated_segment(context_key)
            ).resolve()
            if not generated_context_root.is_relative_to(generated_root):
                raise ValueError("Generated output root escaped workspace")
            roots.append(generated_context_root)
        return roots

    def generated_job_dir(
        self,
        context_key: str,
        job_id: str,
        *,
        owner_user_id: str | None = None,
    ) -> Path:
        safe_context = safe_generated_segment(context_key)
        safe_job = safe_generated_segment(job_id)
        _mkdir_private(self._base_dir, parents=True)
        generated_root = self._base_dir / "generated"
        _mkdir_private(generated_root)
        context_root = generated_root / safe_context
        _mkdir_private(context_root)
        path = context_root / safe_job
        _mkdir_private(path, exist_ok=False)
        if owner_user_id is not None:
            _write_private_text(path / ".owner-user-id", str(owner_user_id))
        return path

    def resolve_generated_file_path(self, generated_path: str, *, must_exist: bool = False) -> Path:
        raw = str(generated_path or "").strip()
        if raw.startswith("/"):
            raise ValueError("Generated path must be relative")
        raw_parts = raw.split("/")
        if not raw_parts or raw_parts[0] != "generated":
            raise ValueError("Generated path must start with generated/")
        if any(part in {"", ".", ".."} for part in raw_parts):
            raise ValueError("Path traversal is not allowed")
        requested = Path(raw)
        if requested.is_absolute():
            raise ValueError("Generated path must be relative")
        root = (self._base_dir / "generated").resolve()
        candidate = self._base_dir / requested
        if must_exist and not candidate.exists():
            raise FileNotFoundError(f"Generated file not found: {raw}")
        resolved = candidate.resolve(strict=False)
        if not resolved.is_relative_to(root):
            raise ValueError("Path traversal is not allowed")
        return resolved

    def resolve_context_generated_file(
        self,
        generated_path: str,
        *,
        context_key: str,
        must_exist: bool = False,
    ) -> ResolvedGeneratedFile:
        path = self.resolve_generated_file_path(
            generated_path,
            must_exist=must_exist,
        )
        relative_path = self.relative_generated_file_path(path)
        root = self.allowed_output_roots(context_key=context_key)[0]
        if not path.is_relative_to(root):
            raise ValueError("Generated file is outside this conversation context")
        raw_candidate = root.parent.parent / Path(generated_path)
        if path_contains_symlink(root, raw_candidate):
            raise ValueError("Generated paths may not include symlinks")
        if path.is_symlink() or (must_exist and not path.is_file()):
            raise ValueError("Generated path is not a file")
        return ResolvedGeneratedFile(path=path, relative_path=relative_path, root=root)

    def relative_generated_file_path(self, path: Path) -> str:
        root = (self._base_dir / "generated").resolve()
        resolved = path.resolve(strict=False)
        if not resolved.is_relative_to(root):
            raise ValueError("Path is outside generated workspace")
        return Path("generated", resolved.relative_to(root)).as_posix()

    def create_job_dir(self, workspace_key: WorkspaceKey, job_id: str | None = None) -> Path:
        job = uuid.uuid4().hex if job_id is None else _validate_explicit_job_id(job_id)
        owner_root = self.ensure(workspace_key)
        jobs_root = owner_root / "jobs"
        _mkdir_private(jobs_root)
        path = jobs_root / job
        _mkdir_private(path, exist_ok=False)
        return path

    def delete_owner_dirs(self, user_id: str) -> int:
        """Remove every per-guild workspace dir belonging to ``user_id``.

        The owner key is ``<user>__<guild>`` (see ``workspace_owner_key``), so a
        user's files spread across one top-level dir per guild. Full data
        deletion (``/privacy`` → Delete my data) wipes them all by prefix. The
        ``__`` delimiter prevents prefix bleed between numeric user ids. Generated
        jobs carry an exact owner marker so full deletion can remove one user's
        artifacts without erasing other participants' jobs in a shared conversation.

        All owned paths are attempted before an aggregate ``OSError`` is raised.
        Callers can therefore report a partial deletion honestly without one
        broken path preventing removal of the user's other workspace data.
        """
        if not self._base_dir.exists():
            return 0
        prefix = f"{safe_generated_segment(user_id)}__"
        removed = 0
        failures: list[tuple[Path, BaseException]] = []
        for child in self._base_dir.iterdir():
            if child.name == "generated" or not child.name.startswith(prefix):
                continue
            try:
                if child.is_symlink() or not child.is_dir():
                    failures.append((child, OSError("Owned workspace path is not a directory.")))
                    continue
            except OSError as exc:
                failures.append((child, exc))
                continue
            try:
                shutil.rmtree(child)
            except FileNotFoundError:
                continue
            except OSError as exc:
                failures.append((child, exc))
                continue
            removed += 1
            log.debug("Removed workspace dir for full deletion: %s", child)
        failures.extend(self._delete_generated_jobs_for_owner(user_id))
        if failures:
            failed_paths = ", ".join(str(path) for path, _error in failures[:3])
            if len(failures) > 3:
                failed_paths = f"{failed_paths}, and {len(failures) - 3} more"
            raise OSError(
                f"Failed to delete or verify {len(failures)} owned workspace "
                f"path(s): {failed_paths}"
            ) from failures[0][1]
        return removed

    def _delete_generated_jobs_for_owner(
        self,
        user_id: str,
    ) -> list[tuple[Path, BaseException]]:
        failures: list[tuple[Path, BaseException]] = []
        generated_root = self._base_dir / "generated"
        if not generated_root.is_dir() or generated_root.is_symlink():
            return failures
        for context_root in generated_root.iterdir():
            if not context_root.is_dir() or context_root.is_symlink():
                continue
            for job_root in context_root.iterdir():
                if not job_root.is_dir() or job_root.is_symlink():
                    continue
                marker = job_root / ".owner-user-id"
                try:
                    if marker.is_symlink() or marker.read_text(encoding="utf-8") != user_id:
                        continue
                except OSError, UnicodeError:
                    continue
                try:
                    shutil.rmtree(job_root)
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    failures.append((job_root, exc))
            with contextlib.suppress(OSError):
                context_root.rmdir()
        return failures

    async def sweep_expired(self, *, excluded_workspace_keys: frozenset[str] = frozenset()) -> int:
        return await asyncio.to_thread(
            self._sweep_expired_sync, excluded_workspace_keys=excluded_workspace_keys
        )

    def _sweep_expired_sync(self, *, excluded_workspace_keys: frozenset[str] = frozenset()) -> int:
        if not self._base_dir.exists():
            return 0

        removed = 0
        now = time.time()

        for root_dir in self._base_dir.iterdir():
            if not root_dir.is_dir():
                continue
            if root_dir.name in excluded_workspace_keys:
                continue

            # Regenerable env dirs (.venv/.pio) are swept as whole units below,
            # separate from documents, so their files never enter the doc TTL /
            # oversize passes (which would leave a broken half-venv).
            removed += self._sweep_env_dirs(root_dir, now)

            files: list[_FileRecord] = []
            for path in self._iter_sweep_files(root_dir):
                try:
                    if path.is_symlink() or not path.is_file():
                        continue
                    if _has_env_dir_part(path.relative_to(root_dir).parts):
                        continue
                    stat = path.stat()
                except OSError:
                    continue
                files.append(_FileRecord(path=path, mtime=stat.st_mtime, size=stat.st_size))

            remaining: list[_FileRecord] = []
            for record in sorted(files, key=lambda record: record.mtime):
                age = now - record.mtime
                if age > self._file_ttl:
                    try:
                        record.path.unlink()
                    except OSError:
                        continue
                    removed += 1
                    log.debug("Removed expired file: %s", record.path)
                else:
                    remaining.append(record)

            total_size = sum(record.size for record in remaining)
            for record in sorted(remaining, key=lambda record: record.mtime):
                if total_size <= self._max_size_bytes:
                    break
                try:
                    record.path.unlink()
                except OSError:
                    continue
                total_size -= record.size
                removed += 1
                log.debug("Pruned oversized file: %s", record.path)

            for directory in sorted(
                (p for p in self._iter_sweep_directories(root_dir)),
                key=lambda p: len(p.parts),
                reverse=True,
            ):
                marker = directory / ".owner-user-id"
                if marker.is_file() and not marker.is_symlink():
                    try:
                        has_payload = any(child != marker for child in directory.iterdir())
                    except OSError:
                        has_payload = True
                    if not has_payload:
                        marker.unlink(missing_ok=True)
                with contextlib.suppress(OSError):
                    directory.rmdir()

        return removed

    def _sweep_env_dirs(self, root_dir: Path, now: float) -> int:
        """Age out / quota-prune regenerable env dirs as whole units.

        Entry counting includes directories, links, special nodes, and empty files.
        A root that crosses the entry allowance is removed after limit + 1 entries;
        it is never fully enumerated merely to decide that it is already abusive.
        """
        if root_dir.name == "generated":
            return 0
        # path, bytes, entries (including root), newest mtime, over cap, scan failed
        infos: list[tuple[Path, int, int, float, bool, bool]] = []
        for env_root in self._iter_env_dir_roots(root_dir):
            try:
                env_root.lstat()
            except OSError:
                # The root was discovered immediately before this measurement.
                # A race or unreadable metadata must not turn into a quota bypass.
                infos.append((env_root, 0, 1, 0.0, False, True))
                continue
            size = 0
            entry_count = 1
            newest = 0.0
            over_entries = False
            scan_failed = False
            pending = [env_root]
            while pending and not over_entries and not scan_failed:
                directory = pending.pop()
                try:
                    iterator = os.scandir(directory)
                except OSError:
                    scan_failed = True
                    break
                with iterator:
                    for entry in iterator:
                        entry_count += 1
                        if self._env_max_files > 0 and entry_count > self._env_max_files:
                            over_entries = True
                            break
                        try:
                            path_stat = entry.stat(follow_symlinks=False)
                        except OSError:
                            scan_failed = True
                            break
                        size += path_stat.st_size
                        newest = max(newest, path_stat.st_mtime)
                        if stat.S_ISDIR(path_stat.st_mode):
                            pending.append(Path(entry.path))
            infos.append((env_root, size, entry_count, newest, over_entries, scan_failed))

        removed = 0
        surviving: list[tuple[Path, int, int, float]] = []
        for env_root, size, entry_count, newest, over_entries, scan_failed in infos:
            expired = bool(newest and (now - newest) > self._file_ttl)
            if over_entries or expired or scan_failed:
                try:
                    remove_owned_tree(env_root)
                except OSError:
                    surviving.append((env_root, size, entry_count, newest))
                    continue
                removed += 1
                reason = (
                    "unreadable tree"
                    if scan_failed
                    else "entry limit"
                    if over_entries
                    else "expiry"
                )
                log.debug("Removed env dir for %s: %s", reason, env_root)
            else:
                surviving.append((env_root, size, entry_count, newest))

        env_bytes = sum(size for _, size, _, _ in surviving)
        env_entries = sum(entry_count for _, _, entry_count, _ in surviving)
        for env_root, size, entry_count, _ in sorted(surviving, key=lambda item: item[3]):
            if (self._env_max_bytes <= 0 or env_bytes <= self._env_max_bytes) and (
                self._env_max_files <= 0 or env_entries <= self._env_max_files
            ):
                break
            try:
                remove_owned_tree(env_root)
            except OSError:
                continue
            env_bytes -= size
            env_entries -= entry_count
            removed += 1
            log.debug("Pruned oversized env dir: %s", env_root)
        return removed

    def _iter_env_dir_roots(self, root_dir: Path):
        """Yield outermost env dirs (.venv/.pio) under files/, not descending in."""
        files_dir = root_dir / "files"
        if not files_dir.exists():
            return
        stack = [files_dir]
        while stack:
            current = stack.pop()
            try:
                entries = os.scandir(current)
            except OSError:
                continue
            with entries:
                for entry in entries:
                    try:
                        if not entry.is_dir(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    child = Path(entry.path)
                    if entry.name in ENV_DIR_NAMES:
                        yield child
                    else:
                        stack.append(child)

    def _iter_sweep_files(self, root_dir: Path):
        if root_dir.name == "generated":
            yield from (path for path in root_dir.rglob("*") if path.name != ".owner-user-id")
            return
        files_dir = root_dir / "files"
        if files_dir.exists():
            yield from files_dir.rglob("*")
        jobs_dir = root_dir / "jobs"
        if jobs_dir.exists():
            yield from jobs_dir.rglob("*")

    def _iter_sweep_directories(self, root_dir: Path):
        if root_dir.name == "generated":
            yield from (p for p in root_dir.rglob("*") if p.is_dir())
            return
        jobs_dir = root_dir / "jobs"
        if jobs_dir.exists():
            yield from (p for p in jobs_dir.rglob("*") if p.is_dir())


def safe_generated_segment(value: str) -> str:
    clean = GENERATED_SEGMENT_RE.sub("_", value).strip("._")
    return clean or "generated"
