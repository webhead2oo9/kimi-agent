from __future__ import annotations

import io
import stat
import tarfile
import zipfile
from pathlib import Path

import pytest

from tools.archive import (
    ArchiveError,
    ExtractLimits,
    archive_kind,
    default_dest_name,
    safe_extract,
)

LIMITS = ExtractLimits(
    max_entries=1000,
    max_file_bytes=10 * 1024 * 1024,
    max_total_bytes=50 * 1024 * 1024,
)


def _write_tar(path: Path, files: dict[str, bytes]) -> None:
    with tarfile.open(path, "w:gz") as tf:
        for name, data in files.items():
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))


def _write_zip(path: Path, files: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)


def test_archive_kind_and_default_dest() -> None:
    assert archive_kind("repo-main.tar.gz") == "tar"
    assert archive_kind("repo-main.tgz") == "tar"
    assert archive_kind("repo-main.zip") == "zip"
    assert archive_kind("repo-main.rar") is None
    assert default_dest_name("repo-main.tar.gz") == "repo-main"
    assert default_dest_name("repo-main.zip") == "repo-main"


def test_safe_extract_tar_strips_top_level(tmp_path: Path) -> None:
    arc = tmp_path / "repo-main.tar.gz"
    _write_tar(arc, {"repo-abc123/README.md": b"hello", "repo-abc123/src/app.py": b"print(1)"})
    dest = tmp_path / "out"
    result = safe_extract(arc, dest, strip_top_level=True, limits=LIMITS)
    assert (dest / "README.md").read_bytes() == b"hello"
    assert (dest / "src" / "app.py").read_bytes() == b"print(1)"
    assert not (dest / "repo-abc123").exists()
    assert result.entries == 2
    assert result.total_bytes == len(b"hello") + len(b"print(1)")
    assert result.stripped_top_level == "repo-abc123"


def test_safe_extract_zip_no_strip(tmp_path: Path) -> None:
    arc = tmp_path / "files.zip"
    _write_zip(arc, {"a.txt": b"a", "b.txt": b"bb"})
    dest = tmp_path / "out"
    result = safe_extract(arc, dest, strip_top_level=True, limits=LIMITS)
    assert (dest / "a.txt").read_bytes() == b"a"
    assert (dest / "b.txt").read_bytes() == b"bb"
    assert result.stripped_top_level is None
    assert result.entries == 2


def test_safe_extract_zip_rejects_duplicate_files_before_creating_dest(
    tmp_path: Path,
) -> None:
    arc = tmp_path / "duplicate.zip"
    with zipfile.ZipFile(arc, "w") as zf:
        zf.writestr("same.txt", b"first")
        with pytest.warns(UserWarning, match="Duplicate name"):
            zf.writestr("same.txt", b"second")
    dest = tmp_path / "out"

    with pytest.raises(ArchiveError, match="duplicate archive destination"):
        safe_extract(arc, dest, strip_top_level=False, limits=LIMITS)

    assert not dest.exists()


def test_safe_extract_zip_rejects_file_directory_same_path_before_creating_dest(
    tmp_path: Path,
) -> None:
    arc = tmp_path / "file-directory.zip"
    with zipfile.ZipFile(arc, "w") as zf:
        zf.writestr("node/", b"")
        zf.writestr("node", b"file")
    dest = tmp_path / "out"

    with pytest.raises(ArchiveError, match="file/directory archive destination conflict"):
        safe_extract(arc, dest, strip_top_level=False, limits=LIMITS)

    assert not dest.exists()


@pytest.mark.parametrize(
    "entries",
    [
        [("parent", b"file"), ("parent/child.txt", b"child")],
        [("parent/child.txt", b"child"), ("parent", b"file")],
    ],
    ids=["file-first", "file-last"],
)
def test_safe_extract_zip_rejects_file_parent_in_both_orders_before_creating_dest(
    tmp_path: Path, entries: list[tuple[str, bytes]]
) -> None:
    arc = tmp_path / "file-parent.zip"
    with zipfile.ZipFile(arc, "w") as zf:
        for name, data in entries:
            zf.writestr(name, data)
    dest = tmp_path / "out"

    with pytest.raises(ArchiveError, match="ancestor of another member"):
        safe_extract(arc, dest, strip_top_level=False, limits=LIMITS)

    assert not dest.exists()


def test_safe_extract_zip_rejects_backslash_normalization_collision(
    tmp_path: Path,
) -> None:
    arc = tmp_path / "backslash.zip"
    with zipfile.ZipFile(arc, "w") as zf:
        zf.writestr("folder!file.txt", b"backslash")
        zf.writestr("folder/file.txt", b"slash")
    # ZipInfo normalizes platform separators while writing on Windows. Patch the
    # equal-length local and central names so the archive itself contains `\`.
    archive_bytes = arc.read_bytes()
    assert archive_bytes.count(b"folder!file.txt") == 2
    arc.write_bytes(archive_bytes.replace(b"folder!file.txt", b"folder\\file.txt"))
    dest = tmp_path / "out"

    with pytest.raises(ArchiveError, match="duplicate archive destination"):
        safe_extract(arc, dest, strip_top_level=False, limits=LIMITS)

    assert not dest.exists()


def test_safe_extract_zip_rejects_file_mapped_to_stripped_root(
    tmp_path: Path,
) -> None:
    # Stripping maps "project" onto the extraction root while its child requires
    # that same root to be a directory.
    arc = tmp_path / "strip-root.zip"
    with zipfile.ZipFile(arc, "w") as zf:
        zf.writestr("project", b"file")
        zf.writestr("project/readme.txt", b"child")
    dest = tmp_path / "out"

    with pytest.raises(ArchiveError, match="file/directory archive destination conflict"):
        safe_extract(arc, dest, strip_top_level=True, limits=LIMITS)

    assert not dest.exists()


def test_safe_extract_rejects_traversal_tar(tmp_path: Path) -> None:
    arc = tmp_path / "evil.tar.gz"
    with tarfile.open(arc, "w:gz") as tf:
        ti = tarfile.TarInfo("../escape.txt")
        ti.size = 3
        tf.addfile(ti, io.BytesIO(b"bad"))
    dest = tmp_path / "out"
    with pytest.raises(ArchiveError):
        safe_extract(arc, dest, strip_top_level=False, limits=LIMITS)
    assert not (tmp_path / "escape.txt").exists()


def test_safe_extract_rejects_absolute_zip(tmp_path: Path) -> None:
    arc = tmp_path / "evil.zip"
    with zipfile.ZipFile(arc, "w") as zf:
        zf.writestr("/etc/evil.txt", b"bad")
    dest = tmp_path / "out"
    with pytest.raises(ArchiveError):
        safe_extract(arc, dest, strip_top_level=False, limits=LIMITS)


def test_safe_extract_rejects_tar_symlink(tmp_path: Path) -> None:
    arc = tmp_path / "sym.tar.gz"
    with tarfile.open(arc, "w:gz") as tf:
        ti = tarfile.TarInfo("link")
        ti.type = tarfile.SYMTYPE
        ti.linkname = "/etc/passwd"
        tf.addfile(ti)
    with pytest.raises(ArchiveError):
        safe_extract(arc, tmp_path / "out", strip_top_level=False, limits=LIMITS)


def test_safe_extract_rejects_tar_hardlink(tmp_path: Path) -> None:
    arc = tmp_path / "hard.tar.gz"
    with tarfile.open(arc, "w:gz") as tf:
        data = b"x"
        reg = tarfile.TarInfo("real.txt")
        reg.size = len(data)
        tf.addfile(reg, io.BytesIO(data))
        ti = tarfile.TarInfo("hard")
        ti.type = tarfile.LNKTYPE
        ti.linkname = "real.txt"
        tf.addfile(ti)
    with pytest.raises(ArchiveError):
        safe_extract(arc, tmp_path / "out", strip_top_level=False, limits=LIMITS)


def test_safe_extract_skips_special_files(tmp_path: Path) -> None:
    arc = tmp_path / "dev.tar.gz"
    with tarfile.open(arc, "w:gz") as tf:
        ti = tarfile.TarInfo("null")
        ti.type = tarfile.CHRTYPE
        ti.devmajor = 1
        ti.devminor = 3
        tf.addfile(ti)
        data = b"ok"
        reg = tarfile.TarInfo("real.txt")
        reg.size = len(data)
        tf.addfile(reg, io.BytesIO(data))
    result = safe_extract(arc, tmp_path / "out", strip_top_level=False, limits=LIMITS)
    assert (tmp_path / "out" / "real.txt").read_bytes() == b"ok"
    assert not (tmp_path / "out" / "null").exists()
    assert result.entries == 1


def test_safe_extract_rejects_zip_symlink(tmp_path: Path) -> None:
    arc = tmp_path / "sym.zip"
    with zipfile.ZipFile(arc, "w") as zf:
        zi = zipfile.ZipInfo("link")
        zi.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(zi, "/etc/passwd")
    with pytest.raises(ArchiveError):
        safe_extract(arc, tmp_path / "out", strip_top_level=False, limits=LIMITS)


def test_safe_extract_skips_zip_special(tmp_path: Path) -> None:
    arc = tmp_path / "special.zip"
    with zipfile.ZipFile(arc, "w") as zf:
        zi = zipfile.ZipInfo("fifo")
        zi.external_attr = (stat.S_IFIFO | 0o644) << 16
        zf.writestr(zi, b"")
        zf.writestr("real.txt", b"ok")
    result = safe_extract(arc, tmp_path / "out", strip_top_level=False, limits=LIMITS)
    assert (tmp_path / "out" / "real.txt").read_bytes() == b"ok"
    assert not (tmp_path / "out" / "fifo").exists()
    assert result.entries == 1


def test_safe_extract_entry_count_cap(tmp_path: Path) -> None:
    arc = tmp_path / "many.tar.gz"
    with tarfile.open(arc, "w:gz") as tf:
        for i in range(5):
            ti = tarfile.TarInfo(f"f{i}.txt")
            ti.size = 1
            tf.addfile(ti, io.BytesIO(b"x"))
    limits = ExtractLimits(max_entries=3, max_file_bytes=1024, max_total_bytes=1024)
    with pytest.raises(ArchiveError):
        safe_extract(arc, tmp_path / "out", strip_top_level=False, limits=limits)


def test_safe_extract_tar_aborts_before_materializing_all_members(
    tmp_path: Path, monkeypatch
) -> None:
    # The cap must fire while streaming headers, not after parsing the whole
    # archive; otherwise a header flood exhausts CPU/memory before the check.
    arc = tmp_path / "many.tar.gz"
    with tarfile.open(arc, "w:gz") as tf:
        for i in range(50):
            ti = tarfile.TarInfo(f"f{i}.txt")
            ti.size = 1
            tf.addfile(ti, io.BytesIO(b"x"))

    import tools.archive as archive_mod

    realized = {"n": 0}
    original = archive_mod._member_from_tar

    def counting(member):
        realized["n"] += 1
        return original(member)

    monkeypatch.setattr(archive_mod, "_member_from_tar", counting)
    limits = ExtractLimits(max_entries=5, max_file_bytes=1024, max_total_bytes=1024)
    with pytest.raises(ArchiveError):
        safe_extract(arc, tmp_path / "out", strip_top_level=False, limits=limits)

    # Only enough members to trip the cap are realized, not all 50.
    assert realized["n"] <= limits.max_entries + 1


def test_safe_extract_counts_skipped_entries_against_cap(tmp_path: Path) -> None:
    # Members that normalize to nothing ('.', empty arcname) still count, so a
    # flood of skipped entries cannot bypass the cap.
    arc = tmp_path / "dots.tar.gz"
    with tarfile.open(arc, "w:gz") as tf:
        for _ in range(10):
            ti = tarfile.TarInfo(".")
            ti.size = 0
            tf.addfile(ti, io.BytesIO(b""))
        real = tarfile.TarInfo("real.txt")
        real.size = 1
        tf.addfile(real, io.BytesIO(b"x"))
    limits = ExtractLimits(max_entries=3, max_file_bytes=1024, max_total_bytes=1024)
    with pytest.raises(ArchiveError):
        safe_extract(arc, tmp_path / "out", strip_top_level=False, limits=limits)


def test_safe_extract_zip_entry_count_cap(tmp_path: Path) -> None:
    arc = tmp_path / "many.zip"
    _write_zip(arc, {f"f{i}.txt": b"x" for i in range(5)})
    limits = ExtractLimits(max_entries=3, max_file_bytes=1024, max_total_bytes=1024)
    with pytest.raises(ArchiveError):
        safe_extract(arc, tmp_path / "out", strip_top_level=False, limits=limits)


def test_zip_entry_cap_fires_before_zipfile_materializes_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arc = tmp_path / "many.zip"
    _write_zip(arc, {f"f{i}.txt": b"x" for i in range(5)})
    constructed = False

    def forbidden_zipfile(*args, **kwargs):
        nonlocal constructed
        constructed = True
        raise AssertionError("ZipFile must not be constructed above the entry cap")

    monkeypatch.setattr("tools.archive.zipfile.ZipFile", forbidden_zipfile)
    limits = ExtractLimits(max_entries=3, max_file_bytes=1024, max_total_bytes=1024)

    with pytest.raises(ArchiveError, match="more than 3 entries"):
        safe_extract(arc, tmp_path / "out", strip_top_level=False, limits=limits)

    assert constructed is False


def test_safe_extract_per_file_cap(tmp_path: Path) -> None:
    arc = tmp_path / "big.tar.gz"
    with tarfile.open(arc, "w:gz") as tf:
        ti = tarfile.TarInfo("big.txt")
        ti.size = 100
        tf.addfile(ti, io.BytesIO(b"x" * 100))
    limits = ExtractLimits(max_entries=10, max_file_bytes=10, max_total_bytes=1000)
    with pytest.raises(ArchiveError):
        safe_extract(arc, tmp_path / "out", strip_top_level=False, limits=limits)


def test_safe_extract_total_cap(tmp_path: Path) -> None:
    arc = tmp_path / "total.tar.gz"
    with tarfile.open(arc, "w:gz") as tf:
        for i in range(3):
            ti = tarfile.TarInfo(f"f{i}.txt")
            ti.size = 100
            tf.addfile(ti, io.BytesIO(b"x" * 100))
    limits = ExtractLimits(max_entries=10, max_file_bytes=1000, max_total_bytes=150)
    with pytest.raises(ArchiveError):
        safe_extract(arc, tmp_path / "out", strip_top_level=False, limits=limits)


def test_safe_extract_tar_bomb_bounded_by_decompression_cap(tmp_path: Path) -> None:
    """A gzip tar bomb (one huge, trivially-compressible member) must trip the
    total-decompression cap during streaming enumeration instead of inflating
    unbounded. That is the whole point of the two-pass streaming extractor."""
    arc = tmp_path / "bomb.tar.gz"
    # 200 MB of zeros compresses to a few hundred KB but would inflate past the
    # 50 MB total cap; the streaming reader must abort while skipping it.
    bomb = b"\x00" * (200 * 1024 * 1024)
    with tarfile.open(arc, "w:gz") as tf:
        ti = tarfile.TarInfo("big.bin")
        ti.size = len(bomb)
        tf.addfile(ti, io.BytesIO(bomb))
    assert arc.stat().st_size < 5 * 1024 * 1024  # small on disk (compresses well)
    with pytest.raises(ArchiveError, match="decompress|size limit"):
        safe_extract(arc, tmp_path / "out", strip_top_level=False, limits=LIMITS)
    # Nothing was written past the cap (dest either absent or empty of the bomb).
    assert not (tmp_path / "out" / "big.bin").exists()


def test_safe_extract_tar_roundtrip_and_strip_preserved(tmp_path: Path) -> None:
    """The streaming rewrite must still extract normal archives and strip a common
    top-level directory."""
    arc = tmp_path / "proj.tar.gz"
    _write_tar(
        arc,
        {
            "proj/readme.txt": b"hello",
            "proj/src/main.py": b"print(1)\n",
        },
    )
    dest = tmp_path / "out"
    result = safe_extract(arc, dest, strip_top_level=True, limits=LIMITS)
    assert result.stripped_top_level == "proj"
    assert (dest / "readme.txt").read_bytes() == b"hello"
    assert (dest / "src" / "main.py").read_bytes() == b"print(1)\n"
    assert result.entries == 2
