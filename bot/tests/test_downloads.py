from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import aiohttp
import pytest

from tools import downloads
from tools.downloads import FetchUrlError, validate_fetch_url


@pytest.fixture
def download_response(monkeypatch):
    class FakeResponse:
        status = 200
        headers = {"Content-Type": "text/plain"}

        def __init__(self, chunks):
            self.chunks = chunks
            self.content = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def iter_chunked(self, _size):
            for chunk in self.chunks:
                if isinstance(chunk, Exception):
                    raise chunk
                yield chunk

    class FakeSession:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        def get(self, _url, *, allow_redirects):
            return response

    response = FakeResponse([b"ok"])
    monkeypatch.setattr(downloads.aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(downloads.aiohttp, "TCPConnector", lambda **kwargs: None)
    return response


@pytest.mark.asyncio
async def test_download_streams_to_destination_off_event_loop(
    monkeypatch, tmp_path, download_response
):
    loop_thread = threading.get_ident()
    open_file = Path.open
    operations = []

    class CheckedFile:
        def __init__(self, handle):
            self.handle = handle

        def write(self, chunk):
            assert threading.get_ident() != loop_thread
            operations.append("write")
            return self.handle.write(chunk)

        def close(self):
            assert threading.get_ident() != loop_thread
            operations.append("close")
            self.handle.close()

    def checked_open(path, *args, **kwargs):
        assert threading.get_ident() != loop_thread
        operations.append("open")
        return CheckedFile(open_file(path, *args, **kwargs))

    download_response.chunks = [b"hello", b" world"]
    destination = tmp_path / "nested" / "download.txt"
    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", checked_open)
        result = await downloads.fetch_url_to_file(
            "https://example.com/file", destination, max_bytes=11, timeout_seconds=1
        )

    assert destination.read_bytes() == b"hello world"
    assert result == downloads.FetchResult(size_bytes=11, content_type="text/plain")
    assert operations == ["open", "write", "write", "close"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("last_chunk", "error_type", "message"),
    [
        (b"oversized", FetchUrlError, "maximum size"),
        (aiohttp.ClientPayloadError("broken stream"), aiohttp.ClientPayloadError, "broken stream"),
        (TimeoutError(), FetchUrlError, "timed out"),
    ],
)
async def test_failed_download_removes_partial_file(
    tmp_path, download_response, last_chunk, error_type, message
):
    download_response.chunks = [b"ok", last_chunk]
    destination = tmp_path / "download.txt"
    with pytest.raises(error_type, match=message):
        await downloads.fetch_url_to_file(
            "https://example.com/file", destination, max_bytes=4, timeout_seconds=1
        )
    assert not destination.exists()


@pytest.mark.asyncio
async def test_rejected_url_preserves_existing_destination(tmp_path, download_response):
    destination = tmp_path / "existing.txt"
    destination.write_bytes(b"existing content")
    with pytest.raises(FetchUrlError, match="Only https"):
        await downloads.fetch_url_to_file(
            "http://example.com/file", destination, max_bytes=4, timeout_seconds=1
        )
    assert destination.read_bytes() == b"existing content"


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_operation", ["open", "write"])
async def test_cancelled_download_finishes_disk_operation_before_cleanup(
    monkeypatch, tmp_path, download_response, blocked_operation
):
    loop_thread = threading.get_ident()
    started = threading.Event()
    release = threading.Event()
    open_file = Path.open
    closed = False

    def block(operation):
        assert threading.get_ident() != loop_thread
        if operation == blocked_operation:
            started.set()
            assert release.wait(timeout=5), "disk operation was never released"

    class SlowFile:
        def __init__(self, handle):
            self.handle = handle

        def write(self, chunk):
            block("write")
            assert not closed
            return self.handle.write(chunk)

        def close(self):
            nonlocal closed
            assert release.is_set()
            self.handle.close()
            closed = True

    def slow_open(path, *args, **kwargs):
        handle = open_file(path, *args, **kwargs)
        block("open")
        return SlowFile(handle)

    destination = tmp_path / "download.txt"
    monkeypatch.setattr(Path, "open", slow_open)
    task = asyncio.create_task(
        downloads.fetch_url_to_file(
            "https://example.com/file", destination, max_bytes=10, timeout_seconds=10
        )
    )
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert not closed
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert closed
    assert not destination.exists()


def test_validate_allows_listed_host_and_subdomain():
    validate_fetch_url(
        "https://public6.wolframalpha.com/files/x.png",
        allowed_host_suffixes=("wolframalpha.com",),
    )


def test_validate_rejects_unlisted_host():
    with pytest.raises(FetchUrlError, match="allowed list"):
        validate_fetch_url(
            "https://evil.example.com/x.png",
            allowed_host_suffixes=("wolframalpha.com",),
        )


def test_validate_rejects_lookalike_suffix():
    # endswith("wolframalpha.com") but NOT ".wolframalpha.com"
    with pytest.raises(FetchUrlError, match="allowed list"):
        validate_fetch_url(
            "https://evilwolframalpha.com/x.png",
            allowed_host_suffixes=("wolframalpha.com",),
        )


def test_validate_rejects_credentials_even_if_host_allowed():
    with pytest.raises(FetchUrlError, match="credentials"):
        validate_fetch_url(
            "https://u:p@public6.wolframalpha.com/x.png",
            allowed_host_suffixes=("wolframalpha.com",),
        )


def test_validate_without_allowlist_is_unrestricted_host():
    # No allowlist -> any public https host is fine (existing fetch_url behavior).
    validate_fetch_url("https://example.com/x.png")


def test_validate_rejects_plain_http():
    # HTTPS only: plain http allows a LAN MITM to inject downloaded file
    # contents. Per-hop redirect re-validation makes this also reject
    # https -> http downgrade redirects.
    with pytest.raises(FetchUrlError, match="https"):
        validate_fetch_url("http://example.com/x.png")


def test_validate_rejects_non_http_schemes():
    for url in ("ftp://example.com/x", "file:///etc/passwd", "gopher://example.com/x"):
        with pytest.raises(FetchUrlError, match="https"):
            validate_fetch_url(url)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("redirect_url", "error"),
    [
        ("https://cdn.example.test/file.pdf", None),
        ("http://cdn.example.test/file.pdf", "Only https"),
        ("https://127.0.0.1/file.pdf", "Private or internal"),
    ],
)
async def test_fetch_url_trusted_redirects_keep_download_policy(
    monkeypatch, tmp_path, redirect_url, error
):
    seen: list[tuple[str, tuple[str, ...] | None]] = []

    def fake_validate(url, *, allowed_host_suffixes=None):
        seen.append((url, allowed_host_suffixes))
        validate_fetch_url(url, allowed_host_suffixes=allowed_host_suffixes)

    class FakeResponse:
        def __init__(self, status, location=None):
            self.status = status
            self.headers = {"Location": location} if location else {"Content-Type": "text/plain"}
            self.content = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def iter_chunked(self, _size):
            yield b"ok"

    class FakeSession:
        def __init__(self, *args, **kwargs):
            self.calls = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        def get(self, _url, *, allow_redirects):
            self.calls += 1
            if self.calls == 1:
                return FakeResponse(302, redirect_url)
            return FakeResponse(200)

    class FakeConnector:
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(downloads, "validate_fetch_url", fake_validate)
    monkeypatch.setattr(downloads.aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(downloads.aiohttp, "TCPConnector", FakeConnector)

    request = downloads.fetch_url_to_file(
        "https://content.openalex.org/works/W1.pdf",
        tmp_path / "paper.pdf",
        max_bytes=100,
        timeout_seconds=1,
        allowed_host_suffixes=("openalex.org",),
        allow_redirects_to_any_public_host=True,
    )
    if error is None:
        await request
        assert (tmp_path / "paper.pdf").read_bytes() == b"ok"
    else:
        with pytest.raises(FetchUrlError, match=error):
            await request
        assert not (tmp_path / "paper.pdf").exists()

    assert seen == [
        ("https://content.openalex.org/works/W1.pdf", ("openalex.org",)),
        (redirect_url, None),
    ]
