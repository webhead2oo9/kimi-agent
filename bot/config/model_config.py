from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from branding import provider_identity
from providers.factory import ProviderConfig, SUPPORTED_PROVIDER_NAMES
from providers.failure_policy import failure_adapter_names
from providers.types import REASONING_EFFORT_ORDER

_API_KEY_SETTINGS_FIELDS = {
    "MODEL_API_KEY": "model_api_key",
    "OPENCODE_GO_API_KEY": "opencode_go_api_key",
    "RUNINFRA_GATEWAY_KEY": "runinfra_gateway_key",
    "ANTHROPIC_API_KEY": "anthropic_api_key",
    "GROK_API_KEY": "grok_api_key",
    "FIREWORKS_API_KEY": "fireworks_api_key",
    "ZAI_API_KEY": "zai_api_key",
    "KIMI_CODING_API_KEY": "kimi_coding_api_key",
    "COMPACTION_API_KEY": "compaction_api_key",
}
# Derive parser support from the settings-field map so every accepted environment
# name resolves to a Settings secret field.
SUPPORTED_API_KEY_ENVS = frozenset(_API_KEY_SETTINGS_FIELDS)
_REASONING_EFFORT_PROVIDER_TYPES = frozenset({"codex", "anthropic_compat", "openai_responses"})
_PROFILE_REASONING_EFFORT_PROVIDER_TYPES = _REASONING_EFFORT_PROVIDER_TYPES | {"openai_compat"}
# Anthropic's `output_config.effort` ladder. Narrower than REASONING_EFFORT_ORDER,
# and a value outside it is a deterministic 400 that never fails over.
ANTHROPIC_EFFORT_LEVELS = frozenset({"low", "medium", "high", "xhigh", "max"})


@dataclass(frozen=True)
class Scope:
    guild_id: str | None = None
    channel_id: str | None = None
    user_id: str | None = None
    command: str | None = None


class CircuitBreakerPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outage_cooldown_seconds: float = Field(default=300.0, gt=0)
    quota_cooldown_seconds: float = Field(default=1800.0, gt=0)
    rate_limit_cooldown_seconds: float = Field(default=60.0, gt=0)


class ProviderProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    base_url: str = ""
    api_key_env: str = ""
    # Optional OpenAI-compatible model catalog used to filter owner-selectable
    # models at startup. Multiple profiles may share one endpoint; discovery
    # de-duplicates those requests by endpoint and credential.
    models_endpoint: str = ""
    # The endpoint supplies its own upstream credentials (a local gateway holding
    # OAuth accounts, e.g. ccflare), so no API key is read or required.
    keyless: bool = False
    # Send an Anthropic prompt-cache breakpoint on the final message block. On by
    # default; set false for a gateway that rejects cache_control or bills cache
    # writes at a rate that outweighs the reads.
    prompt_caching: bool = True
    provider_routing: dict[str, Any] = Field(default_factory=dict)
    # Empty inherits BOT_NAME when the profile is resolved. Operators can set a
    # distinct provider-facing identity explicitly when needed.
    app_name: str = ""
    app_url: str = ""
    service_tier: str = ""
    timeout_seconds: float = 900.0
    # Optional hard output-token ceiling imposed by the endpoint. This is kept
    # on the provider profile so it applies to every model using that gateway,
    # without lowering the global limit for other providers.
    max_output_tokens: int | None = Field(default=None, gt=0)
    # Optional per-request tracing header used by OpenAI-compatible gateways.
    request_id_header: str = ""
    # Optional per-profile reasoning-effort default. Codex and openai_responses
    # send it as ``reasoning.effort``; anthropic_compat sends it as
    # ``output_config.effort``; openai_compat sends it for DeepSeek thinking
    # turns. This lets models share one credential-bearing endpoint at different
    # depths.
    reasoning_effort: str = ""
    failure_adapter: str = "generic"
    circuit_breaker: CircuitBreakerPolicy = Field(default_factory=lambda: CircuitBreakerPolicy())

    @field_validator("type")
    @classmethod
    def _known_provider_type(cls, value: str) -> str:
        if value not in SUPPORTED_PROVIDER_NAMES:
            supported = ", ".join(sorted(SUPPORTED_PROVIDER_NAMES))
            raise ValueError(f"unknown provider type {value!r}; expected one of: {supported}")
        return value

    @field_validator("api_key_env")
    @classmethod
    def _supported_api_key_env(cls, value: str) -> str:
        if value and value not in SUPPORTED_API_KEY_ENVS:
            supported = ", ".join(sorted(SUPPORTED_API_KEY_ENVS))
            raise ValueError(f"unsupported api_key_env {value!r}; expected one of: {supported}")
        return value

    @field_validator("failure_adapter")
    @classmethod
    def _known_failure_adapter(cls, value: str) -> str:
        if value not in failure_adapter_names():
            supported = ", ".join(sorted(failure_adapter_names()))
            raise ValueError(f"unknown failure_adapter {value!r}; expected one of: {supported}")
        return value

    @field_validator("reasoning_effort")
    @classmethod
    def _supported_reasoning_effort(cls, value: str) -> str:
        normalized = value.strip().lower()
        supported = {"", *REASONING_EFFORT_ORDER}
        if normalized not in supported:
            choices = ", ".join(sorted(supported - {""}))
            raise ValueError(f"unsupported reasoning_effort {value!r}; expected one of: {choices}")
        return normalized

    @model_validator(mode="after")
    def _reasoning_effort_supported_by_type(self) -> ProviderProfile:
        if not self.reasoning_effort:
            return self
        if self.type not in _PROFILE_REASONING_EFFORT_PROVIDER_TYPES:
            supported = ", ".join(sorted(_PROFILE_REASONING_EFFORT_PROVIDER_TYPES))
            raise ValueError(f"reasoning_effort is only supported for provider types: {supported}")
        if self.type == "anthropic_compat" and self.reasoning_effort not in ANTHROPIC_EFFORT_LEVELS:
            supported = ", ".join(sorted(ANTHROPIC_EFFORT_LEVELS))
            raise ValueError(
                f"unsupported reasoning_effort {self.reasoning_effort!r} for "
                f"provider type 'anthropic_compat'; expected one of: {supported}"
            )
        return self

    @model_validator(mode="after")
    def _keyless_profile_is_a_gateway(self) -> ProviderProfile:
        # A keyless profile bypasses the startup credential gate, so it must name
        # the gateway that holds the real credentials rather than defaulting to a
        # vendor endpoint we would then call unauthenticated.
        if not self.keyless:
            return self
        if self.api_key_env:
            raise ValueError("keyless profiles must not set api_key_env")
        if not self.base_url:
            raise ValueError("keyless profiles must set base_url")
        return self


class ModelPricing(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input: float | None = None
    output: float | None = None
    cached_read: float | None = None
    cache_write: float | None = None

    @field_validator("input", "output", "cached_read", "cache_write")
    @classmethod
    def _non_negative(cls, value: float | None) -> float | None:
        if value is not None and value < 0:
            raise ValueError("pricing rates must be >= 0")
        return value


class ModelEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str
    context_window: int = 0
    capabilities: list[str] = Field(default_factory=list)
    pricing: ModelPricing | None = None
    # Model-specific, monotonic reasoning escalation. Keys are effort levels;
    # values are tool names that raise subsequent ReAct iterations to at least
    # that effort for the remainder of the current turn.
    reasoning_after_tools: dict[str, list[str]] = Field(default_factory=dict)

    @field_validator("context_window")
    @classmethod
    def _non_negative_context_window(cls, value: int) -> int:
        if value < 0:
            raise ValueError("context_window must be >= 0")
        return value

    @field_validator("reasoning_after_tools")
    @classmethod
    def _valid_reasoning_after_tools(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        normalized: dict[str, list[str]] = {}
        for raw_effort, raw_tools in value.items():
            effort = raw_effort.strip().lower()
            if effort not in REASONING_EFFORT_ORDER:
                supported = ", ".join(REASONING_EFFORT_ORDER)
                raise ValueError(
                    f"unsupported reasoning_after_tools effort {raw_effort!r}; "
                    f"expected one of: {supported}"
                )
            if effort in normalized:
                raise ValueError(f"reasoning_after_tools contains duplicate effort {effort!r}")
            tools = [name.strip() for name in raw_tools]
            if not tools or any(not name for name in tools):
                raise ValueError(
                    f"reasoning_after_tools.{effort} must contain non-empty tool names"
                )
            if len(tools) != len(set(tools)):
                raise ValueError(f"reasoning_after_tools.{effort} contains duplicate tool names")
            normalized[effort] = tools
        return normalized


class RoleAssignments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Each role's `<role>_fallbacks` is an ordered list of alternate model names
    # tried, in order, only when the primary raises a transient availability error
    # (connection drop, timeout, 429, 5xx). See docs/providers.md "Failover".
    chat: str
    chat_fallbacks: list[str] = Field(default_factory=list)
    # Optional: route image turns to a dedicated vision model when ``chat``
    # lacks the ``image_input`` capability; text-only turns keep using ``chat``.
    # Scope overrides on ``chat`` that pick a vision-capable model suppress this
    # routing. Unset means the single ``chat`` model handles every turn. See
    # docs/providers.md "Image routing".
    chat_images: str | None = None
    chat_images_fallbacks: list[str] = Field(default_factory=list)
    compaction: str
    compaction_fallbacks: list[str] = Field(default_factory=list)
    # Optional: compiles user persona overrides. Unset leaves persona_set,
    # persona_show, and persona_clear unregistered. See docs/persona.md.
    persona: str | None = None
    persona_fallbacks: list[str] = Field(default_factory=list)
    # Optional durable background coding agent. Unset keeps existing deployments
    # unchanged and leaves every coding-task control unregistered.
    coding: str | None = None
    coding_fallbacks: list[str] = Field(default_factory=list)

    # The fields above are the role schema: each is either `<role>` or
    # `<role>_fallbacks`, and `extra="forbid"` turns an unknown role key into a
    # startup failure rather than a silent ignore. The helpers derive role names
    # from these fields so validation, assignment, and fallback handling share
    # one schema.
    # test_role_names_match_declared_fields guards the invariant.
    @classmethod
    def role_names(cls) -> tuple[str, ...]:
        return tuple(name for name in cls.model_fields if not name.endswith("_fallbacks"))

    def assigned_roles(self) -> dict[str, str]:
        """Role -> model name for every role this file actually assigns."""
        return {
            name: value
            for name in self.role_names()
            if isinstance(value := getattr(self, name), str) and value
        }

    def model_for_role(self, role: str) -> str:
        if role not in self.role_names():
            raise ValueError(f"Unknown model role: {role}")
        model_name = getattr(self, role)
        if not model_name:
            raise ValueError(f"Model role {role!r} is not configured")
        return str(model_name)

    def fallbacks_for(self, role: str) -> list[str]:
        fallbacks = getattr(self, f"{role}_fallbacks", None)
        return list(fallbacks) if isinstance(fallbacks, list) else []


class ChatOverride(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chat: str


class ScopeOverrides(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channels: dict[str, ChatOverride] = Field(default_factory=dict)
    guilds: dict[str, ChatOverride] = Field(default_factory=dict)
    users: dict[str, ChatOverride] = Field(default_factory=dict)
    commands: dict[str, ChatOverride] = Field(default_factory=dict)

    def model_names(self) -> set[str]:
        names: set[str] = set()
        for group in (self.channels, self.guilds, self.users, self.commands):
            names.update(override.chat for override in group.values())
        return names


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    providers: dict[str, ProviderProfile]
    models: dict[str, ModelEntry]
    roles: RoleAssignments
    overrides: ScopeOverrides = Field(default_factory=ScopeOverrides)
    selectable_chat_models: list[str] = Field(default_factory=list)

    @field_validator("selectable_chat_models")
    @classmethod
    def _validate_selectable_chat_models(cls, value: list[str]) -> list[str]:
        names = [name.strip() for name in value]
        if any(not name for name in names):
            raise ValueError("selectable_chat_models entries must not be blank")
        if len(names) != len(set(names)):
            raise ValueError("selectable_chat_models entries must be unique")
        if len(names) > 120:
            raise ValueError("selectable_chat_models supports at most 120 entries")
        return names

    @model_validator(mode="after")
    def _validate_references(self) -> ModelConfig:
        for model_name, entry in self.models.items():
            if entry.provider not in self.providers:
                raise ValueError(
                    f"models.{model_name}.provider references unknown provider {entry.provider!r}"
                )
            provider_type = self.providers[entry.provider].type
            if (
                entry.reasoning_after_tools
                and provider_type not in _REASONING_EFFORT_PROVIDER_TYPES
            ):
                supported = ", ".join(sorted(_REASONING_EFFORT_PROVIDER_TYPES))
                raise ValueError(
                    f"models.{model_name}.reasoning_after_tools is only supported "
                    f"for provider types: {supported}"
                )
            if provider_type == "anthropic_compat":
                unsupported = sorted(set(entry.reasoning_after_tools) - ANTHROPIC_EFFORT_LEVELS)
                if unsupported:
                    # An escalation into an effort Anthropic rejects is a
                    # deterministic 400 mid-turn, which never fails over.
                    supported = ", ".join(sorted(ANTHROPIC_EFFORT_LEVELS))
                    raise ValueError(
                        f"models.{model_name}.reasoning_after_tools has efforts "
                        f"{unsupported} unsupported by provider type 'anthropic_compat'; "
                        f"expected one of: {supported}"
                    )
        for role_name, model_name in self.roles.assigned_roles().items():
            self._require_model(f"roles.{role_name}", model_name)
        if self.roles.chat_images is not None:
            self._require_image_capability("roles.chat_images", self.roles.chat_images)
        if self.roles.coding is not None:
            self._require_capabilities("roles.coding", self.roles.coding, {"text", "tool_calling"})
        for role_name in self.roles.role_names():
            for index, model_name in enumerate(self.roles.fallbacks_for(role_name)):
                self._require_model(f"roles.{role_name}_fallbacks[{index}]", model_name)
        if self.roles.chat_images is not None:
            for index, model_name in enumerate(self.roles.chat_images_fallbacks):
                self._require_image_capability(f"roles.chat_images_fallbacks[{index}]", model_name)
        if self.roles.coding is not None:
            for index, model_name in enumerate(self.roles.coding_fallbacks):
                self._require_capabilities(
                    f"roles.coding_fallbacks[{index}]",
                    model_name,
                    {"text", "tool_calling"},
                )
        for model_name in self.selectable_chat_models:
            self._require_model("selectable_chat_models", model_name)
            capabilities = set(self.models[model_name].capabilities)
            missing = {"text", "tool_calling"} - capabilities
            if missing:
                raise ValueError(
                    f"selectable chat model {model_name!r} is missing capabilities "
                    + ", ".join(sorted(missing))
                )
        for group_name, group in (
            ("channels", self.overrides.channels),
            ("guilds", self.overrides.guilds),
            ("users", self.overrides.users),
            ("commands", self.overrides.commands),
        ):
            for scope_key, override in group.items():
                self._require_model(f"overrides.{group_name}.{scope_key}.chat", override.chat)
        return self

    def _require_model(self, path: str, model_name: str) -> None:
        if model_name not in self.models:
            raise ValueError(f"{path} references unknown model {model_name!r}")

    def _require_image_capability(self, path: str, model_name: str) -> None:
        if "image_input" not in self.models[model_name].capabilities:
            raise ValueError(
                f"{path} references model {model_name!r} which lacks the "
                f"'image_input' capability required to serve image turns"
            )

    def _require_capabilities(self, path: str, model_name: str, required: set[str]) -> None:
        missing = required - set(self.models[model_name].capabilities)
        if missing:
            raise ValueError(
                f"{path} references model {model_name!r} which lacks required "
                f"capabilities: {', '.join(sorted(missing))}"
            )

    def model_supports_image_input(self, model_name: str) -> bool:
        return "image_input" in self.models[model_name].capabilities

    def model_name_for_role(
        self, role: str, scope: Scope | None = None, *, images: bool = False
    ) -> str:
        if role == "chat":
            return self.chat_model_name(scope, images=images)
        return self.roles.model_for_role(role)

    def model_names_for_role(
        self, role: str, scope: Scope | None = None, *, images: bool = False
    ) -> list[str]:
        """Ordered [primary, *fallbacks] chain for a role, deduped preserving order.

        The primary honors scope overrides; fallbacks are appended from the role's
        `<role>_fallbacks` list. For the chat role with ``images=True``, image
        routing uses the configured image chain. A single-element result means no
        failover.
        """
        if role == "chat":
            return self.model_names_for_selected_chat(
                self._scope_chat_model_name(scope),
                images=images,
            )
        return list(
            dict.fromkeys([self.roles.model_for_role(role), *self.roles.fallbacks_for(role)])
        )

    def model_names_for_selected_chat(
        self,
        model_name: str,
        *,
        images: bool = False,
    ) -> list[str]:
        """Route one selected chat primary through the normal chat fallback policy."""
        primary = model_name
        fallbacks = list(self.roles.chat_fallbacks)
        image_model = self.roles.chat_images
        if images and image_model is not None and not self.model_supports_image_input(primary):
            primary = image_model
            fallbacks = list(self.roles.chat_images_fallbacks)
        if images and self.model_supports_image_input(primary):
            fallbacks = [name for name in fallbacks if self.model_supports_image_input(name)]
        return list(dict.fromkeys([primary, *fallbacks]))

    def chat_model_name(self, scope: Scope | None = None, *, images: bool = False) -> str:
        base = self._scope_chat_model_name(scope)
        if images and self.roles.chat_images is not None and self._chat_images_redirected(scope):
            return self.roles.chat_images
        return base

    def _chat_images_redirected(self, scope: Scope | None) -> bool:
        """True when image routing redirects the scoped chat model to chat_images.

        Derived from the pre-routing model's capability, never from name equality
        with chat_images. A scope override that happens to pin the same model
        chat_images names is a suppressed redirect, not a redirect.
        """
        if self.roles.chat_images is None:
            return False
        return not self.model_supports_image_input(self._scope_chat_model_name(scope))

    def _scope_chat_model_name(self, scope: Scope | None) -> str:
        if scope is not None:
            if scope.command and scope.command in self.overrides.commands:
                return self.overrides.commands[scope.command].chat
            if scope.channel_id and scope.channel_id in self.overrides.channels:
                return self.overrides.channels[scope.channel_id].chat
            if scope.user_id and scope.user_id in self.overrides.users:
                return self.overrides.users[scope.user_id].chat
            if scope.guild_id and scope.guild_id in self.overrides.guilds:
                return self.overrides.guilds[scope.guild_id].chat
        return self.roles.chat

    def chat_model_names(self) -> set[str]:
        names = {
            self.roles.chat,
            *self.roles.chat_fallbacks,
            *self.selectable_chat_models,
            *self.overrides.model_names(),
        }
        if self.roles.chat_images is not None:
            names.add(self.roles.chat_images)
            names.update(self.roles.chat_images_fallbacks)
        return names

    def reachable_model_names(
        self,
        *,
        include_compaction: bool,
        include_coding: bool = False,
    ) -> set[str]:
        names = set(self.chat_model_names())
        if include_compaction:
            names.add(self.roles.compaction)
        if include_coding and self.roles.coding is not None:
            names.add(self.roles.coding)
            names.update(self.roles.coding_fallbacks)
        return names

    def profile_for_model(self, model_name: str) -> ProviderProfile:
        return self.providers[self.models[model_name].provider]


def parse_model_config_text(text: str) -> ModelConfig:
    """Parse and validate exact ``models.yaml`` text."""

    raw = yaml.safe_load(text) or {}
    if not isinstance(raw, dict):
        raise ValueError("config/models.yaml must contain a YAML mapping")
    return ModelConfig.model_validate(raw)


def load_model_config_with_revision(
    path: str | Path = "config/models.yaml",
) -> tuple[ModelConfig, str]:
    """Load model configuration and the exact source-text revision."""

    try:
        source = Path(path).read_bytes()
    except FileNotFoundError as exc:
        # The live file is untracked instance state, so a fresh checkout has the
        # template and no models.yaml. Say that here rather than surfacing a bare
        # FileNotFoundError from inside provider construction. Never fall back
        # to the template, which would boot the bot onto backends the operator
        # did not choose.
        raise FileNotFoundError(
            f"Model routing file not found: {path}. Copy "
            f"config/models.example.yaml to {path}, then replace its placeholders."
        ) from exc
    text = source.decode("utf-8")
    return parse_model_config_text(text), sha256(source).hexdigest()


def load_model_config(path: str | Path = "config/models.yaml") -> ModelConfig:
    return load_model_config_with_revision(path)[0]


def resolve_provider_config(
    model_config: ModelConfig,
    model_name: str,
    *,
    settings: Any,
) -> ProviderConfig:
    entry = model_config.models[model_name]
    profile = model_config.providers[entry.provider]
    api_key = _secret_from_settings(settings, profile.api_key_env)
    provider_name = profile.type
    configured_bot_name = str(getattr(settings, "bot_name", "")).strip()
    app_name = provider_identity(profile.app_name.strip() or configured_bot_name)
    return ProviderConfig(
        provider_name=provider_name,
        api_key=api_key,
        keyless=profile.keyless,
        anthropic_prompt_caching=profile.prompt_caching,
        anthropic_effort=(profile.reasoning_effort if provider_name == "anthropic_compat" else ""),
        openai_reasoning_effort=(
            profile.reasoning_effort
            if provider_name in {"openai_compat", "openai_responses"}
            else ""
        ),
        openai_request_id_header=(
            profile.request_id_header if provider_name == "openai_compat" else ""
        ),
        base_url=profile.base_url,
        model=entry.model,
        openai_service_tier=profile.service_tier,
        openai_timeout_seconds=profile.timeout_seconds,
        stream_stall_timeout_seconds=settings.provider_stream_stall_timeout_seconds,
        openrouter_provider_json=(
            json.dumps(profile.provider_routing) if profile.provider_routing else ""
        ),
        openrouter_app_url=profile.app_url,
        openrouter_app_name=app_name,
        user_agent=app_name,
        codex_token_file=settings.codex_token_file,
        codex_model=entry.model if provider_name == "codex" else settings.codex_model,
        codex_reasoning_effort=(profile.reasoning_effort or settings.codex_reasoning_effort),
        codex_image_quality=settings.codex_image_quality,
        codex_image_format=settings.codex_image_format,
        codex_ws_idle_timeout=settings.codex_ws_idle_timeout,
        codex_ws_read_timeout=settings.codex_ws_read_timeout,
        codex_verbose=settings.codex_verbose,
    )


def _secret_from_settings(settings: Any, env_name: str) -> str:
    if not env_name:
        return ""
    field_name = _API_KEY_SETTINGS_FIELDS[env_name]
    value = getattr(settings, field_name)
    get_secret_value = getattr(value, "get_secret_value", None)
    if callable(get_secret_value):
        return str(get_secret_value())
    return str(value or "")
