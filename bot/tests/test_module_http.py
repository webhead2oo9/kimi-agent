"""Module HTTP: declared hosts only, revalidated per hop, bounded bodies."""

from __future__ import annotations

import asyncio
import math
import socket
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, cast

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer

from bram_agent_module_api.contracts import (
    HostNotAllowed,
    HttpHostRule,
    ModuleContractError,
    ResponseTooLarge,
)
from modules.http import (
    MetadataSafeResolver,
    ModuleHttpError,
    ModuleHttpRuntime,
    ResolvedHostRule,
    resolve_host_rules,
)
from modules import http as module_http


def test_resolve_host_rules_expands_cdn_and_settings() -> None:
    rules = resolve_host_rules(
        "img",
        (
            HttpHostRule(host="discord-cdn"),
            HttpHostRule(host="${hub_base_url}", network="private"),
            HttpHostRule(host="api.example.org", ports=(8443,)),
        ),
        {"hub_base_url": "http://127.0.0.1:9000/api/"},
    )
    hosts = {rule.host: rule for rule in rules}
    assert set(hosts) == {
        "cdn.discordapp.com",
        "media.discordapp.net",
        "127.0.0.1",
        "api.example.org",
    }
    assert hosts["127.0.0.1"] == ResolvedHostRule(
        "127.0.0.1", frozenset({"http"}), frozenset({9000}), True
    )
    assert hosts["api.example.org"].ports == frozenset({8443})
    with pytest.raises(ModuleContractError):
        resolve_host_rules("img", (HttpHostRule(host="${missing}"),), {})
    with pytest.raises(ModuleContractError):
        resolve_host_rules("img", (HttpHostRule(host="169.254.169.254", network="private"),), {})


def test_resolve_host_rules_expands_sequence_setting() -> None:
    rules = resolve_host_rules(
        "bridge",
        (HttpHostRule(host="${backend_urls}", network="private"),),
        {
            "backend_urls": (
                "http://127.0.0.1:9000/api/",
                "https://commands.example.org/v1",
            )
        },
    )

    assert rules == (
        ResolvedHostRule("127.0.0.1", frozenset({"http"}), frozenset({9000}), True),
        ResolvedHostRule("commands.example.org", frozenset({"https"}), frozenset(), True),
    )
    assert (
        resolve_host_rules(
            "bridge",
            (HttpHostRule(host="${backend_urls}", network="private"),),
            {"backend_urls": ()},
        )
        == ()
    )


def test_module_http_keeps_each_declared_port_for_the_same_host() -> None:
    rules = resolve_host_rules(
        "bridge",
        (HttpHostRule(host="${backend_urls}", network="private"),),
        {
            "backend_urls": (
                "http://127.0.0.1:58749",
                "http://127.0.0.1:58750",
            )
        },
    )
    client = ModuleHttpRuntime().client_for("bridge", rules)

    first, _ = client._check("http://127.0.0.1:58749/health")
    second, _ = client._check("http://127.0.0.1:58750/health")

    assert first.ports == frozenset({58749})
    assert second.ports == frozenset({58750})


@pytest.mark.asyncio
@pytest.mark.parametrize("alias", ["2852039166", "0xA9FEA9FE"])
async def test_private_resolver_blocks_numeric_metadata_aliases(alias: str) -> None:
    with pytest.raises(HostNotAllowed, match="metadata"):
        await MetadataSafeResolver().resolve(alias, 80)


@pytest.mark.asyncio
@pytest.mark.parametrize("address", ["169.254.0.23", "fe80::1234"])
async def test_private_resolver_blocks_all_link_local_addresses(
    monkeypatch: pytest.MonkeyPatch, address: str
) -> None:
    loop = asyncio.get_running_loop()
    family = socket.AF_INET6 if ":" in address else socket.AF_INET

    async def link_local_result(*_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 80))]

    monkeypatch.setattr(loop, "getaddrinfo", link_local_result)
    with pytest.raises(HostNotAllowed, match="metadata"):
        await MetadataSafeResolver().resolve("owned.internal", 80)


@pytest.mark.asyncio
async def test_private_resolver_blocks_dns_alias_to_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()

    async def metadata_result(*_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("169.254.169.254", 80),
            )
        ]

    monkeypatch.setattr(loop, "getaddrinfo", metadata_result)
    with pytest.raises(HostNotAllowed, match="metadata"):
        await MetadataSafeResolver().resolve("owned.internal", 80)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "alias",
    [
        "::ffff:169.254.169.254",
        "::ffff:a9fe:a9fe",
        "fd00:0ec2:0000:0000:0000:0000:0000:0254",
        "fe80::1234",
    ],
)
async def test_private_http_blocks_metadata_ipv6_literals_before_connect(alias: str) -> None:
    with pytest.raises(ModuleContractError, match="may not target"):
        resolve_host_rules(
            "bridge",
            (HttpHostRule(host="${backend}", network="private"),),
            {"backend": f"http://[{alias}]:8080"},
        )

    runtime = ModuleHttpRuntime()
    client = runtime.client_for(
        "bridge",
        (ResolvedHostRule(alias, frozenset({"http"}), frozenset({8080}), True),),
    )
    try:
        with pytest.raises(HostNotAllowed, match="metadata"):
            await client.get(f"http://[{alias}]:8080/health")
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_private_http_blocks_ipv4_link_local_literal_before_connect() -> None:
    alias = "169.254.0.23"
    with pytest.raises(ModuleContractError, match="may not target"):
        resolve_host_rules(
            "bridge",
            (HttpHostRule(host="${backend}", network="private"),),
            {"backend": f"http://{alias}:8080"},
        )

    runtime = ModuleHttpRuntime()
    client = runtime.client_for(
        "bridge",
        (ResolvedHostRule(alias, frozenset({"http"}), frozenset({8080}), True),),
    )
    try:
        with pytest.raises(HostNotAllowed, match="metadata"):
            await client.get(f"http://{alias}:8080/health")
    finally:
        await runtime.close()


@pytest_asyncio.fixture
async def server() -> AsyncIterator[TestServer]:
    app = web.Application()

    async def ok(request: web.Request) -> web.Response:
        return web.json_response({"echo": request.headers.get("X-Test", ""), "path": request.path})

    async def big(request: web.Request) -> web.Response:
        return web.Response(body=b"x" * 5000)

    async def hop(request: web.Request) -> web.Response:
        raise web.HTTPFound(location="/ok")

    async def away(request: web.Request) -> web.Response:
        raise web.HTTPFound(location="https://evil.example.org/x")

    async def cross_origin(request: web.Request) -> web.Response:
        raise web.HTTPFound(location=f"http://localhost:{request.url.port}/headers")

    async def malformed_port(_request: web.Request) -> web.Response:
        return web.Response(
            status=302,
            headers={"Location": "http://localhost:not-a-port/headers"},
        )

    async def headers(request: web.Request) -> web.Response:
        return web.json_response(
            {
                "authorization": request.headers.get("Authorization"),
                "cookie": request.headers.get("Cookie"),
                "proxy_authorization": request.headers.get("Proxy-Authorization"),
                "x_api_key": request.headers.get("X-API-Key"),
                "x_google_api_key": request.headers.get("X-Goog-Api-Key"),
                "x_test": request.headers.get("X-Test"),
            }
        )

    async def posted(request: web.Request) -> web.Response:
        return web.json_response({"got": await request.json()})

    app.router.add_get("/ok", ok)
    app.router.add_get("/big", big)
    app.router.add_get("/hop", hop)
    app.router.add_get("/away", away)
    app.router.add_get("/cross-origin", cross_origin)
    app.router.add_get("/malformed-port", malformed_port)
    app.router.add_get("/headers", headers)
    app.router.add_post("/post", posted)
    test_server = TestServer(app)
    await test_server.start_server()
    try:
        yield test_server
    finally:
        await test_server.close()


def _port(server: TestServer) -> int:
    assert server.port is not None
    return int(server.port)


def _client(server: TestServer, runtime: ModuleHttpRuntime, *, private: bool = True):
    rule = ResolvedHostRule("127.0.0.1", frozenset({"http"}), frozenset({_port(server)}), private)
    return runtime.client_for("mod", (rule,))


@pytest.mark.asyncio
async def test_get_post_and_download_within_declared_host(server: TestServer) -> None:
    runtime = ModuleHttpRuntime()
    client = _client(server, runtime)
    try:
        base = f"http://127.0.0.1:{_port(server)}"
        response = await client.get(f"{base}/ok", headers={"X-Test": "1"})
        assert response.status == 200 and response.json() == {"echo": "1", "path": "/ok"}
        posted = await client.post_json(f"{base}/post", {"a": 1})
        assert posted.json() == {"got": {"a": 1}}
        chunks = [chunk async for chunk in client.download(f"{base}/big", max_bytes=10_000)]
        assert sum(len(c) for c in chunks) == 5000
        # Same-host redirects are followed and revalidated.
        hopped = await client.get(f"{base}/hop")
        assert hopped.json()["path"] == "/ok"
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_policy_refuses_undeclared_hosts_schemes_ports_and_redirects(
    server: TestServer,
) -> None:
    runtime = ModuleHttpRuntime()
    client = _client(server, runtime)
    try:
        base = f"http://127.0.0.1:{_port(server)}"
        with pytest.raises(HostNotAllowed):
            await client.get("http://localhost:1/x")
        with pytest.raises(HostNotAllowed):
            await client.get(f"https://127.0.0.1:{_port(server)}/ok")
        with pytest.raises(HostNotAllowed):
            await client.get(f"http://127.0.0.1:{_port(server) + 1}/ok")
        with pytest.raises(HostNotAllowed):
            await client.get(f"http://user:pw@127.0.0.1:{_port(server)}/ok")
        with pytest.raises(HostNotAllowed):
            await client.get(f"{base}/away")  # redirect to an undeclared host
        with pytest.raises(ResponseTooLarge):
            await client.get(f"{base}/big", max_bytes=100)
        with pytest.raises(ResponseTooLarge):
            async for _ in client.download(f"{base}/big", max_bytes=100):
                pass
        with pytest.raises(ModuleContractError):
            await client.post_json(f"{base}/post", {"bad": object()})
        # A public rule for a loopback address is refused before any connection.
        public = _client(server, runtime, private=False)
        with pytest.raises(HostNotAllowed):
            await public.get(f"{base}/ok")
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_cross_origin_redirect_strips_sensitive_headers(server: TestServer) -> None:
    runtime = ModuleHttpRuntime()
    port = _port(server)
    client = runtime.client_for(
        "mod",
        (
            ResolvedHostRule("127.0.0.1", frozenset({"http"}), frozenset({port}), True),
            ResolvedHostRule("localhost", frozenset({"http"}), frozenset({port}), True),
        ),
    )
    try:
        response = await client.get(
            f"http://127.0.0.1:{port}/cross-origin",
            headers={
                "Authorization": "Bearer secret",
                "Cookie": "session=secret",
                "Proxy-Authorization": "Basic secret",
                "X-API-Key": "secret-key",
                "X-Goog-Api-Key": "gemini-secret-key",
                "X-Test": "kept",
            },
        )
        assert response.json() == {
            "authorization": None,
            "cookie": None,
            "proxy_authorization": None,
            "x_api_key": None,
            "x_google_api_key": None,
            "x_test": "kept",
        }
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_redirect_with_malformed_port_is_a_policy_error(server: TestServer) -> None:
    runtime = ModuleHttpRuntime()
    client = _client(server, runtime)
    try:
        with pytest.raises(HostNotAllowed, match="invalid port"):
            await client.get(f"http://127.0.0.1:{_port(server)}/malformed-port")
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_transport_failures_are_wrapped() -> None:
    runtime = ModuleHttpRuntime()
    client = runtime.client_for(
        "mod", (ResolvedHostRule("127.0.0.1", frozenset({"http"}), frozenset({9}), True),)
    )
    try:
        with pytest.raises(ModuleHttpError):
            await client.get("http://127.0.0.1:9/nothing", timeout_seconds=1)
    finally:
        await runtime.close()


class _DeadlineClock:
    def __init__(self) -> None:
        self.now = 100.0

    def monotonic(self) -> float:
        return self.now


class _DeadlineContent:
    def __init__(self, clock: _DeadlineClock, delays: tuple[float, ...]) -> None:
        self._clock = clock
        self._delays = delays

    async def iter_chunked(self, _size: int) -> AsyncIterator[bytes]:
        for delay in self._delays:
            self._clock.now += delay
            yield b"chunk"


class _DeadlineResponse:
    def __init__(
        self,
        clock: _DeadlineClock,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        body_delays: tuple[float, ...] = (),
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self.content_length = None
        self.content = _DeadlineContent(clock, body_delays)
        self.released = False

    def release(self) -> None:
        self.released = True


class _DeadlineSession:
    def __init__(self, clock: _DeadlineClock, responses: list[_DeadlineResponse]) -> None:
        self.clock = clock
        self.responses = responses
        self.timeouts: list[float] = []

    async def request(self, *_args: Any, **kwargs: Any) -> _DeadlineResponse:
        timeout = kwargs["timeout"].total
        assert isinstance(timeout, float)
        self.timeouts.append(timeout)
        request_delay = 0.6
        self.clock.now += request_delay
        if timeout < request_delay:
            raise TimeoutError
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_redirect_hops_share_one_total_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _DeadlineClock()
    responses = [
        _DeadlineResponse(clock, status=302, headers={"Location": "/next"}),
        _DeadlineResponse(clock),
    ]
    session = _DeadlineSession(clock, responses)
    runtime = SimpleNamespace(session=lambda *, private: session)
    client = module_http.ModuleHttpImpl(
        cast(ModuleHttpRuntime, runtime),
        "mod",
        (ResolvedHostRule("example.org", frozenset({"https"}), frozenset(), False),),
    )
    monkeypatch.setattr(module_http.time, "monotonic", clock.monotonic)

    with pytest.raises(ModuleHttpError, match="timed out"):
        await client.get("https://example.org/start", timeout_seconds=1.0)

    assert session.timeouts == pytest.approx([1.0, 0.4])


@pytest.mark.asyncio
@pytest.mark.parametrize("download", [False, True])
async def test_streamed_bodies_share_the_request_deadline(
    monkeypatch: pytest.MonkeyPatch, download: bool
) -> None:
    clock = _DeadlineClock()
    response = _DeadlineResponse(clock, body_delays=(0.6, 0.6))
    session = _DeadlineSession(clock, [response])

    # Headers arrive immediately in this case; only the streamed body advances time.
    async def immediate_request(*_args: Any, **kwargs: Any) -> _DeadlineResponse:
        return session.responses.pop(0)

    session.request = immediate_request  # type: ignore[method-assign]
    runtime = SimpleNamespace(session=lambda *, private: session)
    client = module_http.ModuleHttpImpl(
        cast(ModuleHttpRuntime, runtime),
        "mod",
        (ResolvedHostRule("example.org", frozenset({"https"}), frozenset(), False),),
    )
    monkeypatch.setattr(module_http.time, "monotonic", clock.monotonic)

    with pytest.raises(ModuleHttpError, match="timed out"):
        if download:
            async for _chunk in client.download("https://example.org/body", timeout_seconds=1.0):
                pass
        else:
            await client.get("https://example.org/body", timeout_seconds=1.0)

    assert response.released is True


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout_seconds", [math.nan, math.inf, -math.inf])
async def test_nonfinite_deadlines_are_rejected(timeout_seconds: float) -> None:
    runtime = SimpleNamespace(
        session=lambda *, private: (_ for _ in ()).throw(AssertionError("request started"))
    )
    client = module_http.ModuleHttpImpl(
        cast(ModuleHttpRuntime, runtime),
        "mod",
        (ResolvedHostRule("example.org", frozenset({"https"}), frozenset(), False),),
    )

    with pytest.raises(ModuleContractError, match="finite and positive"):
        await client.get("https://example.org/data", timeout_seconds=timeout_seconds)


class _StalledHeaderSession:
    def __init__(self, *, redirect_first: bool) -> None:
        self.redirect_first = redirect_first
        self.calls = 0
        self.redirect_response: _DeadlineResponse | None = None

    async def request(self, *_args: Any, **_kwargs: Any) -> _DeadlineResponse:
        self.calls += 1
        if self.redirect_first and self.calls == 1:
            self.redirect_response = _DeadlineResponse(
                _DeadlineClock(),
                status=302,
                headers={"Location": "/stalled"},
            )
            return self.redirect_response
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


@pytest.mark.asyncio
@pytest.mark.parametrize("redirect_first", [False, True])
async def test_exact_deadline_bounds_stalled_header_acquisition_and_redirects(
    redirect_first: bool,
) -> None:
    session = _StalledHeaderSession(redirect_first=redirect_first)
    runtime = SimpleNamespace(session=lambda *, private: session)
    client = module_http.ModuleHttpImpl(
        cast(ModuleHttpRuntime, runtime),
        "mod",
        (ResolvedHostRule("example.org", frozenset({"https"}), frozenset(), False),),
    )

    with pytest.raises(ModuleHttpError, match="timed out"):
        await asyncio.wait_for(
            client.get("https://example.org/start", timeout_seconds=0.01),
            timeout=0.25,
        )

    assert session.calls == (2 if redirect_first else 1)
    if session.redirect_response is not None:
        assert session.redirect_response.released is True


def _buffering_client(
    response: _DeadlineResponse,
) -> tuple[module_http.ModuleHttpImpl, _DeadlineSession]:
    session = _DeadlineSession(_DeadlineClock(), [response])

    async def immediate_request(*_args: Any, **_kwargs: Any) -> _DeadlineResponse:
        return session.responses.pop(0)

    session.request = immediate_request  # type: ignore[method-assign]
    runtime = SimpleNamespace(session=lambda *, private: session)
    return (
        module_http.ModuleHttpImpl(
            cast(ModuleHttpRuntime, runtime),
            "mod",
            (ResolvedHostRule("example.org", frozenset({"https"}), frozenset(), False),),
        ),
        session,
    )


@pytest.mark.asyncio
async def test_download_releases_response_before_consumer_stops_early() -> None:
    response = _DeadlineResponse(_DeadlineClock(), body_delays=(0.0, 0.0))
    client, _session = _buffering_client(response)

    async for chunk in client.download("https://example.org/file"):
        assert chunk
        break

    assert response.released is True


@pytest.mark.asyncio
@pytest.mark.parametrize("method_name", ["get", "post_json", "download"])
async def test_http_rejects_caps_above_host_limit(method_name: str) -> None:
    response = _DeadlineResponse(_DeadlineClock(), body_delays=(0.0,))
    client, session = _buffering_client(response)
    max_bytes = module_http.DEFAULT_MAX_BYTES + 1

    with pytest.raises(ValueError, match="max_bytes"):
        if method_name == "get":
            await client.get("https://example.org/file", max_bytes=max_bytes)
        elif method_name == "post_json":
            await client.post_json("https://example.org/file", {}, max_bytes=max_bytes)
        else:
            async for _chunk in client.download("https://example.org/file", max_bytes=max_bytes):
                pass

    assert session.responses == [response]


@pytest.mark.asyncio
async def test_download_releases_response_when_consumer_is_cancelled_between_chunks() -> None:
    response = _DeadlineResponse(_DeadlineClock(), body_delays=(0.0, 0.0))
    client, _session = _buffering_client(response)
    first_chunk_seen = asyncio.Event()

    async def consume() -> None:
        async for _chunk in client.download("https://example.org/file"):
            first_chunk_seen.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(consume())
    await first_chunk_seen.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert response.released is True
