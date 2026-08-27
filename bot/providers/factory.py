from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from branding import DEFAULT_BOT_NAME
from providers.base import LLMProvider

if TYPE_CHECKING:
    from codex.auth import CodexAuthManager


SUPPORTED_PROVIDER_NAMES = frozenset(
    {
        "openai_compat",
        "openai_responses",
        "anthropic",
        "anthropic_compat",
        "openrouter",
        "codex",
    }
)

_codex_auth_managers: dict[str, CodexAuthManager] = {}


@dataclass(frozen=True)
class ProviderConfig:
    provider_name: str
    api_key: str
    base_url: str
    model: str
    # The gateway injects its own upstream credentials; api_key is empty by design.
    keyless: bool = False
    anthropic_prompt_caching: bool = True
    # Baseline `output_config.effort`; empty sends no output_config at all.
    anthropic_effort: str = ""
    # Baseline `reasoning_effort` for DeepSeek-compatible chat-completion models.
    openai_reasoning_effort: str = ""
    # Optional per-request UUID header for OpenAI-compatible gateways that use
    # caller request IDs for tracing or idempotency.
    openai_request_id_header: str = ""
    openai_service_tier: str = ""
    openai_timeout_seconds: float = 900.0
    stream_stall_timeout_seconds: float = 90.0
    openrouter_provider_json: str = ""
    openrouter_app_url: str = ""
    openrouter_app_name: str = DEFAULT_BOT_NAME
    user_agent: str = DEFAULT_BOT_NAME
    codex_token_file: str = "secrets/codex-auth.json"
    codex_model: str = "gpt-5.5"
    codex_reasoning_effort: str = "high"
    codex_image_quality: str = "auto"
    codex_image_format: str = "png"
    codex_ws_idle_timeout: int = 3000
    codex_ws_read_timeout: float = 120.0
    codex_verbose: bool = False


def create_provider(config: ProviderConfig) -> LLMProvider:
    match config.provider_name:
        case "openai_compat":
            from providers.openai_compat import OpenAICompatProvider

            return OpenAICompatProvider(
                api_key=config.api_key,
                base_url=config.base_url,
                model=config.model,
                service_tier=_openai_service_tier(config),
                reasoning_effort=config.openai_reasoning_effort,
                request_id_header=config.openai_request_id_header,
                timeout_seconds=config.openai_timeout_seconds,
                stall_timeout_seconds=config.stream_stall_timeout_seconds,
                user_agent=config.user_agent,
            )
        case "openai_responses":
            from providers.openai_responses import OpenAIResponsesProvider

            return OpenAIResponsesProvider(
                api_key=config.api_key,
                base_url=config.base_url or None,
                model=config.model,
                service_tier=_openai_service_tier(config),
                reasoning_effort=config.openai_reasoning_effort,
                timeout_seconds=config.openai_timeout_seconds,
                user_agent=config.user_agent,
            )
        case "anthropic":
            from providers.anthropic import AnthropicProvider

            return AnthropicProvider(
                api_key=config.api_key,
                model=config.model,
                timeout_seconds=config.openai_timeout_seconds,
            )
        case "anthropic_compat":
            from providers.anthropic_compat import AnthropicCompatProvider

            return AnthropicCompatProvider(
                api_key=config.api_key,
                base_url=config.base_url,
                model=config.model,
                timeout_seconds=config.openai_timeout_seconds,
                prompt_caching=config.anthropic_prompt_caching,
                effort=config.anthropic_effort,
            )
        case "openrouter":
            from providers.openrouter import OpenRouterProvider

            return OpenRouterProvider(
                api_key=config.api_key,
                model=config.model,
                provider_routing=_parse_openrouter_provider_json(config.openrouter_provider_json),
                app_url=config.openrouter_app_url or None,
                app_name=config.openrouter_app_name or None,
                user_agent=config.user_agent,
            )
        case "codex":
            from codex.transport import CodexTransport
            from providers.codex import CodexProvider

            auth_manager = get_codex_auth_manager(config.codex_token_file)
            transport = CodexTransport(
                auth_manager,
                idle_timeout=config.codex_ws_idle_timeout,
                read_timeout=config.codex_ws_read_timeout,
                verbose=config.codex_verbose,
            )
            return CodexProvider(
                auth_manager=auth_manager,
                transport=transport,
                model=config.codex_model or config.model or "gpt-5.5",
                reasoning_effort=config.codex_reasoning_effort,
                image_quality=config.codex_image_quality,
                image_format=config.codex_image_format,
            )
        case _:
            raise ValueError(f"Unknown LLM provider: {config.provider_name}")


def _openai_service_tier(config: ProviderConfig) -> str | None:
    # service_tier ("flex" etc.) is an OpenAI-only feature: a compat gateway
    # rejects the field outright, so it is forwarded only when the endpoint is
    # really OpenAI. An empty base_url means the SDK default, which *is*
    # api.openai.com.
    if not config.openai_service_tier:
        return None
    base_url = config.base_url.rstrip("/")
    if not base_url or base_url.startswith("https://api.openai.com"):
        return config.openai_service_tier
    return None


def _parse_openrouter_provider_json(value: str) -> dict[str, Any]:
    if not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("OPENROUTER_PROVIDER_JSON must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("OPENROUTER_PROVIDER_JSON must decode to a JSON object")
    return parsed


def get_codex_auth_manager(token_file: str) -> CodexAuthManager:
    """Return the process-wide manager for one resolved Codex token file."""
    from codex.auth import CodexAuthManager

    key = str(Path(token_file).expanduser().resolve(strict=False))
    manager = _codex_auth_managers.get(key)
    if manager is None:
        manager = CodexAuthManager(token_file)
        _codex_auth_managers[key] = manager
    return manager
