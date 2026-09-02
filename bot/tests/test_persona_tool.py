from __future__ import annotations

import json
from typing import Any

import pytest

from providers.types import ProviderResponse
from tests.helpers import make_message_context
from tools.browse import init_browse_tools
from tools.persona import (
    _COMPILER_SYSTEM,
    LLMPersonaCompiler,
    PersonaCompileError,
    PersonaToolConfig,
    init_persona_tools,
)
from tools.registry import ToolRegistry
from trust.tiers import TrustTier
from usage.normalization import UsageBreakdown


class FakePersonaStore:
    def __init__(self) -> None:
        self.personas: dict[str, str] = {}

    async def get_persona(self, user_id: str) -> str:
        return self.personas.get(user_id, "")

    async def set_persona(self, user_id: str, persona: str) -> bool:
        changed = self.personas.get(user_id, "") != persona
        self.personas[user_id] = persona
        return changed

    async def clear_persona(self, user_id: str) -> bool:
        return self.personas.pop(user_id, None) is not None


class FakeCompiler:
    def __init__(self, persona: str = "Roleplay as a cheerful robot mechanic.") -> None:
        self.persona = persona
        self.calls: list[dict[str, str]] = []

    async def compile(
        self,
        *,
        request: str,
        user_name: str,
        on_response: Any | None = None,
    ) -> str:
        self.calls.append({"request": request, "user_name": user_name})
        if on_response is not None:
            await on_response(
                ProviderResponse(
                    usage={"input_tokens": 12, "output_tokens": 3},
                    model="claude-served",
                ),
                "claude-priced",
            )
        return self.persona


class RejectingCompiler:
    async def compile(
        self,
        *,
        request: str,
        user_name: str,
        on_response: Any | None = None,
    ) -> str:
        _ = (request, user_name, on_response)
        raise PersonaCompileError("That persona cannot be made 13+ safe.")


def _register(
    store: FakePersonaStore,
    compiler: Any,
) -> ToolRegistry:
    registry = ToolRegistry()
    init_browse_tools(registry)
    init_persona_tools(
        registry,
        lambda: store,
        compiler,
        PersonaToolConfig(max_request_chars=200),
    )
    return registry


@pytest.mark.asyncio
async def test_persona_tools_are_hidden_until_loaded() -> None:
    store = FakePersonaStore()
    registry = _register(store, FakeCompiler())

    member_visible = {schema["name"] for schema in registry.get_tool_schemas(TrustTier.MEMBER)}
    member_catalog = {entry.name for entry in registry.catalog(TrustTier.MEMBER)}
    persona_tools = {"persona_set", "persona_show", "persona_clear"}

    assert persona_tools.isdisjoint(member_visible)
    assert persona_tools.isdisjoint(member_catalog)

    regular_visible = {schema["name"] for schema in registry.get_tool_schemas(TrustTier.REGULAR)}
    regular_catalog = {entry.name for entry in registry.catalog(TrustTier.REGULAR)}

    assert persona_tools.isdisjoint(regular_visible)
    assert persona_tools <= regular_catalog

    regular_loaded = {
        schema["name"]
        for schema in registry.get_tool_schemas(TrustTier.REGULAR, activated=persona_tools)
    }
    assert persona_tools <= regular_loaded


@pytest.mark.asyncio
async def test_persona_tools_are_masked_below_regular_even_when_loaded() -> None:
    store = FakePersonaStore()
    registry = _register(store, FakeCompiler())
    ctx = make_message_context(activated={"persona_set"}, trust_tier=TrustTier.MEMBER)

    result = json.loads(
        await registry.dispatch(
            "persona_set",
            {"request": "Talk to me as a friendly space mechanic."},
            ctx,
        )
    )

    assert result["error"] == "Unknown tool: persona_set"
    assert store.personas == {}


@pytest.mark.asyncio
async def test_persona_set_compiles_and_stores_for_current_user() -> None:
    store = FakePersonaStore()
    compiler = FakeCompiler()
    registry = _register(store, compiler)
    ctx = make_message_context(
        activated={"persona_set"},
        user_id="123",
        user_name="Alice",
        trust_tier=TrustTier.REGULAR,
    )
    ctx.usage_sink = []

    result = json.loads(
        await registry.dispatch(
            "persona_set",
            {"request": "Talk to me as a friendly space mechanic."},
            ctx,
        )
    )

    assert result["saved"] is True
    assert result["changed"] is True
    assert result["persona"] == "Roleplay as a cheerful robot mechanic."
    assert store.personas == {"123": "Roleplay as a cheerful robot mechanic."}
    assert compiler.calls == [
        {"request": "Talk to me as a friendly space mechanic.", "user_name": "Alice"}
    ]
    assert ctx.usage_sink is not None
    [usage_call] = ctx.usage_sink
    assert (usage_call.model, usage_call.pricing_model, usage_call.role) == (
        "claude-served",
        "claude-priced",
        "persona_compile",
    )
    assert usage_call.usage == UsageBreakdown(input_tokens=12, output_tokens=3)


@pytest.mark.asyncio
async def test_persona_compile_usage_carries_router_attribution() -> None:
    # The sink is the same list the turn event renders as provider_calls, so a
    # nested compile through OpenRouter must carry its routing attribution too.
    class RoutedCompiler(FakeCompiler):
        async def compile(
            self,
            *,
            request: str,
            user_name: str,
            on_response: Any | None = None,
        ) -> str:
            self.calls.append({"request": request, "user_name": user_name})
            if on_response is not None:
                await on_response(
                    ProviderResponse(
                        usage={"input_tokens": 12, "output_tokens": 3},
                        model="moonshotai/kimi-k2",
                        upstream_provider="Moonshot AI",
                        service_tier="flex",
                        openrouter_charge_usd=0.0125,
                        is_byok=False,
                    ),
                    "moonshotai/kimi-k2",
                )
            return self.persona

    store = FakePersonaStore()
    registry = _register(store, RoutedCompiler())
    ctx = make_message_context(
        activated={"persona_set"},
        user_id="123",
        user_name="Alice",
        trust_tier=TrustTier.REGULAR,
    )
    ctx.usage_sink = []

    await registry.dispatch("persona_set", {"request": "Be a space mechanic."}, ctx)

    assert ctx.usage_sink is not None
    [usage_call] = ctx.usage_sink
    assert usage_call.upstream_provider == "Moonshot AI"
    assert usage_call.service_tier == "flex"
    assert usage_call.openrouter_charge_usd == 0.0125
    assert usage_call.is_byok is False


@pytest.mark.asyncio
async def test_persona_set_surfaces_compiler_rejection() -> None:
    store = FakePersonaStore()
    registry = _register(store, RejectingCompiler())
    ctx = make_message_context(
        activated={"persona_set"},
        user_id="123",
        trust_tier=TrustTier.REGULAR,
    )

    result = json.loads(await registry.dispatch("persona_set", {"request": "unsafe persona"}, ctx))

    assert result["error"] == "That persona cannot be made 13+ safe."
    assert store.personas == {}


@pytest.mark.asyncio
async def test_persona_show_and_clear_are_current_user_scoped() -> None:
    store = FakePersonaStore()
    store.personas["123"] = "Roleplay as a starship engineer."
    store.personas["456"] = "Roleplay as someone else."
    registry = _register(store, FakeCompiler())
    ctx = make_message_context(
        activated={"persona_show", "persona_clear"},
        user_id="123",
        trust_tier=TrustTier.REGULAR,
    )

    shown = json.loads(await registry.dispatch("persona_show", {}, ctx))
    cleared = json.loads(await registry.dispatch("persona_clear", {}, ctx))

    assert shown == {
        "has_persona": True,
        "persona": "Roleplay as a starship engineer.",
    }
    assert cleared == {"cleared": True}
    assert store.personas == {"456": "Roleplay as someone else."}


@pytest.mark.asyncio
async def test_persona_compiler_parses_json_response() -> None:
    class Provider:
        def __init__(self) -> None:
            self.requests: list[Any] = []

        async def run_turn(self, request: Any) -> ProviderResponse:
            self.requests.append(request)
            return ProviderResponse(
                content='{"ok": true, "persona": "Roleplay as a gentle moon wizard."}'
            )

    provider = Provider()
    compiler = LLMPersonaCompiler(provider, max_chars=100)  # type: ignore[arg-type]

    persona = await compiler.compile(request="moon wizard", user_name="Alice")

    assert persona == "Roleplay as a gentle moon wizard."
    [request] = provider.requests
    assert request.reasoning_enabled is False
    assert request.max_tokens == 32_000
    assert request.tools == []


def test_persona_tools_do_not_register_without_compiler() -> None:
    registry = ToolRegistry()
    assert (
        init_persona_tools(registry, lambda: FakePersonaStore(), None, PersonaToolConfig()) is False
    )
    assert not registry.has_tool("persona_set")


def test_persona_compiler_instruct_allows_specific_protected_characters() -> None:
    normalized = " ".join(_COMPILER_SYSTEM.split())

    assert "copyrighted/trademarked characters are allowed" in normalized
    assert "do not reject a persona solely because it references" in normalized
    assert "Be specific to the user's request" in normalized
    assert "rather than replacing them with a generic archetype" in normalized


def _persona_provider_manager(persona_line: str) -> Any:
    from app import providers as provider_runtime
    from config.model_config import parse_model_config_text
    from config.settings import Settings

    config = parse_model_config_text(
        f"""
providers:
  main:
    type: openai_compat
    base_url: https://llm-gateway.example.invalid/v1
    api_key_env: MODEL_API_KEY
models:
  primary: {{ provider: main, model: Kimi-K2.6 }}
roles:
  chat: primary
  compaction: primary
{persona_line}"""
    )
    return provider_runtime.ProviderManager(
        settings=Settings.model_validate({"model_api_key": "key"}),
        model_config=config,
    )


def test_persona_tools_register_from_the_persona_role(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import providers as provider_runtime
    from app.tools import _register_persona_tools
    from config.settings import Settings

    class DummyProvider:
        capabilities: set[Any] = set()

        def __init__(self, model: str) -> None:
            self.model = model

    monkeypatch.setattr(
        provider_runtime,
        "create_provider",
        lambda provider_config: DummyProvider(provider_config.model),
    )
    manager = _persona_provider_manager("  persona: primary\n")
    registry = ToolRegistry()

    _register_persona_tools(
        Settings(_env_file=None),  # type: ignore[call-arg]
        registry,
        manager,
        lambda: FakePersonaStore(),
    )

    assert registry.has_tool("persona_set")
    assert registry.has_tool("persona_show")
    assert registry.has_tool("persona_clear")
    assert manager.persona is not None


def test_persona_tools_skip_without_a_persona_role() -> None:
    from app.tools import _register_persona_tools
    from config.settings import Settings

    manager = _persona_provider_manager("")
    registry = ToolRegistry()

    _register_persona_tools(
        Settings(_env_file=None),  # type: ignore[call-arg]
        registry,
        manager,
        lambda: FakePersonaStore(),
    )

    assert not registry.has_tool("persona_set")
    # No role means nothing was resolved, so nothing needs closing either.
    assert manager.persona is None
