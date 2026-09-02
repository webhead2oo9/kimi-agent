"""fetch_url: downloading into a workspace."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tools.workspace import fetch as workspace_fetch
from tools.downloads import (
    FetchResult,
    _is_public_address,
)
from tools.workspace import (
    WorkspaceToolConfig,
)
from tools.registry import UNTRUSTED_CONTEXT_NOTE

from tests.workspace_tool_helpers import (
    WS,
    _make_ctx,
    _register,
)


@pytest.mark.asyncio
async def test_fetch_url_downloads_to_workspace_without_attaching(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_fetch(
        url: str,
        destination: Path,
        *,
        max_bytes: int,
        timeout_seconds: float,
        max_redirects: int,
    ) -> FetchResult:
        assert url == "https://example.com/report.txt"
        assert max_bytes == 25
        assert timeout_seconds == 30
        assert max_redirects == 5
        destination.write_bytes(b"downloaded")
        return FetchResult(size_bytes=10, content_type="text/plain")

    monkeypatch.setattr(workspace_fetch, "fetch_url_to_file", fake_fetch)
    reg, mgr = _register(tmp_path, WorkspaceToolConfig(max_file_bytes=25))
    ctx = _make_ctx()

    result = await reg.dispatch(
        "fetch_url",
        {"url": "https://example.com/report.txt"},
        ctx,
    )

    saved = mgr.user_files_dir(WS) / "report.txt"
    assert json.loads(result) == {
        "path": "report.txt",
        "filename": "report.txt",
        "size_bytes": 10,
        "content_type": "text/plain",
        "attached": False,
        "attachment_hint": (
            "Not attached. Before saying the file is attached or available to download, call "
            "queue_file with its path and confirm it returns queued: true."
        ),
        "context_is_untrusted": True,
        "note": UNTRUSTED_CONTEXT_NOTE,
    }
    entry = next(item for item in reg.get_all_tools() if item.name == "fetch_url")
    assert entry.untrusted is True
    assert saved.read_bytes() == b"downloaded"
    assert ctx.output_files == []
    assert ctx.allowed_file_roots == []

    queued = json.loads(await reg.dispatch("queue_file", {"path": "report.txt"}, ctx))
    assert queued["queued"] is True
    assert ctx.output_files == [str(saved.resolve())]
    assert ctx.allowed_file_roots == [str(mgr.user_files_dir(WS).resolve())]


@pytest.mark.asyncio
async def test_fetch_url_scrubs_workspace_paths_from_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_fetch(
        url: str,
        destination: Path,
        *,
        max_bytes: int,
        timeout_seconds: float,
        max_redirects: int,
    ) -> FetchResult:
        destination.write_bytes(b"x")
        return FetchResult(size_bytes=1, content_type="text/plain")

    monkeypatch.setattr(workspace_fetch, "fetch_url_to_file", fake_fetch)
    reg, mgr = _register(tmp_path)
    ctx = _make_ctx()
    workspace_root = mgr.user_files_dir(WS).resolve()
    (workspace_root / "blocked_name.txt").mkdir()

    result = await reg.dispatch(
        "fetch_url",
        {
            "url": "https://example.com/report.txt",
            "filename": "blocked name.txt",
        },
        ctx,
    )

    parsed = json.loads(result)
    assert parsed["error"]
    assert str(tmp_path) not in parsed["error"]
    assert str(workspace_root) not in parsed["error"]


@pytest.mark.asyncio
async def test_fetch_url_uses_unique_name_for_auto_derived_downloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_fetch(
        url: str,
        destination: Path,
        *,
        max_bytes: int,
        timeout_seconds: float,
        max_redirects: int,
    ) -> FetchResult:
        destination.write_text(url, encoding="utf-8")
        return FetchResult(size_bytes=len(url), content_type="text/plain")

    monkeypatch.setattr(workspace_fetch, "fetch_url_to_file", fake_fetch)
    reg, mgr = _register(tmp_path)
    ctx = _make_ctx()

    first = json.loads(await reg.dispatch("fetch_url", {"url": "https://example.com"}, ctx))
    second = json.loads(await reg.dispatch("fetch_url", {"url": "https://example.org"}, ctx))

    assert first["path"] == "download"
    assert second["path"] == "download-2"
    assert (mgr.user_files_dir(WS) / "download").read_text(
        encoding="utf-8"
    ) == "https://example.com"
    assert (mgr.user_files_dir(WS) / "download-2").read_text(
        encoding="utf-8"
    ) == "https://example.org"


@pytest.mark.asyncio
async def test_fetch_url_serializes_same_user_downloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active_fetches = 0
    max_active_fetches = 0

    async def fake_fetch(
        url: str,
        destination: Path,
        *,
        max_bytes: int,
        timeout_seconds: float,
        max_redirects: int,
    ) -> FetchResult:
        nonlocal active_fetches, max_active_fetches
        active_fetches += 1
        max_active_fetches = max(max_active_fetches, active_fetches)
        await asyncio.sleep(0.01)
        destination.write_text(url, encoding="utf-8")
        active_fetches -= 1
        return FetchResult(size_bytes=len(url), content_type="text/plain")

    monkeypatch.setattr(workspace_fetch, "fetch_url_to_file", fake_fetch)
    reg, _mgr = _register(tmp_path)
    ctx = _make_ctx()

    await asyncio.gather(
        reg.dispatch("fetch_url", {"url": "https://example.com/a"}, ctx),
        reg.dispatch("fetch_url", {"url": "https://example.com/b"}, ctx),
    )

    assert max_active_fetches == 1


@pytest.mark.asyncio
async def test_fetch_url_rejects_private_hosts_without_downloading(tmp_path: Path) -> None:
    reg, _mgr = _register(tmp_path)
    ctx = _make_ctx()

    result = await reg.dispatch(
        "fetch_url",
        {"url": "https://127.0.0.1/report.txt"},
        ctx,
    )

    assert "private" in json.loads(result)["error"].lower()


def test_is_public_address_rejects_explicit_internal_flags() -> None:
    class ReservedButGlobal:
        is_global = True
        is_link_local = False
        is_loopback = False
        is_multicast = False
        is_private = False
        is_reserved = True
        is_unspecified = False

    assert _is_public_address(ReservedButGlobal()) is False  # type: ignore[arg-type]
