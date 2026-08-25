from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from workspace.manager import (
    ENV_DIR_NAMES,
    OwnedTreeRemovalError,
    WorkspaceKey,
    WorkspaceManager,
    open_owned_directory_at,
    open_owned_directory_path_at,
    remove_owned_tree_at,
)


@dataclass(frozen=True)
class FileState:
    size: int
    mtime_ns: int
    kind: str = "file"


@dataclass(frozen=True)
class QuotaCleanup:
    removed_entries: int = 0
    removed_bytes: int = 0
    removed_env_dirs: int = 0
    complete: bool = True


def snapshot_workspace(
    workspace_manager: WorkspaceManager,
    user_id: WorkspaceKey,
    *,
    max_workspace_files: int,
    max_env_roots: int,
) -> tuple[dict[str, FileState], bool]:
    """Return a bounded snapshot and whether the complete tree was captured."""
    root = workspace_manager.user_files_dir(user_id)
    snapshot: dict[str, FileState] = {}
    pending = [root]
    ordinary_entries = 0
    env_roots = 0
    complete = True
    while pending:
        directory = pending.pop()
        try:
            entries = os.scandir(directory)
        except OSError:
            complete = False
            continue
        with entries:
            for entry in entries:
                path = Path(entry.path)
                try:
                    rel = path.relative_to(root).as_posix()
                    path_stat = entry.stat(follow_symlinks=False)
                except OSError, ValueError:
                    complete = False
                    continue
                # Env trees churn thousands of files per install/build. Record
                # their roots for quota cleanup, but prune traversal entirely so
                # an abusive tree cannot make every snapshot enumerate it.
                if entry.name in ENV_DIR_NAMES:
                    env_roots += 1
                    snapshot[rel] = FileState(
                        size=path_stat.st_size,
                        mtime_ns=path_stat.st_mtime_ns,
                        kind="env_root",
                    )
                    if max_env_roots > 0 and env_roots > max_env_roots:
                        return snapshot, False
                    continue
                ordinary_entries += 1
                if max_workspace_files > 0 and ordinary_entries > max_workspace_files:
                    return snapshot, False
                kind = (
                    "file"
                    if stat.S_ISREG(path_stat.st_mode)
                    else "dir"
                    if stat.S_ISDIR(path_stat.st_mode)
                    else "other"
                )
                snapshot[rel] = FileState(
                    size=path_stat.st_size,
                    mtime_ns=path_stat.st_mtime_ns,
                    kind=kind,
                )
                if kind == "dir":
                    pending.append(path)
    return snapshot, complete


def cleanup_quota_created_entries(
    workspace_manager: WorkspaceManager,
    user_id: WorkspaceKey,
    before: dict[str, FileState],
    *,
    remove_preexisting_envs: bool = False,
    remove_new_ordinary: bool = True,
) -> QuotaCleanup:
    """Prune a quota-violating run without rolling back ordinary user files.

    Ordinary paths are removed only when a complete bounded snapshot proves they
    did not exist before the run. Environment trees are explicitly regenerable,
    so an environment quota violation may remove roots captured before the run.
    """
    root = workspace_manager.user_files_dir(user_id)
    if not remove_new_ordinary and not remove_preexisting_envs:
        return QuotaCleanup()
    removed_entries = 0
    removed_bytes = 0
    removed_env_dirs = 0
    complete = True

    def remove_env_root(
        parent_fd: int,
        name: str,
        path_stat: os.stat_result,
    ) -> None:
        nonlocal complete, removed_entries, removed_bytes, removed_env_dirs
        try:
            if stat.S_ISDIR(path_stat.st_mode):
                remove_owned_tree_at(parent_fd, name)
                removed_env_dirs += 1
            else:
                os.unlink(name, dir_fd=parent_fd)
        except FileNotFoundError:
            return
        except OwnedTreeRemovalError as exc:
            removed_entries += exc.removal.entries
            removed_bytes += exc.removal.bytes
            complete = False
            return
        except OSError:
            complete = False
            return
        # Descendant statistics would require a duplicate walk of hostile trees.
        removed_entries += 1
        removed_bytes += path_stat.st_size

    owner_fd = -1
    root_fd = -1
    try:
        owner_fd = os.open(
            root.parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        root_fd = open_owned_directory_at(owner_fd, root.name)

        if not remove_new_ordinary:
            # An incomplete snapshot cannot prove which ordinary paths are new,
            # but exact reserved names remain safe to remove when their dedicated
            # quota was violated. Inspect only the root and snapshot-proven
            # directories; never enumerate uncertain ordinary contents.
            parent_paths: set[tuple[str, ...]] = {()}
            parent_paths.update(
                tuple(rel.split("/")) for rel, state in before.items() if state.kind == "dir"
            )
            for parts in parent_paths:
                parent_directory_fd = -1
                try:
                    parent_directory_fd = open_owned_directory_path_at(
                        root_fd,
                        parts,
                    )
                    for env_name in ENV_DIR_NAMES:
                        try:
                            path_stat = os.stat(
                                env_name,
                                dir_fd=parent_directory_fd,
                                follow_symlinks=False,
                            )
                        except FileNotFoundError:
                            continue
                        except OSError:
                            complete = False
                            continue
                        remove_env_root(parent_directory_fd, env_name, path_stat)
                except FileNotFoundError:
                    continue
                except OSError:
                    complete = False
                finally:
                    if parent_directory_fd >= 0:
                        os.close(parent_directory_fd)
            return QuotaCleanup(
                removed_entries=removed_entries,
                removed_bytes=removed_bytes,
                removed_env_dirs=removed_env_dirs,
                complete=complete,
            )

        pending: list[tuple[str, ...]] = [()]
        while pending:
            directory_parts = pending.pop()
            directory_fd = -1
            try:
                directory_fd = open_owned_directory_path_at(root_fd, directory_parts)
                try:
                    with os.scandir(directory_fd) as iterator:
                        entries = list(iterator)
                except OSError:
                    complete = False
                    continue
                for entry in entries:
                    rel_parts = (*directory_parts, entry.name)
                    rel = "/".join(rel_parts)
                    try:
                        path_stat = os.stat(
                            entry.name,
                            dir_fd=directory_fd,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError:
                        continue
                    except OSError:
                        complete = False
                        continue
                    if entry.name in ENV_DIR_NAMES:
                        if rel in before and not remove_preexisting_envs:
                            continue
                        remove_env_root(directory_fd, entry.name, path_stat)
                        continue

                    if stat.S_ISDIR(path_stat.st_mode):
                        if rel in before:
                            pending.append(rel_parts)
                            continue
                        try:
                            removal = remove_owned_tree_at(directory_fd, entry.name)
                        except OwnedTreeRemovalError as exc:
                            removed_entries += exc.removal.entries
                            removed_bytes += exc.removal.bytes
                            complete = False
                            continue
                        except OSError:
                            complete = False
                            continue
                        removed_entries += removal.entries
                        removed_bytes += removal.bytes
                        continue
                    if rel in before:
                        continue
                    try:
                        os.unlink(entry.name, dir_fd=directory_fd)
                    except FileNotFoundError:
                        continue
                    except OSError:
                        complete = False
                        continue
                    removed_entries += 1
                    removed_bytes += path_stat.st_size
            except FileNotFoundError:
                continue
            except OSError:
                complete = False
                continue
            finally:
                if directory_fd >= 0:
                    os.close(directory_fd)
    except OSError:
        complete = False
    finally:
        if root_fd >= 0:
            os.close(root_fd)
        if owner_fd >= 0:
            os.close(owner_fd)

    return QuotaCleanup(
        removed_entries=removed_entries,
        removed_bytes=removed_bytes,
        removed_env_dirs=removed_env_dirs,
        complete=complete,
    )
