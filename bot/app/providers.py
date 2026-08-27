from __future__ import annotations

import asyncio
import inspect
import logging
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

import aiohttp

from agent.compaction import CompactionConfig, Compactor
from config.model_config import (
    ModelConfig,
    Scope,
    load_model_config,
    load_model_config_with_revision,
    resolve_provider_config,
)
from config.settings import Settings
from providers import LLMProvider, ProviderConfig, create_provider
from providers.circuit_breaker import (
    CircuitRecord,
    CircuitStore,
    CircuitTarget,
    ProviderCircuitBreaker,
)
from providers.failover import FailoverBackend, FailoverProvider
from providers.failure_policy import CooldownPolicy, get_failure_classifier
from providers.types import (
    ProviderCapability,
    ProviderRequest,
    ProviderResponse,
    ReasoningEscalation,
)

log = logging.getLogger(__name__)

_MODEL_DECLARED_CAPABILITIES = {
    ProviderCapability.TEXT,
    ProviderCapability.IMAGE_INPUT,
    ProviderCapability.IMAGE_OUTPUT,
    ProviderCapability.TOOL_CALLING,
}


def _normalized_origin(base_url: str) -> str:
    parsed = urlsplit(base_url)
    if not parsed.scheme or not parsed.netloc:
        return "default"
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


class CodexTokenSource(Protocol):
    async def get_access_token(self) -> str: ...


@dataclass(frozen=True)
class ContextWindowWarning:
    model_name: str
    model_id: str
    context_window: int


@dataclass
class ProviderManager:
    settings: Settings
    model_config: ModelConfig | None = None
    model_config_revision: str | None = None
    active_chat_model: str | None = None
    # Runtime-filtered subset of model_config.selectable_chat_models. None means
    # no configured catalog exists; an empty tuple means discovery ran but no
    # catalog-backed candidates were available.
    _discovered_selectable_chat_models: tuple[str, ...] | None = None
    # Optional lifecycle handles for resolved or injected providers; the normal
    # runtime path uses model_config + the cache below.
    main: LLMProvider | None = None
    compaction: LLMProvider | None = None
    persona: LLMProvider | None = None
    # Underlying single-model providers, one per model name (lifecycle owner).
    _providers: dict[str, LLMProvider] = field(default_factory=dict)
    # FailoverProvider wrappers, keyed by their ordered model-name chain. These
    # only reference _providers entries, so they are not closed independently.
    _chains: dict[tuple[str, ...], LLMProvider] = field(default_factory=dict)
    circuit_breaker: ProviderCircuitBreaker = field(default_factory=ProviderCircuitBreaker)

    def resolve(
        self, role: str, scope: Scope | None = None, *, images: bool = False
    ) -> LLMProvider:
        model_config = self.model_config
        if model_config is None:
            if role == "chat" and self.main is not None:
                return self.main
            raise RuntimeError("ProviderManager requires model_config to resolve providers")
        if role == "chat" and self.active_chat_model is not None:
            model_names = model_config.model_names_for_selected_chat(
                self.active_chat_model,
                images=images,
            )
        else:
            model_names = model_config.model_names_for_role(role, scope, images=images)
        links = [self._resolve_single(model_config, name) for name in model_names]
        key = tuple(model_names)
        provider = self._chains.get(key)
        if provider is None:
            provider = FailoverProvider(
                links,
                circuit_breaker=self.circuit_breaker,
                backends=[self._failover_backend(model_config, name) for name in model_names],
            )
            self._chains[key] = provider
        if role == "chat" and scope is None and not images:
            self.main = provider
        return provider

    def _failover_backend(self, model_config: ModelConfig, model_name: str) -> FailoverBackend:
        entry = model_config.models[model_name]
        profile = model_config.providers[entry.provider]
        origin = _normalized_origin(profile.base_url)
        credential_source = profile.api_key_env or ("keyless" if profile.keyless else profile.type)
        target = CircuitTarget.create(
            model_identity=f"{entry.provider}|{profile.type}|{origin}|{entry.model}",
            account_identity=f"{profile.type}|{origin}|{credential_source}",
            label=f"{entry.provider}/{entry.model}",
        )
        return FailoverBackend(
            target=target,
            classifier=get_failure_classifier(profile.failure_adapter),
            cooldown=CooldownPolicy(
                outage_seconds=profile.circuit_breaker.outage_cooldown_seconds,
                quota_seconds=profile.circuit_breaker.quota_cooldown_seconds,
            ),
        )

    async def initialize_circuits(self, store: CircuitStore) -> None:
        model_config = self.model_config
        keys: set[str] = set()
        if model_config is not None:
            for model_name in model_config.models:
                target = self._failover_backend(model_config, model_name).target
                keys.update((target.model_scope_key, target.account_scope_key))
        await self.circuit_breaker.initialize(store, keys)

    async def circuit_snapshots(self) -> tuple[CircuitRecord, ...]:
        return await self.circuit_breaker.snapshots()

    async def reset_all_circuits(self) -> None:
        await self.circuit_breaker.reset_all()

    @property
    def selectable_chat_models(self) -> tuple[str, ...]:
        if self.model_config is None:
            return ()
        if self._discovered_selectable_chat_models is not None:
            return self._discovered_selectable_chat_models
        return tuple(self.model_config.selectable_chat_models)

    async def refresh_selectable_chat_models(self) -> None:
        """Filter configured choices through their providers' live model catalogs."""
        model_config = self.model_config
        if model_config is None:
            self._discovered_selectable_chat_models = ()
            return
        candidates = tuple(model_config.selectable_chat_models)
        groups: dict[tuple[str, str], list[str]] = {}
        available = {
            name
            for name in candidates
            if not model_config.providers[model_config.models[name].provider].models_endpoint
        }
        for name in candidates:
            entry = model_config.models[name]
            profile = model_config.providers[entry.provider]
            endpoint = profile.models_endpoint.strip()
            if endpoint:
                groups.setdefault((endpoint, profile.api_key_env), []).append(name)
        if not groups:
            self._discovered_selectable_chat_models = None
            return
        for (endpoint, _api_key_env), names in groups.items():
            provider_config = resolve_provider_config(
                model_config,
                names[0],
                settings=self.settings,
            )
            try:
                model_ids = await _fetch_model_ids(endpoint, provider_config.api_key)
            except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
                log.warning("Could not refresh model catalog %s (%s)", endpoint, exc)
                continue
            available.update(name for name in names if model_config.models[name].model in model_ids)
        self._discovered_selectable_chat_models = tuple(
            name for name in candidates if name in available
        )
        log.info(
            "Selectable model catalog: %d/%d configured choice(s) available",
            len(self._discovered_selectable_chat_models),
            len(candidates),
        )

    def validate_active_chat_model(self, model_name: str | None) -> None:
        if model_name is not None and model_name not in self.selectable_chat_models:
            raise ValueError(f"Chat model {model_name!r} is not operator-selectable")

    def set_active_chat_model(self, model_name: str | None) -> None:
        self.validate_active_chat_model(model_name)
        self.active_chat_model = model_name
        # Some direct test callers read ``main`` directly. Force their next lookup
        # through resolve rather than leaving an obsolete global provider handle.
        self.main = None

    def resolved_chat_model_name(
        self,
        scope: Scope | None = None,
        *,
        images: bool = False,
    ) -> str:
        model_config = self.model_config
        if model_config is None:
            return ""
        if self.active_chat_model is None:
            return model_config.model_name_for_role("chat", scope, images=images)
        names = model_config.model_names_for_selected_chat(
            self.active_chat_model,
            images=images,
        )
        return names[0] if names else ""

    def _resolve_single(self, model_config: ModelConfig, model_name: str) -> LLMProvider:
        provider = self._providers.get(model_name)
        if provider is None:
            raw_provider = create_provider(
                resolve_provider_config(model_config, model_name, settings=self.settings)
            )
            provider = _ModelCapabilityProvider(
                raw_provider,
                _configured_capabilities(model_config, model_name),
                _configured_reasoning_escalations(model_config, model_name),
                model_config.profile_for_model(model_name).max_output_tokens,
            )
            self._providers[model_name] = provider
        return provider

    def ensure_compaction(self) -> LLMProvider:
        if self.compaction is None:
            self.compaction = self.resolve("compaction", None)
        return self.compaction

    def ensure_persona(self) -> LLMProvider:
        if self.persona is None:
            self.persona = self.resolve("persona", None)
        return self.persona

    def build_compactor(self, llm_semaphore: asyncio.Semaphore | None = None) -> Compactor:
        return Compactor(
            CompactionConfig(
                trigger_tokens=self.settings.compaction_trigger_tokens,
                keep_recent_iterations=self.settings.compaction_keep_recent_iterations,
                keep_recent_tokens=self.settings.compaction_keep_recent_tokens,
                max_tokens=self.settings.compaction_max_tokens,
                max_iteration_tool_output_tokens=(
                    self.settings.compaction_max_iteration_tool_output_tokens
                ),
            ),
            self.ensure_compaction(),
            llm_semaphore,
        )

    def has_active_llm_credentials(self) -> bool:
        if self.model_config is None:
            return self.main is not None
        return _has_active_llm_credentials(self.settings, self.model_config)

    def context_window_warnings(self) -> list[ContextWindowWarning]:
        if self.model_config is None:
            return []
        required_tokens = self.settings.compaction_trigger_tokens + self.settings.react_max_tokens
        warnings: list[ContextWindowWarning] = []
        for model_name in sorted(self.model_config.chat_model_names()):
            entry = self.model_config.models[model_name]
            if entry.context_window > 0 and required_tokens > entry.context_window:
                warnings.append(
                    ContextWindowWarning(
                        model_name=model_name,
                        model_id=entry.model,
                        context_window=entry.context_window,
                    )
                )
        return warnings

    async def close(self) -> None:
        closed_provider_ids: set[int] = set()
        providers = [
            *self._providers.values(),
            self.main,
            self.compaction,
            self.persona,
        ]
        for provider in providers:
            if provider is None:
                continue
            provider_id = id(provider)
            if provider_id in closed_provider_ids:
                continue
            closed_provider_ids.add(provider_id)
            await close_provider(provider)
        self._providers.clear()
        self._chains.clear()
        self.main = None
        self.compaction = None
        self.persona = None


def build_provider_manager(
    settings: Settings, *, model_config_path: Path | None = None
) -> ProviderManager:
    model_config, revision = load_model_config_with_revision(
        model_config_path or Path(settings.config_dir) / "models.yaml"
    )
    return ProviderManager(
        settings=settings,
        model_config=model_config,
        model_config_revision=revision,
    )


async def _fetch_model_ids(endpoint: str, api_key: str) -> frozenset[str]:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(endpoint, headers=headers) as response:
            response.raise_for_status()
            payload = await response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("model catalog response must contain a data list")
    model_ids = frozenset(
        model_id
        for item in payload["data"]
        if isinstance(item, dict) and isinstance((model_id := item.get("id")), str) and model_id
    )
    if not model_ids:
        raise ValueError("model catalog returned no model IDs")
    return model_ids


def codex_tokens_available(settings: Settings) -> bool:
    from codex.auth import CodexAuthManager

    return CodexAuthManager(settings.codex_token_file).is_available()


def codex_startup_check(
    settings: Settings,
    manager: CodexTokenSource | None = None,
    *,
    model_config: ModelConfig | None = None,
) -> None:
    """Validate Codex credentials before serving.

    A present refresh token is only proof we have something to try; it does not
    prove the token is still valid. Proactively obtain an access token at startup
    so an expired token is refreshed ahead of the first turn, and a revoked token
    fails fast with an actionable message instead of erroring on a user's message.
    Transient/network failures are tolerated because they retry on first use.
    """
    active_config = model_config or load_model_config(Path(settings.config_dir) / "models.yaml")
    if not _codex_is_reachable(settings, active_config):
        return
    from codex.auth import CodexAuthError, CodexAuthRevokedError

    if manager is None:
        from providers.factory import _get_codex_auth_manager

        manager = _get_codex_auth_manager(settings.codex_token_file)
    try:
        asyncio.run(manager.get_access_token())
    except CodexAuthRevokedError as exc:
        log.error(
            "Codex authentication rejected (%s). Run: python scripts/codex_auth.py --token-file %s",
            exc,
            settings.codex_token_file,
        )
        sys.exit(1)
    except (CodexAuthError, aiohttp.ClientError, TimeoutError) as exc:
        log.warning(
            "Could not validate Codex token at startup (%s); will retry on first use.",
            exc,
        )


def _has_active_llm_credentials(settings: Settings, model_config: ModelConfig) -> bool:
    return all(
        _provider_has_credentials(
            resolve_provider_config(model_config, model_name, settings=settings),
            settings,
        )
        for model_name in model_config.reachable_model_names(
            include_compaction=True,
            include_coding=settings.coding_tasks_enabled,
        )
    )


def _provider_has_credentials(config: ProviderConfig, settings: Settings) -> bool:
    if config.keyless:
        # The gateway holds the upstream credentials; there is nothing local to check.
        return True
    if config.provider_name == "codex":
        return codex_tokens_available(settings)
    return bool(config.api_key)


def _codex_is_reachable(settings: Settings, model_config: ModelConfig) -> bool:
    for model_name in model_config.reachable_model_names(
        include_compaction=True,
        include_coding=settings.coding_tasks_enabled,
    ):
        if model_config.profile_for_model(model_name).type == "codex":
            return True
    return False


async def close_provider(provider: LLMProvider | None) -> None:
    if provider is None:
        return
    close = getattr(provider, "close", None)
    if close is None:
        return
    try:
        result = close()
        if inspect.isawaitable(result):
            await result
    except Exception:
        provider_key = getattr(provider, "provider_key", type(provider).__name__)
        log.exception("Failed to close LLM provider %s", provider_key)


@dataclass
class _ModelCapabilityProvider(LLMProvider):
    provider: LLMProvider
    declared_capabilities: set[ProviderCapability]
    configured_reasoning_escalations: tuple[ReasoningEscalation, ...]
    max_output_tokens: int | None = None

    @property
    def provider_key(self) -> str:
        return self.provider.provider_key

    @property
    def model(self) -> str:
        return self.provider.model

    @property
    def capabilities(self) -> set[ProviderCapability]:
        provider_caps = set(self.provider.capabilities)
        if not self.declared_capabilities:
            return provider_caps
        return (provider_caps - _MODEL_DECLARED_CAPABILITIES) | (
            provider_caps & self.declared_capabilities
        )

    @property
    def reasoning_escalations(self) -> tuple[ReasoningEscalation, ...]:
        return self.configured_reasoning_escalations

    async def run_turn(self, request: ProviderRequest) -> ProviderResponse:
        if self.max_output_tokens is not None:
            request = replace(
                request,
                max_tokens=min(request.max_tokens, self.max_output_tokens),
            )
        return await self.provider.run_turn(request)

    async def close(self) -> None:
        await close_provider(self.provider)


def _configured_capabilities(
    model_config: ModelConfig,
    model_name: str,
) -> set[ProviderCapability]:
    configured: set[ProviderCapability] = set()
    for value in model_config.models[model_name].capabilities:
        try:
            configured.add(ProviderCapability(value))
        except ValueError:
            log.warning("Ignoring unknown capability %r on model %s", value, model_name)
    return configured


def _configured_reasoning_escalations(
    model_config: ModelConfig,
    model_name: str,
) -> tuple[ReasoningEscalation, ...]:
    return tuple(
        ReasoningEscalation(effort=effort, tool_names=frozenset(tool_names))
        for effort, tool_names in model_config.models[model_name].reasoning_after_tools.items()
    )
