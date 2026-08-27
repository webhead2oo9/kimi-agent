import json
import time
from pathlib import Path

import aiohttp
import pytest

from app import providers as provider_runtime
from codex.auth import CodexAuthError, CodexAuthRevokedError
from config.model_config import ModelConfig
from config.settings import Settings
from providers.codex import CodexProvider
from providers.factory import ProviderConfig, create_provider, get_codex_auth_manager


def _settings(**kwargs: object) -> Settings:
    return Settings.model_validate(kwargs)


def _model_config(provider_type: str) -> ModelConfig:
    provider: dict[str, object] = {"type": provider_type}
    if provider_type != "codex":
        provider.update(
            {
                "base_url": "https://llm-gateway.example.invalid/v1",
                "api_key_env": "MODEL_API_KEY",
            }
        )
    return ModelConfig.model_validate(
        {
            "providers": {"main": provider},
            "models": {"chat": {"provider": "main", "model": "gpt-5.5"}},
            "roles": {"chat": "chat", "compaction": "chat"},
        }
    )


def _write_token_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "access_token": "access",
                "refresh_token": "refresh",
                "id_token": "id",
                "account_id": "acct",
                "expires_at": int((time.time() + 3600) * 1000),
            }
        ),
        encoding="utf-8",
    )


def test_factory_creates_codex_provider_without_api_key(tmp_path: Path) -> None:
    token_file = tmp_path / "codex-auth.json"
    _write_token_file(token_file)

    provider = create_provider(
        ProviderConfig(
            provider_name="codex",
            api_key="",
            base_url="",
            model="",
            codex_token_file=str(token_file),
            codex_model="gpt-5.5",
            codex_reasoning_effort="high",
            codex_image_quality="auto",
            codex_image_format="png",
            codex_ws_idle_timeout=3000,
        )
    )

    assert isinstance(provider, CodexProvider)
    assert provider.provider_key == "codex"
    assert provider.model == "gpt-5.5"


def test_factory_reuses_codex_auth_manager_for_same_token_file(tmp_path: Path) -> None:
    token_file = tmp_path / "codex-auth.json"
    _write_token_file(token_file)

    chat_provider = create_provider(
        ProviderConfig(
            provider_name="codex",
            api_key="",
            base_url="",
            model="",
            codex_token_file=str(token_file),
            codex_model="gpt-5.5",
        )
    )
    eval_provider = create_provider(
        ProviderConfig(
            provider_name="codex",
            api_key="",
            base_url="",
            model="",
            codex_token_file=str(token_file),
            codex_model="gpt-5.5-mini",
        )
    )

    assert isinstance(chat_provider, CodexProvider)
    assert isinstance(eval_provider, CodexProvider)
    assert chat_provider._auth_manager is eval_provider._auth_manager


def test_codex_credentials_detected_from_token_file_without_api_key(
    tmp_path: Path,
) -> None:
    token_file = tmp_path / "codex-auth.json"
    _write_token_file(token_file)
    settings = _settings(
        model_api_key="",
        codex_token_file=str(token_file),
    )

    assert provider_runtime._has_active_llm_credentials(settings, _model_config("codex"))


def test_codex_auth_manager_is_shared_by_resolved_token_path(tmp_path: Path) -> None:
    token_file = tmp_path / "codex-auth.json"

    first = get_codex_auth_manager(str(token_file))
    second = get_codex_auth_manager(str(tmp_path / "." / "codex-auth.json"))

    assert first is second


def test_codex_factory_passes_read_timeout_to_transport(tmp_path: Path) -> None:
    token_file = tmp_path / "codex-auth.json"
    _write_token_file(token_file)

    provider = create_provider(
        ProviderConfig(
            provider_name="codex",
            api_key="",
            base_url="",
            model="",
            codex_token_file=str(token_file),
            codex_ws_read_timeout=42.0,
        )
    )

    assert isinstance(provider, CodexProvider)
    assert provider._transport._read_timeout == 42.0


def test_codex_startup_check_exits_on_revoked_token(monkeypatch) -> None:
    class _RevokedManager:
        async def get_access_token(self) -> str:
            raise CodexAuthRevokedError("revoked")

    settings = _settings()

    with pytest.raises(SystemExit):
        provider_runtime.codex_startup_check(
            settings,
            manager=_RevokedManager(),
            model_config=_model_config("codex"),
        )


def test_codex_startup_check_tolerates_transient_errors(monkeypatch) -> None:
    class _FlakyManager:
        async def get_access_token(self) -> str:
            raise CodexAuthError("network unavailable")

    settings = _settings()

    # Transient failures must not block startup; they retry on first use.
    provider_runtime.codex_startup_check(
        settings,
        manager=_FlakyManager(),
        model_config=_model_config("codex"),
    )


@pytest.mark.parametrize(
    "exc",
    [
        aiohttp.ClientConnectionError("network unavailable"),
        TimeoutError("refresh timed out"),
    ],
)
def test_codex_startup_check_tolerates_transient_refresh_io(
    monkeypatch,
    exc: Exception,
) -> None:
    class _FlakyManager:
        async def get_access_token(self) -> str:
            raise exc

    settings = _settings()

    provider_runtime.codex_startup_check(
        settings,
        manager=_FlakyManager(),
        model_config=_model_config("codex"),
    )


@pytest.mark.parametrize("auth_mode", ["oauth", "auto", "api_key"])
def test_codex_startup_check_skips_optional_image_tool_without_codex_chat(
    auth_mode: str,
) -> None:
    calls = {"n": 0}

    class _Manager:
        async def get_access_token(self) -> str:
            calls["n"] += 1
            raise CodexAuthRevokedError("revoked")

    provider_runtime.codex_startup_check(
        _settings(image_gen_enabled=True, image_gen_auth_mode=auth_mode),
        manager=_Manager(),
        model_config=_model_config("openai_compat"),
    )

    assert calls["n"] == 0


def test_codex_startup_check_noop_when_codex_inactive(monkeypatch) -> None:
    calls = {"n": 0}

    class _Manager:
        async def get_access_token(self) -> str:
            calls["n"] += 1
            return "token"

    settings = _settings(
        media_image_provider="",
    )

    provider_runtime.codex_startup_check(
        settings,
        manager=_Manager(),
        model_config=_model_config("openai_compat"),
    )

    assert calls["n"] == 0
