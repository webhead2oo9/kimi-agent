from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app import providers as provider_runtime
from config.model_config import (
    _API_KEY_SETTINGS_FIELDS,
    SUPPORTED_API_KEY_ENVS,
    ModelConfig,
    Scope,
    _secret_from_settings,
    load_model_config,
    parse_model_config_text,
    resolve_provider_config,
)
from config.settings import Settings
from providers.failure_policy import generic_failure_policy, register_failure_adapter
from providers.types import ProviderRequest, ProviderResponse


def _settings(**kwargs: object) -> Settings:
    return Settings.model_validate(kwargs)


def test_every_supported_api_key_env_maps_to_a_real_settings_field() -> None:
    # Every API-key environment name accepted by a model profile must map to a
    # Settings field. Otherwise that profile fails with AttributeError at startup.
    settings = _settings()

    assert frozenset(_API_KEY_SETTINGS_FIELDS) == SUPPORTED_API_KEY_ENVS
    for env_name, field_name in _API_KEY_SETTINGS_FIELDS.items():
        assert hasattr(settings, field_name), f"{env_name} -> missing Settings.{field_name}"
        assert _secret_from_settings(_settings(**{field_name: "x"}), env_name) == "x"


def test_kimi_coding_api_key_is_a_supported_profile_key_env() -> None:
    # Kimi Code (membership coding plan) anthropic_compat profiles authenticate
    # with a Kimi Code Console key referenced as KIMI_CODING_API_KEY.
    assert "KIMI_CODING_API_KEY" in SUPPORTED_API_KEY_ENVS
    assert _API_KEY_SETTINGS_FIELDS["KIMI_CODING_API_KEY"] == "kimi_coding_api_key"
    assert _secret_from_settings(_settings(kimi_coding_api_key="x"), "KIMI_CODING_API_KEY") == "x"


def test_runinfra_gateway_key_is_a_supported_profile_key_env() -> None:
    assert "RUNINFRA_GATEWAY_KEY" in SUPPORTED_API_KEY_ENVS
    assert _API_KEY_SETTINGS_FIELDS["RUNINFRA_GATEWAY_KEY"] == "runinfra_gateway_key"
    assert _secret_from_settings(_settings(runinfra_gateway_key="x"), "RUNINFRA_GATEWAY_KEY") == "x"


def test_zai_api_key_is_a_supported_profile_key_env() -> None:
    assert "ZAI_API_KEY" in SUPPORTED_API_KEY_ENVS
    assert _API_KEY_SETTINGS_FIELDS["ZAI_API_KEY"] == "zai_api_key"
    assert _secret_from_settings(_settings(zai_api_key="x"), "ZAI_API_KEY") == "x"


def _write_config(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def _base_config(*, provider_type: str = "openai_compat") -> str:
    return f"""
providers:
  main:
    type: {provider_type}
    base_url: https://llm-gateway.example.invalid/v1
    api_key_env: MODEL_API_KEY
models:
  chat:
    provider: main
    model: Kimi-K2.6
roles:
  chat: chat
  compaction: chat
"""


def test_model_config_rejects_codex_operational_fields(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path / "models.yaml",
        """
providers:
  codex-local:
    type: codex
    token_file: secrets/codex-auth.json
models:
  codex-chat:
    provider: codex-local
    model: gpt-5.5
roles:
  chat: codex-chat
  compaction: codex-chat
""",
    )

    with pytest.raises(ValidationError, match="token_file"):
        load_model_config(path)


def test_resolve_anthropic_compat_maps_opencode_go_key(tmp_path: Path) -> None:
    config = load_model_config(
        _write_config(
            tmp_path / "models.yaml",
            """
providers:
  opencode-go:
    type: anthropic_compat
    base_url: https://opencode.ai/zen/go/v1
    api_key_env: OPENCODE_GO_API_KEY
models:
  minimax-m3:
    provider: opencode-go
    model: minimax-m3
roles:
  chat: minimax-m3
  compaction: minimax-m3
""",
        )
    )

    provider_config = resolve_provider_config(
        config,
        "minimax-m3",
        settings=_settings(opencode_go_api_key="sk-go"),
    )

    assert provider_config.provider_name == "anthropic_compat"
    assert provider_config.api_key == "sk-go"
    assert provider_config.base_url == "https://opencode.ai/zen/go/v1"
    assert provider_config.model == "minimax-m3"


def test_resolve_openai_compat_maps_opencode_go_key(tmp_path: Path) -> None:
    config = load_model_config(
        _write_config(
            tmp_path / "models.yaml",
            """
providers:
  opencode:
    type: openai_compat
    base_url: https://opencode.ai/zen/go/v1
    api_key_env: OPENCODE_GO_API_KEY
models:
  chat:
    provider: opencode
    model: kimi-k2.6
roles:
  chat: chat
  compaction: chat
""",
        )
    )

    provider_config = resolve_provider_config(
        config,
        "chat",
        settings=_settings(opencode_go_api_key="sk-go"),
    )

    assert provider_config.provider_name == "openai_compat"
    assert provider_config.api_key == "sk-go"
    assert provider_config.base_url == "https://opencode.ai/zen/go/v1"
    assert provider_config.model == "kimi-k2.6"


def test_runinfra_profile_forwards_reasoning_request_id_and_key(tmp_path: Path) -> None:
    config = load_model_config(
        _write_config(
            tmp_path / "models.yaml",
            """
providers:
  runinfra:
    type: openai_compat
    base_url: https://api.runinfra.ai/v1
    api_key_env: RUNINFRA_GATEWAY_KEY
    reasoning_effort: max
    request_id_header: X-Client-Request-Id
    max_output_tokens: 32768
models:
  flash:
    provider: runinfra
    model: deepseek-v4-flash
roles:
  chat: flash
  compaction: flash
""",
        )
    )

    provider_config = resolve_provider_config(
        config,
        "flash",
        settings=_settings(runinfra_gateway_key="runinfra-key"),
    )

    assert provider_config.api_key == "runinfra-key"
    assert provider_config.openai_reasoning_effort == "max"
    assert provider_config.openai_request_id_header == "X-Client-Request-Id"
    assert config.providers["runinfra"].max_output_tokens == 32768


def test_public_model_template_is_safe_and_resolvable() -> None:
    # The tracked template must remain structurally valid without mirroring a
    # deployment's provider, model catalog, pricing, or role choices.
    config = load_model_config("config/models.example.yaml")

    provider_config = resolve_provider_config(
        config,
        "primary-chat",
        settings=_settings(model_api_key="example-key"),
    )

    assert provider_config.provider_name == "openai_compat"
    assert provider_config.api_key == "example-key"
    assert provider_config.base_url.endswith(".example.invalid/v1")
    assert provider_config.model == "provider/chat-model"
    assert config.roles.chat == "primary-chat"
    assert config.models["primary-chat"].pricing is None


def test_resolve_native_anthropic_maps_anthropic_key(tmp_path: Path) -> None:
    config = load_model_config(
        _write_config(
            tmp_path / "models.yaml",
            """
providers:
  claude:
    type: anthropic
    api_key_env: ANTHROPIC_API_KEY
models:
  sonnet:
    provider: claude
    model: claude-sonnet-4-20250514
roles:
  chat: sonnet
  compaction: sonnet
""",
        )
    )

    provider_config = resolve_provider_config(
        config,
        "sonnet",
        settings=_settings(anthropic_api_key="sk-ant"),
    )

    assert provider_config.provider_name == "anthropic"
    assert provider_config.api_key == "sk-ant"
    assert provider_config.base_url == ""
    assert provider_config.model == "claude-sonnet-4-20250514"


def test_unknown_api_key_env_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="unsupported api_key_env"):
        load_model_config(
            _write_config(
                tmp_path / "models.yaml",
                """
providers:
  main:
    type: openai_compat
    base_url: https://llm-gateway.example.invalid/v1
    api_key_env: LLM_API_KEY
models:
  chat:
    provider: main
    model: Kimi-K2.6
roles:
  chat: chat
  compaction: chat
""",
            )
        )


def test_resolve_openrouter_serializes_provider_routing(tmp_path: Path) -> None:
    config = load_model_config(
        _write_config(
            tmp_path / "models.yaml",
            """
providers:
  router:
    type: openrouter
    api_key_env: MODEL_API_KEY
    provider_routing:
      order: [anthropic]
      allow_fallbacks: false
    app_name: Router Console
    app_url: https://example.test/app
models:
  router-chat:
    provider: router
    model: anthropic/claude-sonnet-4-6
roles:
  chat: router-chat
  compaction: router-chat
""",
        )
    )

    provider_config = resolve_provider_config(
        config,
        "router-chat",
        settings=_settings(model_api_key="router-key"),
    )

    assert provider_config.provider_name == "openrouter"
    assert provider_config.api_key == "router-key"
    assert json.loads(provider_config.openrouter_provider_json) == {
        "order": ["anthropic"],
        "allow_fallbacks": False,
    }
    assert provider_config.openrouter_app_name == "Router Console"
    assert provider_config.user_agent == "Router Console"
    assert provider_config.openrouter_app_url == "https://example.test/app"


def test_provider_identity_inherits_the_configured_bot_name(tmp_path: Path) -> None:
    config = load_model_config(
        _write_config(
            tmp_path / "models.yaml",
            """
providers:
  main:
    type: openai_responses
    api_key_env: MODEL_API_KEY
models:
  chat:
    provider: main
    model: gpt-5.6-luna
roles:
  chat: chat
  compaction: chat
""",
        )
    )

    provider_config = resolve_provider_config(
        config,
        "chat",
        settings=_settings(model_api_key="key", bot_name="Commúnity Helper 🤖"),
    )

    assert provider_config.openrouter_app_name == "Community Helper"
    assert provider_config.user_agent == "Community Helper"


def test_resolve_codex_uses_settings_operational_fields_and_model_entry(
    tmp_path: Path,
) -> None:
    config = load_model_config(
        _write_config(
            tmp_path / "models.yaml",
            """
providers:
  codex-local:
    type: codex
models:
  codex-chat:
    provider: codex-local
    model: gpt-5.5-override
roles:
  chat: codex-chat
  compaction: codex-chat
""",
        )
    )

    provider_config = resolve_provider_config(
        config,
        "codex-chat",
        settings=_settings(
            codex_token_file="secrets/custom-codex.json",
            codex_model="settings-codex-model",
            codex_reasoning_effort="medium",
            codex_image_quality="high",
            codex_image_format="webp",
            codex_ws_idle_timeout=99,
            codex_ws_read_timeout=12.5,
            codex_verbose=True,
        ),
    )

    assert provider_config.provider_name == "codex"
    assert provider_config.codex_model == "gpt-5.5-override"
    assert provider_config.codex_token_file == "secrets/custom-codex.json"
    assert provider_config.codex_reasoning_effort == "medium"
    assert provider_config.codex_image_quality == "high"
    assert provider_config.codex_image_format == "webp"
    assert provider_config.codex_ws_idle_timeout == 99
    assert provider_config.codex_ws_read_timeout == 12.5
    assert provider_config.codex_verbose is True


def test_provider_manager_exposes_primary_model_reasoning_policy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Synthetic config: the mechanism under test is that a model's
    # reasoning_after_tools map reaches the resolved provider, independent of
    # which model production routes chat to.
    config = load_model_config(
        _write_config(
            tmp_path / "models.yaml",
            """
providers:
  codex-low:
    type: codex
    reasoning_effort: low
models:
  chat:
    provider: codex-low
    model: gpt-5.6-sol
    context_window: 200000
    capabilities: [text, tool_calling]
    reasoning_after_tools:
      medium: [knowledge_lookup]
      high: [read_file]
roles:
  chat: chat
  compaction: chat
""",
        )
    )

    class DummyProvider:
        provider_key = "dummy"
        capabilities: set[Any] = set()

        def __init__(self, model: str) -> None:
            self.model = model

        async def run_turn(self, request: Any) -> Any:  # pragma: no cover
            raise AssertionError("not called")

    monkeypatch.setattr(
        provider_runtime,
        "create_provider",
        lambda provider_config: DummyProvider(provider_config.model),
    )
    manager = provider_runtime.ProviderManager(
        settings=_settings(),
        model_config=config,
    )

    policies = {
        policy.effort: policy.tool_names for policy in manager.resolve("chat").reasoning_escalations
    }

    assert "knowledge_lookup" in policies["medium"]
    assert "read_file" in policies["high"]


def test_reasoning_effort_profile_field_configures_openai_compat(
    tmp_path: Path,
) -> None:
    config = load_model_config(
        _write_config(
            tmp_path / "models.yaml",
            """
providers:
  main:
    type: openai_compat
    base_url: https://opencode.ai/zen/go/v1
    api_key_env: OPENCODE_GO_API_KEY
    reasoning_effort: xhigh
models:
  chat:
    provider: main
    model: deepseek-v4-flash
roles:
  chat: chat
  compaction: chat
""",
        )
    )

    provider_config = resolve_provider_config(
        config,
        "chat",
        settings=_settings(opencode_go_api_key="test-key"),
    )

    assert provider_config.openai_reasoning_effort == "xhigh"


def test_reasoning_effort_profile_field_configures_openai_responses(
    tmp_path: Path,
) -> None:
    # openai_responses sends it as `reasoning.effort` on the Responses API, so
    # the profile baseline has to survive resolution the same way codex's does.
    config = load_model_config(
        _write_config(
            tmp_path / "models.yaml",
            """
providers:
  main:
    type: openai_responses
    base_url: https://opencode.ai/zen/go/v1
    api_key_env: OPENCODE_GO_API_KEY
    reasoning_effort: high
models:
  chat:
    provider: main
    model: gpt-5.6-luna
    reasoning_after_tools:
      max: [read_file]
roles:
  chat: chat
  compaction: chat
""",
        )
    )

    provider_config = resolve_provider_config(
        config,
        "chat",
        settings=_settings(opencode_go_api_key="test-key"),
    )

    assert provider_config.openai_reasoning_effort == "high"
    assert config.models["chat"].reasoning_after_tools == {"max": ["read_file"]}


def test_reasoning_after_tools_rejects_unsupported_provider_type(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="reasoning_after_tools is only supported"):
        load_model_config(
            _write_config(
                tmp_path / "models.yaml",
                """
providers:
  main:
    type: openai_compat
models:
  chat:
    provider: main
    model: chat
    reasoning_after_tools:
      high: [read_file]
roles:
  chat: chat
  compaction: chat
""",
            )
        )


def test_reasoning_after_tools_rejects_unknown_effort(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="unsupported reasoning_after_tools effort"):
        load_model_config(
            _write_config(
                tmp_path / "models.yaml",
                """
providers:
  main:
    type: codex
models:
  chat:
    provider: main
    model: chat
    reasoning_after_tools:
      extreme: [read_file]
roles:
  chat: chat
  compaction: chat
""",
            )
        )


def test_model_router_resolves_scope_precedence_and_caches_by_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = load_model_config(
        _write_config(
            tmp_path / "models.yaml",
            """
providers:
  main:
    type: openai_compat
    base_url: https://llm-gateway.example.invalid/v1
    api_key_env: MODEL_API_KEY
models:
  default-model: { provider: main, model: default }
  guild-model: { provider: main, model: guild }
  user-model: { provider: main, model: user }
  channel-model: { provider: main, model: channel }
  command-model: { provider: main, model: command }
roles:
  chat: default-model
  compaction: default-model
overrides:
  guilds: { "guild-1": { chat: guild-model } }
  users: { "user-1": { chat: user-model } }
  channels: { "channel-1": { chat: channel-model } }
  commands: { translate: { chat: command-model } }
""",
        )
    )
    created: list[str] = []

    class DummyProvider:
        capabilities: set[Any] = set()

        def __init__(self, model: str) -> None:
            self.model = model

    def fake_create(provider_config):
        created.append(provider_config.model)
        return DummyProvider(provider_config.model)

    monkeypatch.setattr(provider_runtime, "create_provider", fake_create)
    manager = provider_runtime.ProviderManager(
        settings=_settings(model_api_key="key"),
        model_config=config,
    )

    assert manager.resolve("chat", Scope(guild_id="guild-1")).model == "guild"
    assert manager.resolve("chat", Scope(guild_id="guild-1", user_id="user-1")).model == "user"
    assert (
        manager.resolve(
            "chat",
            Scope(guild_id="guild-1", user_id="user-1", channel_id="channel-1"),
        ).model
        == "channel"
    )
    assert (
        manager.resolve(
            "chat",
            Scope(
                guild_id="guild-1",
                user_id="user-1",
                channel_id="channel-1",
                command="translate",
            ),
        ).model
        == "command"
    )
    first_default = manager.resolve("chat", None)
    second_default = manager.resolve("chat", None)

    assert first_default is second_default
    assert created.count("default") == 1


def test_chat_override_missing_secret_blocks_active_credentials(tmp_path: Path) -> None:
    config = load_model_config(
        _write_config(
            tmp_path / "models.yaml",
            """
providers:
  main:
    type: openai_compat
    base_url: https://llm-gateway.example.invalid/v1
    api_key_env: MODEL_API_KEY
  private:
    type: openai_compat
    base_url: https://private.example/v1
    api_key_env: COMPACTION_API_KEY
models:
  default-model: { provider: main, model: default }
  private-model: { provider: private, model: private }
roles:
  chat: default-model
  compaction: default-model
overrides:
  channels: { "channel-1": { chat: private-model } }
""",
        )
    )
    manager = provider_runtime.ProviderManager(
        settings=_settings(model_api_key="main-key", compaction_api_key=""),
        model_config=config,
    )

    assert manager.has_active_llm_credentials() is False


def test_unreferenced_model_entry_does_not_block_active_chat_credentials(
    tmp_path: Path,
) -> None:
    """A cataloged model no role reaches is never credential-checked."""
    config = load_model_config(
        _write_config(
            tmp_path / "models.yaml",
            """
providers:
  main:
    type: openai_compat
    base_url: https://llm-gateway.example.invalid/v1
    api_key_env: MODEL_API_KEY
  spare:
    type: openai_compat
    base_url: https://spare.example/v1
    api_key_env: COMPACTION_API_KEY
models:
  chat-model: { provider: main, model: chat }
  spare-model: { provider: spare, model: spare }
roles:
  chat: chat-model
  compaction: chat-model
""",
        )
    )
    manager = provider_runtime.ProviderManager(
        settings=_settings(model_api_key="main-key", compaction_api_key=""),
        model_config=config,
    )

    assert manager.has_active_llm_credentials() is True


def test_codex_startup_check_runs_for_chat_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = load_model_config(
        _write_config(
            tmp_path / "models.yaml",
            """
providers:
  main:
    type: openai_compat
    base_url: https://llm-gateway.example.invalid/v1
    api_key_env: MODEL_API_KEY
  codex-local:
    type: codex
models:
  chat-model: { provider: main, model: chat }
  codex-model: { provider: codex-local, model: gpt-5.5 }
roles:
  chat: chat-model
  compaction: chat-model
overrides:
  users: { "user-1": { chat: codex-model } }
""",
        )
    )
    calls = {"n": 0}

    class Manager:
        async def get_access_token(self) -> str:
            calls["n"] += 1
            return "token"

    provider_runtime.codex_startup_check(
        _settings(model_api_key="main-key"),
        model_config=config,
        manager=Manager(),
    )

    assert calls["n"] == 1


def test_context_window_warnings_include_override_models(tmp_path: Path) -> None:
    config = load_model_config(
        _write_config(
            tmp_path / "models.yaml",
            """
providers:
  main:
    type: openai_compat
    base_url: https://llm-gateway.example.invalid/v1
    api_key_env: MODEL_API_KEY
models:
  default-model:
    provider: main
    model: default
    context_window: 200000
  small-model:
    provider: main
    model: small
    context_window: 1000
roles:
  chat: default-model
  compaction: default-model
overrides:
  channels: { "channel-1": { chat: small-model } }
""",
        )
    )
    manager = provider_runtime.ProviderManager(
        settings=_settings(
            model_api_key="main-key",
            compaction_trigger_tokens=900,
            react_max_tokens=200,
        ),
        model_config=config,
    )

    warnings = manager.context_window_warnings()

    assert [(item.model_name, item.context_window) for item in warnings] == [("small-model", 1000)]


def test_distill_role_is_not_supported(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="distill"):
        load_model_config(
            _write_config(
                tmp_path / "models.yaml",
                """
providers:
  main:
    type: openai_compat
    base_url: https://llm-gateway.example.invalid/v1
    api_key_env: MODEL_API_KEY
models:
  chat:
    provider: main
    model: Kimi-K2.6
roles:
  chat: chat
  compaction: chat
  distill: chat
""",
            )
        )


def test_scheduler_role_is_not_supported(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="scheduler"):
        load_model_config(
            _write_config(
                tmp_path / "models.yaml",
                """
providers:
  main:
    type: openai_compat
    base_url: https://llm-gateway.example.invalid/v1
    api_key_env: MODEL_API_KEY
models:
  chat:
    provider: main
    model: Kimi-K2.6
roles:
  chat: chat
  compaction: chat
  scheduler: chat
""",
            )
        )


def _config_with_fallbacks(tmp_path: Path) -> Any:
    return load_model_config(
        _write_config(
            tmp_path / "models.yaml",
            """
providers:
  main:
    type: openai_compat
    base_url: https://llm-gateway.example.invalid/v1
    api_key_env: MODEL_API_KEY
models:
  primary: { provider: main, model: Kimi-K2.6 }
  backup: { provider: main, model: backup-model }
roles:
  chat: primary
  chat_fallbacks: [backup]
  compaction: primary
""",
        )
    )


def test_fallbacks_resolve_into_ordered_chain(tmp_path: Path) -> None:
    config = _config_with_fallbacks(tmp_path)

    assert config.model_names_for_role("chat") == ["primary", "backup"]
    # Roles without a fallback list resolve to a single-element chain.
    assert config.model_names_for_role("compaction") == ["primary"]
    # Fallback models are reachable for credential/context checks.
    assert "backup" in config.reachable_model_names(include_compaction=True)


def test_chain_dedupes_primary_appearing_in_fallbacks(tmp_path: Path) -> None:
    config = load_model_config(
        _write_config(
            tmp_path / "models.yaml",
            """
providers:
  main:
    type: openai_compat
    base_url: https://llm-gateway.example.invalid/v1
    api_key_env: MODEL_API_KEY
models:
  primary: { provider: main, model: Kimi-K2.6 }
  backup: { provider: main, model: backup-model }
roles:
  chat: primary
  chat_fallbacks: [primary, backup]
  compaction: primary
""",
        )
    )

    assert config.model_names_for_role("chat") == ["primary", "backup"]


def test_unknown_fallback_model_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match=r"chat_fallbacks\[0\] references unknown model"):
        load_model_config(
            _write_config(
                tmp_path / "models.yaml",
                """
providers:
  main:
    type: openai_compat
    base_url: https://llm-gateway.example.invalid/v1
    api_key_env: MODEL_API_KEY
models:
  primary: { provider: main, model: Kimi-K2.6 }
roles:
  chat: primary
  chat_fallbacks: [ghost]
  compaction: primary
""",
            )
        )


def test_provider_manager_wraps_chain_in_failover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from providers import FailoverProvider

    config = _config_with_fallbacks(tmp_path)

    class DummyProvider:
        capabilities: set[Any] = set()

        def __init__(self, model: str) -> None:
            self.model = model

    def fake_create(provider_config: Any) -> DummyProvider:
        return DummyProvider(provider_config.model)

    monkeypatch.setattr(provider_runtime, "create_provider", fake_create)
    manager = provider_runtime.ProviderManager(
        settings=_settings(model_api_key="key"),
        model_config=config,
    )

    chat = manager.resolve("chat", None)
    assert isinstance(chat, FailoverProvider)
    # Underlying links are the cached single-model providers, in chain order.
    assert [p.model for p in chat._providers] == ["Kimi-K2.6", "backup-model"]
    # The wrapper is cached and stable across turns.
    assert manager.resolve("chat", None) is chat
    # Single-provider roles use the same wrapper so circuit checks are universal.
    assert isinstance(manager.resolve("compaction", None), FailoverProvider)


def test_provider_circuit_policy_defaults_and_adapter_override() -> None:
    config = parse_model_config_text(
        """
providers:
  main:
    type: openai_compat
    base_url: https://example.test/v1
    keyless: true
    failure_adapter: zai
    circuit_breaker:
      outage_cooldown_seconds: 60
      quota_cooldown_seconds: 18000
models:
  chat: { provider: main, model: chat, capabilities: [text, tool_calling] }
roles: { chat: chat, compaction: chat }
"""
    )

    profile = config.providers["main"]
    assert profile.failure_adapter == "zai"
    assert profile.circuit_breaker.outage_cooldown_seconds == 60
    assert profile.circuit_breaker.quota_cooldown_seconds == 18000


def test_provider_rejects_unknown_failure_adapter() -> None:
    with pytest.raises(ValueError, match="unknown failure_adapter"):
        parse_model_config_text(
            """
providers:
  main:
    type: openai_compat
    base_url: https://example.test/v1
    keyless: true
    failure_adapter: mystery
models:
  chat: { provider: main, model: chat, capabilities: [text, tool_calling] }
roles: { chat: chat, compaction: chat }
"""
        )


def test_registered_failure_adapter_is_accepted_by_config() -> None:
    register_failure_adapter("test_provider", generic_failure_policy)
    config = parse_model_config_text(
        """
providers:
  main:
    type: openai_compat
    base_url: https://example.test/v1
    keyless: true
    failure_adapter: test_provider
models:
  chat: { provider: main, model: chat, capabilities: [text, tool_calling] }
roles: { chat: chat, compaction: chat }
"""
    )

    assert config.providers["main"].failure_adapter == "test_provider"


def test_provider_profile_max_output_tokens_clamps_runtime_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_model_config(
        _write_config(
            tmp_path / "models.yaml",
            """
providers:
  runinfra:
    type: openai_compat
    base_url: https://api.runinfra.ai/v1
    api_key_env: RUNINFRA_GATEWAY_KEY
    max_output_tokens: 32768
models:
  flash:
    provider: runinfra
    model: deepseek-v4-flash
roles:
  chat: flash
  compaction: flash
""",
        )
    )
    captured: list[int] = []

    class CapturingProvider:
        model = "deepseek-v4-flash"
        provider_key = "openai_compat"
        capabilities: set[Any] = set()

        async def run_turn(self, request: ProviderRequest) -> ProviderResponse:
            captured.append(request.max_tokens)
            return ProviderResponse(content="ok")

    monkeypatch.setattr(provider_runtime, "create_provider", lambda _config: CapturingProvider())
    manager = provider_runtime.ProviderManager(
        settings=_settings(runinfra_gateway_key="key"),
        model_config=config,
    )
    provider = manager.resolve("chat")
    asyncio.run(
        provider.run_turn(
            ProviderRequest(
                conversation_id=1,
                system_prompt="",
                messages=[],
                current_user_parts=[],
                tools=[],
                max_tokens=65536,
            )
        )
    )

    assert captured == [32768]


def _image_routing_config(tmp_path: Path) -> Path:
    return _write_config(
        tmp_path / "models.yaml",
        """
providers:
  main:
    type: openai_compat
    base_url: https://llm-gateway.example.invalid/v1
    api_key_env: MODEL_API_KEY
models:
  text-only: { provider: main, model: text-model }
  text-backup: { provider: main, model: text-backup }
  vision: { provider: main, model: vision-model, capabilities: [text, image_input] }
  vision-backup: { provider: main, model: vision-backup, capabilities: [text, image_input] }
  channel-vision: { provider: main, model: channel-vision, capabilities: [text, image_input] }
roles:
  chat: text-only
  chat_fallbacks: [text-backup, vision]
  chat_images: vision
  chat_images_fallbacks: [vision-backup]
  compaction: text-only
overrides:
  channels:
    "channel-1": { chat: channel-vision }
    "channel-2": { chat: vision }
""",
    )


def test_chat_images_text_turn_stays_on_text_model(tmp_path: Path) -> None:
    config = load_model_config(_image_routing_config(tmp_path))

    # Text-only turn: no images means the text chat model is used, with the full
    # text fallback chain (image capability is irrelevant here).
    assert config.model_name_for_role("chat", images=False) == "text-only"
    assert config.model_names_for_role("chat", images=False) == [
        "text-only",
        "text-backup",
        "vision",
    ]


def test_chat_images_image_turn_routes_to_vision_model(tmp_path: Path) -> None:
    config = load_model_config(_image_routing_config(tmp_path))

    # Image turn routes to chat_images with its own fallback chain.
    assert config.model_name_for_role("chat", images=True) == "vision"
    assert config.model_names_for_role("chat", images=True) == [
        "vision",
        "vision-backup",
    ]


def test_chat_images_image_routing_noop_when_chat_model_has_vision(
    tmp_path: Path,
) -> None:
    # Scope override picks a vision-capable chat model -> image routing is
    # suppressed; text and image turns both use the overridden model.
    config = load_model_config(_image_routing_config(tmp_path))
    scope = Scope(channel_id="channel-1")

    assert config.chat_model_name(scope, images=False) == "channel-vision"
    assert config.chat_model_name(scope, images=True) == "channel-vision"
    # The overridden vision model is the primary; its transient-failure chain
    # draws from the image-capable subset of the global chat_fallbacks (not
    # chat_images_fallbacks), because text-only fallbacks would strip image_input
    # from the failover wrapper's capability intersection.
    assert config.model_names_for_role("chat", scope, images=True) == [
        "channel-vision",
        "vision",
    ]


def test_chat_images_models_are_reachable(tmp_path: Path) -> None:
    config = load_model_config(_image_routing_config(tmp_path))

    names = config.reachable_model_names(include_compaction=False)
    assert "vision" in names
    assert "vision-backup" in names
    assert "text-only" in names


def test_override_matching_chat_images_model_is_not_a_redirect(tmp_path: Path) -> None:
    # A scope override that pins the very model chat_images names is a SUPPRESSED
    # redirect (the scoped model is image-capable), not a redirect: the chain must
    # draw from the image-capable subset of chat_fallbacks, never from
    # chat_images_fallbacks by name coincidence.
    config = load_model_config(_image_routing_config(tmp_path))
    scope = Scope(channel_id="channel-2")

    assert config.chat_model_name(scope, images=True) == "vision"
    assert config.model_names_for_role("chat", scope, images=True) == ["vision"]


def test_image_turn_chain_drops_text_only_fallbacks(tmp_path: Path) -> None:
    # Image turns may use only image-capable fallbacks. A text-only fallback
    # removes image_input from the failover chain's capability intersection.
    path = _write_config(
        tmp_path / "models.yaml",
        """
providers:
  main:
    type: openai_compat
    base_url: https://llm-gateway.example.invalid/v1
    api_key_env: MODEL_API_KEY
models:
  vision-primary: { provider: main, model: vp, capabilities: [text, image_input] }
  text-a: { provider: main, model: ta }
  text-b: { provider: main, model: tb }
  vision-backup: { provider: main, model: vb, capabilities: [text, image_input] }
roles:
  chat: vision-primary
  chat_fallbacks: [text-a, text-b, vision-backup]
  chat_images: vision-backup
  chat_images_fallbacks: []
  compaction: text-a
""",
    )
    config = load_model_config(path)

    # Text turns keep the full chain.
    assert config.model_names_for_role("chat", images=False) == [
        "vision-primary",
        "text-a",
        "text-b",
        "vision-backup",
    ]
    # Image turns stay on the image-capable primary with only image-capable
    # fallbacks, so the chain's capability intersection retains image_input.
    assert config.model_names_for_role("chat", images=True) == [
        "vision-primary",
        "vision-backup",
    ]


def test_chat_images_missing_vision_capability_rejected(tmp_path: Path) -> None:
    with pytest.raises(
        ValidationError,
        match=r"roles.chat_images references model 'text-only' which lacks",
    ):
        load_model_config(
            _write_config(
                tmp_path / "models.yaml",
                """
providers:
  main:
    type: openai_compat
    base_url: https://llm-gateway.example.invalid/v1
    api_key_env: MODEL_API_KEY
models:
  text-only: { provider: main, model: text-model }
roles:
  chat: text-only
  chat_images: text-only
  compaction: text-only
""",
            )
        )


def test_chat_images_fallback_missing_vision_capability_rejected(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        ValidationError,
        match=r"roles.chat_images_fallbacks\[0\] references model 'text-only' which lacks",
    ):
        load_model_config(
            _write_config(
                tmp_path / "models.yaml",
                """
providers:
  main:
    type: openai_compat
    base_url: https://llm-gateway.example.invalid/v1
    api_key_env: MODEL_API_KEY
models:
  text-only: { provider: main, model: text-model }
  vision: { provider: main, model: vision-model, capabilities: [text, image_input] }
roles:
  chat: text-only
  chat_images: vision
  chat_images_fallbacks: [text-only]
  compaction: text-only
""",
            )
        )


def test_chat_images_unset_keeps_single_chat_chain(tmp_path: Path) -> None:
    config = load_model_config(
        _write_config(
            tmp_path / "models.yaml",
            """
providers:
  main:
    type: openai_compat
    base_url: https://llm-gateway.example.invalid/v1
    api_key_env: MODEL_API_KEY
models:
  text-only: { provider: main, model: text-model }
roles:
  chat: text-only
  compaction: text-only
""",
        )
    )
    assert config.roles.chat_images is None
    # images flag is a no-op when chat_images is unset.
    assert config.model_name_for_role("chat", images=True) == "text-only"
    assert config.model_names_for_role("chat", images=True) == ["text-only"]


def test_provider_manager_resolves_image_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from providers import FailoverProvider

    config = load_model_config(_image_routing_config(tmp_path))

    class DummyProvider:
        capabilities: set[Any] = set()

        def __init__(self, model: str) -> None:
            self.model = model

    def fake_create(provider_config: Any) -> DummyProvider:
        return DummyProvider(provider_config.model)

    monkeypatch.setattr(provider_runtime, "create_provider", fake_create)
    manager = provider_runtime.ProviderManager(
        settings=_settings(model_api_key="key"),
        model_config=config,
    )

    # Image turn resolves to a distinct failover chain on the vision models.
    image_chat = manager.resolve("chat", None, images=True)
    assert isinstance(image_chat, FailoverProvider)
    assert [p.model for p in image_chat._providers] == ["vision-model", "vision-backup"]

    # Text turn resolves to its own chain and does not get clobbered over main.
    text_chat = manager.resolve("chat", None, images=False)
    assert isinstance(text_chat, FailoverProvider)
    assert [p.model for p in text_chat._providers] == [
        "text-model",
        "text-backup",
        "vision-model",
    ]
    # main is only assigned for the default (non-image) chat resolution.
    assert manager.main is text_chat
    assert manager.main is not image_chat


def _keyless_config(tmp_path: Path, extra: str = "") -> Path:
    return _write_config(
        tmp_path / "models.yaml",
        f"""
providers:
  gateway:
    type: anthropic_compat
    base_url: http://localhost:8080/v1/ccflare/anthropic
    keyless: true{extra}
models:
  chat:
    provider: gateway
    model: anthropic/claude-opus-5
roles:
  chat: chat
  compaction: chat
""",
    )


def test_keyless_profile_satisfies_the_credential_gate(tmp_path: Path) -> None:
    config = load_model_config(_keyless_config(tmp_path))

    resolved = resolve_provider_config(config, "chat", settings=_settings())
    assert resolved.keyless is True
    assert resolved.api_key == ""

    # No local key exists for a gateway that injects its own OAuth credentials;
    # the startup gate must not read that as "no LLM credentials configured".
    manager = provider_runtime.ProviderManager(settings=_settings(), model_config=config)
    assert manager.has_active_llm_credentials() is True


def test_keyless_profile_rejects_api_key_env(tmp_path: Path) -> None:
    with pytest.raises(ValidationError) as exc:
        load_model_config(_keyless_config(tmp_path, extra="\n    api_key_env: ANTHROPIC_API_KEY"))

    assert "must not set api_key_env" in str(exc.value)


def test_keyless_profile_requires_base_url(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path / "models.yaml",
        """
providers:
  gateway:
    type: anthropic_compat
    keyless: true
models:
  chat:
    provider: gateway
    model: anthropic/claude-opus-5
roles:
  chat: chat
  compaction: chat
""",
    )

    with pytest.raises(ValidationError) as exc:
        load_model_config(path)

    assert "must set base_url" in str(exc.value)


def test_prompt_caching_defaults_on_and_is_overridable(tmp_path: Path) -> None:
    config = load_model_config(_keyless_config(tmp_path))
    assert resolve_provider_config(config, "chat", settings=_settings()).anthropic_prompt_caching

    opted_out = load_model_config(_keyless_config(tmp_path, extra="\n    prompt_caching: false"))
    resolved = resolve_provider_config(opted_out, "chat", settings=_settings())
    assert resolved.anthropic_prompt_caching is False


def _ccflare_config(tmp_path: Path, *, effort: str = "low", after_tools: str = "") -> Path:
    escalation = f"\n    reasoning_after_tools:\n{after_tools}" if after_tools else ""
    return _write_config(
        tmp_path / "models.yaml",
        f"""
providers:
  gateway:
    type: anthropic_compat
    base_url: http://localhost:8080/v1/ccflare/anthropic
    keyless: true
    reasoning_effort: {effort}
models:
  chat:
    provider: gateway
    model: anthropic/claude-opus-5{escalation}
roles:
  chat: chat
  compaction: chat
""",
    )


def test_anthropic_compat_profile_effort_reaches_provider_config(tmp_path: Path) -> None:
    config = load_model_config(_ccflare_config(tmp_path))

    resolved = resolve_provider_config(config, "chat", settings=_settings())
    assert resolved.anthropic_effort == "low"


def test_anthropic_compat_rejects_effort_outside_anthropic_ladder(tmp_path: Path) -> None:
    # "ultra" is valid in the agent's internal ladder but not Anthropic's, and a
    # bad effort is a deterministic 400 that never fails over.
    with pytest.raises(ValidationError) as exc:
        load_model_config(_ccflare_config(tmp_path, effort="ultra"))

    assert "anthropic_compat" in str(exc.value)


def test_anthropic_compat_rejects_escalation_outside_anthropic_ladder(tmp_path: Path) -> None:
    with pytest.raises(ValidationError) as exc:
        load_model_config(_ccflare_config(tmp_path, after_tools="      ultra: [read_file]\n"))

    assert "reasoning_after_tools" in str(exc.value)


def test_anthropic_compat_accepts_supported_escalation(tmp_path: Path) -> None:
    config = load_model_config(_ccflare_config(tmp_path, after_tools="      high: [read_file]\n"))

    assert config.models["chat"].reasoning_after_tools == {"high": ["read_file"]}


def _selectable_models_config(tmp_path: Path) -> ModelConfig:
    return load_model_config(
        _write_config(
            tmp_path / "models.yaml",
            """
providers:
  main:
    type: openai_compat
    base_url: https://llm-gateway.example.invalid/v1
    api_key_env: MODEL_API_KEY
models:
  default:
    provider: main
    model: default-id
    capabilities: [text, tool_calling]
  alternate:
    provider: main
    model: alternate-id
    capabilities: [text, tool_calling]
roles:
  chat: default
  compaction: default
selectable_chat_models: [default, alternate]
overrides:
  guilds: { "guild-1": { chat: default } }
""",
        )
    )


def test_selectable_models_reject_unknown_or_non_tool_model(tmp_path: Path) -> None:
    text = """
providers:
  main: { type: openai_compat, base_url: https://example.test/v1, keyless: true }
models:
  text-only: { provider: main, model: text, capabilities: [text] }
roles: { chat: text-only, compaction: text-only }
selectable_chat_models: [text-only]
"""
    with pytest.raises(ValidationError, match="missing capabilities tool_calling"):
        load_model_config(_write_config(tmp_path / "models.yaml", text))

    with pytest.raises(ValidationError, match="unknown model 'missing'"):
        load_model_config(
            _write_config(
                tmp_path / "models.yaml",
                text.replace("[text-only]", "[missing]"),
            )
        )


def test_global_selection_switches_existing_and_new_scopes_without_restart(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _selectable_models_config(tmp_path)

    class DummyProvider:
        capabilities: set[Any] = set()

        def __init__(self, model: str) -> None:
            self.model = model

    monkeypatch.setattr(
        provider_runtime,
        "create_provider",
        lambda provider_config: DummyProvider(provider_config.model),
    )
    manager = provider_runtime.ProviderManager(
        settings=_settings(model_api_key="key"),
        model_config=config,
    )
    existing_scope = Scope(guild_id="guild-1", channel_id="channel-1")
    existing_before = manager.resolve("chat", existing_scope)
    assert existing_before.model == "default-id"

    manager.set_active_chat_model("alternate")

    assert manager.resolve("chat", existing_scope).model == "alternate-id"
    assert (
        manager.resolve("chat", Scope(guild_id="guild-2", channel_id="channel-2")).model
        == "alternate-id"
    )
    assert manager.resolved_chat_model_name(existing_scope) == "alternate"
    assert existing_before.model == "default-id"

    manager.set_active_chat_model(None)
    assert manager.resolve("chat", existing_scope).model == "default-id"


def test_global_selection_rejects_models_outside_operator_list(tmp_path: Path) -> None:
    manager = provider_runtime.ProviderManager(
        settings=_settings(model_api_key="key"),
        model_config=_selectable_models_config(tmp_path),
    )

    with pytest.raises(ValueError, match="not operator-selectable"):
        manager.set_active_chat_model("missing")


@pytest.mark.asyncio
async def test_model_catalog_filters_choices_and_deduplicates_shared_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = ModelConfig.model_validate(
        {
            "providers": {
                "openai": {
                    "type": "openai_compat",
                    "base_url": "https://example.test/v1",
                    "api_key_env": "MODEL_API_KEY",
                    "models_endpoint": "https://example.test/v1/models",
                },
                "anthropic": {
                    "type": "anthropic_compat",
                    "base_url": "https://example.test/v1",
                    "api_key_env": "MODEL_API_KEY",
                    "models_endpoint": "https://example.test/v1/models",
                },
                "static": {
                    "type": "openai_compat",
                    "base_url": "https://static.test/v1",
                    "keyless": True,
                },
            },
            "models": {
                "available": {
                    "provider": "openai",
                    "model": "available-id",
                    "capabilities": ["text", "tool_calling"],
                },
                "missing": {
                    "provider": "anthropic",
                    "model": "missing-id",
                    "capabilities": ["text", "tool_calling"],
                },
                "static": {
                    "provider": "static",
                    "model": "static-id",
                    "capabilities": ["text", "tool_calling"],
                },
            },
            "roles": {"chat": "available", "compaction": "available"},
            "selectable_chat_models": ["available", "missing", "static"],
        }
    )
    calls: list[tuple[str, str]] = []

    async def fake_fetch(endpoint: str, api_key: str) -> frozenset[str]:
        calls.append((endpoint, api_key))
        return frozenset({"available-id"})

    monkeypatch.setattr(provider_runtime, "_fetch_model_ids", fake_fetch)
    manager = provider_runtime.ProviderManager(
        settings=_settings(model_api_key="key"),
        model_config=config,
    )

    await manager.refresh_selectable_chat_models()

    assert calls == [("https://example.test/v1/models", "key")]
    assert manager.selectable_chat_models == ("available", "static")


@pytest.mark.asyncio
async def test_model_catalog_failure_hides_catalog_backed_choices(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _selectable_models_config(tmp_path)
    config.providers["main"].models_endpoint = "https://example.test/v1/models"

    async def failed_fetch(_endpoint: str, _api_key: str) -> frozenset[str]:
        raise ValueError("bad catalog")

    monkeypatch.setattr(provider_runtime, "_fetch_model_ids", failed_fetch)
    manager = provider_runtime.ProviderManager(
        settings=_settings(model_api_key="key"),
        model_config=config,
    )

    await manager.refresh_selectable_chat_models()

    assert manager.selectable_chat_models == ()


def _persona_config_text(persona_lines: str) -> str:
    return f"""
providers:
  main:
    type: openai_compat
    base_url: https://llm-gateway.example.invalid/v1
    api_key_env: MODEL_API_KEY
models:
  primary: {{ provider: main, model: Kimi-K2.6 }}
  backup: {{ provider: main, model: backup-model }}
roles:
  chat: primary
  compaction: primary
{persona_lines}"""


def test_role_names_match_declared_fields() -> None:
    """Guards the invariant the role helpers derive from.

    RoleAssignments' fields are the role schema, so a new non-role string field
    would silently be read as a role. Adding a role means updating this tuple.
    """
    from config.model_config import RoleAssignments

    assert RoleAssignments.role_names() == (
        "chat",
        "chat_images",
        "compaction",
        "persona",
        "coding",
    )


def test_coding_role_and_fallback_require_text_and_tools(tmp_path: Path) -> None:
    text = """
providers:
  main: { type: openai_compat, base_url: https://example.test/v1, keyless: true }
models:
  capable: { provider: main, model: capable, capabilities: [text, tool_calling] }
  text-only: { provider: main, model: text-only, capabilities: [text] }
roles:
  chat: capable
  compaction: capable
  coding: text-only
"""
    with pytest.raises(ValidationError, match=r"roles\.coding.*tool_calling"):
        load_model_config(_write_config(tmp_path / "primary.yaml", text))

    fallback_text = text.replace(
        "coding: text-only", "coding: capable\n  coding_fallbacks: [text-only]"
    )
    with pytest.raises(ValidationError, match=r"coding_fallbacks\[0\].*tool_calling"):
        load_model_config(_write_config(tmp_path / "fallback.yaml", fallback_text))


def test_coding_models_are_reachable_only_when_feature_is_enabled(tmp_path: Path) -> None:
    config = load_model_config(
        _write_config(
            tmp_path / "models.yaml",
            """
providers:
  main: { type: openai_compat, base_url: https://example.test/v1, keyless: true }
models:
  chat: { provider: main, model: chat, capabilities: [text, tool_calling] }
  coding: { provider: main, model: coding, capabilities: [text, tool_calling] }
  coding-backup: { provider: main, model: backup, capabilities: [text, tool_calling] }
roles:
  chat: chat
  compaction: chat
  coding: coding
  coding_fallbacks: [coding-backup]
""",
        )
    )

    assert "coding" not in config.reachable_model_names(include_compaction=True)
    assert {"coding", "coding-backup"} <= config.reachable_model_names(
        include_compaction=True, include_coding=True
    )


def test_persona_role_is_optional(tmp_path: Path) -> None:
    config = load_model_config(_write_config(tmp_path / "models.yaml", _persona_config_text("")))

    assert config.roles.persona is None
    with pytest.raises(ValueError, match="persona"):
        config.model_names_for_role("persona")


def test_persona_role_resolves_into_ordered_chain(tmp_path: Path) -> None:
    config = load_model_config(
        _write_config(
            tmp_path / "models.yaml",
            _persona_config_text("  persona: primary\n  persona_fallbacks: [backup]\n"),
        )
    )

    assert config.model_name_for_role("persona") == "primary"
    assert config.model_names_for_role("persona") == ["primary", "backup"]


def test_unknown_persona_model_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match=r"roles\.persona references unknown model"):
        load_model_config(
            _write_config(tmp_path / "models.yaml", _persona_config_text("  persona: ghost\n"))
        )


def test_unknown_persona_fallback_model_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match=r"persona_fallbacks\[0\] references unknown model"):
        load_model_config(
            _write_config(
                tmp_path / "models.yaml",
                _persona_config_text("  persona: primary\n  persona_fallbacks: [ghost]\n"),
            )
        )


def test_persona_models_are_not_startup_gated(tmp_path: Path) -> None:
    """An optional feature must not be able to abort boot.

    Startup exits when a reachable model has no credential, so a persona-only
    model stays out of that set and a missing key surfaces on first use instead.
    """
    config = load_model_config(
        _write_config(
            tmp_path / "models.yaml",
            _persona_config_text("  persona: backup\n"),
        )
    )

    assert "backup" not in config.reachable_model_names(include_compaction=True)


def test_persona_chain_wraps_in_failover(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from providers import FailoverProvider

    config = load_model_config(
        _write_config(
            tmp_path / "models.yaml",
            _persona_config_text("  persona: primary\n  persona_fallbacks: [backup]\n"),
        )
    )

    class DummyProvider:
        capabilities: set[Any] = set()

        def __init__(self, model: str) -> None:
            self.model = model

    monkeypatch.setattr(
        provider_runtime,
        "create_provider",
        lambda provider_config: DummyProvider(provider_config.model),
    )
    manager = provider_runtime.ProviderManager(
        settings=_settings(model_api_key="key"),
        model_config=config,
    )

    persona = manager.ensure_persona()

    assert isinstance(persona, FailoverProvider)
    assert [p.model for p in persona._providers] == ["Kimi-K2.6", "backup-model"]
    # Cached like every other role, so repeat turns reuse one provider.
    assert manager.ensure_persona() is persona
