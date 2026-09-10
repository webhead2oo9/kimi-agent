from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from app.dashboard_auth import CHALLENGE_COOKIE, SESSION_COOKIE, DashboardAuth, secure_cookie


def auth():
    return DashboardAuth(
        application_id="42",
        bot_token="bot-secret",
        client_secret="oauth-secret",
        session_seconds=300,
        max_sessions=4,
        http=None,
    )


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
