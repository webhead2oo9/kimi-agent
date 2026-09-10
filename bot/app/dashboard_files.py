"""Private, bounded file snapshots and inert previews for the dashboard."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import shutil
import stat
import tempfile
import time
from dataclasses import dataclass
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from aiohttp import web

from config.settings import Settings
from storage.dashboard import DashboardConversation, DashboardFile, DashboardStore
from storage.dashboard_branches import FileCopier
from tools.downloads import safe_filename
from tools.workspace.common import UserLocks
from tools.workspace.documents import (
    ANYDOC_EXTENSIONS,
    _convert_office_document,
    _extract_pdf_text,
    _run_parser_worker,
)
from utils.asyncio import await_uncancellable
from utils.image_types import decoded_image_media_type
from workspace import WorkspaceKey, WorkspaceManager, workspace_owner_key

PREVIEW_CHARS = 100_000
_RASTER_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
log = logging.getLogger(__name__)


def read_regular(path: Path, limit: int) -> bytes:
    """Walk with directory descriptors so symlink swaps cannot escape a snapshot."""
    if not path.is_absolute():
        raise ValueError("Expected an absolute file path")
    if ".." in path.parts:
        raise ValueError("Path traversal is not allowed")
    parent = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != os.getuid()
                or info.st_size > limit
            ):
                raise ValueError("File is unavailable or exceeds the download limit")
            payload = handle.read(limit + 1)
            if len(payload) > limit:
                raise ValueError("File exceeds the download limit")
            return payload
    finally:
        os.close(parent)


@dataclass(frozen=True, slots=True)
class DashboardAttachment:
    filename: str
    size: int
    content_type: str
    payload: bytes
    url: str = ""

    async def read(self, **_kwargs: Any) -> bytes:
        return self.payload


class DashboardFiles:
    def __init__(
        self,
        *,
        store: DashboardStore,
        workspace: WorkspaceManager,
        locks: UserLocks,
        settings: Settings,
    ) -> None:
        self.store, self.workspace, self.locks, self.settings = store, workspace, locks, settings
        self._parser = asyncio.Semaphore(1)
        self._io = asyncio.Semaphore(4)

    @property
    def upload_limit(self) -> int:
        return min(
            self.settings.workspace_tool_max_import_bytes,
            self.settings.workspace_tool_max_file_bytes,
        )

    @staticmethod
    def context(chat_id: str, kind: str) -> str:
        return f"dashboard-{'uploads' if kind == 'upload' else 'delivery'}:{chat_id}"

    async def save(
        self,
        chat: DashboardConversation,
        filename: str,
        payload: bytes,
        *,
        kind: str,
        workspace_guard_held: bool = False,
    ) -> DashboardFile:
        limit = (
            self.upload_limit if kind == "upload" else self.settings.workspace_tool_max_file_bytes
        )
        if len(payload) > limit:
            raise web.HTTPRequestEntityTooLarge(max_size=limit, actual_size=len(payload))
        name = safe_filename(filename or "attachment")[:180]
        key = workspace_owner_key(chat.user_id, chat.guild_id)
        # Callers with an outer workspace lease must not reacquire the global
        # maintenance barrier: a queued sweep would deadlock both leases.
        file_key = WorkspaceKey(f"dashboard-files:{key}")
        guard = (
            self.locks.serialized_user(file_key)
            if workspace_guard_held
            else self.locks.activity(file_key)
        )
        async with guard:
            async with self.store.db.conn.execute(
                "SELECT coalesce(sum(size),0),count(*) FROM dashboard_files "
                "WHERE owner_user_id=? AND guild_id=? AND created_at>?",
                (chat.user_id, chat.guild_id, time.time() - self.settings.workspace_file_ttl),
            ) as cursor:
                row = await cursor.fetchone()
            assert row is not None
            if (
                row[0] + len(payload) > self.settings.workspace_tool_max_user_bytes
                or row[1] >= 1000
            ):
                raise web.HTTPConflict(
                    reason="Dashboard file quota reached; let older files expire"
                )

            created_directory: Path | None = None
            saved = False

            def write() -> tuple[str, str]:
                nonlocal created_directory
                directory = self.workspace.generated_job_dir(
                    self.context(chat.id, kind),
                    uuid4().hex,
                    owner_user_id=chat.user_id,
                )
                created_directory = directory
                path = directory / name
                with path.open("xb") as output:
                    output.write(payload)
                path.chmod(0o600)
                raster = (
                    decoded_image_media_type(payload) if len(payload) <= self.upload_limit else None
                )
                media = raster or mimetypes.guess_type(name)[0] or "application/octet-stream"
                return self.workspace.relative_generated_file_path(path), media

            async def persist() -> DashboardFile:
                nonlocal saved
                relative, media = await asyncio.to_thread(write)
                record = await self.store.add_file(
                    chat,
                    path=relative,
                    filename=name,
                    media_type=media,
                    size=len(payload),
                    kind=kind,
                )
                saved = True
                return record

            try:
                return await await_uncancellable(persist())
            finally:
                if not saved and created_directory is not None:
                    await await_uncancellable(asyncio.to_thread(shutil.rmtree, created_directory))

    async def payload(self, record: DashboardFile) -> bytes:
        def read() -> bytes:
            resolved = self.workspace.resolve_context_generated_file(
                record.path,
                context_key=self.context(record.dashboard_id, record.kind),
                must_exist=True,
            )
            return read_regular(resolved.path, self.settings.workspace_tool_max_file_bytes)

        try:
            async with self._io:
                return await await_uncancellable(asyncio.to_thread(read))
        except OSError, ValueError:
            raise web.HTTPGone(reason="This file expired or is no longer available") from None

    @asynccontextmanager
    async def branch_copies(self, owner: DashboardConversation) -> AsyncIterator[FileCopier]:
        """Lease files before the DB transaction; the caller persists copied metadata.

        Wrap the whole context and transaction in await_uncancellable so writes
        and rollback cleanup finish together even if the HTTP client disconnects.
        """
        created: list[tuple[Path, str]] = []
        key = WorkspaceKey(f"dashboard-files:{workspace_owner_key(owner.user_id, owner.guild_id)}")

        async def copy(file_id: str, destination: str) -> DashboardFile | None:
            source = await self.store.file(file_id, user_id=owner.user_id, guild_id=owner.guild_id)
            if source is None:
                return None
            try:
                payload = await self.payload(source)
            except web.HTTPGone:
                return None
            async with self.store.db.conn.execute(
                "SELECT coalesce(sum(size),0),count(*) FROM dashboard_files "
                "WHERE owner_user_id=? AND guild_id=? AND created_at>?",
                (owner.user_id, owner.guild_id, time.time() - self.settings.workspace_file_ttl),
            ) as cursor:
                row = await cursor.fetchone()
            assert row is not None
            if (
                row[0] + len(payload) > self.settings.workspace_tool_max_user_bytes
                or row[1] >= 1000
            ):
                raise web.HTTPConflict(
                    reason="Dashboard file quota reached; let older files expire"
                )
            name = safe_filename(source.filename)[:180]

            def write() -> str:
                directory = self.workspace.generated_job_dir(
                    self.context(destination, source.kind), uuid4().hex, owner_user_id=owner.user_id
                )
                path = directory / name
                relative = self.workspace.relative_generated_file_path(path)
                created.append((directory, relative))
                with path.open("xb") as output:
                    output.write(payload)
                path.chmod(0o600)
                return relative

            path = await asyncio.to_thread(write)
            return DashboardFile(
                uuid4().hex,
                destination,
                owner.user_id,
                owner.guild_id,
                path,
                name,
                source.media_type,
                len(payload),
                source.kind,
                time.time(),
            )

        async with self.locks.activity(key):
            try:
                yield copy
            except BaseException:
                # Retain committed copies if a later read failed. Only remove
                # this attempt's paths that have no durable metadata reference.
                for directory, relative in created:
                    async with self.store.db.conn.execute(
                        "SELECT 1 FROM dashboard_files WHERE path=? LIMIT 1",
                        (relative,),
                    ) as cursor:
                        referenced = await cursor.fetchone()
                    if referenced is None:
                        await asyncio.to_thread(shutil.rmtree, directory)
                raise

    async def delete_chat(self, chat: DashboardConversation) -> None:
        """Remove private snapshots before their quota records are deleted."""
        key = workspace_owner_key(chat.user_id, chat.guild_id)

        def remove() -> None:
            for kind in ("upload", "output"):
                root = self.workspace.allowed_output_roots(context_key=self.context(chat.id, kind))[
                    0
                ]
                if root.is_symlink():
                    raise ValueError("Invalid dashboard snapshot directory")
                if root.exists():
                    shutil.rmtree(root)

        async with self.locks.activity(WorkspaceKey(f"dashboard-files:{key}")):
            await await_uncancellable(asyncio.to_thread(remove))

    async def attachments(
        self,
        chat: DashboardConversation,
        file_ids: list[str],
    ) -> tuple[list[DashboardAttachment], list[dict[str, Any]]]:
        if len(file_ids) > 10 or len(set(file_ids)) != len(file_ids):
            raise web.HTTPBadRequest(reason="Attach at most 10 different files")
        attachments, public = [], []
        total = 0
        for file_id in file_ids:
            record = await self.store.file(file_id, user_id=chat.user_id, guild_id=chat.guild_id)
            if record is None or record.dashboard_id != chat.id or record.kind != "upload":
                raise web.HTTPNotFound(reason="Attachment not found")
            total += record.size
            if total > self.upload_limit:
                raise web.HTTPRequestEntityTooLarge(max_size=self.upload_limit, actual_size=total)
            payload = await self.payload(record)
            attachments.append(
                DashboardAttachment(record.filename, len(payload), record.media_type, payload)
            )
            public.append(record.public())
        return attachments, public

    async def snapshot(
        self,
        chat: DashboardConversation,
        paths: tuple[str, ...],
        *,
        source_context: str | None = None,
        workspace_guard_held: bool = False,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        key = workspace_owner_key(chat.user_id, chat.guild_id)
        if not paths:
            return results
        if not workspace_guard_held:
            async with self.locks.activity(key):
                return await self.snapshot(
                    chat, paths, source_context=source_context, workspace_guard_held=True
                )
        for raw in paths[:10]:

            def read(raw: str = raw) -> tuple[str, bytes]:
                # The shared turn runner stages files relative to WORKSPACE_DIR
                # when that setting is relative. Make the path absolute without
                # resolving symlinks; read_regular must still reject those.
                path = Path(raw).absolute()
                roots = self.workspace.allowed_output_roots(
                    key, context_key=source_context or chat.key
                )
                if not any(path.is_relative_to(root) for root in roots):
                    raise ValueError("Output is outside this conversation's files")
                return path.name, read_regular(path, self.settings.workspace_tool_max_file_bytes)

            try:
                name, payload = await await_uncancellable(asyncio.to_thread(read))
                record = await self.save(
                    chat, name, payload, kind="output", workspace_guard_held=True
                )
                results.append(record.public())
            except (OSError, ValueError, web.HTTPException) as exc:
                log.warning("Dashboard attachment snapshot failed (%s)", type(exc).__name__)
                results.append(
                    {"filename": safe_filename(Path(raw).name)[:180], "unavailable": True}
                )
        return results

    async def workspace_files(
        self, chat: DashboardConversation, directory: str = ""
    ) -> list[dict[str, Any]]:
        key = workspace_owner_key(chat.user_id, chat.guild_id)

        def listing() -> list[dict[str, Any]]:
            parent = self.workspace.resolve_user_file_path(
                key, directory, allow_root=True, must_exist=True
            )
            result: list[dict[str, Any]] = []
            for path in sorted(parent.iterdir(), key=lambda p: p.name.casefold()):
                if path.name.startswith(".") or path.is_symlink():
                    continue
                if len(result) >= 200:
                    break
                result.append(
                    {
                        "path": self.workspace.relative_user_file_path(key, path),
                        "filename": path.name,
                        "directory": path.is_dir(),
                        "size": path.stat().st_size if path.is_file() else 0,
                    }
                )
            return result

        async with self.locks.activity(key):
            return await await_uncancellable(asyncio.to_thread(listing))

    async def snapshot_workspace(self, chat: DashboardConversation, relative: str) -> DashboardFile:
        key = workspace_owner_key(chat.user_id, chat.guild_id)

        def read() -> tuple[str, bytes]:
            path = self.workspace.resolve_user_file_path(key, relative, must_exist=True)
            return path.name, read_regular(path, self.settings.workspace_tool_max_file_bytes)

        async with self.locks.activity(key):
            name, payload = await await_uncancellable(asyncio.to_thread(read))
            return await self.save(chat, name, payload, kind="output", workspace_guard_held=True)

    async def preview(self, record: DashboardFile) -> dict[str, Any]:
        payload = await self.payload(record)

        def parse() -> dict[str, Any]:
            if (
                record.media_type in _RASTER_TYPES
                and decoded_image_media_type(payload) in _RASTER_TYPES
            ):
                return {"kind": "image"}
            suffix = Path(record.filename).suffix.lower()
            if suffix == ".pdf" or suffix in ANYDOC_EXTENSIONS:
                # Parsers see a private immutable copy, never a mutable workspace
                # pathname. No external links or embedded document code execute.
                with tempfile.NamedTemporaryFile(suffix=suffix) as temporary:
                    temporary.write(payload)
                    temporary.flush()
                    path = Path(temporary.name)
                    if suffix == ".pdf":
                        text = _extract_pdf_text(
                            path,
                            record.filename,
                            max_pages=200,
                            max_output_bytes=PREVIEW_CHARS,
                        ).text
                    else:
                        text = _convert_office_document(path, PREVIEW_CHARS).markdown
                return {
                    "kind": "markdown",
                    "text": text[:PREVIEW_CHARS],
                    "truncated": len(text) >= PREVIEW_CHARS,
                }
            if b"\0" in payload[:8192]:
                return {"kind": "download"}
            try:
                text = payload[: PREVIEW_CHARS * 4].decode("utf-8-sig")
            except UnicodeDecodeError:
                return {"kind": "download"}
            return {
                "kind": "markdown" if suffix in {".md", ".markdown"} else "text",
                "text": text[:PREVIEW_CHARS],
                "truncated": len(payload) > PREVIEW_CHARS,
            }

        try:
            return await _run_parser_worker(self._parser, parse)
        except Exception:
            return {"kind": "download", "notice": "Text preview is unavailable for this file"}
