from __future__ import annotations

from typing import Any

import pytest

from providers.errors import ProviderBackendAccessError
from providers.types import ContentPart, ProviderRequest, ProviderResponse
from providers.xai import XaiProvider
from xai.credentials import XaiCredentialResolver


class FakeManager:
    def __init__(self) -> None:
        self.refresh_calls = 0

    def is_available(self) -> bool:
        return True

    async def get_access_token(self) -> str:
        return "oauth-token"

    async def refresh_tokens(self, *, force: bool = False) -> None:
        assert force is True
        self.refresh_calls += 1


class RequestFailure(RuntimeError):
    def __init__(self, status: int, body: dict[str, Any] | None = None) -> None:
        super().__init__(f"failed {status}")
        self.status_code = status
        self.body = body


def _request() -> ProviderRequest:
    return ProviderRequest(
        conversation_id=1,
        system_prompt="",
        messages=[],
        current_user_parts=[ContentPart.from_text("hello")],
        tools=[],
        max_tokens=100,
    )


def _provider(mode: str, manager: FakeManager, *, api_key: str = "paid-key") -> XaiProvider:
    return XaiProvider(
        credential_resolver=XaiCredentialResolver(
            auth_mode=mode,
            oauth_manager=manager,  # type: ignore[arg-type]
            api_key=api_key,
        ),
        model="grok-4.6",
    )


def _install_delegate(monkeypatch: pytest.MonkeyPatch, outcomes: list[Any]) -> list[str]:
    keys: list[str] = []

    class FakeDelegate:
        def __init__(self, *, api_key: str, **_kwargs: Any) -> None:
            keys.append(api_key)

        async def run_turn(self, _request: ProviderRequest) -> ProviderResponse:
            outcome = outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        async def close(self) -> None:
            return None

    monkeypatch.setattr("providers.xai.OpenAIResponsesProvider", FakeDelegate)
    return keys


@pytest.mark.asyncio
async def test_auto_refreshes_oauth_once_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = FakeManager()
    keys = _install_delegate(
        monkeypatch,
        [RequestFailure(401), ProviderResponse(content="ok")],
    )

    response = await _provider("auto", manager).run_turn(_request())

    assert response.content == "ok"
    assert manager.refresh_calls == 1
    assert keys == ["oauth-token", "oauth-token"]


@pytest.mark.asyncio
async def test_oauth_mode_does_not_use_api_key_after_repeated_401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = FakeManager()
    keys = _install_delegate(monkeypatch, [RequestFailure(401), RequestFailure(401)])

    with pytest.raises(ProviderBackendAccessError):
        await _provider("oauth", manager).run_turn(_request())

    assert keys == ["oauth-token", "oauth-token"]
    assert "paid-key" not in keys


@pytest.mark.asyncio
async def test_auto_uses_api_key_for_structured_entitlement_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = FakeManager()
    keys = _install_delegate(
        monkeypatch,
        [
            RequestFailure(403, {"error": {"code": "insufficient_scope"}}),
            ProviderResponse(content="api success"),
        ],
    )

    response = await _provider("auto", manager).run_turn(_request())

    assert response.content == "api success"
    assert keys == ["oauth-token", "paid-key"]


@pytest.mark.asyncio
async def test_auto_does_not_switch_credentials_for_transient_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = FakeManager()
    failure = RequestFailure(503)
    keys = _install_delegate(monkeypatch, [failure])

    with pytest.raises(RequestFailure) as caught:
        await _provider("auto", manager).run_turn(_request())

    assert caught.value is failure
    assert keys == ["oauth-token"]
