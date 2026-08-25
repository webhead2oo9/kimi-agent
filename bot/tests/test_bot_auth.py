"""Provider startup auth-detection helpers."""

from __future__ import annotations

from app import providers as provider_runtime
from config.model_config import ModelConfig
from config.settings import Settings


def _settings(**kwargs: object) -> Settings:
    return Settings.model_validate(kwargs)


def _single_model_config(
    *,
    provider_type: str,
    api_key_env: str = "MODEL_API_KEY",
    base_url: str = "https://llm-gateway.example.invalid/v1",
) -> ModelConfig:
    return ModelConfig.model_validate(
        {
            "providers": {
                "main": {
                    "type": provider_type,
                    "base_url": base_url,
                    "api_key_env": api_key_env,
                }
            },
            "models": {"chat": {"provider": "main", "model": "model"}},
            "roles": {"chat": "chat", "compaction": "chat"},
        }
    )


def test_llm_credentials_follow_the_profile_api_key_env() -> None:
    config = _single_model_config(provider_type="openai_compat")
    settings = _settings(model_api_key="k")
    assert provider_runtime._has_active_llm_credentials(settings, config) is True

    settings = _settings(model_api_key="")
    assert provider_runtime._has_active_llm_credentials(settings, config) is False
