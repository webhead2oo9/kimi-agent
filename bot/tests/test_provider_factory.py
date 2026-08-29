import pytest

from providers.factory import ProviderConfig, create_provider
from providers.openai_responses import OpenAIResponsesProvider
from providers.types import ProviderCapability


def test_factory_creates_anthropic_provider() -> None:
    provider = create_provider(
        ProviderConfig(
            provider_name="anthropic",
            api_key="key",
            base_url="",
            model="claude-sonnet-4-20250514",
        )
    )

    assert provider.provider_key == "anthropic"


def test_factory_rejects_invalid_openrouter_provider_json() -> None:
    with pytest.raises(ValueError, match="OPENROUTER_PROVIDER_JSON"):
        create_provider(
            ProviderConfig(
                provider_name="openrouter",
                api_key="key",
                base_url="",
                model="openai/gpt-4.1",
                openrouter_provider_json="{bad",
            )
        )


def test_factory_forwards_openrouter_routing_tier_and_timeout() -> None:
    provider = create_provider(
        ProviderConfig(
            provider_name="openrouter",
            api_key="key",
            base_url="",
            model="openai/gpt-5",
            openrouter_provider_json='{"zdr":true,"allow_fallbacks":false}',
            openrouter_service_tier="priority",
            openai_timeout_seconds=123,
        )
    )

    assert provider._provider_routing == {  # type: ignore[attr-defined]
        "zdr": True,
        "allow_fallbacks": False,
    }
    assert provider._service_tier == "priority"  # type: ignore[attr-defined]
    assert provider._client.timeout == 123  # type: ignore[attr-defined]


def test_factory_does_not_send_flex_to_non_openai_compatible_base_url() -> None:
    provider = create_provider(
        ProviderConfig(
            provider_name="openai_compat",
            api_key="key",
            base_url="https://api.deepseek.com",
            model="test-deepseek-model",
            openai_service_tier="flex",
        )
    )

    assert "flex_service_tier" not in {cap.value for cap in provider.capabilities}


def test_factory_forwards_timeout_to_openai_compatible_client() -> None:
    provider = create_provider(
        ProviderConfig(
            provider_name="openai_compat",
            api_key="key",
            base_url="https://gateway.example/v1",
            model="test-model",
            openai_timeout_seconds=123,
        )
    )

    assert provider._client.timeout == 123  # type: ignore[attr-defined]


def test_factory_does_not_send_flex_to_responses_provider_on_a_gateway() -> None:
    provider = create_provider(
        ProviderConfig(
            provider_name="openai_responses",
            api_key="key",
            base_url="https://opencode.ai/zen/go/v1",
            model="gpt-5.6-luna",
            openai_service_tier="flex",
        )
    )

    assert "flex_service_tier" not in {cap.value for cap in provider.capabilities}


def test_factory_sends_flex_to_responses_provider_on_real_openai() -> None:
    provider = create_provider(
        ProviderConfig(
            provider_name="openai_responses",
            api_key="key",
            base_url="https://api.openai.com/v1",
            model="gpt-5.6",
            openai_service_tier="flex",
        )
    )

    assert ProviderCapability.FLEX_SERVICE_TIER in provider.capabilities


def test_factory_creates_stateless_openai_responses_provider() -> None:
    provider = create_provider(
        ProviderConfig(
            provider_name="openai_responses",
            api_key="sk-go",
            base_url="https://opencode.ai/zen/go/v1",
            model="gpt-5.6-luna",
        )
    )

    assert isinstance(provider, OpenAIResponsesProvider)
    assert provider.model == "gpt-5.6-luna"
    assert provider.base_url == "https://opencode.ai/zen/go/v1"
    assert ProviderCapability.IMAGE_INPUT in provider.capabilities
    assert ProviderCapability.SERVER_SIDE_CONTEXT not in provider.capabilities


def test_factory_forwards_reasoning_effort_to_the_responses_provider() -> None:
    provider = create_provider(
        ProviderConfig(
            provider_name="openai_responses",
            api_key="sk-go",
            base_url="https://opencode.ai/zen/go/v1",
            model="gpt-5.6-luna",
            openai_reasoning_effort="high",
        )
    )

    assert provider._reasoning_effort == "high"  # type: ignore[attr-defined]


def test_factory_creates_anthropic_compat_provider() -> None:
    from providers.anthropic_compat import AnthropicCompatProvider

    provider = create_provider(
        ProviderConfig(
            provider_name="anthropic_compat",
            api_key="sk-zen",
            base_url="https://opencode.ai/zen/go/v1",
            model="minimax-m3",
        )
    )

    assert isinstance(provider, AnthropicCompatProvider)
    assert provider.provider_key == "anthropic_compat"
    assert provider.model == "minimax-m3"
    assert provider.base_url == "https://opencode.ai/zen/go/v1"
