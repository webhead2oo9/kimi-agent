"""Module API contract rules, frozen before any service is implemented.

These tests pin the declaration validators, naming rules, and import isolation
that every runtime service builds on. Changing a rule here is a contract change.
"""

from __future__ import annotations

import ast
import dataclasses
import sys
from pathlib import Path

import pytest

from app.modules import validate_module_selection
from config.plugin_settings import PluginSetting, PluginSettingsDefinition
from config.settings import Settings
from kimi_agent_module_api import (
    MODULE_API_VERSION,
    GuildSettingsSchema,
    ModuleLoadContext,
    ModulePermissions,
    ModuleRuntimeContext,
    ModuleSetting,
    ModuleSettingsDefinition,
    ModuleSpec,
    ServiceDeclaration,
    ServiceRequirement,
    TrustTier,
)
from kimi_agent_module_api.contracts import (
    ALL_DISCORD_ACTIONS,
    CUSTOM_ID_MAX_LENGTH,
    Backoff,
    EventTopicError,
    GuildSettingField,
    HttpHostRule,
    ModuleContractError,
    build_custom_id,
    parse_custom_id,
    split_topic,
    table_prefix,
    validate_guild_settings_schema,
    validate_host_rule,
    validate_permissions,
    validate_publish_topic,
    validate_services,
    validate_subscription,
)
from kimi_agent_module_api.events import CORE_TOPICS
from trust.tiers import TrustTier as CoreTrustTier

SDK_ROOT = (
    Path(__file__).resolve().parents[1]
    / "packages"
    / "kimi-agent-module-api"
    / "src"
    / "kimi_agent_module_api"
)


def _spec(name: str = "demo", **overrides: object) -> ModuleSpec:
    def create(ctx: ModuleLoadContext) -> object:  # pragma: no cover - never called
        raise AssertionError("preflight must not create modules")

    return ModuleSpec(name=name, version="0.0.0", create=create, **overrides)  # type: ignore[arg-type]


def _settings() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


# --- declarations default to nothing --------------------------------------


def test_spec_declares_nothing_by_default() -> None:
    spec = _spec()
    assert spec.permissions == ModulePermissions()
    assert spec.guild_settings is None
    assert spec.provides == ()
    assert spec.consumes == ()
    assert spec.api_version == MODULE_API_VERSION == 1


def test_core_and_modules_share_the_public_trust_enum() -> None:
    assert CoreTrustTier is TrustTier


def test_runtime_context_requires_every_service_port() -> None:
    required = {
        f.name for f in dataclasses.fields(ModuleRuntimeContext) if f.default is dataclasses.MISSING
    }
    assert {
        "events",
        "scheduler",
        "storage",
        "health",
        "discord",
        "interactions",
        "http",
        "services",
        "trust",
        "current_config_dir",
    } <= required
    assert "bot" not in required and "database" not in required


def test_public_module_setting_fields_match_the_core_translation() -> None:
    assert [field.name for field in dataclasses.fields(ModuleSetting)] == [
        field.name for field in dataclasses.fields(PluginSetting)
    ]
    assert [field.name for field in dataclasses.fields(ModuleSettingsDefinition)] == [
        field.name for field in dataclasses.fields(PluginSettingsDefinition)
    ]


# --- naming rules ---------------------------------------------------------


def test_table_prefix_normalizes_hyphens() -> None:
    assert table_prefix("image-fingerprints") == "image_fingerprints"


def test_selection_preflight_rejects_colliding_normalized_prefixes() -> None:
    installed = {
        "image-fingerprints": _spec("image-fingerprints"),
        "image_fingerprints": _spec("image_fingerprints"),
    }
    with pytest.raises(RuntimeError, match="share normalized prefix 'image_fingerprints'"):
        validate_module_selection(
            tuple(installed),
            core_settings=_settings(),
            installed=installed,
        )


def test_selection_preflight_accepts_one_hyphenated_name() -> None:
    spec = _spec("image-fingerprints")
    selected = validate_module_selection(
        (spec.name,),
        core_settings=_settings(),
        installed={spec.name: spec},
    )
    assert selected == (spec,)


def test_proposals_is_a_reserved_core_module_name() -> None:
    with pytest.raises(RuntimeError, match="reserved by core"):
        validate_module_selection(
            ("proposals",),
            core_settings=_settings(),
            installed={"proposals": _spec("proposals")},
        )


def test_unsupported_module_api_version_is_rejected_clearly() -> None:
    with pytest.raises(RuntimeError, match="requires module API 2; core provides 1"):
        validate_module_selection(
            ("legacy",),
            core_settings=_settings(),
            installed={"legacy": _spec("legacy", api_version=2)},
        )


@pytest.mark.parametrize("topic", ["discord.message", "community_moderation.case_created"])
def test_split_topic_accepts_namespace_dot_name(topic: str) -> None:
    namespace, name = split_topic(topic)
    assert f"{namespace}.{name}" == topic


@pytest.mark.parametrize("topic", ["", "nodot", "Upper.case", "a.b.c", ".x", "x."])
def test_split_topic_rejects_malformed(topic: str) -> None:
    with pytest.raises(EventTopicError):
        split_topic(topic)


def test_core_topics_live_under_discord_namespace() -> None:
    assert CORE_TOPICS
    assert all(split_topic(topic)[0] == "discord" for topic in CORE_TOPICS)


def test_publish_is_limited_to_own_namespace() -> None:
    validate_publish_topic("community-moderation", "community_moderation.case_created")
    with pytest.raises(EventTopicError):
        validate_publish_topic("image_fingerprints", "community_moderation.case_created")
    with pytest.raises(EventTopicError):
        validate_publish_topic("image_fingerprints", "discord.message")


def test_subscription_needs_declaration_except_own_namespace() -> None:
    perms = ModulePermissions(event_topics=("discord.message", "community_moderation.*"))
    validate_subscription("image_fingerprints", perms, "image_fingerprints.anything")
    validate_subscription("image_fingerprints", perms, "discord.message")
    validate_subscription("image_fingerprints", perms, "community_moderation.case_created")
    with pytest.raises(EventTopicError):
        validate_subscription("image_fingerprints", perms, "discord.member_join")
    with pytest.raises(EventTopicError):
        validate_subscription("image_fingerprints", ModulePermissions(), "discord.*")


def test_custom_id_round_trips_and_is_bounded() -> None:
    custom_id = build_custom_id("community_moderation", "ban_confirm", "123", "456")
    assert custom_id == "m:community_moderation:ban_confirm:123:456"
    assert parse_custom_id(custom_id) == ("community_moderation", "ban_confirm", ("123", "456"))
    assert parse_custom_id("core:whatever") is None
    with pytest.raises(ModuleContractError):
        build_custom_id("m", "k", "a:b")
    with pytest.raises(ModuleContractError):
        build_custom_id("m", "key", "x" * CUSTOM_ID_MAX_LENGTH)


# --- permissions ----------------------------------------------------------


def test_permissions_reject_unknown_actions_and_own_topics() -> None:
    validate_permissions("demo", ModulePermissions(discord_actions=ALL_DISCORD_ACTIONS))
    with pytest.raises(ModuleContractError):
        validate_permissions("demo", ModulePermissions(discord_actions=frozenset({"nuke"})))
    with pytest.raises(EventTopicError):
        validate_permissions("demo", ModulePermissions(event_topics=("demo.self",)))


def test_target_policy_override_requires_a_targeted_action() -> None:
    with pytest.raises(ModuleContractError):
        validate_permissions("demo", ModulePermissions(override_target_policy=True))
    validate_permissions(
        "demo",
        ModulePermissions(discord_actions=frozenset({"ban"}), override_target_policy=True),
    )


@pytest.mark.parametrize(
    "rule",
    [
        HttpHostRule(host="hub.example.org"),
        HttpHostRule(host="discord-cdn"),
        HttpHostRule(host="${fingerprint_hub_base_url}", network="private"),
        HttpHostRule(host="localhost", schemes=("http",), ports=(8080,), network="private"),
    ],
)
def test_host_rules_accept_exact_hosts_tokens_and_setting_refs(rule: HttpHostRule) -> None:
    validate_host_rule(rule)


@pytest.mark.parametrize(
    "rule",
    [
        HttpHostRule(host="*.example.org"),
        HttpHostRule(host="Example.org"),
        HttpHostRule(host="discord-cdn", network="private"),
        HttpHostRule(host="example.org", schemes=("ftp",)),
        HttpHostRule(host="example.org", ports=(70000,)),
    ],
)
def test_host_rules_reject_wildcards_and_bad_transport(rule: HttpHostRule) -> None:
    with pytest.raises(ModuleContractError):
        validate_host_rule(rule)


# --- services -------------------------------------------------------------


def test_consumed_service_must_come_from_a_declared_dependency() -> None:
    requirement = ServiceRequirement("moderation.cases", 1, provider="community_moderation")
    validate_services("image_fingerprints", ("community_moderation",), (), (requirement,))
    with pytest.raises(ModuleContractError):
        validate_services("image_fingerprints", (), (), (requirement,))
    with pytest.raises(ModuleContractError):
        validate_services("x", ("x",), (), (ServiceRequirement("a", 1, provider="x"),))


def test_provided_services_are_unique_and_versioned_from_one() -> None:
    validate_services("m", (), (ServiceDeclaration("moderation.cases", 1),), ())
    with pytest.raises(ModuleContractError):
        validate_services("m", (), (ServiceDeclaration("moderation.cases", 0),), ())
    with pytest.raises(ModuleContractError):
        validate_services(
            "m",
            (),
            (ServiceDeclaration("a.b", 1), ServiceDeclaration("a.b", 1)),
            (),
        )


def test_one_consumed_service_cannot_name_multiple_providers() -> None:
    with pytest.raises(ModuleContractError, match="from multiple providers"):
        validate_services(
            "consumer",
            ("intended", "rogue"),
            (),
            (
                ServiceRequirement("cases", 1, provider="intended"),
                ServiceRequirement("cases", 1, provider="rogue"),
            ),
        )


@pytest.mark.parametrize(
    "backoff",
    (
        {"base_seconds": 0},
        {"base_seconds": -1},
        {"base_seconds": float("nan")},
        {"max_seconds": 0},
        {"max_seconds": -1},
        {"max_seconds": float("inf")},
        {"multiplier": 0},
        {"multiplier": 0.5},
        {"multiplier": float("nan")},
        {"multiplier": True},
    ),
)
def test_scheduler_backoff_rejects_unsafe_values(backoff: dict[str, object]) -> None:
    with pytest.raises(ModuleContractError, match="backoff"):
        Backoff(**backoff)  # type: ignore[arg-type]


def test_scheduler_backoff_accepts_positive_finite_values_and_multiplier() -> None:
    assert Backoff(base_seconds=2, max_seconds=1, multiplier=1) == Backoff(2, 1, 1)


# --- guild settings -------------------------------------------------------


def test_guild_settings_schema_defaults_to_fail_closed() -> None:
    assert GuildSettingsSchema(fields=()).invalid_policy == "disable_guild"


def test_guild_settings_schema_rules() -> None:
    validate_guild_settings_schema(
        "m",
        GuildSettingsSchema(
            fields=(
                GuildSettingField("mod_log_channel_id", "id"),
                GuildSettingField("mode", "enum", choices=("a", "b"), default="a"),
            )
        ),
    )
    for bad in (
        (GuildSettingField("Bad", "int"),),
        (GuildSettingField("x", "int"), GuildSettingField("x", "str")),
        (GuildSettingField("x", "enum"),),
        (GuildSettingField("x", "str", choices=("a",)),),
        (GuildSettingField("x", "int", required=True, default=1),),
        (GuildSettingField("x", "int", default="not-an-int"),),
        (GuildSettingField("x", "enum", choices=("a", "b"), default="c"),),
    ):
        with pytest.raises(ModuleContractError):
            validate_guild_settings_schema("m", GuildSettingsSchema(fields=bad))


# --- preflight integration -------------------------------------------------


def test_selection_preflight_rejects_invalid_declarations() -> None:
    bad = _spec(permissions=ModulePermissions(discord_actions=frozenset({"nuke"})))
    with pytest.raises(RuntimeError, match="invalid declaration"):
        validate_module_selection(["demo"], core_settings=_settings(), installed={"demo": bad})


def test_selection_preflight_accepts_full_declarations() -> None:
    provider = _spec("provider", provides=(ServiceDeclaration("moderation.cases", 1),))
    consumer = _spec(
        "consumer",
        dependencies=("provider",),
        consumes=(ServiceRequirement("moderation.cases", 1, provider="provider"),),
        permissions=ModulePermissions(
            discord_actions=frozenset({"delete_message", "timeout"}),
            event_topics=("discord.message", "provider.*"),
            http_hosts=(HttpHostRule(host="discord-cdn"),),
        ),
        guild_settings=GuildSettingsSchema(fields=(GuildSettingField("channels", "id_list"),)),
    )
    ordered = validate_module_selection(
        ["consumer", "provider"],
        core_settings=_settings(),
        installed={"provider": provider, "consumer": consumer},
    )
    assert [spec.name for spec in ordered] == ["provider", "consumer"]


# --- import isolation ------------------------------------------------------


@pytest.mark.parametrize("module", ["contracts", "events"])
def test_contract_modules_import_only_stdlib_and_each_other(module: str) -> None:
    """Declarations must be checkable without Discord, the database, or core services.

    Checked statically in the standalone package source tree.
    """
    path = SDK_ROOT / f"{module}.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    allowed = set(sys.stdlib_module_names) | {"kimi_agent_module_api"}
    assert imported <= allowed, (
        f"{module}.py imports outside the contract layer: {imported - allowed}"
    )


def test_entire_sdk_has_no_core_runtime_imports() -> None:
    allowed = set(sys.stdlib_module_names) | {
        "kimi_agent_module_api",
        "pydantic_settings",
    }
    # testing.MemoryStorage imports aiosqlite lazily behind the ``testing`` extra;
    # it is a test convenience, not a core runtime package.
    per_file_allowed = {"testing.py": {"aiosqlite"}}
    outside: dict[str, set[str]] = {}
    for path in SDK_ROOT.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        if unexpected := imported - allowed - per_file_allowed.get(path.name, set()):
            outside[path.name] = unexpected
    assert outside == {}


@pytest.mark.asyncio
async def test_fake_scheduler_retries_failures_with_backoff_like_the_host() -> None:
    from kimi_agent_module_api.contracts import Backoff, JobRun
    from kimi_agent_module_api.testing import FakeScheduler

    scheduler = FakeScheduler()
    attempts: list[int] = []

    async def flaky(run: JobRun) -> None:
        attempts.append(run.attempt)
        if run.attempt < 3:
            raise RuntimeError("not yet")

    scheduler.register("h", flaky)
    await scheduler.run_at("once", 100.0, "h")
    await scheduler.run_every("often", 60.0, "h", backoff=Backoff(base_seconds=5, multiplier=2))

    assert await scheduler.run_due(now=100.0) == 2
    # Both failed: kept, attempt preserved, retried after their backoff delay.
    assert scheduler.jobs["once"].run_at == 100.0 + 30.0
    assert scheduler.jobs["often"].run_at == 100.0 + 5.0
    assert await scheduler.run_due(now=130.0) == 2
    # Second failures back off further: 30*2 and 5*2 seconds.
    assert scheduler.jobs["once"].run_at == 130.0 + 60.0
    assert scheduler.jobs["often"].run_at == 130.0 + 10.0
    assert await scheduler.run_due(now=200.0) == 2
    assert "once" not in scheduler.jobs, "a one-shot that finally succeeds is deleted"
    assert scheduler.jobs["often"].run_at == 200.0 + 60.0
    assert scheduler.jobs["often"].attempt == 0
    assert attempts == [1, 1, 2, 2, 3, 3]


def test_render_guild_settings_matches_the_host_document_format() -> None:
    from kimi_agent_module_api import render_guild_settings

    rendered = render_guild_settings({"b": True, "a": [1, 2], "c": "x", "d": 3})

    assert rendered == "---\na: [1, 2]\nb: true\nc: x\nd: 3\n---\n"


def test_fake_service_registry_typed_get() -> None:
    from kimi_agent_module_api.contracts import ServiceUnavailable
    from kimi_agent_module_api.testing import FakeServiceRegistry

    class Board: ...

    registry = FakeServiceRegistry()
    registry.provide("kudos.board", 1, Board())

    assert isinstance(registry.get("kudos.board", 1, Board), Board)
    with pytest.raises(TypeError):
        registry.get("kudos.board", 1, int)
    with pytest.raises(ServiceUnavailable):
        registry.get("missing", 1)


@pytest.mark.asyncio
async def test_fake_discord_actions_gate_fetch_roles_and_can_view_channel() -> None:
    from kimi_agent_module_api.contracts import RoleSnapshot, UndeclaredDiscordAction
    from kimi_agent_module_api.testing import FakeDiscordActions

    actions = FakeDiscordActions("m", frozenset({"fetch_roles"}))
    actions.roles[1] = (RoleSnapshot(1, 10, "mod", 5),)

    assert await actions.fetch_roles(1) == (RoleSnapshot(1, 10, "mod", 5),)
    with pytest.raises(UndeclaredDiscordAction):
        await actions.can_view_channel(1, 2, 3)


def test_fake_interaction_exposes_the_component_message() -> None:
    from kimi_agent_module_api.contracts import MessageRef
    from kimi_agent_module_api.testing import FakeInteraction

    ref = MessageRef(1, 2, 3)
    assert FakeInteraction(message=ref).message == ref
    assert FakeInteraction().message is None


@pytest.mark.asyncio
async def test_memory_storage_serializes_writers_and_runs_migrations() -> None:
    from kimi_agent_module_api.contracts import MigrationContext
    from kimi_agent_module_api.testing import MemoryStorage

    async def create(ctx: MigrationContext) -> None:
        await ctx.connection.execute(f"CREATE TABLE {ctx.table('t')} (n INTEGER)")

    async with MemoryStorage.open("my-mod") as storage:
        assert storage.table("t") == '"my_mod_t"'
        await storage.migrate((("001", create),))
        async with storage.write_transaction() as conn:
            await conn.execute('INSERT INTO "my_mod_t" (n) VALUES (1)')
        cursor = await storage.connection.execute('SELECT COUNT(*) FROM "my_mod_t"')
        assert (await cursor.fetchone())[0] == 1
