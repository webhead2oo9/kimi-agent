"""Shared bounded sources for hosted and local video analysis."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Protocol

from tools.registry import MessageContext
from tools.workspace.common import UserLocks
from utils.asyncio import await_uncancellable
from utils.video_types import video_media_type
from video_understanding.service import UploadedVideoSource
from workspace import ENV_DIR_NAMES, WorkspaceKey, WorkspaceManager

_MAX_UPLOAD_BYTES = 500 * 1024 * 1024
_SOURCE_READ_CHUNK_BYTES = 1024 * 1024


class _VideoAttachment(Protocol):
    filename: str
    size: int
    content_type: str | None

    def iter_video_chunks(
        self,
        *,
        chunk_size: int,
        max_bytes: int,
    ) -> AsyncIterator[bytes]: ...


@dataclass(frozen=True, slots=True)
class _AttachmentBytes:
    attachment: _VideoAttachment

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self.attachment.iter_video_chunks(
            chunk_size=_SOURCE_READ_CHUNK_BYTES,
            max_bytes=_MAX_UPLOAD_BYTES,
        )


@dataclass(frozen=True, slots=True)
class _WorkspaceBytes:
    manager: WorkspaceManager
    locks: UserLocks
    workspace_key: WorkspaceKey
    path_arg: str
    expected_size: int

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._chunks()

    async def _chunks(self) -> AsyncIterator[bytes]:
        # Take the workspace lock only while resolving/opening. The no-follow fd
        # remains bound to that inode while network backpressure pauses reads,
        # without blocking unrelated workspace maintenance for the whole upload.
        opened: tuple[int, int, str] | None = None

        def open_source() -> None:
            nonlocal opened
            opened = _open_workspace_video(self.manager, self.workspace_key, self.path_arg)

        try:
            async with self.locks.activity(self.workspace_key):
                await await_uncancellable(asyncio.to_thread(open_source))
            assert opened is not None
            descriptor, size, _relative = opened
            if size != self.expected_size:
                raise ValueError("workspace video changed before upload")
            total = 0
            while True:
                chunk = await await_uncancellable(
                    asyncio.to_thread(os.read, descriptor, _SOURCE_READ_CHUNK_BYTES)
                )
                if not chunk:
                    break
                total += len(chunk)
                if total > self.expected_size or total > _MAX_UPLOAD_BYTES:
                    raise ValueError("workspace video exceeded its size limit")
                yield chunk
            if total != self.expected_size:
                raise ValueError("workspace video ended before its declared size")
        finally:
            if opened is not None:
                os.close(opened[0])


def attachment_source(ctx: MessageContext, filename: str) -> UploadedVideoSource:
    matches = [item for item in ctx.attachments if item.filename == filename]
    if not matches:
        available = ", ".join(item.filename for item in ctx.attachments) or "none"
        raise ValueError(f"no attachment named {filename}; available: {available}")
    if len(matches) > 1:
        raise ValueError("multiple attachments have that filename; rename and resend one")
    attachment = matches[0]
    mime_type = video_media_type(attachment.filename, attachment.content_type)
    if mime_type is None:
        raise ValueError("attachment must be a supported video file")
    if attachment.size <= 0 or attachment.size > _MAX_UPLOAD_BYTES:
        raise ValueError("video attachment must be between 1 byte and 500 MiB")
    display_name = _safe_display_name(attachment.filename)
    return UploadedVideoSource(
        kind="attachment",
        display_name=display_name,
        locator=display_name,
        mime_type=mime_type,
        byte_size=attachment.size,
        bytes=_AttachmentBytes(attachment),
    )


async def workspace_source(
    manager: WorkspaceManager,
    locks: UserLocks,
    workspace_key: WorkspaceKey,
    path_arg: str,
) -> UploadedVideoSource:
    def inspect_source() -> tuple[int, str]:
        descriptor, size, relative = _open_workspace_video(manager, workspace_key, path_arg)
        try:
            return size, relative
        finally:
            os.close(descriptor)

    async with locks.activity(workspace_key):
        size, relative = await await_uncancellable(asyncio.to_thread(inspect_source))
    display_name = _safe_display_name(PurePosixPath(relative).name)
    mime_type = video_media_type(display_name, None)
    if mime_type is None:
        raise ValueError("workspace path must identify a supported video file")
    if size <= 0 or size > _MAX_UPLOAD_BYTES:
        raise ValueError("workspace video must be between 1 byte and 500 MiB")
    return UploadedVideoSource(
        kind="workspace",
        display_name=display_name,
        locator=relative,
        mime_type=mime_type,
        byte_size=size,
        bytes=_WorkspaceBytes(manager, locks, workspace_key, path_arg, size),
    )


def _open_workspace_video(
    manager: WorkspaceManager,
    workspace_key: WorkspaceKey,
    path_arg: str,
) -> tuple[int, int, str]:
    path = manager.resolve_user_file_path(workspace_key, path_arg)
    relative = manager.relative_user_file_path(workspace_key, path)
    parts = PurePosixPath(relative).parts
    if any(part in ENV_DIR_NAMES for part in parts):
        raise ValueError("video path cannot be inside a reserved environment directory")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("video path must identify a regular file")
        if metadata.st_size <= 0 or metadata.st_size > _MAX_UPLOAD_BYTES:
            raise ValueError("workspace video must be between 1 byte and 500 MiB")
        return descriptor, metadata.st_size, PurePosixPath(relative).as_posix()
    except BaseException:
        os.close(descriptor)
        raise


def _safe_display_name(value: str) -> str:
    cleaned = re.sub(r"[\x00-\x1f\x7f]+", " ", Path(value).name).strip()
    return (cleaned or "video")[:512]
