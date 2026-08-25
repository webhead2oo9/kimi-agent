"""grep_workspace and glob_workspace."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from tools.workspace import (
    WorkspaceToolConfig,
)
from trust.tiers import TrustTier

from tests.workspace_tool_helpers import (
    WS,
    _make_ctx,
    _register,
    _requires_symlinks,
)


@pytest.mark.asyncio
async def test_grep_workspace_uses_literal_matching_by_default(tmp_path: Path) -> None:
    reg, _mgr = _register(tmp_path)
    ctx = _make_ctx()
    await reg.dispatch(
        "write_file",
        {"path": "patterns.txt", "content": "abc\na.b\nxyz"},
        ctx,
    )

    literal = json.loads(await reg.dispatch("grep_workspace", {"pattern": "."}, ctx))
    assert literal["matches"] == [{"file": "patterns.txt", "line_number": 2, "text": "a.b"}]

    regex = json.loads(
        await reg.dispatch(
            "grep_workspace",
            {"pattern": ".", "regex": True},
            _make_ctx(TrustTier.STAFF),
        )
    )
    assert len(regex["matches"]) == 3


@pytest.mark.asyncio
async def test_grep_workspace_allows_regex_for_members(tmp_path: Path) -> None:
    reg, _mgr = _register(tmp_path)
    ctx = _make_ctx()
    await reg.dispatch(
        "write_file",
        {"path": "patterns.txt", "content": "abc\na.b\nxyz"},
        ctx,
    )

    result = json.loads(
        await reg.dispatch("grep_workspace", {"pattern": "a.c", "regex": True}, ctx)
    )

    # Regex is workspace-isolated and ReDoS-guarded, so members can use it;
    # "a.c" only matches "abc" when treated as a regex (no literal "a.c" exists).
    assert result["count"] == 1
    assert result["matches"][0]["text"] == "abc"


@pytest.mark.asyncio
async def test_grep_workspace_rejects_catastrophic_regex(tmp_path: Path) -> None:
    reg, _mgr = _register(tmp_path)
    staff = _make_ctx(TrustTier.STAFF)
    await reg.dispatch(
        "write_file", {"path": "f.txt", "content": "aaaaaaaaaaaaaaaaaaaaaaaaX"}, staff
    )

    # wait_for so a regression (guard removed) fails via timeout instead of
    # hanging the whole suite on catastrophic backtracking.
    result = json.loads(
        await asyncio.wait_for(
            reg.dispatch("grep_workspace", {"pattern": "(a+)+$", "regex": True}, staff),
            timeout=5,
        )
    )
    assert "error" in result
    assert "quantifier" in result["error"].lower()


def test_search_bounded_raises_when_budget_exhausted() -> None:

    import regex

    from tools.workspace.search import GrepTimeoutError, _search_bounded

    matcher = regex.compile("anything")
    # A deadline already in the past means no budget remains: abort rather than
    # start an unbounded match. This is the guard that stops a catastrophic line
    # from pinning the (GIL-holding) regex engine after the budget is spent.
    with pytest.raises(GrepTimeoutError):
        _search_bounded(matcher, "some line", deadline=time.monotonic() - 1.0)


@pytest.mark.asyncio
async def test_grep_workspace_surfaces_timeout_error(tmp_path: Path) -> None:
    # A zero budget forces the timeout path deterministically without needing a
    # genuinely catastrophic pattern: every real grep would exceed 0 seconds of
    # match time, so the walk aborts with the time-budget error instead of hanging.
    reg, _mgr = _register(tmp_path, WorkspaceToolConfig(grep_timeout_seconds=0.0))
    staff = _make_ctx(TrustTier.STAFF)
    await reg.dispatch("write_file", {"path": "f.txt", "content": "hello world\n"}, staff)

    result = json.loads(
        await asyncio.wait_for(
            reg.dispatch("grep_workspace", {"pattern": "world"}, staff),
            timeout=5,
        )
    )
    assert "error" in result
    assert "time budget" in result["error"].lower()


@pytest.mark.asyncio
async def test_grep_workspace_allows_safe_staff_regex(tmp_path: Path) -> None:
    reg, _mgr = _register(tmp_path)
    staff = _make_ctx(TrustTier.STAFF)

    # A linear pattern and a benign quantified group must both still work: the
    # detector targets only nested quantifiers, not all quantified groups.
    await reg.dispatch("write_file", {"path": "f.txt", "content": "aaab"}, staff)
    linear = json.loads(
        await reg.dispatch("grep_workspace", {"pattern": "a+b", "regex": True}, staff)
    )
    assert "error" not in linear
    assert linear["count"] == 1

    grouped = json.loads(
        await reg.dispatch("grep_workspace", {"pattern": "(aa)+b", "regex": True}, staff)
    )
    assert "error" not in grouped


@pytest.mark.asyncio
async def test_grep_workspace_searches_files_larger_than_read_cap(tmp_path: Path) -> None:
    # grep streams line-by-line, so it is not bound by read_file's whole-file read limit.
    reg, _mgr = _register(
        tmp_path,
        WorkspaceToolConfig(max_read_bytes=16, max_file_bytes=10_000),
    )
    ctx = _make_ctx()
    await reg.dispatch(
        "write_file",
        {"path": "big.txt", "content": "filler\n" * 50 + "needle here\n"},
        ctx,
    )

    result = json.loads(await reg.dispatch("grep_workspace", {"pattern": "needle"}, ctx))

    assert result["count"] == 1
    assert result["matches"][0]["file"] == "big.txt"
    assert result["matches"][0]["text"] == "needle here"
    assert "skipped" not in result


@pytest.mark.asyncio
async def test_grep_workspace_includes_context_lines(tmp_path: Path) -> None:
    reg, _mgr = _register(tmp_path)
    ctx = _make_ctx()
    await reg.dispatch(
        "write_file",
        {"path": "log.txt", "content": "a\nb\nTRACE\nc\nd"},
        ctx,
    )

    result = json.loads(
        await reg.dispatch("grep_workspace", {"pattern": "TRACE", "context": 2}, ctx)
    )

    assert result["matches"] == [
        {
            "file": "log.txt",
            "line_number": 3,
            "text": "TRACE",
            "before": ["a", "b"],
            "after": ["c", "d"],
        }
    ]


@pytest.mark.asyncio
async def test_grep_workspace_truncates_long_match_lines(tmp_path: Path) -> None:
    reg, _mgr = _register(tmp_path)
    ctx = _make_ctx()
    await reg.dispatch(
        "write_file",
        {"path": "blob.txt", "content": "a" * 2000},
        ctx,
    )

    result = json.loads(await reg.dispatch("grep_workspace", {"pattern": "aaa"}, ctx))

    assert result["matches"][0]["text"] == "a" * 1000 + "…[+1000 chars]"


@pytest.mark.asyncio
async def test_grep_workspace_flags_truncated_results(tmp_path: Path) -> None:
    reg, _mgr = _register(tmp_path)
    ctx = _make_ctx()
    await reg.dispatch(
        "write_file",
        {"path": "many.txt", "content": "hit\n" * 10},
        ctx,
    )

    result = json.loads(
        await reg.dispatch("grep_workspace", {"pattern": "hit", "max_results": 3}, ctx)
    )

    assert result["count"] == 3
    assert result["truncated"] is True


@pytest.mark.asyncio
async def test_grep_workspace_skips_binary_files_with_reason(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path)
    ctx = _make_ctx()
    await reg.dispatch(
        "write_file",
        {"path": "good.txt", "content": "match here"},
        ctx,
    )
    (mgr.user_files_dir(WS) / "blob.bin").write_bytes(b"\x00\x01match\x00")

    result = json.loads(await reg.dispatch("grep_workspace", {"pattern": "match"}, ctx))

    assert result["matches"] == [{"file": "good.txt", "line_number": 1, "text": "match here"}]
    assert result["skipped"] == [
        {"file": "blob.bin", "reason": "Binary or non-UTF-8 file cannot be searched"}
    ]


def test_grep_workspace_schema_shows_regex_for_all_tiers(tmp_path: Path) -> None:
    reg, _mgr = _register(tmp_path)

    member_schema = next(
        schema
        for schema in reg.get_tool_schemas(TrustTier.MEMBER)
        if schema["name"] == "grep_workspace"
    )
    staff_schema = next(
        schema
        for schema in reg.get_tool_schemas(TrustTier.STAFF)
        if schema["name"] == "grep_workspace"
    )

    assert "regex" in member_schema["parameters"]["properties"]
    assert "regex" in staff_schema["parameters"]["properties"]


@pytest.mark.asyncio
async def test_glob_workspace_matches_extension_at_any_depth(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path)
    ctx = _make_ctx()
    root = mgr.user_files_dir(WS)
    (root / "a.py").write_text("x", encoding="utf-8")
    (root / "sub").mkdir()
    (root / "sub" / "b.py").write_text("y", encoding="utf-8")
    (root / "c.txt").write_text("z", encoding="utf-8")

    result = await reg.dispatch("glob_workspace", {"pattern": "*.py"}, ctx)
    body = json.loads(result)
    assert sorted(body["matches"]) == ["a.py", "sub/b.py"]
    assert body["count"] == 2
    assert "truncated" not in body


@pytest.mark.asyncio
async def test_glob_workspace_matches_bare_basename_anywhere(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path)
    ctx = _make_ctx()
    root = mgr.user_files_dir(WS)
    (root / "deep").mkdir()
    (root / "deep" / "config.json").write_text("{}", encoding="utf-8")

    result = await reg.dispatch("glob_workspace", {"pattern": "config.json"}, ctx)
    assert json.loads(result)["matches"] == ["deep/config.json"]


@pytest.mark.asyncio
async def test_glob_workspace_scoped_to_subdir(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path)
    ctx = _make_ctx()
    root = mgr.user_files_dir(WS)
    (root / "top.py").write_text("x", encoding="utf-8")
    (root / "sub").mkdir()
    (root / "sub" / "inner.py").write_text("y", encoding="utf-8")

    result = await reg.dispatch("glob_workspace", {"pattern": "*.py", "path": "sub"}, ctx)
    assert json.loads(result)["matches"] == ["sub/inner.py"]


@pytest.mark.asyncio
@_requires_symlinks
async def test_glob_workspace_skips_symlinks(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path)
    ctx = _make_ctx()
    root = mgr.user_files_dir(WS)
    real = root / "real.txt"
    real.write_text("data", encoding="utf-8")
    # A symlink that even points at an out-of-workspace file must never surface.
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    (root / "link.txt").symlink_to(outside)

    result = await reg.dispatch("glob_workspace", {"pattern": "*.txt"}, ctx)
    assert json.loads(result)["matches"] == ["real.txt"]


@pytest.mark.asyncio
async def test_glob_workspace_truncates_at_cap(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path, WorkspaceToolConfig(glob_max_results=2))
    ctx = _make_ctx()
    root = mgr.user_files_dir(WS)
    for i in range(4):
        (root / f"{i}.log").write_text("x", encoding="utf-8")

    result = await reg.dispatch("glob_workspace", {"pattern": "*.log"}, ctx)
    body = json.loads(result)
    assert body["count"] == 2
    assert body["truncated"] is True


@pytest.mark.asyncio
async def test_glob_workspace_streams_until_result_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reg, mgr = _register(tmp_path, WorkspaceToolConfig(glob_max_results=1))
    ctx = _make_ctx()
    root = mgr.user_files_dir(WS)
    first = root / "first.log"
    first.write_text("x", encoding="utf-8")

    def fake_rglob(self: Path, pattern: str):
        yield first
        raise AssertionError("glob walk was materialized before applying max_results")

    monkeypatch.setattr(Path, "rglob", fake_rglob)

    result = await reg.dispatch("glob_workspace", {"pattern": "*.log"}, ctx)
    body = json.loads(result)
    assert body["matches"] == ["first.log"]
    assert body["count"] == 1
    assert body["truncated"] is True


@pytest.mark.asyncio
async def test_glob_workspace_no_match(tmp_path: Path) -> None:
    reg, mgr = _register(tmp_path)
    ctx = _make_ctx()
    (mgr.user_files_dir(WS) / "a.py").write_text("x", encoding="utf-8")
    result = await reg.dispatch("glob_workspace", {"pattern": "*.zzz"}, ctx)
    assert json.loads(result) == {"matches": [], "count": 0}


@pytest.mark.asyncio
async def test_glob_workspace_requires_pattern(tmp_path: Path) -> None:
    reg, _ = _register(tmp_path)
    ctx = _make_ctx()
    result = await reg.dispatch("glob_workspace", {}, ctx)
    assert json.loads(result) == {"error": "pattern is required"}
