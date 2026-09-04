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
    BASELINE_CAPABILITIES,
    MODULE_API_VERSION,
    AppModule,
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
    ButtonSpec,
    CommandOption,
    CommandSpec,
    EventTopicError,
    GuildSettingField,
    HttpHostRule,
    ModuleContractError,
    SelectSpec,
    build_custom_id,
    parse_custom_id,
    split_topic,
    table_prefix,
    validate_command_spec,
    validate_component_spec,
    validate_guild_settings_schema,
    validate_select_spec,
    validate_host_rule,
    validate_module_name,
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


def test_sdk_baseline_capabilities_remain_host_independent() -> None:
    assert frozenset({"discord.history.v1", "proposals.v2"}) == BASELINE_CAPABILITIES


def _spec(name: str = "demo", **overrides: object) -> ModuleSpec:
    def create(ctx: ModuleLoadContext) -> AppModule:  # pragma: no cover - never called
        raise AssertionError("preflight must not create modules")

    api_version = overrides.pop("api_version", MODULE_API_VERSION)
    return ModuleSpec(
        name=name,
        version="0.0.0",
        create=create,
        api_version=api_version,  # type: ignore[arg-type]
        **overrides,  # type: ignore[arg-type]
    )


def _settings() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


# --- declarations default to nothing --------------------------------------


def test_spec_declares_nothing_by_default() -> None:
    spec = _spec()
    assert spec.permissions == ModulePermissions()
    assert spec.guild_settings is None
    assert spec.provides == ()
    assert spec.consumes == ()
    assert spec.api_version == MODULE_API_VERSION == 2


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
    assert table_prefix("audit-log") == "audit_log"


def test_selection_preflight_rejects_colliding_normalized_prefixes() -> None:
    installed = {
        "audit-log": _spec("audit-log"),
        "audit_log": _spec("audit_log"),
    }
    with pytest.raises(RuntimeError, match="share normalized prefix 'audit_log'"):
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
    with pytest.raises(ModuleContractError, match="reserved by core"):
        validate_module_name("proposals")
    with pytest.raises(RuntimeError, match="reserved by core"):
        validate_module_selection(
            ("proposals",),
            core_settings=_settings(),
            installed={"proposals": _spec("proposals")},
        )


def test_discord_is_a_reserved_core_module_name_and_event_namespace() -> None:
    with pytest.raises(ModuleContractError, match="reserved by core"):
        validate_module_name("discord")
    with pytest.raises(RuntimeError, match="reserved by core"):
        validate_module_selection(
            ("discord",),
            core_settings=_settings(),
            installed={"discord": _spec("discord")},
        )
    with pytest.raises(EventTopicError, match="reserved by core"):
        validate_publish_topic("discord", "discord.message")


def test_module_settings_name_must_match_module_name() -> None:
    spec = _spec(
        settings=ModuleSettingsDefinition(
            name="other",
            label="Other settings",
            model=Settings,
            exposed=(),
        )
    )
    with pytest.raises(RuntimeError, match="settings name 'other' does not match"):
        validate_module_selection(
            (spec.name,),
            core_settings=_settings(),
            installed={spec.name: spec},
        )


def test_unsupported_module_api_version_is_rejected_clearly() -> None:
    with pytest.raises(RuntimeError, match="requires module API 3; core provides 2"):
        validate_module_selection(
            ("legacy",),
            core_settings=_settings(),
            installed={"legacy": _spec("legacy", api_version=MODULE_API_VERSION + 1)},
        )


@pytest.mark.parametrize("topic", ["discord.message", "case_manager.record_created"])
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
    validate_publish_topic("case-manager", "case_manager.record_created")
    with pytest.raises(EventTopicError):
        validate_publish_topic("case_audit", "case_manager.record_created")
    with pytest.raises(EventTopicError):
        validate_publish_topic("case_audit", "discord.message")


def test_subscription_needs_declaration_except_own_namespace() -> None:
    perms = ModulePermissions(event_topics=("discord.message", "case_manager.*"))
    validate_subscription("case_audit", perms, "case_audit.anything")
    validate_subscription("case_audit", perms, "discord.message")
    validate_subscription("case_audit", perms, "case_manager.record_created")
    with pytest.raises(EventTopicError):
        validate_subscription("case_audit", perms, "discord.member_join")
    with pytest.raises(EventTopicError):
        validate_subscription("case_audit", ModulePermissions(), "discord.*")


def test_custom_id_round_trips_and_is_bounded() -> None:
    custom_id = build_custom_id("case_manager", "approve", "123", "456")
    assert custom_id == "m:case_manager:approve:123:456"
    assert parse_custom_id(custom_id) == ("case_manager", "approve", ("123", "456"))
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
        HttpHostRule(host="${api_base_url}", network="private"),
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
    requirement = ServiceRequirement("records.cases", 1, provider="case_manager")
    validate_services("case_audit", ("case_manager",), (), (requirement,))
    with pytest.raises(ModuleContractError):
        validate_services("case_audit", (), (), (requirement,))
    with pytest.raises(ModuleContractError):
        validate_services("x", ("x",), (), (ServiceRequirement("a", 1, provider="x"),))


def test_provided_services_are_unique_and_versioned_from_one() -> None:
    validate_services("m", (), (ServiceDeclaration("records.cases", 1),), ())
    with pytest.raises(ModuleContractError):
        validate_services("m", (), (ServiceDeclaration("records.cases", 0),), ())
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


def test_guild_settings_schema_rejects_unknown_invalid_policy() -> None:
    schema = GuildSettingsSchema(
        fields=(),
        invalid_policy="disable_everything",  # type: ignore[arg-type]
    )

    with pytest.raises(ModuleContractError, match="invalid policy"):
        validate_guild_settings_schema("m", schema)


# --- preflight integration -------------------------------------------------


def test_selection_preflight_rejects_invalid_declarations() -> None:
    bad = _spec(permissions=ModulePermissions(discord_actions=frozenset({"nuke"})))
    with pytest.raises(RuntimeError, match="invalid declaration"):
        validate_module_selection(["demo"], core_settings=_settings(), installed={"demo": bad})


def test_selection_preflight_accepts_full_declarations() -> None:
    provider = _spec("provider", provides=(ServiceDeclaration("records.cases", 1),))
    consumer = _spec(
        "consumer",
        dependencies=("provider",),
        consumes=(ServiceRequirement("records.cases", 1, provider="provider"),),
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


@pytest.mark.asyncio
async def test_fake_scheduler_caps_extreme_backoff_without_overflow() -> None:
    from kimi_agent_module_api.contracts import Backoff, JobRun
    from kimi_agent_module_api.testing import FakeScheduler

    scheduler = FakeScheduler()
    attempts: list[int] = []

    async def always_fails(run: JobRun) -> None:
        attempts.append(run.attempt)
        raise RuntimeError("not yet")

    scheduler.register("h", always_fails)
    await scheduler.run_every(
        "often",
        60.0,
        "h",
        backoff=Backoff(base_seconds=1, max_seconds=5, multiplier=1e308),
    )

    assert await scheduler.run_due(now=0.0) == 1
    assert scheduler.jobs["often"].run_at == 1.0
    assert await scheduler.run_due(now=1.0) == 1
    assert scheduler.jobs["often"].run_at == 6.0
    assert await scheduler.run_due(now=6.0) == 1
    assert scheduler.jobs["often"].run_at == 11.0
    assert attempts == [1, 2, 3]


def test_render_guild_settings_matches_the_host_document_format() -> None:
    from kimi_agent_module_api import render_guild_settings

    rendered = render_guild_settings({"b": True, "a": [1, 2], "c": "x: y", "d": 3, "e": None})

    assert rendered == '---\na: [1, 2]\nb: true\nc: "x: y"\nd: 3\n---\n'


@pytest.mark.parametrize("key", ["bad:key", "bad\ninjected", "UPPER", ""])
def test_render_guild_settings_rejects_invalid_keys(key: str) -> None:
    from kimi_agent_module_api import render_guild_settings

    with pytest.raises(ValueError, match="invalid guild setting name"):
        render_guild_settings({key: True})


def test_fake_service_registry_typed_get() -> None:
    from kimi_agent_module_api.contracts import ServiceUnavailable
    from kimi_agent_module_api.testing import FakeServiceRegistry

    class Board:
        def answer(self) -> int:
            return 42

    registry = FakeServiceRegistry()
    registration = registry.provide("kudos.board", 1, Board())

    proxy = registry.get("kudos.board", 1, Board)
    assert proxy.answer() == 42
    registration.close()
    with pytest.raises(ServiceUnavailable):
        proxy.answer()
    registry.provide("kudos.board", 1, Board())
    with pytest.raises(TypeError):
        registry.get("kudos.board", 1, int)
    with pytest.raises(ServiceUnavailable):
        registry.get("missing", 1)


@pytest.mark.asyncio
async def test_fake_discord_actions_gate_fetch_roles_invites_and_can_view_channel() -> None:
    from kimi_agent_module_api.contracts import (
        InviteSnapshot,
        RoleSnapshot,
        UndeclaredDiscordAction,
    )
    from kimi_agent_module_api.testing import FakeDiscordActions

    actions = FakeDiscordActions("m", frozenset({"fetch_roles", "fetch_invites"}))
    actions.roles[1] = (RoleSnapshot(1, 10, "mod", 5),)
    actions.invites[1] = (InviteSnapshot(1, "welcome", uses=2),)

    assert await actions.fetch_roles(1) == (RoleSnapshot(1, 10, "mod", 5),)
    assert await actions.fetch_invites(1) == (InviteSnapshot(1, "welcome", uses=2),)
    with pytest.raises(UndeclaredDiscordAction):
        await actions.can_view_channel(1, 2, 3)


def test_fake_interaction_exposes_the_component_message() -> None:
    from kimi_agent_module_api.contracts import MessageRef
    from kimi_agent_module_api.testing import FakeInteraction

    ref = MessageRef(1, 2, 3)
    assert FakeInteraction(message=ref).message == ref
    assert FakeInteraction().message is None


@pytest.mark.asyncio
async def test_fake_interaction_records_modal_values_and_layouts() -> None:
    from kimi_agent_module_api.contracts import (
        LayoutText,
        ModalSpec,
        OutgoingLayout,
        SelectSpec,
        TextInputSpec,
    )
    from kimi_agent_module_api.testing import FakeInteraction

    interaction = FakeInteraction(text_values={"title": "Hello"}, module_name="demo")
    modal = ModalSpec("edit", "Edit", (TextInputSpec("title", "Title"),))
    layout = OutgoingLayout((LayoutText("Preview"),))

    await interaction.show_modal(modal)
    await interaction.respond(layout=layout)

    assert interaction.text_values == {"title": "Hello"}
    assert interaction.shown_modals == [modal]
    assert interaction.last.layout == layout

    with pytest.raises(ModuleContractError, match="must continue to use layout"):
        await interaction.edit_original("legacy")

    with pytest.raises(ModuleContractError):
        await interaction.show_modal(ModalSpec("edit", "", modal.inputs))
    with pytest.raises(ModuleContractError):
        await interaction.respond(layout=OutgoingLayout(()))
    with pytest.raises(ModuleContractError, match="five action rows"):
        await interaction.respond(
            layout=layout,
            components=tuple(
                SelectSpec(f"select_{index}", (("One", "1", None),)) for index in range(6)
            ),
        )
    from kimi_agent_module_api.contracts import LayoutSection

    with pytest.raises(ModuleContractError, match="40 components"):
        await interaction.respond(
            layout=OutgoingLayout(
                tuple(
                    LayoutSection((str(index),), "https://example.com/thumb.png")
                    for index in range(14)
                )
            )
        )


def test_fake_interactions_reject_duplicate_and_invalid_registrations() -> None:
    from kimi_agent_module_api.contracts import CommandSpec, ModuleContractError
    from kimi_agent_module_api.testing import FakeInteractions

    router = FakeInteractions("example")

    async def handler(_interaction: object) -> None:
        pass

    router.add_command(CommandSpec(name="ping", description="Ping"), handler)
    with pytest.raises(ModuleContractError, match="already registered"):
        router.add_command(CommandSpec(name="ping", description="Ping again"), handler)

    router.register_component("button", "confirm", handler)
    with pytest.raises(ModuleContractError, match="already registered"):
        router.register_component("button", "confirm", handler)
    router.register_component("modal", "edit", handler)
    with pytest.raises(ModuleContractError, match="unsupported component kind"):
        router.register_component("other", "confirm", handler)  # type: ignore[arg-type]


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


def test_fake_service_proxy_stays_closed_after_a_re_provide() -> None:
    from kimi_agent_module_api.contracts import ServiceUnavailable
    from kimi_agent_module_api.testing import FakeServiceRegistry

    class Board:
        def answer(self) -> int:
            return 1

    registry = FakeServiceRegistry()
    registration = registry.provide("s", 1, Board())
    old = registry.get("s", 1, Board)
    registration.close()
    registry.provide("s", 1, Board())

    with pytest.raises(ServiceUnavailable):
        old.answer()
    assert registry.get("s", 1, Board).answer() == 1
    # A second live provider is refused, as in the host.
    from kimi_agent_module_api.contracts import ModuleContractError

    with pytest.raises(ModuleContractError):
        registry.provide("s", 1, Board())


@pytest.mark.parametrize(
    "spec",
    [
        CommandSpec(name="Bad", description="d"),
        CommandSpec(name="x", description="d", group="Bad"),
        CommandSpec(name="x", description=""),
        CommandSpec(name="x", description="d" * 101),
        CommandSpec(name="x", description="d", group="g", group_description="d" * 101),
        CommandSpec(
            name="x",
            description="d",
            options=tuple(CommandOption(f"o{i}", "string", "d") for i in range(26)),
        ),
        CommandSpec(
            name="x",
            description="d",
            options=(CommandOption("a", "string", "d"), CommandOption("a", "string", "d")),
        ),
        CommandSpec(name="x", description="d", options=(CommandOption("a", "string", ""),)),
        CommandSpec(name="x", description="d", options=(CommandOption("a", "string", "d" * 101),)),
        CommandSpec(
            name="x",
            description="d",
            options=(CommandOption("a", "number", "d"),),  # type: ignore[arg-type]
        ),
        CommandSpec(
            name="x",
            description="d",
            options=(
                CommandOption(
                    "a",
                    "string",
                    "d",
                    choices=tuple((f"n{i}", f"v{i}") for i in range(26)),
                ),
            ),
        ),
        CommandSpec(
            name="x",
            description="d",
            options=(CommandOption("a", "string", "d", choices=(("N", "v"),), autocomplete=True),),
        ),
        CommandSpec(
            name="x",
            description="d",
            options=(CommandOption("a", "boolean", "d", choices=(("N", "v"),)),),
        ),
        CommandSpec(
            name="x",
            description="d",
            options=(CommandOption("a", "user", "d", autocomplete=True),),
        ),
        CommandSpec(
            name="x",
            description="d",
            options=(CommandOption("a", "string", "d", choices=(("A", "v"), ("B", "v"))),),
        ),
        CommandSpec(
            name="x",
            description="d",
            options=(CommandOption("a", "integer", "d", min_value=5, max_value=2),),
        ),
        CommandSpec(
            name="x", description="d", options=(CommandOption("a", "string", "d", min_value=1),)
        ),
    ],
)
def test_command_spec_validation_rejects_payloads_discord_refuses(spec: CommandSpec) -> None:
    # tree.sync() is one bulk PUT, so a single malformed command rejects the
    # whole scope; discord.py checks names but none of these.
    with pytest.raises(ModuleContractError):
        validate_command_spec(spec)


def test_command_spec_validation_accepts_a_fully_populated_spec() -> None:
    validate_command_spec(
        CommandSpec(
            name="warn",
            description="Warn someone",
            group="mod",
            group_description="Moderation",
            options=(
                CommandOption("user", "user", "Who", required=True),
                CommandOption("days", "integer", "Days", min_value=0, max_value=7),
                CommandOption("mode", "string", "Mode", choices=(("Soft", "soft"),)),
                CommandOption("query", "string", "Query", autocomplete=True),
            ),
        )
    )


@pytest.mark.parametrize(
    "select",
    [
        SelectSpec(key="k", options=()),
        SelectSpec(key="k", options=tuple((f"l{i}", f"v{i}", None) for i in range(26))),
        SelectSpec(key="k", options=(("", "a", None),)),
        SelectSpec(key="k", options=(("A", "a" * 101, None),)),
        SelectSpec(key="k", options=(("A", "a", ""),)),
        SelectSpec(key="k", options=(("A", "a", None), ("B", "a", None)), max_values=2),
        SelectSpec(key="k", options=(("A", "a", None),), placeholder="p" * 151),
        SelectSpec(
            key="k", options=(("A", "a", None), ("B", "b", None)), min_values=2, max_values=1
        ),
        SelectSpec(key="k", options=(("A", "a", None),), min_values=26, max_values=26),
        SelectSpec(
            key="k", options=(("A", "a", None), ("B", "b", None)), min_values=3, max_values=5
        ),
        SelectSpec(key="Bad", options=(("A", "a", None),)),
        SelectSpec(key="k", options=(("A", "a", None),), parts=("bad:part",)),
    ],
)
def test_select_spec_validation_rejects_payloads_discord_refuses(select: SelectSpec) -> None:
    with pytest.raises(ModuleContractError):
        validate_select_spec(select)


def test_select_spec_validation_accepts_a_multi_select() -> None:
    validate_select_spec(
        SelectSpec(
            key="page",
            options=(("A", "a", None), ("B", "b", "second")),
            placeholder="Pick",
            min_values=0,
            max_values=2,
        )
    )


@pytest.mark.parametrize(
    "spec",
    [
        # discord.py checks command names but not option names, so an uppercase
        # one reached Discord verbatim and rejected the whole bulk PUT.
        CommandSpec(name="x", description="d", options=(CommandOption("Days", "string", "d"),)),
        CommandSpec(name="x", description="d", options=(CommandOption("", "string", "d"),)),
        CommandSpec(name="x", description="d", options=(CommandOption("a" * 33, "string", "d"),)),
        # Legal for Discord, impossible as a Python parameter: inspect.Parameter
        # would raise a bare ValueError from inside registration instead.
        CommandSpec(name="x", description="d", options=(CommandOption("my-opt", "string", "d"),)),
        CommandSpec(name="x", description="d", options=(CommandOption("1st", "string", "d"),)),
        CommandSpec(name="x", description="d", options=(CommandOption("class", "string", "d"),)),
        # A choice value whose type disagrees with its option is a 400.
        CommandSpec(
            name="x",
            description="d",
            options=(CommandOption("a", "integer", "d", choices=(("N", "text"),)),),
        ),
        CommandSpec(
            name="x",
            description="d",
            options=(CommandOption("a", "string", "d", choices=(("N", 7),)),),
        ),
    ],
)
def test_command_spec_validation_rejects_option_names_and_choice_types(spec: CommandSpec) -> None:
    with pytest.raises(ModuleContractError):
        validate_command_spec(spec)


def test_command_spec_validation_accepts_matching_choice_types() -> None:
    validate_command_spec(
        CommandSpec(
            name="x",
            description="d",
            options=(
                CommandOption("count", "integer", "d", choices=(("Seven", 7),)),
                CommandOption("mode", "string", "d", choices=(("Soft", "soft"),)),
            ),
        )
    )


@pytest.mark.parametrize(
    "option",
    [
        CommandOption("n", "integer", "d", choices=(("Too high", 1 << 53),)),
        CommandOption("n", "integer", "d", choices=(("Too low", -(1 << 53)),)),
        CommandOption("n", "integer", "d", min_value=-(1 << 53)),
        CommandOption("n", "integer", "d", max_value=1 << 53),
    ],
)
def test_command_spec_validation_rejects_integers_outside_discord_safe_range(
    option: CommandOption,
) -> None:
    with pytest.raises(ModuleContractError, match="between"):
        validate_command_spec(CommandSpec(name="x", description="d", options=(option,)))


def test_command_spec_validation_accepts_discord_safe_integer_endpoints() -> None:
    maximum = (1 << 53) - 1
    validate_command_spec(
        CommandSpec(
            name="x",
            description="d",
            options=(
                CommandOption(
                    "n",
                    "integer",
                    "d",
                    choices=(("Minimum", -maximum), ("Maximum", maximum)),
                    min_value=-maximum,
                    max_value=maximum,
                ),
            ),
        )
    )


@pytest.mark.parametrize("name", ["_query", "café", "検索"])
def test_command_spec_validation_accepts_discord_python_identifier_intersection(
    name: str,
) -> None:
    validate_command_spec(
        CommandSpec(
            name="x",
            description="d",
            options=(CommandOption(name, "string", "d"),),
        )
    )


@pytest.mark.parametrize(
    "button",
    [
        ButtonSpec(key="k", label=""),
        ButtonSpec(key="k", label="x" * 81),
        ButtonSpec(key="Bad", label="Go"),
        ButtonSpec(key="k", label="Go", style="rainbow"),  # type: ignore[arg-type]
        ButtonSpec(key="k", label="Go", parts=("bad:part",)),
        ButtonSpec(key="k", label="Go", emoji=""),
    ],
)
def test_button_spec_validation_rejects_payloads_discord_refuses(button: ButtonSpec) -> None:
    with pytest.raises(ModuleContractError):
        validate_component_spec(button)


def test_button_spec_validation_accepts_an_ordinary_button() -> None:
    validate_component_spec(ButtonSpec(key="confirm", label="Confirm", style="danger", emoji="✅"))


def test_button_spec_validation_accepts_an_emoji_without_a_label() -> None:
    validate_component_spec(ButtonSpec(key="confirm", label="", emoji="✅"))
