"""Outbound HTTP for modules, bounded by each module's declared host rules.

A module lists the hosts it talks to in ``permissions.http_hosts``; core
resolves ``discord-cdn`` and ``${setting}`` entries at load and then checks
every request, and every redirect hop, against the resolved rules: exact
host, allowed schemes, allowed ports, and network policy. ``public`` hosts
resolve through ``PublicOnlyResolver`` so DNS can never land on a private or
metadata address; a ``private`` rule allows exactly that host and nothing
wider. Bodies are capped while streaming, timeouts are bounded, and errors
never echo headers or credentials.
"""

from __future__ import annotations

import ipaddress
import json
import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

import aiohttp

from community_agent_module_api.contracts import (
    DISCORD_CDN_HOSTS,
    HostNotAllowed,
    HttpHostRule,
    HttpResponse,
    ModuleContractError,
    ResponseTooLarge,
)
from tools.downloads import PublicOnlyResolver

log = logging.getLogger(__name__)

MAX_REDIRECTS = 5
DEFAULT_MAX_BYTES = 8 * 1024 * 1024
_CHUNK = 64 * 1024
_METADATA_HOSTS = frozenset({"metadata.google.internal", "metadata", "instance-data"})
_METADATA_ADDRESSES = ("169.254.169.254", "fd00:ec2::254", "100.100.100.200")
_CROSS_ORIGIN_SENSITIVE_HEADERS = frozenset({"authorization", "cookie", "proxy-authorization"})


class ModuleHttpError(RuntimeError):
    """Transport-level failure; the message is safe to show staff."""


@dataclass(frozen=True, slots=True)
class ResolvedHostRule:
    host: str
    schemes: frozenset[str]
    ports: frozenset[int]  # empty = scheme default only
    private: bool

    def allows_port(self, scheme: str, port: int | None) -> bool:
        effective = port if port is not None else (443 if scheme == "https" else 80)
        if self.ports:
            return effective in self.ports
        return effective == (443 if scheme == "https" else 80)


def _host_from_setting(value: Any) -> tuple[str, str | None, int | None]:
    """Accept a bare host or a URL; return (host, scheme, port)."""
    text = str(value or "").strip()
    if not text:
        raise ModuleContractError("http host setting is empty")
    if "://" not in text:
        text = "https://" + text
    parsed = urlsplit(text)
    if not parsed.hostname:
        raise ModuleContractError("http host setting has no host")
    return parsed.hostname.lower(), parsed.scheme.lower() or None, parsed.port


def resolve_host_rules(
    module_name: str,
    rules: Sequence[HttpHostRule],
    settings: Mapping[str, Any] | None,
) -> tuple[ResolvedHostRule, ...]:
    """Expand declared rules into exact hosts; done once at module load."""
    resolved: list[ResolvedHostRule] = []
    for rule in rules:
        if rule.is_discord_cdn:
            for host in sorted(DISCORD_CDN_HOSTS):
                resolved.append(ResolvedHostRule(host, frozenset({"https"}), frozenset(), False))
            continue
        setting = rule.setting_name
        if setting is not None:
            if settings is None or setting not in settings:
                raise ModuleContractError(
                    f"module {module_name!r} http host ${{{setting}}} names an unknown setting"
                )
            host, scheme, port = _host_from_setting(settings[setting])
            schemes = frozenset({scheme} if scheme else rule.schemes)
            ports = frozenset({port} if port else rule.ports)
        else:
            host, schemes, ports = rule.host.lower(), frozenset(rule.schemes), frozenset(rule.ports)
        if host in _METADATA_HOSTS or host in _METADATA_ADDRESSES:
            raise ModuleContractError(f"module {module_name!r} may not target {host!r}")
        resolved.append(ResolvedHostRule(host, schemes, ports, rule.network == "private"))
    return tuple(resolved)


def _is_private_address(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return not ip.is_global


def _origin(url: str) -> tuple[str, str, int | None]:
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise HostNotAllowed("invalid port") from exc
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), port


class ModuleHttpRuntime:
    """Process-wide sessions: one public-only, one for declared private hosts."""

    def __init__(self, *, user_agent: str = "KimiAgent-Module") -> None:
        self._user_agent = user_agent
        self._public: aiohttp.ClientSession | None = None
        self._private: aiohttp.ClientSession | None = None

    def session(self, *, private: bool) -> aiohttp.ClientSession:
        if private:
            if self._private is None or self._private.closed:
                self._private = aiohttp.ClientSession(
                    headers={"User-Agent": self._user_agent}, trust_env=False
                )
            return self._private
        if self._public is None or self._public.closed:
            self._public = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(resolver=PublicOnlyResolver()),
                headers={"User-Agent": self._user_agent},
                trust_env=False,
            )
        return self._public

    async def close(self) -> None:
        for session in (self._public, self._private):
            if session is not None and not session.closed:
                await session.close()
        self._public = None
        self._private = None

    def client_for(self, module_name: str, rules: Sequence[ResolvedHostRule]) -> ModuleHttpImpl:
        return ModuleHttpImpl(self, module_name, tuple(rules))


class ModuleHttpImpl:
    """The ``ModuleHttp`` port handed to one module."""

    def __init__(
        self, runtime: ModuleHttpRuntime, module_name: str, rules: tuple[ResolvedHostRule, ...]
    ) -> None:
        self._runtime = runtime
        self._module_name = module_name
        self._rules = {rule.host: rule for rule in rules}

    # ---- policy -------------------------------------------------------------

    def _check(self, url: str) -> tuple[ResolvedHostRule, str]:
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").rstrip(".").lower()
        if scheme not in ("http", "https"):
            raise HostNotAllowed(f"module {self._module_name!r} may only use http(s) URLs")
        if not host:
            raise HostNotAllowed("URL has no host")
        if parsed.username or parsed.password:
            raise HostNotAllowed("URLs with embedded credentials are not allowed")
        rule = self._rules.get(host)
        if rule is None:
            raise HostNotAllowed(f"module {self._module_name!r} did not declare host {host!r}")
        if scheme not in rule.schemes:
            raise HostNotAllowed(f"scheme {scheme!r} is not allowed for {host!r}")
        try:
            port = parsed.port
        except ValueError as exc:
            raise HostNotAllowed("invalid port") from exc
        if not rule.allows_port(scheme, port):
            raise HostNotAllowed(f"port {port} is not allowed for {host!r}")
        if not rule.private and _is_private_address(host):
            raise HostNotAllowed("private or internal addresses are not allowed")
        if host in _METADATA_ADDRESSES or host in _METADATA_HOSTS:
            raise HostNotAllowed("cloud metadata endpoints are never allowed")
        return rule, url

    # ---- requests -----------------------------------------------------------

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None,
        timeout_seconds: float,
        json_body: Any = None,
    ) -> tuple[aiohttp.ClientResponse, ResolvedHostRule]:
        current = url
        request_headers = dict(headers or {})
        for _hop in range(MAX_REDIRECTS + 1):
            rule, current = self._check(current)
            session = self._runtime.session(private=rule.private)
            try:
                response = await session.request(
                    method,
                    current,
                    headers=request_headers,
                    json=json_body,
                    allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=timeout_seconds),
                )
            except (aiohttp.ClientError, TimeoutError, OSError) as exc:
                raise ModuleHttpError(
                    f"request to {rule.host} failed: {type(exc).__name__}"
                ) from exc
            if response.status in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                response.release()
                if not location:
                    raise ModuleHttpError("redirect without a Location header")
                redirected = urljoin(current, location)
                if _origin(redirected) != _origin(current):
                    request_headers = {
                        name: value
                        for name, value in request_headers.items()
                        if name.lower() not in _CROSS_ORIGIN_SENSITIVE_HEADERS
                    }
                current = redirected
                if response.status == 303 or (response.status in (301, 302) and method == "POST"):
                    method, json_body = "GET", None
                continue
            return response, rule
        raise ModuleHttpError(f"too many redirects from {urlsplit(url).hostname}")

    async def _read_capped(self, response: aiohttp.ClientResponse, max_bytes: int) -> bytes:
        declared = response.content_length
        if declared is not None and declared > max_bytes:
            response.release()
            raise ResponseTooLarge(f"response declares {declared} bytes; limit is {max_bytes}")
        body = bytearray()
        try:
            async for chunk in response.content.iter_chunked(_CHUNK):
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise ResponseTooLarge(f"response exceeded {max_bytes} bytes")
        finally:
            response.release()
        return bytes(body)

    async def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout_seconds: float = 20.0,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> HttpResponse:
        response, _ = await self._request(
            "GET", url, headers=headers, timeout_seconds=timeout_seconds
        )
        body = await self._read_capped(response, max_bytes)
        return HttpResponse(response.status, dict(response.headers), body)

    async def post_json(
        self,
        url: str,
        payload: Any,
        *,
        headers: Mapping[str, str] | None = None,
        timeout_seconds: float = 20.0,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> HttpResponse:
        try:
            json.dumps(payload)
        except (TypeError, ValueError) as exc:
            raise ModuleContractError("post_json payload is not JSON-serializable") from exc
        response, _ = await self._request(
            "POST", url, headers=headers, timeout_seconds=timeout_seconds, json_body=payload
        )
        body = await self._read_capped(response, max_bytes)
        return HttpResponse(response.status, dict(response.headers), body)

    async def download(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout_seconds: float = 30.0,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> AsyncIterator[bytes]:
        response, _ = await self._request(
            "GET", url, headers=headers, timeout_seconds=timeout_seconds
        )
        if response.status != 200:
            response.release()
            raise ModuleHttpError(f"download returned HTTP {response.status}")
        declared = response.content_length
        if declared is not None and declared > max_bytes:
            response.release()
            raise ResponseTooLarge(f"download declares {declared} bytes; limit is {max_bytes}")
        received = 0
        try:
            async for chunk in response.content.iter_chunked(_CHUNK):
                received += len(chunk)
                if received > max_bytes:
                    raise ResponseTooLarge(f"download exceeded {max_bytes} bytes")
                yield chunk
        finally:
            response.release()


__all__ = [
    "ModuleHttpError",
    "ModuleHttpImpl",
    "ModuleHttpRuntime",
    "ResolvedHostRule",
    "resolve_host_rules",
]
