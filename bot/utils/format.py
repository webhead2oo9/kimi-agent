"""Small shared formatting helpers."""

from __future__ import annotations

from datetime import UTC, datetime


def human_size(num: int) -> str:
    """Format a byte count as a short human-readable string (B/KB/MB/GB)."""
    size = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def iso_timestamp(epoch_seconds: float) -> str:
    """Format an epoch-seconds value as a UTC ISO-8601 string with a ``Z`` suffix."""
    return (
        datetime.fromtimestamp(epoch_seconds, UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def now_iso(timespec: str = "seconds") -> str:
    """The current UTC time as an ISO-8601 string with a ``Z`` suffix.

    Note `storage/usage.py` deliberately does *not* use this: its `created_at`
    is persisted and then string-compared in SQL (`created_at >= ?`), so the
    `+00:00` suffix already in the table has to keep being written.
    """

    return datetime.now(UTC).isoformat(timespec=timespec).replace("+00:00", "Z")


_MAX_AUTHOR_NAME_LEN = 32


def sanitize_author_name(name: str) -> str:
    """Flatten a display name into a safe single-line transcript label.

    Colons and newlines go because names are rendered as ``<name>: <text>``:
    without this, a display name could forge another speaker's turn in the
    transcript the model reads.
    """

    clean = name.replace("\n", " ").replace("\r", " ").replace(":", "")
    clean = " ".join(clean.split())
    if len(clean) > _MAX_AUTHOR_NAME_LEN:
        clean = clean[:_MAX_AUTHOR_NAME_LEN]
    return clean or "Unknown"
