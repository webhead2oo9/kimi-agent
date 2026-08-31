from __future__ import annotations

import json
from pathlib import Path

from scripts import xai_auth


def test_device_poll_honors_pending_and_slow_down(monkeypatch) -> None:
    outcomes = [
        xai_auth.HttpJsonError(400, json.dumps({"error": "authorization_pending"})),
        xai_auth.HttpJsonError(400, json.dumps({"error": "slow_down"})),
        {"access_token": "access", "refresh_token": "refresh"},
    ]
    sleeps: list[float] = []

    def post_form(_url: str, _payload: dict[str, str]):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(xai_auth, "_post_form", post_form)
    monkeypatch.setattr(xai_auth.time, "sleep", sleeps.append)

    result = xai_auth._poll_for_tokens(
        "https://auth.x.ai/oauth2/token",
        device_code="device",
        interval_seconds=1,
        expires_in_seconds=900,
    )

    assert result["access_token"] == "access"
    assert sleeps == [1, 6]


def test_device_flow_saves_tokens_without_changing_model_config(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class Discovery:
        token_endpoint = "https://auth.x.ai/oauth2/token"
        device_authorization_endpoint = "https://auth.x.ai/oauth2/device/code"

    async def discover():
        return Discovery()

    monkeypatch.setattr(xai_auth, "discover_xai_oauth", discover)
    monkeypatch.setattr(
        xai_auth,
        "_request_device_code",
        lambda _endpoint: {
            "device_code": "device",
            "user_code": "CODE",
            "verification_uri": "https://auth.x.ai/device",
            "interval": 1,
            "expires_in": 60,
        },
    )
    monkeypatch.setattr(
        xai_auth,
        "_poll_for_tokens",
        lambda *_args, **_kwargs: {
            "access_token": "access",
            "refresh_token": "refresh",
            "expires_in": 3600,
        },
    )
    monkeypatch.setattr(xai_auth.webbrowser, "open", lambda _url: False)
    token_file = tmp_path / "xai.json"
    locked_writes = 0
    real_locked_write = xai_auth.write_xai_tokens_locked

    async def record_locked_write(path: Path, tokens: dict) -> None:
        nonlocal locked_writes
        locked_writes += 1
        await real_locked_write(path, tokens)

    monkeypatch.setattr(xai_auth, "write_xai_tokens_locked", record_locked_write)

    xai_auth.device_code_flow(token_file)

    saved = json.loads(token_file.read_text(encoding="utf-8"))
    assert saved["access_token"] == "access"
    assert saved["refresh_token"] == "refresh"
    assert locked_writes == 1
