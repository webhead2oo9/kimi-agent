"""The shared atomic writer's guarantees.

Three call sites depend on these: a failed write must leave the previous
version intact (a half-written SKILL.md is a skill that stops loading), and the
Codex token must never exist at a wider mode than 0600.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from utils.files import atomic_write_bytes, atomic_write_text


def test_writes_replace_the_previous_content(tmp_path: Path) -> None:
    target = tmp_path / "skill.md"
    atomic_write_text(target, "first\n")
    atomic_write_text(target, "second\n")

    assert target.read_text(encoding="utf-8") == "second\n"


def test_parent_directories_are_created(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "deeper" / "note.md"
    atomic_write_text(target, "body")

    assert target.read_text(encoding="utf-8") == "body"


def test_a_failed_write_leaves_the_original_and_no_temp_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "skill.md"
    atomic_write_text(target, "original\n")

    def explode(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", explode)

    with pytest.raises(OSError):
        atomic_write_text(target, "replacement\n")

    assert target.read_text(encoding="utf-8") == "original\n"
    assert [p.name for p in tmp_path.iterdir()] == ["skill.md"]


def test_newlines_are_written_verbatim(tmp_path: Path) -> None:
    """These files round-trip through parsers that count lines."""
    target = tmp_path / "doc.md"
    atomic_write_text(target, "a\nb\n")

    assert target.read_bytes() == b"a\nb\n"


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_mode_is_applied_even_when_the_target_already_exists(tmp_path: Path) -> None:
    target = tmp_path / "codex-auth.json"
    target.write_text("{}", encoding="utf-8")
    target.chmod(0o644)

    atomic_write_text(target, '{"token": "x"}', fsync=False, mode=0o600)

    assert target.stat().st_mode & 0o777 == 0o600


def test_bytes_and_text_agree(tmp_path: Path) -> None:
    as_text = tmp_path / "a.bin"
    as_bytes = tmp_path / "b.bin"
    atomic_write_text(as_text, "héllo")
    atomic_write_bytes(as_bytes, "héllo".encode())

    assert as_text.read_bytes() == as_bytes.read_bytes()
