"""Plugin loader: contract, per-plugin isolation, rollback, and label merging."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest

import agent.activity as activity
from app import tool_surfaces
from app.plugins import (
    PLUGIN_API_VERSION,
    PluginContext,
    build_plugin_context,
    load_plugins_with_settings,
)
from config.settings import Settings
from discord_adapter.gateway import DiscordGateway
from tools.registry import MessageContext, ToolRegistry
from trust.tiers import TrustTier


def _settings(**kwargs: object) -> Settings:
    return Settings.model_validate(kwargs)


def _ctx(registry: ToolRegistry) -> PluginContext:
    return build_plugin_context(
        _settings(),
        registry,
        gateway=cast(DiscordGateway, object()),
    )


async def _noop_handler(args: dict, ctx: MessageContext) -> str:
    return "ok"


def _register_tool(registry: ToolRegistry, name: str) -> None:
    registry.register(
        name=name,
        description=f"{name} description",
        parameters={"type": "object", "properties": {}},
        handler=_noop_handler,
        min_tier=TrustTier.MEMBER,
    )


def _fake_module(monkeypatch: pytest.MonkeyPatch, name: str, **attrs: Any) -> ModuleType:
    module = ModuleType(name)
    attrs.setdefault("PLUGIN_API_VERSION", PLUGIN_API_VERSION)
    for key, value in attrs.items():
        setattr(module, key, value)
    monkeypatch.setitem(sys.modules, name, module)
    return module


def _load(module_names: tuple[str, ...], ctx: PluginContext) -> None:
    load_plugins_with_settings(module_names, ctx)


def test_load_plugins_registers_tools_and_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ToolRegistry()
    monkeypatch.setattr(activity, "_TOOL_LABELS", dict(activity._TOOL_LABELS))

    def register(ctx: PluginContext) -> None:
        _register_tool(ctx.registry, "fake_plugin_tool")
        ctx.register_tool_labels({"fake_plugin_tool": "Doing fake things"})

    _fake_module(monkeypatch, "fake_plugin", register=register)

    _load(("fake_plugin",), _ctx(registry))

    assert registry.is_registered("fake_plugin_tool")
    assert activity.tool_display_label("fake_plugin_tool") == "Doing fake things"


def test_failed_plugin_is_skipped_and_rolled_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ToolRegistry()
    _register_tool(registry, "core_tool")

    def broken_register(ctx: PluginContext) -> None:
        _register_tool(ctx.registry, "broken_tool_a")
        _register_tool(ctx.registry, "broken_tool_b")
        raise RuntimeError("boom")

    def good_register(ctx: PluginContext) -> None:
        _register_tool(ctx.registry, "good_tool")

    _fake_module(monkeypatch, "broken_plugin", register=broken_register)
    _fake_module(monkeypatch, "good_plugin", register=good_register)

    _load(("broken_plugin", "good_plugin"), _ctx(registry))

    assert not registry.is_registered("broken_tool_a")
    assert not registry.is_registered("broken_tool_b")
    assert registry.is_registered("good_tool")
    assert registry.is_registered("core_tool")


def test_plugin_declares_surface_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tool_surfaces, "_SURFACE_TOOLS", {})
    registry = ToolRegistry()

    def register(ctx: PluginContext) -> None:
        _register_tool(ctx.registry, "plugin_writer")
        ctx.declare_surface_tools("eval_record", ["plugin_writer"])
        ctx.declare_surface_tools("eval_stub", ["plugin_writer"])

    _fake_module(monkeypatch, "surface_plugin", register=register)

    _load(("surface_plugin",), _ctx(registry))
    assert tool_surfaces.surface_tools("eval_record") == frozenset({"plugin_writer"})
    assert tool_surfaces.surface_tools("eval_stub") == frozenset({"plugin_writer"})


def test_failed_plugin_rolls_back_surface_declarations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tool_surfaces, "_SURFACE_TOOLS", {})
    registry = ToolRegistry()

    def broken_register(ctx: PluginContext) -> None:
        _register_tool(ctx.registry, "broken_tool")
        ctx.declare_surface_tools("eval_stub", ["broken_tool"])
        raise RuntimeError("boom")

    def good_register(ctx: PluginContext) -> None:
        _register_tool(ctx.registry, "good_tool")
        ctx.declare_surface_tools("eval_stub", ["good_tool"])

    _fake_module(monkeypatch, "broken_surface_plugin", register=broken_register)
    _fake_module(monkeypatch, "good_surface_plugin", register=good_register)

    _load(("broken_surface_plugin", "good_surface_plugin"), _ctx(registry))

    # A skipped plugin leaves nothing behind, not even a stale declaration.
    assert tool_surfaces.surface_tools("eval_stub") == frozenset({"good_tool"})


def test_unknown_surface_name_skips_the_whole_plugin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tool_surfaces, "_SURFACE_TOOLS", {})
    registry = ToolRegistry()

    def register(ctx: PluginContext) -> None:
        _register_tool(ctx.registry, "typo_tool")
        ctx.declare_surface_tools("factcheck", ["typo_tool"])  # not a surface

    _fake_module(monkeypatch, "typo_plugin", register=register)

    _load(("typo_plugin",), _ctx(registry))
    # Fail-closed: the tool is gone entirely rather than live on Fact Check.
    assert not registry.is_registered("typo_tool")


def test_duplicate_tool_name_resolves_in_cores_favor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ToolRegistry()
    _register_tool(registry, "core_tool")

    def register(ctx: PluginContext) -> None:
        _register_tool(ctx.registry, "plugin_only_tool")
        _register_tool(ctx.registry, "core_tool")  # raises: duplicate

    _fake_module(monkeypatch, "colliding_plugin", register=register)

    _load(("colliding_plugin",), _ctx(registry))

    assert registry.is_registered("core_tool")
    # The rollback removes the plugin's partial registrations, not core's tool.
    assert not registry.is_registered("plugin_only_tool")


def test_unknown_missing_or_mismatched_version_and_missing_register_are_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ToolRegistry()
    _fake_module(
        monkeypatch,
        "old_plugin",
        PLUGIN_API_VERSION=PLUGIN_API_VERSION + 1,
        register=lambda ctx: None,
    )
    _fake_module(monkeypatch, "registerless_plugin")
    missing_version = ModuleType("unversioned_plugin")
    missing_version.register = lambda ctx: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "unversioned_plugin", missing_version)

    _load(
        (
            "does_not_exist_plugin",
            "old_plugin",
            "registerless_plugin",
            "unversioned_plugin",
        ),
        _ctx(registry),
    )

    assert registry.registered_names() == frozenset()


def test_register_tool_labels_first_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(activity, "_TOOL_LABELS", dict(activity._TOOL_LABELS))
    existing = activity.tool_display_label("read_file")

    activity.register_tool_labels({"read_file": "Hijacked label", "new_tool": "New label"})

    assert activity.tool_display_label("read_file") == existing
    assert activity.tool_display_label("new_tool") == "New label"


def test_build_runtime_tools_loads_configured_plugins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: PLUGIN_MODULES flows through the composition root."""
    from app.providers import ProviderManager
    from app.tools import build_runtime_tools
    from tests.helpers import StubProviderManager

    seen: dict[str, Any] = {}

    def register(ctx: PluginContext) -> None:
        seen["gateway"] = ctx.gateway
        _register_tool(ctx.registry, "wired_plugin_tool")

    _fake_module(monkeypatch, "wired_plugin", register=register)

    settings = _settings(
        workspace_dir=str(tmp_path / "workspaces"),
        attachment_store_dir=str(tmp_path / "attachments"),
        secrets_file=str(tmp_path / "secrets.yaml"),
        model_api_key="main-key",
        plugin_modules="wired_plugin",
        browser_enabled=False,
    )
    gateway = object()

    runtime_tools = build_runtime_tools(
        settings,
        cast(DiscordGateway, gateway),
        cast(ProviderManager, StubProviderManager(settings)),
    )

    assert runtime_tools.registry.is_registered("wired_plugin_tool")
    assert seen["gateway"] is gateway


def test_core_imports_without_any_plugin_package_installed() -> None:
    """Core must never import a plugin package at module scope.

    Plugin packages are droppable by definition: they load by name through
    PLUGIN_MODULES at composition time, and this deployment configures none.
    Importing the composition modules in a clean subprocess therefore fails
    outright if any of them grew a module-scope ``import <some_plugin>``, which
    is what keeps adding a plugin a file move rather than a code change.
    """
    code = "import app.tools, app.plugins, tools.plan, tools.channel_context\n"
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
