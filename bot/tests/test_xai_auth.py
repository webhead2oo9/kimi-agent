from __future__ import annotations

import asyncio
import json
import os
import stat
import time
from pathlib import Path
from typing import Any

import pytest

from xai.auth import (
    XAI_OAUTH_CLIENT_ID,
    XAI_OAUTH_DISCOVERY_URL,
    XaiAuthError,
    XaiAuthRevokedError,
    XaiOAuthManager,
    discover_xai_oauth,
    token_record,
    write_xai_tokens,
)


class FakeResponse:
    def __init__(self, status: int, payload: dict[str, Any] | str) -> None:
        self.status = status
        self._text = payload if isinstance(payload, str) else json.dumps(payload)

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def text(self) -> str:
        return self._text


class FakeSession:
    def __init__(self, refresh_payload: dict[str, Any] | None = None) -> None:
        self.refresh_payload = refresh_payload or {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 3600,
        }
        self.get_calls: list[str] = []
        self.post_calls: list[dict[str, Any]] = []
        self.refresh_status = 200

    def get(self, url: str) -> FakeResponse:
        self.get_calls.append(url)
        return FakeResponse(
            200,
            {
                "token_endpoint": "https://auth.x.ai/oauth2/token",
                "device_authorization_endpoint": "https://auth.x.ai/oauth2/device/code",
            },
        )

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.post_calls.append({"url": url, **kwargs})
        return FakeResponse(self.refresh_status, self.refresh_payload)


def _expired_tokens() -> dict[str, Any]:
    return {
        "access_token": "old-access",
        "refresh_token": "old-refresh",
        "id_token": "",
        "account_id": "",
        "expires_at": 0,
    }


@pytest.mark.asyncio
async def test_discovery_requires_xai_https_endpoints() -> None:
    session = FakeSession()
    discovery = await discover_xai_oauth(http_session=session)

    assert session.get_calls == [XAI_OAUTH_DISCOVERY_URL]
    assert discovery.token_endpoint == "https://auth.x.ai/oauth2/token"

    class HostileSession(FakeSession):
        def get(self, url: str) -> FakeResponse:
            return FakeResponse(200, {"token_endpoint": "https://attacker.example/token"})

    with pytest.raises(XaiAuthError, match="invalid token_endpoint"):
        await discover_xai_oauth(http_session=HostileSession())


@pytest.mark.asyncio
async def test_refresh_rotates_and_atomically_persists_refresh_token(tmp_path: Path) -> None:
    token_file = tmp_path / "xai.json"
    write_xai_tokens(token_file, _expired_tokens())
    session = FakeSession()
    manager = XaiOAuthManager(token_file, http_session=session)

    assert await manager.get_access_token() == "new-access"

    saved = json.loads(token_file.read_text(encoding="utf-8"))
    assert saved["refresh_token"] == "new-refresh"
    assert saved["expires_at"] > int(time.time() * 1000)
    assert session.post_calls[0]["data"] == {
        "client_id": XAI_OAUTH_CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": "old-refresh",
        "scope": "openid profile email offline_access grok-cli:access api:access",
    }


@pytest.mark.asyncio
async def test_two_managers_double_check_rotated_token_under_file_lock(tmp_path: Path) -> None:
    token_file = tmp_path / "xai.json"
    write_xai_tokens(token_file, _expired_tokens())
    session = FakeSession()
    first = XaiOAuthManager(token_file, http_session=session)
    second = XaiOAuthManager(token_file, http_session=session)

    await asyncio.gather(first.refresh_tokens(force=True), second.refresh_tokens(force=True))

    assert len(session.post_calls) == 1
    assert await first.get_access_token() == "new-access"
    assert await second.get_access_token() == "new-access"


@pytest.mark.asyncio
async def test_terminal_refresh_rejection_requires_login(tmp_path: Path) -> None:
    token_file = tmp_path / "xai.json"
    write_xai_tokens(token_file, _expired_tokens())
    session = FakeSession({"error": "invalid_grant"})
    session.refresh_status = 400
    manager = XaiOAuthManager(token_file, http_session=session)

    with pytest.raises(XaiAuthRevokedError, match="run scripts/xai_auth.py again"):
        await manager.get_access_token()


@pytest.mark.parametrize("error_code", ["server_error", "temporarily_unavailable"])
@pytest.mark.asyncio
async def test_transient_oauth_error_is_not_classified_as_revoked(
    tmp_path: Path,
    error_code: str,
) -> None:
    token_file = tmp_path / "xai.json"
    write_xai_tokens(token_file, _expired_tokens())
    session = FakeSession({"error": error_code})
    session.refresh_status = 400
    manager = XaiOAuthManager(token_file, http_session=session)

    with pytest.raises(XaiAuthError, match="temporarily") as caught:
        await manager.get_access_token()

    assert not isinstance(caught.value, XaiAuthRevokedError)


def test_token_file_io_failure_is_not_treated_as_missing(tmp_path: Path) -> None:
    token_file = tmp_path / "directory-instead-of-file"
    token_file.mkdir()

    with pytest.raises(XaiAuthError, match="Could not read xAI token file"):
        XaiOAuthManager(token_file)


def test_token_record_requires_both_rotating_credentials() -> None:
    with pytest.raises(XaiAuthError, match="access_token"):
        token_record({"refresh_token": "refresh"})
    with pytest.raises(XaiAuthError, match="refresh_token"):
        token_record({"access_token": "access"})


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission contract")
def test_token_write_does_not_change_existing_parent_permissions(tmp_path: Path) -> None:
    parent = tmp_path / "shared"
    parent.mkdir(mode=0o750)
    parent.chmod(0o750)

    write_xai_tokens(parent / "xai.json", _expired_tokens())

    assert stat.S_IMODE(parent.stat().st_mode) == 0o750
    assert stat.S_IMODE((parent / "xai.json").stat().st_mode) == 0o600
