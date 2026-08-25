import base64
import json
import stat
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from codex.auth import CodexAuthError, CodexAuthManager, CodexAuthRevokedError

_requires_posix_modes = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX file-mode bits (0o600) not enforceable on Windows"
)


class FakeResponse:
    def __init__(self, *, status: int, payload: dict[str, Any]) -> None:
        self.status = status
        self._payload = payload
        self._text = json.dumps(payload)

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        return None

    async def json(self) -> dict[str, Any]:
        return self._payload

    async def text(self) -> str:
        return self._text


class FakeSession:
    def __init__(self, payload: dict[str, Any], status: int = 200) -> None:
        self.payload = payload
        self.status = status
        self.calls: list[dict[str, Any]] = []

    def post(
        self,
        url: str,
        *,
        data: Any,
        headers: dict[str, str],
    ) -> FakeResponse:
        self.calls.append({"url": url, "data": data, "headers": headers})
        return FakeResponse(status=self.status, payload=self.payload)


def _id_token(account_id: str) -> str:
    payload = {
        "https://api.openai.com/auth": {
            "chatgpt_account_id": account_id,
        }
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


def _write_tokens(path: Path, **overrides: Any) -> None:
    data = {
        "access_token": "access-old",
        "refresh_token": "refresh-old",
        "id_token": _id_token("acct_old"),
        "account_id": "acct_old",
        "expires_at": int((time.time() + 3600) * 1000),
    }
    data.update(overrides)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_codex_auth_loads_millisecond_token_file(tmp_path: Path) -> None:
    token_file = tmp_path / "secrets" / "codex-auth.json"
    _write_tokens(token_file)
    manager = CodexAuthManager(token_file=token_file)

    assert manager.is_available()
    assert manager.get_account_id() == "acct_old"


@pytest.mark.asyncio
async def test_codex_auth_refreshes_expired_tokens_and_saves_ms_timestamp(
    tmp_path: Path,
) -> None:
    token_file = tmp_path / "codex-auth.json"
    _write_tokens(token_file, expires_at=0)
    session = FakeSession(
        {
            "access_token": "access-new",
            "refresh_token": "refresh-new",
            "id_token": _id_token("acct_new"),
            "expires_in": 7200,
        }
    )
    manager = CodexAuthManager(token_file=token_file, http_session=session)

    token = await manager.get_access_token()

    assert token == "access-new"
    assert manager.get_account_id() == "acct_new"
    assert len(session.calls) == 1
    call = session.calls[0]
    assert call["url"] == "https://auth.openai.com/oauth/token"
    assert call["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    assert call["data"]["grant_type"] == "refresh_token"
    assert call["data"]["refresh_token"] == "refresh-old"
    saved = json.loads(token_file.read_text(encoding="utf-8"))
    assert saved["refresh_token"] == "refresh-new"
    assert saved["expires_at"] > int(time.time() * 1000)


@pytest.mark.asyncio
async def test_codex_auth_concurrent_refresh_uses_one_request(tmp_path: Path) -> None:
    token_file = tmp_path / "codex-auth.json"
    _write_tokens(token_file, expires_at=0)
    session = FakeSession(
        {
            "access_token": "access-new",
            "id_token": _id_token("acct_new"),
            "expires_in": 3600,
        }
    )
    manager = CodexAuthManager(token_file=token_file, http_session=session)

    tokens = await pytest.importorskip("asyncio").gather(
        manager.get_access_token(),
        manager.get_access_token(),
    )

    assert tokens == ["access-new", "access-new"]
    assert len(session.calls) == 1


@pytest.mark.asyncio
@_requires_posix_modes
async def test_codex_auth_token_file_is_owner_only_without_relying_on_post_chmod(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_file = tmp_path / "codex-auth.json"
    _write_tokens(token_file, expires_at=0)
    token_file.chmod(0o644)  # pre-existing world-readable file
    session = FakeSession(
        {
            "access_token": "access-new",
            "refresh_token": "refresh-new",
            "id_token": _id_token("acct_new"),
            "expires_in": 3600,
        }
    )
    manager = CodexAuthManager(token_file=token_file, http_session=session)
    # Simulate a platform where a post-write chmod does nothing/fails; the secret
    # must still never be left world-readable.
    monkeypatch.setattr(Path, "chmod", lambda self, mode: None)

    await manager.get_access_token()

    mode = stat.S_IMODE(token_file.stat().st_mode)
    assert mode == 0o600


@pytest.mark.asyncio
async def test_codex_auth_zero_expires_in_is_treated_as_immediate_expiry(
    tmp_path: Path,
) -> None:
    token_file = tmp_path / "codex-auth.json"
    _write_tokens(token_file, expires_at=0)
    session = FakeSession(
        {
            "access_token": "access-new",
            "refresh_token": "refresh-new",
            "id_token": _id_token("acct_new"),
            "expires_in": 0,
        }
    )
    manager = CodexAuthManager(token_file=token_file, http_session=session)

    await manager.get_access_token()

    saved = json.loads(token_file.read_text(encoding="utf-8"))
    # expires_in=0 must not be silently converted into a one-hour lifetime.
    assert saved["expires_at"] <= int(time.time() * 1000) + 1000


@pytest.mark.asyncio
async def test_codex_auth_refresh_raises_revoked_error_for_unrecoverable_token(
    tmp_path: Path,
) -> None:
    token_file = tmp_path / "codex-auth.json"
    _write_tokens(token_file, expires_at=0)
    session = FakeSession({"error": {"code": "refresh_token_expired"}}, status=400)
    manager = CodexAuthManager(token_file=token_file, http_session=session)

    with pytest.raises(CodexAuthRevokedError):
        await manager.get_access_token()


@pytest.mark.asyncio
async def test_codex_auth_default_session_uses_explicit_timeout_and_no_env_proxies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_file = tmp_path / "codex-auth.json"
    _write_tokens(token_file, expires_at=0)
    inner = FakeSession(
        {
            "access_token": "access-new",
            "refresh_token": "refresh-new",
            "id_token": _id_token("acct_new"),
            "expires_in": 3600,
        }
    )
    captured: dict[str, Any] = {}

    class _CapturingClientSession:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        async def __aenter__(self) -> FakeSession:
            return inner

        async def __aexit__(self, *_exc: object) -> None:
            return None

    monkeypatch.setattr("codex.auth.aiohttp.ClientSession", _CapturingClientSession)
    manager = CodexAuthManager(token_file=token_file)

    token = await manager.get_access_token()

    assert token == "access-new"
    assert captured["trust_env"] is False
    assert captured["timeout"].total == 30


@pytest.mark.asyncio
async def test_codex_auth_missing_access_token_forces_refresh(tmp_path: Path) -> None:
    token_file = tmp_path / "codex-auth.json"
    _write_tokens(token_file, access_token="")
    session = FakeSession(
        {
            "access_token": "access-new",
            "refresh_token": "refresh-new",
            "id_token": _id_token("acct_new"),
            "expires_in": 3600,
        }
    )
    manager = CodexAuthManager(token_file=token_file, http_session=session)

    token = await manager.get_access_token()

    assert token == "access-new"
    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_codex_auth_reloads_newer_same_account_token_before_refresh(
    tmp_path: Path,
) -> None:
    token_file = tmp_path / "codex-auth.json"
    _write_tokens(token_file, expires_at=0)
    session = FakeSession({"access_token": "authority-token"})
    manager = CodexAuthManager(token_file=token_file, http_session=session)
    _write_tokens(
        token_file,
        access_token="access-from-disk",
        refresh_token="refresh-from-disk",
    )

    token = await manager.get_access_token()

    assert token == "access-from-disk"
    assert session.calls == []


@pytest.mark.asyncio
async def test_codex_auth_force_refreshes_unchanged_valid_token(tmp_path: Path) -> None:
    token_file = tmp_path / "codex-auth.json"
    _write_tokens(token_file)
    session = FakeSession(
        {
            "access_token": "access-new",
            "refresh_token": "refresh-new",
            "id_token": _id_token("acct_old"),
            "expires_in": 3600,
        }
    )
    manager = CodexAuthManager(token_file=token_file, http_session=session)

    await manager.refresh_tokens(force=True)

    assert len(session.calls) == 1
    assert session.calls[0]["data"]["refresh_token"] == "refresh-old"
    assert await manager.get_access_token() == "access-new"


@pytest.mark.asyncio
async def test_codex_auth_refuses_cross_account_disk_reload(tmp_path: Path) -> None:
    token_file = tmp_path / "codex-auth.json"
    _write_tokens(token_file, expires_at=0)
    session = FakeSession({"access_token": "must-not-be-used"})
    manager = CodexAuthManager(token_file=token_file, http_session=session)
    _write_tokens(
        token_file,
        access_token="other-account-access",
        refresh_token="other-account-refresh",
        id_token=_id_token("acct_other"),
        account_id="acct_other",
    )

    with pytest.raises(CodexAuthError, match="different account"):
        await manager.get_access_token()

    assert session.calls == []
    saved = json.loads(token_file.read_text(encoding="utf-8"))
    assert saved["account_id"] == "acct_other"
