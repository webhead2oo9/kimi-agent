import json
import stat
import sys
import time
from pathlib import Path

import pytest

from scripts import codex_auth

_requires_posix_modes = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX file-mode bits (0o600) not enforceable on Windows"
)


def test_codex_auth_poll_fails_fast_for_non_pending_http_status(monkeypatch) -> None:
    def fail_post_json(url: str, payload: dict) -> dict:
        raise codex_auth.HttpJsonError(500, "server unavailable")

    monkeypatch.setattr(codex_auth, "_post_json", fail_post_json)

    with pytest.raises(RuntimeError, match=r"device auth failed \(500\)"):
        codex_auth._poll_for_token(
            device_auth_id="device",
            user_code="code",
            interval_seconds=1,
        )


def test_codex_auth_poll_returns_authorization_code(monkeypatch) -> None:
    def post_json(url: str, payload: dict) -> dict:
        return {"authorization_code": "auth", "code_verifier": "verifier"}

    monkeypatch.setattr(codex_auth, "_post_json", post_json)

    assert codex_auth._poll_for_token(
        device_auth_id="device",
        user_code="code",
        interval_seconds=1,
    ) == {"authorization_code": "auth", "code_verifier": "verifier"}


def test_codex_auth_poll_keeps_polling_on_authorization_pending(monkeypatch) -> None:
    calls = {"n": 0}

    def post_json(url: str, payload: dict) -> dict:
        calls["n"] += 1
        if calls["n"] == 1:
            raise codex_auth.HttpJsonError(400, json.dumps({"error": "authorization_pending"}))
        return {"authorization_code": "auth", "code_verifier": "verifier"}

    monkeypatch.setattr(codex_auth, "_post_json", post_json)
    monkeypatch.setattr(codex_auth.time, "sleep", lambda _seconds: None)

    result = codex_auth._poll_for_token(
        device_auth_id="device",
        user_code="code",
        interval_seconds=1,
    )

    assert result == {"authorization_code": "auth", "code_verifier": "verifier"}
    assert calls["n"] == 2


def test_codex_auth_poll_backs_off_when_server_says_slow_down(monkeypatch) -> None:
    calls = {"n": 0}
    sleeps: list[float] = []

    def post_json(url: str, payload: dict) -> dict:
        calls["n"] += 1
        if calls["n"] == 1:
            raise codex_auth.HttpJsonError(400, json.dumps({"error": "slow_down"}))
        return {"authorization_code": "auth", "code_verifier": "verifier"}

    monkeypatch.setattr(codex_auth, "_post_json", post_json)
    monkeypatch.setattr(codex_auth.time, "sleep", sleeps.append)

    result = codex_auth._poll_for_token(
        device_auth_id="device",
        user_code="code",
        interval_seconds=1,
    )

    assert result == {"authorization_code": "auth", "code_verifier": "verifier"}
    assert sleeps == [6]


def test_codex_auth_save_tokens_zero_expires_in_is_immediate(tmp_path: Path) -> None:
    token_file = tmp_path / "codex-auth.json"
    codex_auth._save_tokens(
        token_file,
        {"access_token": "a", "id_token": "", "refresh_token": "r", "expires_in": 0},
    )

    saved = json.loads(token_file.read_text(encoding="utf-8"))
    assert saved["expires_at"] <= int(time.time() * 1000) + 1000


@_requires_posix_modes
def test_codex_auth_save_tokens_is_owner_only(tmp_path: Path, monkeypatch) -> None:
    token_file = tmp_path / "codex-auth.json"
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text("{}", encoding="utf-8")
    token_file.chmod(0o644)
    monkeypatch.setattr(Path, "chmod", lambda self, mode: None)

    codex_auth._save_tokens(
        token_file,
        {"access_token": "a", "id_token": "", "refresh_token": "r", "expires_in": 3600},
    )

    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
