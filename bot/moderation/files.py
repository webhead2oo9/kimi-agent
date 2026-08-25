from __future__ import annotations

MAX_TEXT_MODERATION_BYTES = 256 * 1024
UNSUPPORTED_MODERATION_FILE_MESSAGE = (
    "This attachment cannot be screened by the configured moderation service. "
    "Use a UTF-8 text file no larger than 256 KiB instead."
)


class UnsupportedModerationFile(ValueError):
    """Raised when a file cannot be represented to the moderation backend."""


def text_from_file_bytes(filename: str, payload: bytes) -> str:
    if not payload:
        return ""
    if len(payload) > MAX_TEXT_MODERATION_BYTES:
        raise UnsupportedModerationFile("file is too large to moderate as text")
    if b"\x00" in payload:
        raise UnsupportedModerationFile("binary file cannot be moderated as text")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UnsupportedModerationFile("file is not UTF-8 text") from exc
    stripped = text.strip()
    if not stripped:
        return ""
    return f"Attachment {filename}:\n{stripped}"
