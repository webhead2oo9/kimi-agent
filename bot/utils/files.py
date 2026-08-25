"""Atomic file replacement.

Write to a temp file in the destination directory, then `os.replace` it into
place, so a reader never sees a half-written file and a crash mid-write leaves
the existing file intact.

Durability and permissions are explicit per caller rather than assumed:

- `fsync` costs a disk round trip. Worth it for a skill document an operator
  just edited; not worth it for a cache entry that can be regenerated.
- `mode` matters when the file holds a credential. The temp file is created by
  `mkstemp`, which is already 0600, and the mode is re-applied after the
  replace because `os.replace` preserves the *source* file's mode on POSIX but
  the destination may pre-exist with a wider one.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path

__all__ = ["atomic_write_bytes", "atomic_write_text"]


def atomic_write_bytes(
    path: Path, data: bytes, *, fsync: bool = True, mode: int | None = None
) -> None:
    """Replace `path` with `data`, or raise `OSError` leaving it untouched."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            if fsync:
                os.fsync(handle.fileno())
        if mode is not None and os.name == "posix":
            os.chmod(temporary, mode)
        os.replace(temporary, path)
        if mode is not None and os.name == "posix":
            os.chmod(path, mode)
    except BaseException:
        # fdopen owns the descriptor once it succeeds; closing again is only
        # needed when mkstemp handed one back and fdopen never took it.
        with contextlib.suppress(OSError):
            os.close(fd)
        with contextlib.suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise


def atomic_write_text(
    path: Path,
    text: str,
    *,
    fsync: bool = True,
    mode: int | None = None,
    encoding: str = "utf-8",
) -> None:
    """`atomic_write_bytes` for text, encoded without newline translation.

    Newlines are written through verbatim: these files round-trip through
    parsers that count lines, so a platform rewrite would change content.
    """

    atomic_write_bytes(path, text.encode(encoding), fsync=fsync, mode=mode)
