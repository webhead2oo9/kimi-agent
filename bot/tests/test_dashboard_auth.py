from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from app.dashboard_auth import (
    AVATAR_MAX_BYTES,
    CHALLENGE_COOKIE,
    SESSION_COOKIE,
    DashboardAuth,
    DashboardSession,
    avatar_url,
    secure_cookie,
)

PNG = b"\x89PNG\r\n\x1a\n" + bytes(16)


def auth():
    return DashboardAuth(
        application_id="42",
        bot_token="bot-secret",
        client_secret="oauth-secret",
        session_seconds=300,
        max_sessions=4,
        http=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
async def test_discord_verification_preserves_sessions_during_transient_errors(status):
    service = auth()
    response = MagicMock()
    response.__aenter__ = AsyncMock(return_value=SimpleNamespace(status=status))
    response.__aexit__ = AsyncMock(return_value=False)
    service._http = SimpleNamespace(request=MagicMock(return_value=response))
    expected = (
        web.HTTPServiceUnavailable if status == 429 or status >= 500 else web.HTTPUnauthorized
    )
    with pytest.raises(expected):
        await service._json("GET", "https://discord.example.test/session")


def request(method="POST", *, cookie="", origin="https://42.discordsays.com", csrf=""):
    return make_mocked_request(
        method, "/api/auth", headers={"Origin": origin, "Cookie": cookie, "X-CSRF-Token": csrf}
    )


def challenge(service):
    response = web.Response()
    state = service.challenge(response)
    return state, f"{CHALLENGE_COOKIE}={response.cookies[CHALLENGE_COOKIE].value}"


def instance(**changes):
    return {
        "application_id": "42",
        "instance_id": "instance-1",
        "users": ["1"],
        "location": {"kind": "gc", "guild_id": "2", "channel_id": "3"},
        **changes,
    }


@pytest.mark.asyncio
async def test_oauth_identity_and_instance_are_server_authority():
    service = auth()
    service._json = AsyncMock(side_effect=[{"access_token": "oauth"}, {"id": "1"}, instance()])
    service.fetch_avatar = AsyncMock(return_value=None)
    state, cookie = challenge(service)
    session, token = await service.login(
        request(cookie=cookie), code="code", state=state, instance_id="instance-1"
    )
    assert (session.user_id, session.guild_id, session.channel_id, token) == (
        "1",
        "2",
        "3",
        "oauth",
    )
    assert service._json.call_args.kwargs["headers"] == {"Authorization": "Bot bot-secret"}
    assert service.session(request("GET", cookie=f"{SESSION_COOKIE}={session.token}")) is session
    with pytest.raises(web.HTTPForbidden):
        service.session(request(cookie=f"{SESSION_COOKIE}={session.token}"))
    assert (
        service.session(request(cookie=f"{SESSION_COOKIE}={session.token}", csrf=session.csrf))
        is session
    )
    with pytest.raises(web.HTTPUnauthorized):
        await service.login(
            request(cookie=cookie), code="code", state=state, instance_id="instance-1"
        )
    await service.delete_user("1")
    assert not service.valid(session)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"users": ["9"]},
        {"application_id": "7"},
        {"instance_id": "another"},
        {"location": {"kind": "pc", "guild_id": "2", "channel_id": "3"}},
        {"location": {"kind": "gc", "channel_id": "3"}},
    ],
)
async def test_instance_membership_and_server_context_fail_closed(changes):
    service = auth()
    service._json = AsyncMock(return_value=instance(**changes))
    with pytest.raises(web.HTTPForbidden):
        await service.verify_instance("instance-1", "1")


@pytest.mark.asyncio
async def test_privacy_deletion_revokes_login_in_flight():
    service = auth()
    started, release = asyncio.Event(), asyncio.Event()

    async def verify(*_args):
        started.set()
        await release.wait()
        return "2", "3"

    service._json = AsyncMock(side_effect=[{"access_token": "oauth"}, {"id": "1"}])
    service.verify_instance = verify
    service.fetch_avatar = AsyncMock(return_value=None)
    state, cookie = challenge(service)
    task = asyncio.create_task(
        service.login(request(cookie=cookie), code="code", state=state, instance_id="instance-1")
    )
    await started.wait()
    await service.delete_user("1")
    release.set()
    with pytest.raises(web.HTTPUnauthorized):
        await task
    assert not service._sessions


def test_cookie_flags_origin_and_challenge_limits():
    response = web.Response()
    secure_cookie(response, SESSION_COOKIE, "secret", 300)
    cookie = response.cookies[SESSION_COOKIE]
    assert all(cookie[key] for key in ["secure", "httponly", "partitioned"])
    assert cookie["samesite"] == "None" and cookie["path"] == "/" and not cookie["domain"]
    service = auth()
    with pytest.raises(web.HTTPForbidden):
        service.check_origin(request(origin="https://attacker.example"))
    for _ in range(4):
        challenge(service)
    with pytest.raises(web.HTTPTooManyRequests):
        challenge(service)


@pytest.mark.asyncio
@pytest.mark.parametrize("previous_user", ["1", "9"])
async def test_reopening_replaces_only_the_authenticated_users_session_at_capacity(previous_user):
    import time

    service = auth()
    service._maximum = 1
    previous = DashboardSession(
        "old-token", "old-csrf", previous_user, "2", "3", "old-instance", time.monotonic() + 300
    )
    service._sessions[previous.token] = previous
    service._json = AsyncMock(side_effect=[{"access_token": "oauth"}, {"id": "1"}, instance()])
    service.fetch_avatar = AsyncMock(return_value=None)
    state, cookie = challenge(service)
    login = service.login(
        request(cookie=f"{cookie}; {SESSION_COOKIE}={previous.token}"),
        code="code",
        state=state,
        instance_id="instance-1",
    )
    if previous_user != "1":
        with pytest.raises(web.HTTPTooManyRequests):
            await login
        assert service.valid(previous)
    else:
        session, _ = await login
        assert service.valid(session)
        assert not service.valid(previous)
        assert len(service._sessions) == 1


@pytest.mark.asyncio
async def test_login_inlines_the_avatar_discord_displays_for_the_user():
    service = auth()
    service._json = AsyncMock(
        side_effect=[{"access_token": "oauth"}, {"id": "1", "avatar": "abc_123"}, instance()]
    )
    service.fetch_avatar = AsyncMock(return_value="data:image/png;base64,AA==")
    state, cookie = challenge(service)
    session, _token = await service.login(
        request(cookie=cookie), code="code", state=state, instance_id="instance-1"
    )
    service.fetch_avatar.assert_awaited_once_with(
        "https://cdn.discordapp.com/avatars/1/abc_123.png?size=128"
    )
    assert session.avatar == "data:image/png;base64,AA=="


@pytest.mark.parametrize(
    ("identity", "expected"),
    [
        ({"avatar": "a_deadbeef"}, "avatars/4194304/a_deadbeef.png?size=128"),
        ({"avatar": None}, "embed/avatars/1.png"),
        ({"avatar": "../not-a-hash"}, "embed/avatars/1.png"),
        ({"avatar": None, "discriminator": "0007"}, "embed/avatars/2.png"),
    ],
)
def test_avatar_url_follows_discord_display_rules(identity, expected):
    assert avatar_url("4194304", identity) == f"https://cdn.discordapp.com/{expected}"


def cdn(status=200, content_type="image/png", body=PNG, error=None):
    read = AsyncMock(return_value=body[: AVATAR_MAX_BYTES + 1])
    if len(body) <= AVATAR_MAX_BYTES:
        read.side_effect = asyncio.IncompleteReadError(body, AVATAR_MAX_BYTES + 1)
    response = MagicMock()
    response.__aenter__ = AsyncMock(
        return_value=SimpleNamespace(
            status=status,
            content_type=content_type,
            content=SimpleNamespace(readexactly=read),
        )
    )
    response.__aexit__ = AsyncMock(return_value=False)
    return SimpleNamespace(get=MagicMock(return_value=response, side_effect=error))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("http", "expected"),
    [
        (cdn(), "data:image/png;base64," + __import__("base64").b64encode(PNG).decode()),
        (cdn(body=b"GIF89a" + bytes(16)), None),
        (cdn(content_type="text/html"), None),
        (cdn(status=404), None),
        (cdn(body=PNG + bytes(AVATAR_MAX_BYTES)), None),
        (cdn(error=aiohttp.ClientConnectionError()), None),
        (cdn(error=TimeoutError()), None),
    ],
)
async def test_fetch_avatar_only_inlines_a_verified_png(http, expected):
    service = auth()
    service._http = http
    assert await service.fetch_avatar("https://cdn.discordapp.com/avatars/1/x.png") == expected


@pytest.mark.asyncio
async def test_avatar_download_waits_for_all_network_chunks():
    import base64

    service = auth()
    # StreamReader.read(n) may return fewer than n bytes before EOF.
    reader = aiohttp.StreamReader(MagicMock(), limit=AVATAR_MAX_BYTES)
    reader.feed_data(PNG[:8])
    response = MagicMock()
    response.__aenter__ = AsyncMock(
        return_value=SimpleNamespace(status=200, content_type="image/png", content=reader)
    )
    response.__aexit__ = AsyncMock(return_value=False)
    service._http = SimpleNamespace(get=MagicMock(return_value=response))
    downloading = asyncio.create_task(service.fetch_avatar("https://cdn.discordapp.com/avatar.png"))
    await asyncio.sleep(0)
    reader.feed_data(PNG[8:])
    reader.feed_eof()
    assert await downloading == "data:image/png;base64," + base64.b64encode(PNG).decode()
