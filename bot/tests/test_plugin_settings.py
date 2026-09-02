"""Plugin-owned operator settings: declarations, startup overlays, and isolation."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest
from pydantic import SecretStr
from pydantic_settings import BaseSettings

from app.plugins import PluginContext, build_plugin_context, load_plugins_with_settings
from config.plugin_settings import (
    PluginSetting,
    PluginSettingsDefinition,
    PluginSettingsError,
    PluginSettingsRegistry,
)
from config.settings import Settings
from discord_adapter.gateway import DiscordGateway
from tools.registry import ToolRegistry


class DemoSettings(BaseSettings):
    model_config = {"extra": "ignore"}

    demo_url: str = ""
    demo_api_key: SecretStr = SecretStr("")
    demo_limit: int = 3


class EndpointSettings(BaseSettings):
    model_config = {"extra": "ignore"}

    service_endpoint: str = ""


class BareBoundarySettings(BaseSettings):
    model_config = {"extra": "ignore"}

    base: str = ""
    bin: str = ""
    dir: str = ""
    directory: str = ""
    endpoint: str = ""
    file: str = ""
    host: str = ""
    path: str = ""
    uri: str = ""
    url: str = ""
    api_key: str = ""
    key: str = ""
    token: str = ""
    secret: str = ""
    password: str = ""
    credential: str = ""
    credentials: str = ""
    signing_key: str = ""


DEMO_DEFINITION = PluginSettingsDefinition(
    name="demo",
    label="Demo",
    model=DemoSettings,
    exposed=(PluginSetting("demo_limit", "Demo limit", minimum=1),),
    environment_only=frozenset({"demo_url", "demo_api_key"}),
)


def _ctx(tmp_path: Path) -> PluginContext:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        config_dir=str(tmp_path),
    )
    return build_plugin_context(
        settings,
        ToolRegistry(),
        gateway=cast(DiscordGateway, object()),
    )


def _module(monkeypatch: pytest.MonkeyPatch, register) -> None:
    module = ModuleType("demo_plugin")
    module.PLUGIN_SETTINGS = DEMO_DEFINITION  # type: ignore[attr-defined]
    module.register = register  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "demo_plugin", module)


def test_incomplete_declaration_is_rejected_before_plugin_can_load(tmp_path: Path) -> None:
    incomplete = PluginSettingsDefinition(
        name="demo",
        label="Demo",
        model=DemoSettings,
        exposed=(PluginSetting("demo_limit", "Demo limit", minimum=1),),
        environment_only=frozenset({"demo_url"}),
    )
    registry = PluginSettingsRegistry(config_dir=tmp_path)

    with pytest.raises(PluginSettingsError, match="unclassified"):
        registry.prepare(incomplete)


def test_endpoint_synonyms_cannot_be_exposed(tmp_path: Path) -> None:
    unsafe = PluginSettingsDefinition(
        name="unsafe",
        label="Unsafe",
        model=EndpointSettings,
        exposed=(PluginSetting("service_endpoint", "Service endpoint"),),
        environment_only=frozenset(),
    )

    with pytest.raises(PluginSettingsError, match="environment-only"):
        PluginSettingsRegistry(config_dir=tmp_path).prepare(unsafe)


@pytest.mark.parametrize("field", sorted(BareBoundarySettings.model_fields))
def test_bare_and_generic_deployment_boundary_names_cannot_be_exposed(
    tmp_path: Path, field: str
) -> None:
    unsafe = PluginSettingsDefinition(
        name="unsafe",
        label="Unsafe",
        model=BareBoundarySettings,
        exposed=(PluginSetting(field, "Unsafe field"),),
        environment_only=frozenset(set(BareBoundarySettings.model_fields) - {field}),
    )

    with pytest.raises(PluginSettingsError, match="environment-only"):
        PluginSettingsRegistry(config_dir=tmp_path).prepare(unsafe)


@pytest.mark.parametrize("field", ["demo_url", "demo_api_key"])
def test_endpoint_and_secret_fields_cannot_be_declared_exposed(tmp_path: Path, field: str) -> None:
    exposed = (
        PluginSetting("demo_limit", "Demo limit", minimum=1),
        PluginSetting(field, "Unsafe field"),
    )
    unsafe = PluginSettingsDefinition(
        name="demo",
        label="Demo",
        model=DemoSettings,
        exposed=exposed,
        environment_only=frozenset(
            set(DemoSettings.model_fields) - {entry.field for entry in exposed}
        ),
    )

    with pytest.raises(PluginSettingsError, match="environment-only"):
        PluginSettingsRegistry(config_dir=tmp_path).prepare(unsafe)


def test_presentation_metadata_must_match_the_field_kind(tmp_path: Path) -> None:
    invalid = PluginSettingsDefinition(
        name="demo",
        label="Demo",
        model=DemoSettings,
        exposed=(PluginSetting("demo_limit", "Demo limit", minimum=1, multiline=True),),
        environment_only=frozenset({"demo_url", "demo_api_key"}),
    )

    with pytest.raises(PluginSettingsError, match="multiline is only valid"):
        PluginSettingsRegistry(config_dir=tmp_path).prepare(invalid)


def test_invalid_saved_fragment_remains_repairable_but_skips_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fragment = tmp_path / "plugins" / "demo.md"
    fragment.parent.mkdir()
    fragment.write_text("---\ndemo_limit: 0\n---\n", encoding="utf-8")
    called = False

    def register(_ctx: PluginContext) -> None:
        nonlocal called
        called = True

    _module(monkeypatch, register)
    registry = PluginSettingsRegistry(config_dir=tmp_path)
    loaded, registry = load_plugins_with_settings(
        ("demo_plugin",), _ctx(tmp_path), settings_registry=registry
    )

    entry = registry.get("demo")
    assert loaded == []
    assert called is False
    assert entry is not None
    assert entry.can_register is False
    assert "must be at least 1" in str(entry.load_error)


def test_saved_override_instance_is_the_exact_instance_consumed_by_register(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fragment = tmp_path / "plugins" / "demo.md"
    fragment.parent.mkdir()
    fragment.write_text("---\ndemo_limit: 9\n---\n", encoding="utf-8")
    seen: dict[str, object] = {}

    def register(ctx: PluginContext) -> None:
        seen["raw"] = ctx.plugin_settings
        seen["typed"] = ctx.settings_for(DemoSettings)

    _module(monkeypatch, register)
    loaded, registry = load_plugins_with_settings(
        ("demo_plugin",),
        _ctx(tmp_path),
        settings_registry=PluginSettingsRegistry(config_dir=tmp_path),
    )

    entry = registry.get("demo")
    assert loaded == ["demo_plugin"]
    assert entry is not None
    assert entry.active.demo_limit == 9  # type: ignore[attr-defined]
    assert seen["raw"] is entry.active
    assert seen["typed"] is entry.active


def test_selected_env_file_backs_plugin_settings_instead_of_dotenv_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text("DEMO_LIMIT=2\n", encoding="utf-8")
    (tmp_path / ".env.alt").write_text("DEMO_LIMIT=8\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ENV_FILE", ".env.alt")
    monkeypatch.delenv("DEMO_LIMIT", raising=False)

    entry = PluginSettingsRegistry(config_dir=tmp_path).prepare(DEMO_DEFINITION)

    assert entry.inherited.demo_limit == 8  # type: ignore[attr-defined]
