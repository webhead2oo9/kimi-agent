"""The declarations are valid and load-time wiring registers what the docs promise.

These tests run the same validators the host runs at preflight, so a broken
declaration fails here, before the first bot start.
"""

from __future__ import annotations

import pytest
from kimi_agent_module_api import MODULE_API_VERSION, TrustTier
from kimi_agent_module_api.contracts import (
    validate_guild_settings_schema,
    validate_module_name,
    validate_permissions,
    validate_services,
    validate_subscription,
)
from kimi_agent_module_api.events import TOPIC_MEMBER_REMOVE
from kimi_agent_module_api.testing import load_context

from community_agent_reference_module import SPEC
from community_agent_reference_module.guild_settings import FIELD_DIGEST_CHANNEL, FIELD_GIVER_TIER
from community_agent_reference_module.module import (
    MODULE_NAME,
    TOOL_GIVE,
    TOOL_LEADERBOARD,
    KudosModule,
)
from community_agent_reference_module.settings import KudosSettings
from community_agent_reference_module.spec import create


def test_declarations_pass_host_preflight() -> None:
    assert SPEC.name == MODULE_NAME
    assert SPEC.api_version == MODULE_API_VERSION
    validate_module_name(SPEC.name)
    validate_permissions(SPEC.name, SPEC.permissions)
    validate_services(SPEC.name, SPEC.dependencies, SPEC.provides, SPEC.consumes)
    assert SPEC.guild_settings is not None
    validate_guild_settings_schema(SPEC.name, SPEC.guild_settings)
    # The one core topic the module subscribes to is covered by its declaration.
    validate_subscription(SPEC.name, SPEC.permissions, TOPIC_MEMBER_REMOVE)


def test_guild_validator_rejects_a_digest_for_staff_only_guilds() -> None:
    assert SPEC.guild_settings is not None and SPEC.guild_settings.validate is not None
    validate = SPEC.guild_settings.validate
    assert validate({FIELD_GIVER_TIER: "staff", FIELD_DIGEST_CHANNEL: 1}) != []
    assert validate({FIELD_GIVER_TIER: "regular", FIELD_DIGEST_CHANNEL: 1}) == []


def test_settings_definition_classifies_every_field() -> None:
    assert SPEC.settings is not None
    classified = {setting.field for setting in SPEC.settings.exposed}
    classified |= SPEC.settings.environment_only
    assert classified == set(KudosSettings.model_fields)


def test_create_registers_tools_and_labels() -> None:
    context, recorder = load_context(KudosSettings())

    module = create(context)

    registry, labels = recorder.registry, recorder.labels
    assert isinstance(module, KudosModule)
    assert registry.tools[TOOL_GIVE].min_tier is TrustTier.MEMBER
    assert registry.tools[TOOL_GIVE].searchable is False
    assert registry.tools[TOOL_LEADERBOARD].searchable is True
    assert set(labels) == {TOOL_GIVE, TOOL_LEADERBOARD}
    assert set(registry.tools[TOOL_GIVE].parameters["required"]) == {"user", "reason"}


def test_settings_read_the_module_env_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REFERENCE_KUDOS_DAILY_LIMIT", "9")

    assert KudosSettings().daily_limit == 9
