"""SSRF-safe URL download primitives shared by the workspace fetch tools.

Shared by ``tools/workspace/fetch.py`` and any other URL-fetching tool so every
URL fetch into a user workspace uses one hardened implementation: HTTPS only
(plain http is rejected, including on redirect hops, so a LAN MITM cannot
inject file contents), no embedded credentials, public-IP-only DNS resolution
(defeats DNS rebinding), per-hop redirect re-validation, bounded redirects,
streamed size caps, and an optional host allowlist.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from dataclasses import dataclass
from email.message import Message
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult

DEFAULT_MAX_REDIRECTS = 5
MAX_FILENAME_LENGTH = 120
SAFE_SEGMENT_RE = re.compile(r"[^A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class FetchResult:
    size_bytes: int
    content_type: str
    filename: str | None = None


class FetchUrlError(ValueError):
    pass


def safe_filename(value: str) -> str:
    name = SAFE_SEGMENT_RE.sub("_", value).strip("._")
    if not name:
        name = "download"
    return name[:MAX_FILENAME_LENGTH]


def filename_from_url(url: str) -> str:
    parsed = urlparse(url)
    raw_name = unquote(Path(parsed.path).name)
    return safe_filename(raw_name or "download")


def _filename_from_content_disposition(header: str | None) -> str | None:
    if not header:
        return None
    message = Message()
    message["Content-Disposition"] = header
    filename = message.get_filename()
    return filename or None


def _is_public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return _is_public_address(address.ipv4_mapped)
    if (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        return False
    return address.is_global


class PublicOnlyResolver(AbstractResolver):
    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> list[ResolveResult]:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host,
            port,
            family=family,
            type=socket.SOCK_STREAM,
        )
        results: list[ResolveResult] = []
        seen: set[tuple[str, int]] = set()
        for resolved_family, _type, proto, _canonname, sockaddr in infos:
            raw_address = sockaddr[0]
            if not isinstance(raw_address, str):
                raise FetchUrlError("DNS resolution returned an invalid IP address")
            address = raw_address
            ip = ipaddress.ip_address(address)
            if not _is_public_address(ip):
                raise FetchUrlError("Private or internal URLs are not allowed")
            key = (address, port)
            if key in seen:
                continue
            seen.add(key)
            results.append(
                {
                    "hostname": host,
                    "host": address,
                    "port": port,
                    "family": resolved_family,
                    "proto": proto,
                    "flags": socket.AI_NUMERICHOST,
                }
            )
        return results

    async def close(self) -> None:
        return None


def validate_fetch_url(
    url: str,
    *,
    allowed_host_suffixes: tuple[str, ...] | None = None,
    allow_plain_http: bool = False,
) -> None:
    """Validate a URL against the SSRF policy.

    ``allow_plain_http`` is an explicit opt-in for callers that never connect
    to the URL themselves (e.g. the Jina reader proxies the target remotely);
    anything this process downloads directly must stay https-only.
    """
    parsed = urlparse(url)
    if allow_plain_http:
        if parsed.scheme not in {"http", "https"}:
            raise FetchUrlError("Only http and https URLs are allowed")
    elif parsed.scheme != "https":
        raise FetchUrlError("Only https URLs are allowed")
    if not parsed.hostname:
        raise FetchUrlError("URL host is required")
    if parsed.username or parsed.password:
        raise FetchUrlError("URLs with embedded credentials are not allowed")
    if allowed_host_suffixes is not None:
        host = parsed.hostname.lower()
        if not any(host == s or host.endswith("." + s) for s in allowed_host_suffixes):
            raise FetchUrlError("URL host is not in the allowed list")
    try:
        ip = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        return
    if not _is_public_address(ip):
        raise FetchUrlError("Private or internal URLs are not allowed")


async def fetch_url_to_file(
    url: str,
    destination: Path,
    *,
    max_bytes: int,
    timeout_seconds: float,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    allowed_host_suffixes: tuple[str, ...] | None = None,
    allow_redirects_to_any_public_host: bool = False,
) -> FetchResult:
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    connector = aiohttp.TCPConnector(
        resolver=PublicOnlyResolver(),
        use_dns_cache=False,
        limit_per_host=2,
    )
    current_url = url
    try:
        async with aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            trust_env=False,
        ) as session:
            for _redirect in range(max_redirects + 1):
                hop_allowed_host_suffixes = (
                    None
                    if _redirect > 0 and allow_redirects_to_any_public_host
                    else allowed_host_suffixes
                )
                validate_fetch_url(
                    current_url,
                    allowed_host_suffixes=hop_allowed_host_suffixes,
                )
                async with session.get(current_url, allow_redirects=False) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location")
                        if not location:
                            raise FetchUrlError("Redirect response missing Location header")
                        current_url = urljoin(current_url, location)
                        continue
                    if response.status >= 400:
                        raise FetchUrlError(f"URL fetch failed with HTTP {response.status}")
                    filename = _filename_from_content_disposition(
                        response.headers.get("Content-Disposition")
                    )
                    content_type = response.headers.get("Content-Type", "")
                    size = 0
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with destination.open("wb") as handle:
                        async for chunk in response.content.iter_chunked(64 * 1024):
                            size += len(chunk)
                            if size > max_bytes:
                                raise FetchUrlError(
                                    f"Download exceeds maximum size of {max_bytes} bytes"
                                )
                            handle.write(chunk)
                    return FetchResult(
                        size_bytes=size,
                        content_type=content_type,
                        filename=safe_filename(filename) if filename else None,
                    )
            raise FetchUrlError(f"Too many redirects; maximum is {max_redirects}")
    except TimeoutError as e:
        raise FetchUrlError(f"URL fetch timed out after {timeout_seconds}s") from e
    finally:
        # Cold error/cleanup path: the bounded sync stat/unlink never runs in the
        # hot streaming loop, so the event-loop block is negligible.
        if destination.exists() and destination.stat().st_size > max_bytes:  # noqa: ASYNC240
            destination.unlink(missing_ok=True)  # noqa: ASYNC240
