from __future__ import annotations

import pytest

from tools import downloads
from tools.downloads import FetchUrlError, validate_fetch_url


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
async def test_fetch_url_trusted_redirects_keep_initial_allowlist(monkeypatch, tmp_path):
    seen: list[tuple[str, tuple[str, ...] | None]] = []

    def fake_validate(url, *, allowed_host_suffixes=None):
        seen.append((url, allowed_host_suffixes))

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
                return FakeResponse(302, "https://cdn.example.test/file.pdf")
            return FakeResponse(200)

    class FakeConnector:
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(downloads, "validate_fetch_url", fake_validate)
    monkeypatch.setattr(downloads.aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(downloads.aiohttp, "TCPConnector", FakeConnector)

    await downloads.fetch_url_to_file(
        "https://content.openalex.org/works/W1.pdf",
        tmp_path / "paper.pdf",
        max_bytes=100,
        timeout_seconds=1,
        allowed_host_suffixes=("openalex.org",),
        allow_redirects_to_any_public_host=True,
    )

    assert seen == [
        ("https://content.openalex.org/works/W1.pdf", ("openalex.org",)),
        ("https://cdn.example.test/file.pdf", None),
    ]
