from __future__ import annotations

import json
from typing import Any

import pytest

from xai.credentials import XaiCredentialResolver
from xai.responses import XaiResponsesClient, XaiResponsesError


class FakeManager:
    def __init__(self) -> None:
        self.refreshes = 0

    def is_available(self) -> bool:
        return True

    async def get_access_token(self) -> str:
        return "oauth-token"

    async def refresh_tokens(self, *, force: bool = False) -> None:
        assert force is True
        self.refreshes += 1


class FakeResponse:
    def __init__(self, status: int, payload: dict[str, Any], headers: dict[str, str]) -> None:
        self.status = status
        self.payload = payload
        self.headers = headers

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def text(self) -> str:
        return json.dumps(self.payload)


class FakeSession:
    def __init__(self, outcomes: list[tuple[int, dict[str, Any]]]) -> None:
        self.outcomes = outcomes
        self.authorization: list[str] = []

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    def post(self, _url: str, **kwargs: Any) -> FakeResponse:
        self.authorization.append(kwargs["headers"]["Authorization"])
        status, payload = self.outcomes.pop(0)
        return FakeResponse(status, payload, {})


def _client(
    monkeypatch: pytest.MonkeyPatch,
    outcomes: list[tuple[int, dict[str, Any]]],
    *,
    mode: str = "auto",
) -> tuple[XaiResponsesClient, FakeSession, FakeManager]:
    session = FakeSession(outcomes)
    monkeypatch.setattr("xai.responses.aiohttp.ClientSession", lambda **_kwargs: session)

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("xai.responses.asyncio.sleep", no_sleep)
    manager = FakeManager()
    resolver = XaiCredentialResolver(
        auth_mode=mode,
        oauth_manager=manager,  # type: ignore[arg-type]
        api_key="paid-key",
    )
    return (
        XaiResponsesClient(
            resolver,
            timeout_seconds=10,
            max_retries=2,
            user_agent="Kimi",
        ),
        session,
        manager,
    )


@pytest.mark.asyncio
async def test_transient_retry_keeps_oauth_source(monkeypatch: pytest.MonkeyPatch) -> None:
    client, session, _manager = _client(
        monkeypatch,
        [(503, {"error": "down"}), (200, {"output_text": "ok"})],
    )
    consumed = 0

    def consume() -> None:
        nonlocal consumed
        consumed += 1

    result = await client.create({"store": False}, consume_call=consume)

    assert result.credential_source == "oauth"
    assert session.authorization == ["Bearer oauth-token", "Bearer oauth-token"]
    assert consumed == 2


@pytest.mark.asyncio
async def test_auto_falls_back_for_entitlement_but_oauth_mode_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, session, _manager = _client(
        monkeypatch,
        [
            (403, {"error": {"code": "insufficient_scope", "message": "not entitled"}}),
            (200, {"output_text": "ok"}),
        ],
    )

    result = await client.create({"store": False})

    assert result.credential_source == "api_key"
    assert session.authorization == ["Bearer oauth-token", "Bearer paid-key"]

    strict, strict_session, _manager = _client(
        monkeypatch,
        [(403, {"error": {"code": "insufficient_scope"}})],
        mode="oauth",
    )
    with pytest.raises(XaiResponsesError):
        await strict.create({"store": False})
    assert strict_session.authorization == ["Bearer oauth-token"]


@pytest.mark.asyncio
async def test_401_refreshes_once_before_succeeding(monkeypatch: pytest.MonkeyPatch) -> None:
    client, session, manager = _client(
        monkeypatch,
        [(401, {"error": "expired"}), (200, {"output_text": "ok"})],
    )

    result = await client.create({"store": False})

    assert result.credential_source == "oauth"
    assert manager.refreshes == 1
    assert session.authorization == ["Bearer oauth-token", "Bearer oauth-token"]
