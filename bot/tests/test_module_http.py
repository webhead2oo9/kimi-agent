"""Module HTTP: declared hosts only, revalidated per hop, bounded bodies."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer

from kimi_agent_module_api.contracts import (
    HostNotAllowed,
    HttpHostRule,
    ModuleContractError,
    ResponseTooLarge,
)
from modules.http import (
    ModuleHttpError,
    ModuleHttpRuntime,
    ResolvedHostRule,
    resolve_host_rules,
)


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
                "X-Test": "kept",
            },
        )
        assert response.json() == {
            "authorization": None,
            "cookie": None,
            "proxy_authorization": None,
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
