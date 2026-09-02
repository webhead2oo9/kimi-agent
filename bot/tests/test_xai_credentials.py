from __future__ import annotations

import pytest

from xai.auth import XaiAuthError, XaiAuthRevokedError
from xai.credentials import XaiCredentialResolver


class FakeManager:
    def __init__(
        self,
        *,
        available: bool = True,
        revoked: bool = False,
        transient: bool = False,
    ) -> None:
        self.available = available
        self.revoked = revoked
        self.transient = transient
        self.get_calls = 0
        self.available_calls = 0
        self.refresh_calls: list[bool] = []

    def is_available(self) -> bool:
        self.available_calls += 1
        return self.available

    async def get_access_token(self) -> str:
        self.get_calls += 1
        if self.revoked:
            raise XaiAuthRevokedError("revoked")
        if self.transient:
            raise XaiAuthError("temporarily unavailable")
        return "oauth-token"

    async def refresh_tokens(self, *, force: bool = False) -> None:
        self.refresh_calls.append(force)
        if self.revoked:
            raise XaiAuthRevokedError("revoked")


@pytest.mark.asyncio
async def test_oauth_mode_never_falls_back_to_present_api_key() -> None:
    unavailable = FakeManager(available=False)
    resolver = XaiCredentialResolver(
        auth_mode="oauth",
        oauth_manager=unavailable,  # type: ignore[arg-type]
        api_key="paid-key",
    )

    with pytest.raises(XaiAuthError, match="no OAuth tokens"):
        await resolver.primary()
    assert resolver.api_key_fallback() is None


@pytest.mark.asyncio
async def test_api_key_mode_never_reads_oauth() -> None:
    manager = FakeManager()
    resolver = XaiCredentialResolver(
        auth_mode="api_key",
        oauth_manager=manager,  # type: ignore[arg-type]
        api_key="paid-key",
    )

    credential = await resolver.primary()

    assert credential.source == "api_key"
    assert credential.bearer == "paid-key"
    assert manager.get_calls == 0
    assert resolver.is_available() is True
    assert manager.available_calls == 0


@pytest.mark.asyncio
async def test_auto_is_oauth_first_and_uses_api_key_when_revoked() -> None:
    manager = FakeManager(revoked=True)
    resolver = XaiCredentialResolver(
        auth_mode="auto",
        oauth_manager=manager,  # type: ignore[arg-type]
        api_key="paid-key",
    )

    credential = await resolver.primary()

    assert manager.get_calls == 1
    assert credential.source == "api_key"


@pytest.mark.asyncio
async def test_auto_does_not_use_api_key_for_transient_oauth_failure() -> None:
    manager = FakeManager(transient=True)
    resolver = XaiCredentialResolver(
        auth_mode="auto",
        oauth_manager=manager,  # type: ignore[arg-type]
        api_key="paid-key",
    )

    with pytest.raises(XaiAuthError, match="temporarily unavailable"):
        await resolver.primary()


@pytest.mark.asyncio
async def test_auto_forced_refresh_falls_back_only_after_revocation() -> None:
    manager = FakeManager(revoked=True)
    resolver = XaiCredentialResolver(
        auth_mode="auto",
        oauth_manager=manager,  # type: ignore[arg-type]
        api_key="paid-key",
    )

    replacement = await resolver.after_unauthorized(
        type("Credential", (), {"source": "oauth"})()  # type: ignore[arg-type]
    )

    assert manager.refresh_calls == [True]
    assert replacement is not None and replacement.source == "api_key"
