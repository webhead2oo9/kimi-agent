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

import asyncio
import ipaddress
import json
import logging
import math
import socket
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult

from kimi_agent_module_api.contracts import (
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
_METADATA_IPS = frozenset(ipaddress.ip_address(address) for address in _METADATA_ADDRESSES)
_CROSS_ORIGIN_SENSITIVE_HEADERS = frozenset(
    {"authorization", "cookie", "proxy-authorization", "x-api-key", "x-goog-api-key"}
)


class ModuleHttpError(RuntimeError):
    """Transport-level failure; the message is safe to show staff."""


def _deadline(timeout_seconds: float) -> float:
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ModuleContractError("timeout_seconds must be finite and positive")
    return time.monotonic() + timeout_seconds


def _validated_max_bytes(max_bytes: int) -> int:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int):
        raise ModuleContractError("max_bytes must be an integer")
    if max_bytes <= 0 or max_bytes > DEFAULT_MAX_BYTES:
        raise ModuleContractError(f"max_bytes must be between 1 and {DEFAULT_MAX_BYTES}")
    return max_bytes


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ModuleHttpError("request timed out")
    return remaining


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
            value = settings[setting]
            values = value if isinstance(value, (list, tuple)) else (value,)
            for item in values:
                host, scheme, port = _host_from_setting(item)
                if _is_metadata_host(host):
                    raise ModuleContractError(f"module {module_name!r} may not target {host!r}")
                schemes = frozenset({scheme} if scheme else rule.schemes)
                ports = frozenset({port} if port else rule.ports)
                resolved.append(ResolvedHostRule(host, schemes, ports, rule.network == "private"))
            continue
        host, schemes, ports = rule.host.lower(), frozenset(rule.schemes), frozenset(rule.ports)
        if _is_metadata_host(host):
            raise ModuleContractError(f"module {module_name!r} may not target {host!r}")
        resolved.append(ResolvedHostRule(host, schemes, ports, rule.network == "private"))
    return tuple(resolved)


def _is_private_address(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return not ip.is_global


def _is_metadata_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_link_local or address in _METADATA_IPS


def _literal_ip_address(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        try:
            return ipaddress.ip_address(socket.inet_aton(host))
        except OSError:
            return None


def _is_metadata_host(host: str) -> bool:
    if host in _METADATA_HOSTS:
        return True
    address = _literal_ip_address(host)
    return address is not None and _is_metadata_address(address)


class MetadataSafeResolver(AbstractResolver):
    """Resolve private module hosts while denying cloud metadata aliases."""

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
                raise HostNotAllowed("DNS resolution returned an invalid IP address")
            address = ipaddress.ip_address(raw_address)
            if _is_metadata_address(address):
                raise HostNotAllowed("cloud metadata endpoints are never allowed")
            key = (raw_address, port)
            if key in seen:
                continue
            seen.add(key)
            results.append(
                {
                    "hostname": host,
                    "host": raw_address,
                    "port": port,
                    "family": resolved_family,
                    "proto": proto,
                    "flags": socket.AI_NUMERICHOST,
                }
            )
        return results

    async def close(self) -> None:
        return None


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
                    connector=aiohttp.TCPConnector(resolver=MetadataSafeResolver()),
                    headers={"User-Agent": self._user_agent},
                    trust_env=False,
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
        rules_by_host: dict[str, list[ResolvedHostRule]] = {}
        for rule in rules:
            rules_by_host.setdefault(rule.host, []).append(rule)
        self._rules = {host: tuple(host_rules) for host, host_rules in rules_by_host.items()}

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
        rules = self._rules.get(host)
        if rules is None:
            raise HostNotAllowed(f"module {self._module_name!r} did not declare host {host!r}")
        try:
            port = parsed.port
        except ValueError as exc:
            raise HostNotAllowed("invalid port") from exc
        matching = tuple(
            rule for rule in rules if scheme in rule.schemes and rule.allows_port(scheme, port)
        )
        if not matching:
            if not any(scheme in rule.schemes for rule in rules):
                raise HostNotAllowed(f"scheme {scheme!r} is not allowed for {host!r}")
            raise HostNotAllowed(f"port {port} is not allowed for {host!r}")
        privacy_policies = {rule.private for rule in matching}
        if len(privacy_policies) != 1:
            raise HostNotAllowed(f"ambiguous network policy for {host!r}")
        rule = matching[0]
        if not rule.private and _is_private_address(host):
            raise HostNotAllowed("private or internal addresses are not allowed")
        if _is_metadata_host(host):
            raise HostNotAllowed("cloud metadata endpoints are never allowed")
        return rule, url

    # ---- requests -----------------------------------------------------------

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None,
        deadline: float,
        json_body: Any = None,
    ) -> tuple[aiohttp.ClientResponse, ResolvedHostRule]:
        current = url
        request_headers = dict(headers or {})
        for _hop in range(MAX_REDIRECTS + 1):
            rule, current = self._check(current)
            session = self._runtime.session(private=rule.private)
            try:
                async with asyncio.timeout(_remaining(deadline)):
                    response = await session.request(
                        method,
                        current,
                        headers=request_headers,
                        json=json_body,
                        allow_redirects=False,
                        timeout=aiohttp.ClientTimeout(total=_remaining(deadline)),
                    )
            except TimeoutError as exc:
                raise ModuleHttpError("request timed out") from exc
            except (aiohttp.ClientError, OSError) as exc:
                raise ModuleHttpError(
                    f"request to {rule.host} failed: {type(exc).__name__}"
                ) from exc
            try:
                _remaining(deadline)
            except ModuleHttpError:
                response.release()
                raise
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

    async def _read_capped(
        self,
        response: aiohttp.ClientResponse,
        max_bytes: int,
        deadline: float,
    ) -> bytes:
        declared = response.content_length
        if declared is not None and declared > max_bytes:
            response.release()
            raise ResponseTooLarge(f"response declares {declared} bytes; limit is {max_bytes}")
        body = bytearray()
        try:
            chunks = response.content.iter_chunked(_CHUNK).__aiter__()
            while True:
                try:
                    async with asyncio.timeout(_remaining(deadline)):
                        chunk = await anext(chunks)
                except StopAsyncIteration:
                    break
                except TimeoutError as exc:
                    raise ModuleHttpError("request timed out") from exc
                _remaining(deadline)
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise ResponseTooLarge(f"response exceeded {max_bytes} bytes")
            _remaining(deadline)
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
        max_bytes = _validated_max_bytes(max_bytes)
        deadline = _deadline(timeout_seconds)
        response, _ = await self._request("GET", url, headers=headers, deadline=deadline)
        body = await self._read_capped(response, max_bytes, deadline)
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
        max_bytes = _validated_max_bytes(max_bytes)
        deadline = _deadline(timeout_seconds)
        response, _ = await self._request(
            "POST", url, headers=headers, deadline=deadline, json_body=payload
        )
        body = await self._read_capped(response, max_bytes, deadline)
        return HttpResponse(response.status, dict(response.headers), body)

    async def download(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout_seconds: float = 30.0,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> AsyncIterator[bytes]:
        max_bytes = _validated_max_bytes(max_bytes)
        deadline = _deadline(timeout_seconds)
        response, _ = await self._request("GET", url, headers=headers, deadline=deadline)
        if response.status != 200:
            response.release()
            raise ModuleHttpError(f"download returned HTTP {response.status}")
        body = await self._read_capped(response, max_bytes, deadline)
        for offset in range(0, len(body), _CHUNK):
            yield body[offset : offset + _CHUNK]


__all__ = [
    "ModuleHttpError",
    "ModuleHttpImpl",
    "ModuleHttpRuntime",
    "ResolvedHostRule",
    "resolve_host_rules",
]
