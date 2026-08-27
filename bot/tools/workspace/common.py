from __future__ import annotations

import asyncio
import os
import shutil
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from pathlib import Path

from workspace import WorkspaceKey, ENV_DIR_NAMES, WorkspaceManager
from tools._common import tool_error as tool_error
from utils.parsing import as_bool as as_bool
from tools.output_queue import AttachmentLimitError, enqueue_workspace_file
from tools.registry import MessageContext

from .config import WorkspaceToolConfig


ATTACHMENT_HINT = (
    "Saved to the workspace but not attached; use queue_file to attach it to the reply."
)


class EnvDirWriteError(ValueError):
    """Write refused because the target lives under a reserved environment dir.

    Deliberately distinct from the quota ValueError: surfacing this as "quota
    exceeded" sends the model into a delete-and-retry loop that can never
    succeed. quota_ok re-raises it so the self-correcting message always
    reaches the model.
    """


class UserLocks:
    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._owned_operation_locks: dict[str, asyncio.Lock] = {}
        self._condition = asyncio.Condition()
        self._active = 0
        self._active_workspaces: dict[str, int] = {}
        self._user_reservations: dict[str, int] = {}
        self._maintenance_active = False
        self._maintenance_waiters = 0
        self._writer_counts: dict[str, int] = {}

    def for_user(self, workspace_key: WorkspaceKey) -> asyncio.Lock:
        lock = self._locks.get(workspace_key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[workspace_key] = lock
        return lock

    def owned_operation(self, workspace_key: WorkspaceKey) -> asyncio.Lock:
        """Serialize children while a durable task owns the outer user lock."""

        lock = self._owned_operation_locks.get(workspace_key)
        if lock is None:
            lock = asyncio.Lock()
            self._owned_operation_locks[workspace_key] = lock
        return lock

    @asynccontextmanager
    async def activity(self, workspace_key: WorkspaceKey) -> AsyncIterator[None]:
        """Serialize one workspace and exclude global sweep/deletion maintenance."""

        async with self._condition:
            await self._condition.wait_for(
                lambda: (
                    not self._maintenance_active
                    and self._maintenance_waiters == 0
                    and self._writer_counts.get(workspace_key, 0) == 0
                )
            )
            self._active += 1
            self._active_workspaces[workspace_key] = (
                self._active_workspaces.get(workspace_key, 0) + 1
            )
        try:
            async with self.for_user(workspace_key):
                yield
        finally:
            async with self._condition:
                self._active -= 1
                workspace_active = self._active_workspaces.get(workspace_key, 0) - 1
                if workspace_active > 0:
                    self._active_workspaces[workspace_key] = workspace_active
                else:
                    self._active_workspaces.pop(workspace_key, None)
                self._condition.notify_all()

    @asynccontextmanager
    async def writer(self, workspace_key: WorkspaceKey) -> AsyncIterator[None]:
        """Own one workspace across a durable task without blocking global sweeps."""

        async with self._condition:
            await self._condition.wait_for(
                lambda: (
                    not self._maintenance_active
                    and self._writer_counts.get(workspace_key, 0) == 0
                    and self._active_workspaces.get(workspace_key, 0) == 0
                    and self._user_reservations.get(workspace_key, 0) == 0
                )
            )
            self._writer_counts[workspace_key] = 1
        try:
            yield
        finally:
            await self._release_writer_reference(workspace_key)

    @asynccontextmanager
    async def serialized_user(self, workspace_key: WorkspaceKey) -> AsyncIterator[None]:
        """Take only the per-user lock while excluding a durable writer."""

        async with self._condition:
            await self._condition.wait_for(lambda: self._writer_counts.get(workspace_key, 0) == 0)
            self._user_reservations[workspace_key] = (
                self._user_reservations.get(workspace_key, 0) + 1
            )
        try:
            async with self.for_user(workspace_key):
                yield
        finally:
            async with self._condition:
                remaining = self._user_reservations.get(workspace_key, 0) - 1
                if remaining > 0:
                    self._user_reservations[workspace_key] = remaining
                else:
                    self._user_reservations.pop(workspace_key, None)
                self._condition.notify_all()

    @asynccontextmanager
    async def writer_reference(self, workspace_key: WorkspaceKey) -> AsyncIterator[None]:
        """Keep a durable writer visible while a detached child finishes."""

        async with self._condition:
            if self._writer_counts.get(workspace_key, 0) <= 0:
                raise RuntimeError("workspace writer reference requires an active writer")
            self._writer_counts[workspace_key] += 1
        try:
            yield
        finally:
            await self._release_writer_reference(workspace_key)

    async def _release_writer_reference(self, workspace_key: WorkspaceKey) -> None:
        async with self._condition:
            remaining = self._writer_counts.get(workspace_key, 0) - 1
            if remaining > 0:
                self._writer_counts[workspace_key] = remaining
            else:
                self._writer_counts.pop(workspace_key, None)
            self._condition.notify_all()

    async def writer_keys(self) -> frozenset[str]:
        async with self._condition:
            return frozenset(self._writer_counts)

    async def wait_writer_idle(self, workspace_key: WorkspaceKey) -> None:
        async with self._condition:
            await self._condition.wait_for(lambda: self._writer_counts.get(workspace_key, 0) == 0)

    @asynccontextmanager
    async def barrier(self) -> AsyncIterator[None]:
        """Exclude global maintenance without taking a per-user lock.

        For callers that already hold ``for_user`` (e.g. fetch_url serializes a
        user's downloads across the slow network read, then needs only the sweep
        exclusion for the quick finalize). Composing barrier() inside for_user()
        is deadlock-free: maintenance never takes user locks.
        """
        async with self._condition:
            await self._condition.wait_for(
                lambda: not self._maintenance_active and self._maintenance_waiters == 0
            )
            self._active += 1
        try:
            yield
        finally:
            async with self._condition:
                self._active -= 1
                if self._active == 0:
                    self._condition.notify_all()

    @asynccontextmanager
    async def maintenance(self) -> AsyncIterator[None]:
        """Drain workspace activity and block new work for a global sweep/wipe."""

        async with self._condition:
            self._maintenance_waiters += 1
            try:
                await self._condition.wait_for(
                    lambda: not self._maintenance_active and self._active == 0
                )
                self._maintenance_active = True
            finally:
                self._maintenance_waiters -= 1
                self._condition.notify_all()
        try:
            yield
        finally:
            async with self._condition:
                self._maintenance_active = False
                self._condition.notify_all()


@asynccontextmanager
async def workspace_activity(locks: UserLocks, ctx: MessageContext) -> AsyncIterator[None]:
    """Use the shared workspace lease unless a coding worker already owns it."""

    if ctx.workspace_lock_held:
        async with locks.owned_operation(ctx.workspace_key):
            yield
        return
    async with locks.activity(ctx.workspace_key):
        yield


@asynccontextmanager
async def workspace_user_lock(locks: UserLocks, ctx: MessageContext) -> AsyncIterator[None]:
    """Serialize a workspace unless the durable coding task owns the lease."""

    if ctx.workspace_lock_held:
        async with locks.owned_operation(ctx.workspace_key):
            yield
        return
    async with locks.serialized_user(ctx.workspace_key):
        yield


@asynccontextmanager
async def workspace_barrier(locks: UserLocks, ctx: MessageContext) -> AsyncIterator[None]:
    """Exclude maintenance unless the durable coding task already does so."""

    if ctx.workspace_lock_held:
        yield
        return
    async with locks.barrier():
        yield


def available_destination(
    workspace_manager: WorkspaceManager,
    workspace_key: WorkspaceKey,
    filename: str,
) -> Path:
    destination = workspace_manager.resolve_user_file_path(workspace_key, filename)
    if not destination.exists():
        return destination

    requested = Path(filename)
    parent = requested.parent
    stem = requested.stem or "download"
    suffix = requested.suffix
    for index in range(2, 1000):
        candidate = parent / f"{stem}-{index}{suffix}"
        destination = workspace_manager.resolve_user_file_path(
            workspace_key,
            candidate.as_posix(),
        )
        if not destination.exists():
            return destination
    raise ValueError("Could not choose an unused filename")


def in_env_dir(root: Path, candidate: Path) -> bool:
    """True if candidate lives under a regenerable env dir (.venv/.pio)."""
    try:
        parts = candidate.resolve(strict=False).relative_to(root).parts
    except ValueError:
        return False
    return any(part in ENV_DIR_NAMES for part in parts)


def ensure_not_env_dir(
    workspace_manager: WorkspaceManager,
    workspace_key: WorkspaceKey,
    destination: Path,
) -> None:
    """Refuse a write/move/extract target under a reserved environment dir.

    Env-dir bytes are excluded from user_files_size, so any tool that can place
    files there (not just direct writes, but moves and extractions too) would let a
    user evade the doc quota and hide bytes from listings.
    """
    files_root = workspace_manager.user_files_dir(workspace_key).resolve()
    if in_env_dir(files_root, destination):
        raise EnvDirWriteError(
            "environment directories (.venv/.pio) are reserved for separate workspace accounting and "
            "cannot be written to, moved into, or extracted into; use a normal "
            "workspace path instead"
        )


def format_quota_error(used: int, limit: int) -> str:
    return (
        f"would exceed your workspace quota ({used}/{limit} bytes); "
        "delete files you no longer need to free space"
    )


def ensure_quota(
    workspace_manager: WorkspaceManager,
    workspace_key: WorkspaceKey,
    *,
    new_size: int,
    destination: Path,
    temp_path: Path | None,
    max_user_bytes: int,
    max_entries: int | None = None,
) -> None:
    ensure_not_env_dir(workspace_manager, workspace_key, destination)
    files_root = workspace_manager.user_files_dir(workspace_key).resolve()
    creates_entry = not destination.exists()
    if (
        max_entries is not None
        and creates_entry
        and count_entries_up_to(files_root, max_entries) >= max_entries
    ):
        raise ValueError(
            f"workspace holds too many files (limit {max_entries} entries); "
            "delete files or directories you no longer need"
        )
    current_size = workspace_manager.user_files_size(workspace_key)
    existing_size = 0
    if destination.exists() and destination.is_file() and not destination.is_symlink():
        existing_size = destination.stat().st_size
    temp_size = 0
    if temp_path and temp_path.exists():
        temp_size = temp_path.stat().st_size
    projected_size = current_size - existing_size - temp_size + new_size
    if projected_size > max_user_bytes:
        raise ValueError(format_quota_error(current_size, max_user_bytes))


def quota_ok(
    workspace_manager: WorkspaceManager,
    workspace_key: WorkspaceKey,
    *,
    new_size: int,
    destination: Path,
    temp_path: Path | None,
    max_user_bytes: int,
    max_entries: int | None = None,
) -> bool:
    try:
        ensure_quota(
            workspace_manager,
            workspace_key,
            new_size=new_size,
            destination=destination,
            temp_path=temp_path,
            max_user_bytes=max_user_bytes,
            max_entries=max_entries,
        )
        return True
    except EnvDirWriteError:
        # Never collapse into "quota exceeded"; the model must see the real reason.
        raise
    except ValueError:
        return False


def scrub_user_paths(
    message: str,
    workspace_manager: WorkspaceManager,
    workspace_key: WorkspaceKey,
) -> str:
    """Strip the absolute workspace prefix from error text.

    OSError messages embed absolute server paths; tool results are
    model-visible and can be quoted to Discord users, so only the
    workspace-relative part may appear.
    """
    root = str(workspace_manager.user_files_dir(workspace_key).resolve())
    return message.replace(root + os.sep, "").replace(root, ".")


def delete_tree_with_entry_cap(path: Path, max_entries: int) -> int:
    entry_count = count_entries_up_to(path, max_entries)
    if entry_count > max_entries:
        return entry_count
    shutil.rmtree(path)
    return entry_count


def count_entries_up_to(path: Path, limit: int) -> int:
    count = 0
    for _entry in path.rglob("*"):
        count += 1
        if count > limit:
            return count
    return count


def read_text_file(path: Path, config: WorkspaceToolConfig) -> str:
    try:
        size = path.stat().st_size
    except OSError as e:
        raise ValueError(f"Could not read file: {e}") from e
    if size > config.max_read_bytes:
        raise ValueError(f"File exceeds read limit of {config.max_read_bytes} bytes")
    data = path.read_bytes()
    if b"\x00" in data:
        raise ValueError("Binary files cannot be read")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ValueError("Binary files cannot be read") from e


def clamped_int(
    value: object,
    *,
    name: str = "value",
    default: int,
    minimum: int,
    maximum: int | None = None,
) -> int:
    """Parse an integer argument, clamping above the maximum.

    Named for the half that surprises: `tools/_common.py:get_int` takes the same
    arguments and *raises* above its maximum, so the two are not
    interchangeable.

    Unparseable input and values below the minimum raise (silently coercing
    `offset: "abc"` to a default means the model gets a plausible success for an
    operation it did not ask for and never learns the schema). Values above the
    maximum are clamped: "give me 500" against a 200 cap should return 200
    results, not an error.
    """
    if value is None:
        parsed = default
    elif isinstance(value, bool):
        raise ValueError(f"{name} must be an integer, got a boolean")
    elif isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = int(value.strip())
        except ValueError:
            raise ValueError(f"{name} must be an integer, got {value!r}") from None
    else:
        raise ValueError(f"{name} must be an integer")
    if parsed < minimum:
        raise ValueError(f"{name} must be at least {minimum}, got {parsed}")
    if maximum is not None:
        parsed = min(parsed, maximum)
    return parsed


def try_enqueue_workspace_file(
    ctx: MessageContext,
    workspace_manager: WorkspaceManager,
    path: Path,
    config: WorkspaceToolConfig,
) -> bool:
    try:
        return enqueue_workspace_file(
            ctx,
            workspace_manager,
            path,
            max_attachments=config.max_attachments,
        ).added
    except AttachmentLimitError:
        return False
