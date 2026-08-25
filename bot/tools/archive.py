from __future__ import annotations

import gzip
import stat
import struct
import tarfile
import zipfile
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO, Any, cast

CHUNK = 64 * 1024
_ZIP_EOCD_SIGNATURE = b"PK\x05\x06"
_ZIP64_EOCD_SIGNATURE = b"PK\x06\x06"
_ZIP64_LOCATOR_SIGNATURE = b"PK\x06\x07"
_ZIP_CENTRAL_SIGNATURE = b"PK\x01\x02"
_ZIP_DIGITAL_SIGNATURE = b"PK\x05\x05"
_ZIP_EOCD_MAX = 22 + 0xFFFF


class ArchiveError(ValueError):
    """Raised for any unsafe or malformed archive condition."""


class _CappedDecompressionReader:
    """Wraps a decompressing stream and aborts once cumulative output exceeds a cap.

    A gzip tar bomb declares a huge, trivially-compressible member; tarfile must
    read past every member's body to reach the next header (in stream mode, which
    forces reads through this wrapper), so without a bound the enumeration inflates
    gigabytes and pins a worker. Counting the decompressed bytes lets a bomb trip
    the cap in bounded time/memory instead.
    """

    def __init__(self, stream: IO[bytes], cap_bytes: int) -> None:
        self._stream = stream
        self._cap = cap_bytes
        self._count = 0

    def read(self, size: int = -1) -> bytes:
        chunk = self._stream.read(size)
        self._count += len(chunk)
        if self._count > self._cap:
            raise ArchiveError("archive decompresses to more than the allowed size")
        return chunk

    def close(self) -> None:
        self._stream.close()


@contextmanager
def _counted_tar_stream(archive_path: Path, cap_bytes: int) -> Iterator[tarfile.TarFile]:
    """Open a .tar.gz as a forward-only stream whose decompression is bounded.

    Stream mode (`r|`) forces tarfile to *read* past each member to advance, so the
    skip goes through the decompression counter (a seekable open would skip via
    gzip.seek, bypassing the counter). Only .tar.gz/.tgz reach here (archive_kind),
    so gzip is the only compression.
    """
    raw = open(archive_path, "rb")  # noqa: SIM115 (lifecycle owned by this contextmanager)
    try:
        gz = gzip.GzipFile(fileobj=raw)
        reader = _CappedDecompressionReader(cast("IO[bytes]", gz), cap_bytes)
        # mode="r|": uncompressed tar stream (gzip already peeled off by `reader`).
        # reader is read-only by design; stream mode only calls read(), which is all
        # tarfile needs here, but the stubs don't model that.
        tf = tarfile.open(fileobj=cast("IO[bytes]", reader), mode="r|")  # noqa: SIM115
    except (OSError, tarfile.TarError, EOFError) as e:
        raw.close()
        raise ArchiveError(f"could not read archive: {e}") from e
    try:
        yield tf
    finally:
        tf.close()
        raw.close()


@dataclass(frozen=True)
class ExtractLimits:
    max_entries: int
    max_file_bytes: int
    max_total_bytes: int


@dataclass(frozen=True)
class ExtractResult:
    entries: int
    total_bytes: int
    stripped_top_level: str | None


@dataclass(frozen=True)
class _Member:
    arcname: str
    kind: str  # 'dir' | 'file' | 'symlink' | 'hardlink' | 'special'
    size: int


def archive_kind(name: str) -> str | None:
    lower = name.lower()
    if lower.endswith((".tar.gz", ".tgz")):
        return "tar"
    if lower.endswith(".zip"):
        return "zip"
    return None


def default_dest_name(name: str) -> str:
    lower = name.lower()
    for suffix in (".tar.gz", ".tgz", ".zip"):
        if lower.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _member_from_tar(m: tarfile.TarInfo) -> _Member:
    if m.isdir():
        kind = "dir"
    elif m.issym():
        kind = "symlink"
    elif m.islnk():
        kind = "hardlink"
    elif m.isreg():
        kind = "file"
    else:
        kind = "special"
    return _Member(arcname=m.name, kind=kind, size=m.size if m.isreg() else 0)


def _member_from_zip(info: zipfile.ZipInfo) -> _Member:
    name = info.filename
    if name.endswith("/"):
        return _Member(arcname=name, kind="dir", size=0)
    # Classify on the file-TYPE bits only. zipfile.writestr stores 0o600 (perm
    # bits, no type bits) for normal files, so S_IFMT == 0 must count as a regular
    # file; only an explicit non-regular type (symlink/fifo/dev/...) is special.
    file_type = stat.S_IFMT(info.external_attr >> 16)
    if file_type == stat.S_IFLNK:
        return _Member(arcname=name, kind="symlink", size=0)
    if file_type not in (0, stat.S_IFREG):
        return _Member(arcname=name, kind="special", size=0)
    return _Member(arcname=name, kind="file", size=info.file_size)


def _normalize_arcname(raw: str) -> str | None:
    raw = raw.replace("\\", "/").strip()
    if not raw or raw == ".":
        return None
    p = PurePosixPath(raw)
    if p.is_absolute():
        raise ArchiveError(f"absolute path in archive: {raw}")
    if any(part == ".." for part in p.parts):
        raise ArchiveError(f"path traversal in archive: {raw}")
    cleaned = "/".join(p.parts)
    return cleaned or None


def _strip_prefix(arcnames: list[str]) -> str | None:
    tops = {a.split("/", 1)[0] for a in arcnames}
    if len(tops) == 1 and any("/" in a for a in arcnames):
        return next(iter(tops)) + "/"
    return None


def _apply_strip(arcname: str, strip: str | None) -> str:
    if not strip:
        return arcname
    if arcname == strip.rstrip("/"):
        return ""
    if arcname.startswith(strip):
        return arcname[len(strip) :]
    return arcname


def _safe_target(dest: Path, rel: str) -> Path:
    target = (dest / rel).resolve()
    if target != dest and not target.is_relative_to(dest):
        raise ArchiveError(f"archive member escapes destination: {rel}")
    return target


def _preflight_member_destinations(members: Iterable[_Member], strip: str | None) -> None:
    """Reject ZIP layouts that cannot map to one unambiguous filesystem tree."""
    destinations: dict[str, str] = {}

    for mem in members:
        # Special entries are deliberately ignored during extraction. Links are
        # rejected separately and never reach a write, so only entries that can
        # create filesystem objects participate in destination conflicts.
        if mem.kind not in ("dir", "file"):
            continue
        rel = _apply_strip(mem.arcname, strip)
        previous_kind = destinations.get(rel)
        if previous_kind is not None:
            display = rel or "."
            if previous_kind == mem.kind:
                raise ArchiveError(f"duplicate archive destination: {display}")
            raise ArchiveError(f"file/directory archive destination conflict: {display}")
        destinations[rel] = mem.kind

    # A regular member at the stripped root would need the extraction destination
    # itself to be both a file and a directory.
    if destinations.get("") == "file":
        raise ArchiveError("file/directory archive destination conflict: .")

    regular_files = {destination for destination, kind in destinations.items() if kind == "file"}
    for destination in destinations:
        parts = destination.split("/")
        for part_count in range(1, len(parts)):
            ancestor = "/".join(parts[:part_count])
            if ancestor in regular_files:
                raise ArchiveError(
                    f"regular file archive destination is an ancestor of another member: {ancestor}"
                )


def _write_regular_member(
    src: IO[bytes],
    target: Path,
    arcname: str,
    limits: ExtractLimits,
    total_before: int,
) -> int:
    """Stream one regular member to disk under the per-file and total byte caps,
    returning the new running total. Shared by the zip and tar extract paths."""
    target.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    total = total_before
    with src, open(target, "wb") as out:
        while True:
            chunk = src.read(CHUNK)
            if not chunk:
                break
            written += len(chunk)
            if written > limits.max_file_bytes:
                raise ArchiveError(f"archive member exceeds per-file limit: {arcname}")
            total += len(chunk)
            if total > limits.max_total_bytes:
                raise ArchiveError("archive exceeds total extraction size limit")
            out.write(chunk)
    return total


def _extract(
    pairs: Iterable[tuple[Any, _Member]],
    dest: Path,
    strip_top_level: bool,
    limits: ExtractLimits,
    *,
    open_regular: Callable[[Any], IO[bytes] | None],
) -> ExtractResult:
    dest = dest.resolve()
    members: list[tuple[Any, _Member]] = []
    # Count every raw member as it is parsed and abort the moment the cap is
    # exceeded, BEFORE materializing the whole list. A crafted .tar.gz can
    # declare millions of trivially-compressible 512-byte headers, so checking
    # len() only after full enumeration would let it exhaust CPU/memory first.
    seen = 0
    for raw, mem in pairs:
        seen += 1
        if seen > limits.max_entries:
            raise ArchiveError(f"archive has more than {limits.max_entries} entries")
        arc = _normalize_arcname(mem.arcname)
        if arc is None:
            continue
        members.append((raw, _Member(arcname=arc, kind=mem.kind, size=mem.size)))

    strip = _strip_prefix([m.arcname for _, m in members]) if strip_top_level else None
    _preflight_member_destinations((mem for _, mem in members), strip)

    dest.mkdir(parents=True, exist_ok=False)
    entries = 0
    total = 0
    for raw, mem in members:
        rel = _apply_strip(mem.arcname, strip)
        if rel == "":
            continue
        if mem.kind in ("symlink", "hardlink"):
            raise ArchiveError(f"refusing to extract link member: {mem.arcname}")
        if mem.kind == "special":
            continue
        target = _safe_target(dest, rel)
        if mem.kind == "dir":
            target.mkdir(parents=True, exist_ok=True)
            continue
        if mem.size > limits.max_file_bytes:
            raise ArchiveError(f"archive member exceeds per-file limit: {mem.arcname}")
        src = open_regular(raw)
        if src is None:
            continue
        total = _write_regular_member(src, target, mem.arcname, limits, total)
        entries += 1

    return ExtractResult(
        entries=entries,
        total_bytes=total,
        stripped_top_level=strip.rstrip("/") if strip else None,
    )


def _extract_tar_streaming(
    archive_path: Path,
    dest: Path,
    strip_top_level: bool,
    limits: ExtractLimits,
) -> ExtractResult:
    """Extract a .tar.gz in two forward-only streaming passes, each through the
    decompression counter, so a gzip bomb is bounded during enumeration.

    A seekable open (the zip path's model) cannot be used for tar.gz: tarfile skips
    members via gzip.seek, which decompresses internally and bypasses any read-side
    byte counter. Streaming forces every skip through the counter. Pass 1 reads
    headers to compute the top-level strip and the entry cap; pass 2 extracts.
    """
    arcnames: list[str] = []
    seen = 0
    with _counted_tar_stream(archive_path, limits.max_total_bytes) as tf:
        for m in tf:
            seen += 1
            if seen > limits.max_entries:
                raise ArchiveError(f"archive has more than {limits.max_entries} entries")
            arc = _normalize_arcname(m.name)
            if arc is not None:
                arcnames.append(arc)
    strip = _strip_prefix(arcnames) if strip_top_level else None

    dest = dest.resolve()
    dest.mkdir(parents=True, exist_ok=False)
    entries = 0
    total = 0
    with _counted_tar_stream(archive_path, limits.max_total_bytes) as tf:
        for m in tf:
            mem = _member_from_tar(m)
            arc = _normalize_arcname(mem.arcname)
            if arc is None:
                continue
            rel = _apply_strip(arc, strip)
            if rel == "":
                continue
            if mem.kind in ("symlink", "hardlink"):
                raise ArchiveError(f"refusing to extract link member: {mem.arcname}")
            if mem.kind == "special":
                continue
            target = _safe_target(dest, rel)
            if mem.kind == "dir":
                target.mkdir(parents=True, exist_ok=True)
                continue
            if mem.size > limits.max_file_bytes:
                raise ArchiveError(f"archive member exceeds per-file limit: {mem.arcname}")
            src = tf.extractfile(m)
            if src is None:
                continue
            total = _write_regular_member(src, target, mem.arcname, limits, total)
            entries += 1

    return ExtractResult(
        entries=entries,
        total_bytes=total,
        stripped_top_level=strip.rstrip("/") if strip else None,
    )


def safe_extract(
    archive_path: Path,
    dest: Path,
    *,
    strip_top_level: bool,
    limits: ExtractLimits,
) -> ExtractResult:
    kind = archive_kind(archive_path.name)
    if kind is None:
        raise ArchiveError("unsupported archive type; use .tar.gz, .tgz, or .zip")
    if kind == "tar":
        # Streaming two-pass extraction bounds the forced skip-decompression of a
        # gzip bomb; a seekable open would skip via gzip.seek and evade the counter.
        return _extract_tar_streaming(archive_path, dest, strip_top_level, limits)
    _preflight_zip_central_directory(archive_path, limits.max_entries)
    try:
        zf = zipfile.ZipFile(archive_path)
    except zipfile.BadZipFile as e:
        raise ArchiveError(f"could not read archive: {e}") from e
    with zf:
        # Defer per-entry _Member construction past the cap. (The zip central
        # directory is already parsed in ZipFile(); zip headers are >=46 bytes
        # and not amplifiable like tar, so streaming the directory is enough.)
        zip_pairs = ((info, _member_from_zip(info)) for info in zf.infolist())
        return _extract(
            zip_pairs,
            dest,
            strip_top_level,
            limits,
            open_regular=lambda info: zf.open(info),
        )


def _preflight_zip_central_directory(archive_path: Path, max_entries: int) -> None:
    """Count ZIP central records with O(1) memory before ``ZipFile`` parses them."""

    try:
        with archive_path.open("rb") as source:
            source.seek(0, 2)
            file_size = source.tell()
            tail_size = min(file_size, _ZIP_EOCD_MAX)
            source.seek(file_size - tail_size)
            tail = source.read(tail_size)
            eocd_relative = _find_zip_eocd(tail)
            if eocd_relative is None:
                raise ArchiveError("could not read archive: missing ZIP end record")
            eocd_offset = file_size - tail_size + eocd_relative
            eocd = tail[eocd_relative : eocd_relative + 22]
            (
                disk_number,
                central_disk,
                entries_on_disk,
                declared_entries,
                central_size,
                _central_offset,
                _comment_size,
            ) = struct.unpack_from("<4H2LH", eocd, 4)
            central_end = eocd_offset

            if (
                declared_entries == 0xFFFF
                or entries_on_disk == 0xFFFF
                or central_size == 0xFFFFFFFF
                or _central_offset == 0xFFFFFFFF
            ):
                locator_offset = eocd_offset - 20
                if locator_offset < 0:
                    raise ArchiveError("could not read archive: missing ZIP64 locator")
                source.seek(locator_offset)
                locator = source.read(20)
                if len(locator) != 20 or locator[:4] != _ZIP64_LOCATOR_SIGNATURE:
                    raise ArchiveError("could not read archive: missing ZIP64 locator")
                _, locator_disk, zip64_offset, total_disks = struct.unpack("<4sLQL", locator)
                if locator_disk != 0 or total_disks != 1:
                    raise ArchiveError("multi-disk ZIP archives are not supported")
                source.seek(zip64_offset)
                zip64 = source.read(56)
                if len(zip64) != 56 or zip64[:4] != _ZIP64_EOCD_SIGNATURE:
                    raise ArchiveError("could not read archive: invalid ZIP64 end record")
                (
                    _,
                    _record_size,
                    _made_by,
                    _needed,
                    disk_number,
                    central_disk,
                    entries_on_disk,
                    declared_entries,
                    central_size,
                    _central_offset,
                ) = struct.unpack("<4sQ2H2L4Q", zip64)
                central_end = zip64_offset

            if disk_number != 0 or central_disk != 0 or entries_on_disk != declared_entries:
                raise ArchiveError("multi-disk ZIP archives are not supported")
            if declared_entries > max_entries:
                raise ArchiveError(f"archive has more than {max_entries} entries")
            if central_size > central_end:
                raise ArchiveError("could not read archive: invalid central directory")

            central_start = central_end - central_size
            source.seek(central_start)
            consumed = 0
            counted = 0
            while consumed < central_size:
                signature = source.read(4)
                if signature == _ZIP_CENTRAL_SIGNATURE:
                    fixed_rest = source.read(42)
                    if len(fixed_rest) != 42:
                        raise ArchiveError("could not read archive: truncated central directory")
                    name_size, extra_size, comment_size = struct.unpack_from("<HHH", fixed_rest, 24)
                    record_size = 46 + name_size + extra_size + comment_size
                    if consumed + record_size > central_size:
                        raise ArchiveError("could not read archive: invalid central directory")
                    source.seek(name_size + extra_size + comment_size, 1)
                    consumed += record_size
                    counted += 1
                    if counted > max_entries:
                        raise ArchiveError(f"archive has more than {max_entries} entries")
                    continue
                if signature == _ZIP_DIGITAL_SIGNATURE:
                    size_bytes = source.read(2)
                    if len(size_bytes) != 2:
                        raise ArchiveError("could not read archive: truncated ZIP signature")
                    signature_size = struct.unpack("<H", size_bytes)[0]
                    record_size = 6 + signature_size
                    if consumed + record_size > central_size:
                        raise ArchiveError("could not read archive: invalid ZIP signature")
                    source.seek(signature_size, 1)
                    consumed += record_size
                    continue
                raise ArchiveError("could not read archive: invalid central directory")
            if counted != declared_entries:
                raise ArchiveError("could not read archive: inconsistent ZIP entry count")
    except OSError as exc:
        raise ArchiveError(f"could not read archive: {exc}") from exc


def _find_zip_eocd(tail: bytes) -> int | None:
    search_end = len(tail)
    while True:
        offset = tail.rfind(_ZIP_EOCD_SIGNATURE, 0, search_end)
        if offset < 0:
            return None
        if offset + 22 <= len(tail):
            comment_size = struct.unpack_from("<H", tail, offset + 20)[0]
            if offset + 22 + comment_size == len(tail):
                return offset
        search_end = offset
