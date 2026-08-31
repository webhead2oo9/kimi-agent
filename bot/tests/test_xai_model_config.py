from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from app import providers as provider_runtime
from config.model_config import parse_model_config_text, resolve_provider_config
from config.settings import Settings
from providers.factory import ProviderConfig, create_provider
from providers.xai import XaiProvider
from xai.auth import XaiAuthRevokedError


def _config(profile: str) -> str:
    return f"""
providers:
  grok:
{profile}
models:
  grok-chat:
    provider: grok
    model: grok-4.6
    context_window: 100000
    capabilities: [text, tool_calling]
roles:
  chat: grok-chat
  chat_fallbacks: []
  compaction: grok-chat
  compaction_fallbacks: []
selectable_chat_models: []
overrides:
  channels: {{}}
  guilds: {{}}
  users: {{}}
  commands: {{}}
"""


def test_xai_profile_defaults_to_strict_oauth() -> None:
    parsed = parse_model_config_text(_config("    type: xai"))
    resolved = resolve_provider_config(
        parsed,
        "grok-chat",
        settings=Settings(_env_file=None),  # type: ignore[call-arg]
    )

    assert resolved.provider_name == "xai"
    assert resolved.xai_auth_mode == "oauth"
    assert resolved.api_key == ""


@pytest.mark.parametrize(
    ("profile", "message"),
    [
        (
            "    type: xai\n    auth_mode: oauth\n    api_key_env: GROK_API_KEY",
            "oauth profiles must not set api_key_env",
        ),
        ("    type: xai\n    auth_mode: api_key", "must set api_key_env: GROK_API_KEY"),
        (
            "    type: xai\n    auth_mode: auto\n    api_key_env: MODEL_API_KEY",
            "auto profiles may only use",
        ),
        (
            "    type: xai\n    base_url: https://proxy.example/v1",
            "fixed https://api.x.ai/v1",
        ),
        (
            "    type: openai_responses\n    auth_mode: oauth",
            "auth_mode is only supported",
        ),
    ],
)
def test_xai_profile_rejects_ambiguous_or_unsafe_auth(profile: str, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        parse_model_config_text(_config(profile))


def test_xai_auto_profile_resolves_existing_grok_key() -> None:
    parsed = parse_model_config_text(
        _config("    type: xai\n    auth_mode: auto\n    api_key_env: GROK_API_KEY")
    )
    resolved = resolve_provider_config(
        parsed,
        "grok-chat",
        settings=Settings(  # type: ignore[call-arg]
            _env_file=None,
            grok_api_key=SecretStr("paid-key"),
        ),
    )

    assert resolved.xai_auth_mode == "auto"
    assert resolved.api_key == "paid-key"


def test_factory_builds_native_xai_provider_without_network(tmp_path: Path) -> None:
    provider = create_provider(
        ProviderConfig(
            provider_name="xai",
            api_key="paid-key",
            base_url="",
            model="grok-4.6",
            xai_auth_mode="api_key",
            xai_token_file=str(tmp_path / "unused.json"),
        )
    )

    assert isinstance(provider, XaiProvider)
    assert provider.base_url == "https://api.x.ai/v1"


def test_reachable_xai_credentials_follow_selected_mode(tmp_path: Path) -> None:
    token_file = tmp_path / "xai.json"
    token_file.write_text(
        json.dumps(
            {
                "access_token": "oauth",
                "refresh_token": "refresh",
                "expires_at": 9_999_999_999_999,
            }
        ),
        encoding="utf-8",
    )
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        xai_oauth_token_file=str(token_file),
        grok_api_key=SecretStr("paid-key"),
    )

    oauth = parse_model_config_text(_config("    type: xai\n    auth_mode: oauth"))
    api_key = parse_model_config_text(
        _config("    type: xai\n    auth_mode: api_key\n    api_key_env: GROK_API_KEY")
    )

    assert provider_runtime._has_active_llm_credentials(settings, oauth) is True
    assert provider_runtime._has_active_llm_credentials(settings, api_key) is True


def test_api_key_profile_does_not_read_unusable_oauth_path(tmp_path: Path) -> None:
    unusable_token_path = tmp_path / "directory-instead-of-token-file"
    unusable_token_path.mkdir()
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        xai_oauth_token_file=str(unusable_token_path),
        grok_api_key=SecretStr("paid-key"),
    )
    api_key = parse_model_config_text(
        _config("    type: xai\n    auth_mode: api_key\n    api_key_env: GROK_API_KEY")
    )

    assert provider_runtime._has_active_llm_credentials(settings, api_key) is True


def test_revoked_oauth_is_fatal_only_without_auto_api_fallback(tmp_path: Path) -> None:
    class RevokedManager:
        def is_available(self) -> bool:
            return True

        async def get_access_token(self) -> str:
            raise XaiAuthRevokedError("revoked")

    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        xai_oauth_token_file=str(tmp_path / "xai.json"),
        grok_api_key=SecretStr("paid-key"),
    )
    strict = parse_model_config_text(_config("    type: xai\n    auth_mode: oauth"))
    auto = parse_model_config_text(
        _config("    type: xai\n    auth_mode: auto\n    api_key_env: GROK_API_KEY")
    )

    with pytest.raises(SystemExit):
        provider_runtime.xai_startup_check(
            settings,
            RevokedManager(),  # type: ignore[arg-type]
            model_config=strict,
        )
    provider_runtime.xai_startup_check(
        settings,
        RevokedManager(),  # type: ignore[arg-type]
        model_config=auto,
    )
