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
from config.settings import Settings
from kimi_agent_module_api import (
    MODULE_API_VERSION,
    GuildSettingsSchema,
    ModuleLoadContext,
    ModulePermissions,
    ModuleRuntimeContext,
    ModuleSpec,
    ServiceDeclaration,
    ServiceRequirement,
)
from kimi_agent_module_api.contracts import (
    ALL_DISCORD_ACTIONS,
    CUSTOM_ID_MAX_LENGTH,
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


# --- naming rules ---------------------------------------------------------


def test_table_prefix_normalizes_hyphens() -> None:
    assert table_prefix("image-fingerprints") == "image_fingerprints"


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

    Checked statically: importing a submodule runs the package ``__init__``, which
    still re-exports concrete core types until the cutover removes them.
    """
    path = Path(__file__).resolve().parents[1] / "kimi_agent_module_api" / f"{module}.py"
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
