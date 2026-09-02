"""Representative contract tests owned by the standalone SDK package."""

from __future__ import annotations

import dataclasses

import pytest
from pydantic_settings import BaseSettings

from kimi_agent_module_api import (
    BASELINE_CAPABILITIES,
    MODULE_API_VERSION,
    ModuleCapabilities,
    ModuleLoadContext,
    ModulePermissions,
    ModuleRuntimeContext,
    ModuleSpec,
    TrustTier,
    render_guild_settings,
)
from kimi_agent_module_api.contracts import (
    Backoff,
    ButtonSpec,
    CommandSpec,
    EventTopicError,
    GuildSettingField,
    GuildSettingsSchema,
    ModuleContractError,
    ServiceDeclaration,
    ServiceRequirement,
    build_custom_id,
    parse_custom_id,
    split_topic,
    table_prefix,
    validate_command_spec,
    validate_component_spec,
    validate_guild_settings_schema,
    validate_module_name,
    validate_permissions,
    validate_publish_topic,
    validate_services,
    validate_subscription,
)
from kimi_agent_module_api.events import CORE_TOPICS
from kimi_agent_module_api.images import looks_like_image_attachment, sniff_image_media_type
from kimi_agent_module_api.testing import load_context


class DemoSettings(BaseSettings):
    greeting: str = "hello"


class OtherSettings(BaseSettings):
    pass


def _spec(name: str = "demo") -> ModuleSpec:
    def create(_ctx: ModuleLoadContext) -> object:
        raise AssertionError("not called")

    return ModuleSpec(name=name, version="1.0.0", create=create)  # type: ignore[arg-type]


def test_spec_and_runtime_context_keep_stable_defaults() -> None:
    spec = _spec()
    assert spec.api_version == MODULE_API_VERSION == 1
    assert spec.permissions == ModulePermissions()
    assert spec.dependencies == ()
    required = {
        field.name
        for field in dataclasses.fields(ModuleRuntimeContext)
        if field.default is dataclasses.MISSING
    }
    assert {"events", "scheduler", "storage", "discord", "interactions", "services"} <= required


def test_load_context_exercises_public_create_helpers() -> None:
    settings = DemoSettings(greeting="hi")
    context, recorder = load_context(settings)

    assert context.settings_for(DemoSettings) is settings
    context.register_tool_labels({"demo": "Doing a demo"})
    context.declare_surface_tools("default", ("demo",))

    assert recorder.labels == {"demo": "Doing a demo"}
    assert recorder.surfaces == {"default": ("demo",)}
    assert context.capabilities.available == BASELINE_CAPABILITIES
    with pytest.raises(TypeError, match="prepared module settings"):
        context.settings_for(OtherSettings)


def test_capabilities_and_trust_tiers_fail_closed() -> None:
    capabilities = ModuleCapabilities(BASELINE_CAPABILITIES, False, False)
    capabilities.require("discord.history.v1")
    with pytest.raises(RuntimeError, match="does not provide"):
        capabilities.require("discord.guild_commands.v1")
    assert TrustTier.STAFF > TrustTier.REGULAR > TrustTier.MEMBER


@pytest.mark.parametrize("name", ["Bad", "two words", "discord", "proposals"])
def test_module_names_reject_reserved_or_malformed_values(name: str) -> None:
    with pytest.raises(ModuleContractError):
        validate_module_name(name)


def test_topics_and_component_ids_enforce_module_namespaces() -> None:
    assert table_prefix("audit-log") == "audit_log"
    assert split_topic("discord.message") == ("discord", "message")
    validate_publish_topic("case-manager", "case_manager.changed")
    validate_subscription(
        "case_audit",
        ModulePermissions(event_topics=("case_manager.*",)),
        "case_manager.changed",
    )
    custom_id = build_custom_id("case_manager", "approve", "123")
    assert parse_custom_id(custom_id) == ("case_manager", "approve", ("123",))
    with pytest.raises(EventTopicError):
        validate_publish_topic("case-manager", "other.changed")
    with pytest.raises(ModuleContractError):
        build_custom_id("case_manager", "approve", "bad:part")


def test_permissions_services_and_guild_settings_validate_together() -> None:
    permissions = ModulePermissions(
        discord_actions=frozenset({"send_message"}),
        event_topics=("discord.message",),
    )
    validate_permissions("demo", permissions)
    validate_services(
        "consumer",
        ("provider",),
        (),
        (ServiceRequirement("records.cases", 1, provider="provider"),),
    )
    validate_services("provider", (), (ServiceDeclaration("records.cases", 1),), ())
    schema = GuildSettingsSchema(
        fields=(GuildSettingField("mode", "enum", choices=("safe", "fast"), default="safe"),)
    )
    validate_guild_settings_schema("demo", schema)

    with pytest.raises(ModuleContractError):
        validate_permissions("demo", ModulePermissions(discord_actions=frozenset({"nuke"})))
    with pytest.raises(ModuleContractError):
        validate_guild_settings_schema(
            "demo",
            GuildSettingsSchema(fields=(GuildSettingField("Bad", "int"),)),
        )


def test_command_and_component_validation_match_discord_limits() -> None:
    validate_command_spec(CommandSpec(name="ping", description="Ping the module"))
    validate_component_spec(ButtonSpec("confirm", "Confirm"))
    with pytest.raises(ModuleContractError):
        validate_command_spec(CommandSpec(name="Bad", description="invalid name"))
    with pytest.raises(ModuleContractError):
        validate_component_spec(ButtonSpec("confirm", ""))


def test_scheduler_backoff_rejects_non_finite_or_non_positive_values() -> None:
    assert Backoff(base_seconds=2, max_seconds=8, multiplier=2) == Backoff(2, 8, 2)
    for invalid in (0, -1, float("nan"), float("inf")):
        with pytest.raises(ModuleContractError, match="backoff"):
            Backoff(base_seconds=invalid)


def test_rendered_settings_are_deterministic_and_safe() -> None:
    assert render_guild_settings({"b": True, "a": [1, 2], "c": "x: y", "empty": None}) == (
        '---\na: [1, 2]\nb: true\nc: "x: y"\n---\n'
    )
    with pytest.raises(ValueError, match="invalid guild setting name"):
        render_guild_settings({"bad:key": True})


def test_event_and_image_helpers_remain_host_independent() -> None:
    assert CORE_TOPICS
    assert all(split_topic(topic)[0] == "discord" for topic in CORE_TOPICS)
    assert looks_like_image_attachment("photo.PNG", None)
    assert looks_like_image_attachment(None, "image/jpeg")
    assert sniff_image_media_type(b"\x89PNG\r\n\x1a\nrest") == "image/png"
    assert sniff_image_media_type(b"not an image") is None
