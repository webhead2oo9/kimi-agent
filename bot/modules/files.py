"""Invocation-scoped access to admitted media and the caller's workspace."""

from __future__ import annotations

import asyncio
import base64
import mimetypes
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from kimi_agent_module_api.files import FileAccessError, ToolAttachment, ToolFile
from tools.registry import MessageContext
from tools.workspace.common import UserLocks, workspace_activity
from utils.asyncio import await_uncancellable
from utils.image_types import sniff_image_media_type
from workspace import WorkspaceManager

MAX_TOOL_FILE_BYTES = 50 * 1024 * 1024


class ModuleToolFiles:
    def __init__(
        self,
        ctx: MessageContext,
        manager: WorkspaceManager,
        locks: UserLocks,
        *,
        max_bytes: int = MAX_TOOL_FILE_BYTES,
    ) -> None:
        self._ctx = ctx
        self._manager = manager
        self._locks = locks
        self._active = True
        self._remaining = min(max_bytes, MAX_TOOL_FILE_BYTES)
        self._read_lock = asyncio.Lock()
        self._encoded: dict[str, str] = {}
        entries: list[ToolAttachment] = []
        for index, attachment in enumerate(ctx.attachments):
            available = not attachment.unavailable_reason and bool(attachment.workspace_path)
            entries.append(
                ToolAttachment(
                    id=f"current:{index}",
                    filename=attachment.filename,
                    size=attachment.size,
                    media_type=attachment.content_type,
                    workspace_path=attachment.workspace_path or None,
                    unavailable_reason=None
                    if available
                    else "Attachment was not admitted or saved by the host.",
                )
            )
        for index, part in enumerate(ctx.reply_image_parts):
            url = part.image_url or ""
            prefix, separator, encoded = url.partition(";base64,")
            if not separator or not prefix.startswith("data:image/"):
                continue
            media_type = prefix.removeprefix("data:")
            extension = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}.get(
                media_type
            )
            if extension is None:
                continue
            identifier = f"reply:{index}"
            # Exact decoded size for canonical padded base64, without allocating bytes.
            size = len(encoded) // 4 * 3 - (len(encoded) - len(encoded.rstrip("=")))
            entries.append(
                ToolAttachment(
                    identifier, f"reply-image-{index + 1}{extension}", size, media_type, "reply"
                )
            )
            self._encoded[identifier] = encoded
        self._attachments = tuple(entries)

    def close(self) -> None:
        self._active = False

    def _check_active(self) -> None:
        if not self._active:
            raise FileAccessError("expired", "File access expired when the tool invocation ended.")

    @property
    def attachments(self) -> tuple[ToolAttachment, ...]:
        self._check_active()
        return self._attachments

    async def read_attachment(self, attachment_id: str, *, max_bytes: int) -> ToolFile:
        self._check_active()
        entry = next((entry for entry in self._attachments if entry.id == attachment_id), None)
        if entry is None:
            raise FileAccessError("unknown_attachment", "Unknown attachment for this invocation.")
        if entry.unavailable_reason:
            raise FileAccessError("unavailable", entry.unavailable_reason)
        if entry.workspace_path:
            result = await self.read_workspace(entry.workspace_path, max_bytes=max_bytes)
            return replace(
                result, filename=entry.filename, media_type=result.media_type or entry.media_type
            )

        def read(limit: int) -> ToolFile:
            encoded = self._encoded[entry.id]
            if len(encoded) > ((limit + 2) // 3) * 4:
                raise FileAccessError("too_large", "Attachment exceeds the file read limit.")
            data = base64.b64decode(encoded, validate=True)
            if len(data) > limit:
                raise FileAccessError("too_large", "Attachment exceeds the file read limit.")
            return ToolFile(entry.filename, entry.media_type, data)

        return await self._read(read, max_bytes)

    async def read_workspace(self, path: str, *, max_bytes: int) -> ToolFile:
        self._check_active()
        if not isinstance(path, str) or not path.strip():
            raise FileAccessError("unavailable", "A relative workspace file path is required.")

        def read(limit: int) -> ToolFile:
            resolved = self._manager.resolve_user_file_path(
                self._ctx.workspace_key, path, must_exist=True
            )
            if not resolved.is_file():
                raise FileAccessError("not_file", "The workspace path is not a regular file.")
            with resolved.open("rb") as stream:
                data = stream.read(limit + 1)
            if len(data) > limit:
                raise FileAccessError("too_large", "Workspace file exceeds the file read limit.")
            media_type = sniff_image_media_type(data) or mimetypes.guess_type(resolved.name)[0]
            return ToolFile(Path(path).name, media_type, data)

        return await self._read(read, max_bytes)

    async def _read(self, reader: Callable[[int], ToolFile], max_bytes: int) -> ToolFile:
        if type(max_bytes) is not int or max_bytes < 1:
            raise FileAccessError("invalid_limit", "max_bytes must be a positive integer.")
        async with self._read_lock:
            self._check_active()
            if self._remaining <= 0:
                raise FileAccessError(
                    "budget_exhausted", "The invocation's file read budget is exhausted."
                )
            limit = min(max_bytes, self._remaining)
            try:
                async with workspace_activity(self._locks, self._ctx):
                    self._check_active()
                    # Keep the privacy/workspace lease until a cancelled worker stops.
                    result = await await_uncancellable(asyncio.to_thread(reader, limit))
                self._remaining -= len(result.data)
                return result
            except FileAccessError:
                raise
            except OSError, ValueError:
                raise FileAccessError(
                    "unavailable", "The file is unavailable or outside the caller's workspace."
                ) from None
