from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiohttp import WSServerHandshakeError, WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

from app.dashboard import Dashboard
from app.dashboard_auth import SESSION_COOKIE, DashboardAuth, DashboardSession
from app.dashboard_files import DashboardFiles
from storage.dashboard import DashboardStore
from storage.db import Database
from tests.test_dashboard_store import saved_turn
from tests.helpers import make_settings
from utils.privacy_barrier import UserPrivacyBarrier
from tools.workspace.common import UserLocks
from workspace import WorkspaceManager


@pytest_asyncio.fixture
async def api(tmp_path):
    db = Database(tmp_path / "api.db")
    await db.connect()
    store = DashboardStore(db)
    access = SimpleNamespace(
        resolve=AsyncMock(
            return_value=SimpleNamespace(parent_id="3", channel=SimpleNamespace(name="general"))
        ),
        consent_required=AsyncMock(return_value=False),
    )
    turns = SimpleNamespace(
        submit=AsyncMock(return_value="turn"), delete=AsyncMock(), stop=AsyncMock(return_value=True)
    )
    service = Dashboard(
        bot=None,
        settings=make_settings(),
        store=store,
        access=access,
        files=DashboardFiles(
            store=store,
            workspace=WorkspaceManager(tmp_path / "workspace"),
            locks=UserLocks(),
            settings=make_settings(),
        ),
        turns=turns,
        tasks=None,
        privacy=UserPrivacyBarrier(),
        ready=lambda: True,
    )
    service.auth = DashboardAuth(
        application_id="42",
        bot_token="",
        client_secret="",
        session_seconds=300,
        max_sessions=10,
        http=None,
    )
    session = DashboardSession("token", "csrf", "1", "2", "3", "instance", time.monotonic() + 300)
    service.auth._sessions[session.token] = session
    service.auth.verify_instance = AsyncMock(return_value=("2", "3"))
    async with TestClient(TestServer(service.application(static=False))) as client:
        client.session.headers.update(
            {
                "Cookie": f"{SESSION_COOKIE}=token",
                "Origin": service.auth.origin,
                "X-CSRF-Token": "csrf",
            }
        )
        yield client, service
    await db.close()


async def create(service, user="1", guild="2", channel="3"):
    return await service.store.create(
        user_id=user,
        guild_id=guild,
        channel_id=channel,
        parent_channel_id=channel,
        channel_name="general",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/bootstrap", "/api/auth"])
async def test_unauthenticated_requests_are_throttled_before_work_despite_spoofed_headers(
    api, monkeypatch, path
):
    client, service = api
    service.auth._maximum = 3000
    service.auth.login = AsyncMock(side_effect=web.HTTPUnauthorized(reason="bad login"))
    now = time.monotonic()
    monkeypatch.setattr("app.dashboard.time.monotonic", lambda: now)
    responses = []
    for index in range(61):
        headers = {
            "Cookie": "",
            "X-Forwarded-For": f"10.0.0.{index}",
            "CF-Connecting-IP": f"10.1.0.{index}",
        }
        response = await (
            client.get(path, headers=headers)
            if path.endswith("bootstrap")
            else client.post(path, headers=headers, json={})
        )
        responses.append(response.status)
    assert 429 in responses
    assert responses[-1] == 429
    assert len(service.auth._challenges) < 61
    assert service.auth.login.await_count < 61
    now += 61
    response = await (
        client.get(path, headers={"Cookie": ""})
        if path.endswith("bootstrap")
        else client.post(path, headers={"Cookie": ""}, json={})
    )
    assert response.status != 429


@pytest.mark.asyncio
async def test_http_owner_server_and_saved_channel_boundaries(api):
    client, service = api
    own = await create(service)
    other = await create(service, user="9")
    elsewhere = await create(service, guild="8")
    assert (await client.get(f"/api/chats/{own.id}/events")).status == 200
    for chat in (other, elsewhere):
        assert (await client.get(f"/api/chats/{chat.id}/events")).status == 404
        assert (
            await client.post(
                f"/api/chats/{chat.id}/messages", json={"text": "hello", "request_id": "r"}
            )
        ).status == 404
    assert not service.turns.submit.called

    async def resolve(**kwargs):
        if kwargs["channel_id"] == "4":
            raise web.HTTPForbidden(reason="Revoked")

    revoked = await create(service, channel="4")
    service.access.resolve.side_effect = resolve
    assert (await client.get(f"/api/chats/{revoked.id}/events")).status == 403
    # An owner can still delete a saved chat after losing its originating channel.
    assert (await client.delete(f"/api/chats/{revoked.id}")).status == 200
    service.turns.delete.assert_awaited_once_with(revoked)


@pytest.mark.asyncio
async def test_http_requires_session_csrf_origin_and_object_body(api):
    client, _ = api
    assert (
        await client.post("/api/chats", json={}, headers={"Origin": "https://evil.example"})
    ).status == 403
    assert (
        await client.post("/api/chats", json={}, headers={"X-CSRF-Token": "wrong"})
    ).status == 403
    assert (await client.get("/api/chats", headers={"Cookie": ""})).status == 401
    response = await client.get("/api/chats")
    assert response.headers["Cache-Control"] == "no-store"
    assert "object-src 'none'" in response.headers["Content-Security-Policy"]
    own = (await response.json())["chats"]
    assert own == []
    chat = await create(api[1])
    assert (await client.post(f"/api/chats/{chat.id}/messages", json=[])).status == 400
    assert (await client.get("/api/ws")).status == 400


@pytest.mark.asyncio
async def test_consent_decline_leaves_chatting_gated(api):
    client, service = api
    service.access.preferences = SimpleNamespace(set_consent=AsyncMock())
    response = await client.post("/api/consent", json={"accept": False})
    assert response.status == 200
    service.access.preferences.set_consent.assert_awaited_once_with("1", False)
    for invalid in ({}, {"accept": "false"}, {"accept": 0}):
        assert (await client.post("/api/consent", json=invalid)).status == 400


@pytest.mark.asyncio
async def test_branch_api_keeps_ownership_access_and_idempotent_return(api):
    client, service = api
    parent = await create(service)
    selected = await saved_turn(service.store, parent)
    path = f"/api/chats/{parent.id}/branches"
    body = {"event_id": selected.id, "request_id": "fork"}
    response = await client.post(path, json=body)
    assert response.status == 201
    branch = await response.json()
    assert branch["parent_id"] == parent.id
    assert (await (await client.post(path, json=body)).json())["id"] == branch["id"]
    branch_record = await service.store.get(branch["id"], user_id="1", guild_id="2")
    answer = await saved_turn(service.store, branch_record, "Alternative", "The branch result")
    returned = await client.post(
        f"/api/chats/{branch['id']}/return-result", json={"event_id": answer.id}
    )
    assert returned.status == 200 and (await returned.json())["id"] == parent.id
    service.turns.submit.assert_not_awaited()
    assert (await client.post(path, json=body, headers={"X-CSRF-Token": "wrong"})).status == 403
    for event_id in (True, 0, "1", 2**63):
        assert (await client.post(path, json={**body, "event_id": event_id})).status == 400
    foreign = await create(service, user="9")
    assert (await client.post(f"/api/chats/{foreign.id}/branches", json=body)).status == 404
    service.access.consent_required.return_value = True
    assert (await client.post(path, json=body)).status == 403
    service.access.consent_required.return_value = False
    service.access.resolve.side_effect = web.HTTPForbidden(reason="Revoked")
    assert (await client.post(path, json=body)).status == 403
    assert (
        await client.post(f"/api/chats/{branch['id']}/return-result", json={"event_id": answer.id})
    ).status == 403


@pytest.mark.asyncio
async def test_dashboard_access_denial_covers_existing_sessions_and_websockets(api):
    client, service = api
    chat = await create(service)
    await service.store.event(chat.id, "turn_finished", {"text": "Private result"})
    service.access.resolve.side_effect = web.HTTPForbidden(
        reason="Dashboard access is currently limited to invited users"
    )
    for path in ("/api/session", "/api/chats", f"/api/chats/{chat.id}/events"):
        response = await client.get(path)
        assert response.status == 403
        assert "limited to invited users" in (await response.json())["error"]
    response = await client.post(
        f"/api/chats/{chat.id}/messages", json={"text": "hello", "request_id": "r"}
    )
    assert response.status == 403
    service.turns.submit.assert_not_awaited()
    async with client.ws_connect(f"/api/ws?chat={chat.id}") as ws:
        message = await ws.receive(timeout=3)
        assert message.type == WSMsgType.CLOSE
        assert message.data == 1008


@pytest.mark.asyncio
async def test_temporary_verification_failure_closes_socket_without_private_events(api):
    client, service = api
    chat = await create(service)
    await service.store.event(chat.id, "turn_finished", {"text": "Private result"})
    service.access.resolve.side_effect = web.HTTPServiceUnavailable(reason="Discord unavailable")
    assert (await client.get(f"/api/chats/{chat.id}/events")).status == 503
    async with client.ws_connect(f"/api/ws?chat={chat.id}") as ws:
        message = await ws.receive(timeout=3)
        assert message.type == WSMsgType.CLOSE and message.data == 1013
    assert service.auth.valid(service.auth._sessions["token"])
    service.access.resolve.side_effect = None
    async with client.ws_connect(f"/api/ws?chat={chat.id}") as ws:
        message = await ws.receive_json(timeout=3)
        assert message["ready"] is True
        assert message["events"][0]["payload"]["text"] == "Private result"


@pytest.mark.asyncio
async def test_idle_socket_reports_ready_only_after_instance_verification(api):
    client, service = api
    chat = await create(service)
    service.auth.verify_instance.side_effect = web.HTTPServiceUnavailable(
        reason="Discord unavailable"
    )
    async with client.ws_connect(f"/api/ws?chat={chat.id}") as ws:
        message = await ws.receive(timeout=3)
        assert message.type == WSMsgType.CLOSE and message.data == 1013
    service.auth.verify_instance.side_effect = None
    async with client.ws_connect(f"/api/ws?chat={chat.id}") as ws:
        assert await ws.receive_json(timeout=3) == {"events": [], "ready": True}


@pytest.mark.asyncio
async def test_dashboard_access_denial_revokes_new_login_before_returning_credentials(api):
    client, service = api
    session = service.auth._sessions["token"]
    service.auth.login = AsyncMock(return_value=(session, "private-oauth-token"))
    service.access.resolve.side_effect = web.HTTPForbidden(
        reason="Dashboard access is currently limited to invited users"
    )
    response = await client.post(
        "/api/auth", json={"code": "code", "state": "state", "instance_id": "instance"}
    )
    assert response.status == 403
    assert SESSION_COOKIE not in response.cookies
    assert "access_token" not in await response.json()
    assert not service.auth._sessions


@pytest.mark.asyncio
async def test_socket_replays_durable_events_and_revocation_ends_session(api):
    client, service = api
    chat = await create(service)
    await service.store.event(chat.id, "activity", {"label": "old"})
    first = (await service.store.events(chat.id))[0]
    await service.store.event(chat.id, "turn_finished", {"text": "Done", "status": "completed"})
    async with client.ws_connect(f"/api/ws?chat={chat.id}&after={first.id}") as ws:
        replay = await ws.receive_json(timeout=2)
        assert [e["payload"]["text"] for e in replay["events"]] == ["Done"]
        await service.auth.delete_user("1")
        await ws.receive(timeout=3)
        assert ws.close_code == 1008


@pytest.mark.asyncio
async def test_socket_limit_counts_concurrent_handshakes(api):
    client, service = api
    chat = await create(service)
    original = service._chat
    started, release = asyncio.Event(), asyncio.Event()
    lookups = 0

    async def slow_chat(*args):
        nonlocal lookups
        lookups += 1
        if lookups == 3:
            started.set()
        await release.wait()
        return await original(*args)

    service._chat = slow_chat
    sockets = []

    async def connect():
        socket = await client.ws_connect(f"/api/ws?chat={chat.id}")
        sockets.append(socket)

    try:
        async with asyncio.timeout(3), asyncio.TaskGroup() as group:
            for _ in range(3):
                group.create_task(connect())
            await started.wait()
            try:
                async with client.ws_connect(f"/api/ws?chat={chat.id}") as denied:
                    assert (await denied.receive_json(timeout=3))["code"] == "socket_limit"
                    closed = await denied.receive(timeout=3)
                    assert closed.type == WSMsgType.CLOSE and closed.data == 4008
            finally:
                release.set()
        assert lookups == 3
    finally:
        release.set()
        for socket in sockets:
            await socket.close()


@pytest.mark.asyncio
async def test_denied_socket_handshake_releases_its_slot(api):
    client, service = api
    with pytest.raises(WSServerHandshakeError) as denied:
        await client.ws_connect("/api/ws?chat=missing")
    assert denied.value.status == 404
    assert not service._sockets


@pytest.mark.asyncio
async def test_fourth_socket_receives_an_actionable_tab_limit(api):
    client, service = api
    chat = await create(service)
    sockets = [await client.ws_connect(f"/api/ws?chat={chat.id}") for _ in range(3)]
    try:
        async with client.ws_connect(f"/api/ws?chat={chat.id}") as fourth:
            message = await fourth.receive_json(timeout=3)
            assert message == {
                "code": "socket_limit",
                "error": "Close another dashboard tab before reconnecting",
            }
            closed = await fourth.receive(timeout=3)
            assert closed.type == WSMsgType.CLOSE and closed.data == 4008
        assert len(service._sockets) == 3
    finally:
        for socket in sockets:
            await socket.close()


@pytest.mark.asyncio
async def test_privacy_deletion_revokes_a_pending_socket_handshake(api):
    client, service = api
    chat = await create(service)
    original = service._chat
    started, release = asyncio.Event(), asyncio.Event()
    service.turns.delete_user = AsyncMock()

    async def slow_chat(*args):
        started.set()
        await release.wait()
        return await original(*args)

    service._chat = slow_chat

    async def connect():
        async with client.ws_connect(f"/api/ws?chat={chat.id}") as socket:
            await socket.receive(timeout=3)
            assert socket.close_code == 1008

    async with asyncio.timeout(3), asyncio.TaskGroup() as group:
        group.create_task(connect())
        await started.wait()
        try:
            await service.delete_user("1", "all")
        finally:
            release.set()


@pytest.mark.asyncio
async def test_chat_deleted_while_upload_body_arrives_cannot_recreate_files(api):
    import asyncio

    from aiohttp.test_utils import make_mocked_request

    from app.dashboard import _SESSION
    from app.root_locks import RootLockPool

    _, service = api
    chat = await create(service)
    service.turns.roots = RootLockPool()
    service.files.save = AsyncMock()
    reading, release = asyncio.Event(), asyncio.Event()

    async def body():
        reading.set()
        await release.wait()
        return b"private upload"

    request = make_mocked_request(
        "POST", f"/api/chats/{chat.id}/upload", match_info={"chat": chat.id}
    )
    request[_SESSION] = service.auth._sessions["token"]
    request.read = body
    upload = asyncio.create_task(service.upload(request))
    await reading.wait()
    async with service.turns.roots.hold(chat.key):
        await service.store.delete(chat)
    release.set()
    with pytest.raises(web.HTTPNotFound):
        await upload
    service.files.save.assert_not_called()


@pytest.mark.asyncio
async def test_bootstrap_carries_bot_identity_and_caches_the_avatar(api):
    client, service = api
    launch = await (await client.get("/api/bootstrap")).json()
    assert (launch["bot_name"], launch["bot_avatar"]) == (service.settings.bot_name, None)
    reads = 0

    class Asset:
        def replace(self, **_changes):
            return self

        async def read(self):
            nonlocal reads
            reads += 1
            return b"\x89PNG\r\n\x1a\n" + bytes(16)

    service.bot = SimpleNamespace(user=SimpleNamespace(display_avatar=Asset()))
    service._bot_avatar = (None, 0.0)
    for _ in range(2):
        launch = await (await client.get("/api/bootstrap")).json()
        assert launch["bot_avatar"].startswith("data:image/png;base64,")
    assert reads == 1
    assert (await (await client.get("/api/session")).json())["user_avatar"] is None
