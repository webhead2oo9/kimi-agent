"""Read-only, caller-scoped file access during one module tool invocation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol


class FileAccessError(ValueError):
    """A bounded, safe-to-display file error; never includes host paths or URLs."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ToolAttachment:
    """An admitted attachment or an explicit unavailable entry.

    ``id`` is opaque and valid only for this invocation. ``workspace_path`` is
    relative to the caller's workspace, and may be reused in later turns.
    Reply images have generated filenames when the host has no source name.
    No Discord SDK objects, signed URLs, or absolute paths are exposed.
    """

    id: str
    filename: str
    size: int
    media_type: str | None
    source: Literal["current", "reply"] = "current"
    workspace_path: str | None = None
    unavailable_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ToolFile:
    filename: str
    media_type: str | None
    data: bytes = field(repr=False)


class ToolFiles(Protocol):
    """Available only when ``permissions.tool_files`` is declared.

    Reads are bounded by the requested limit and host limits, and share an
    aggregate byte budget per invocation. They use the caller's workspace and
    admitted turn inputs; they never fetch arbitrary URLs or re-fetch Discord.
    The port expires when the handler returns. Copying bytes does not extend
    the host's privacy/retention guarantees; modules own any further persistence.
    """

    @property
    def attachments(self) -> tuple[ToolAttachment, ...]: ...

    async def read_attachment(self, attachment_id: str, *, max_bytes: int) -> ToolFile: ...

    async def read_workspace(self, path: str, *, max_bytes: int) -> ToolFile: ...


__all__ = ["FileAccessError", "ToolAttachment", "ToolFile", "ToolFiles"]
