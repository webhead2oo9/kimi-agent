"""Module health registry: bounded, observable, and folded into the manager."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from commands.modules_cmd import render_manifest, render_status
from kimi_agent_module_api import (
    MODULE_API_VERSION,
    AppModule,
    ModuleLoadContext,
    ModulePermissions,
    ModuleRuntimeContext,
    ModuleSpec,
    ServiceDeclaration,
)
from kimi_agent_module_api.contracts import ModuleHealth, ScopedModuleMigration
from modules.health import HealthRegistry
from modules.testing import build_test_runtime


def test_registry_bounds_detail_and_metrics_and_redacts_secret_keys() -> None:
    registry = HealthRegistry(clock=lambda: 10.0)
    health = registry.set(
        "m",
        "degraded",
        "x" * 2000,
        {
            "errors": 3,
            "api_key_age": 1.0,
            "not_a_number": "nope",  # type: ignore[dict-item]
            **{f"k{i}": i for i in range(60)},
        },
    )
    assert len(health.detail) == 500
    assert "api_key_age" not in health.metrics
    assert "not_a_number" not in health.metrics
    assert len(health.metrics) <= 32
    assert health.metrics["errors"] == 3.0
    assert health.updated_at == 10.0
    assert registry.get("m") == health


def test_registry_notifies_observers_and_survives_observer_errors() -> None:
    seen: list[tuple[str, str]] = []

    def observer(name: str, health: ModuleHealth) -> None:
        seen.append((name, health.state))
        raise RuntimeError("observer bug")

    registry = HealthRegistry(on_change=observer)
    registry.reporter_for("a").report("healthy")
    registry.set("b", "failed", "boom")
    assert seen == [("a", "healthy"), ("b", "failed")]
    assert registry.snapshot()["b"].state == "failed"
    registry.forget("b")
    assert set(registry.snapshot()) == {"a"}


def test_core_constraints_survive_module_reports_and_clear_independently() -> None:
    registry = HealthRegistry(clock=lambda: 10.0)
    registry.set_constraint("m", "guild_settings", "degraded", "invalid guild settings")
    registry.set("m", "starting")
    registry.reporter_for("m").report("healthy")
    assert registry.get("m") == ModuleHealth("degraded", "invalid guild settings", updated_at=10.0)

    registry.set_constraint("m", "scheduler", "degraded", "missing handler")
    health = registry.get("m")
    assert health is not None and health.detail == "invalid guild settings; missing handler"
    registry.set_constraint("m", "guild_settings", "healthy")
    health = registry.get("m")
    assert health is not None and health.detail == "missing handler"
    registry.set_constraint("m", "scheduler", "healthy")
    health = registry.get("m")
    assert health is not None and health.state == "healthy"


class _Module:
    scoped_migrations: Sequence[ScopedModuleMigration] = ()

    def __init__(self, *, report: str | None = None, fail: bool = False) -> None:
        self.report = report
        self.fail = fail
        self.closed = False

    async def start(self, ctx: ModuleRuntimeContext) -> None:
        assert ctx.health is not None
        if self.report:
            ctx.health.report("degraded", self.report, {"queue": 2})
        if self.fail:
            raise RuntimeError("cannot start")

    async def close(self) -> None:
        self.closed = True


def _spec(name: str, module: _Module, **overrides: object) -> ModuleSpec:
    def create(_ctx: ModuleLoadContext) -> AppModule:
        return module

    api_version = overrides.pop("api_version", MODULE_API_VERSION)
    return ModuleSpec(
        name=name,
        version="1.0.0",
        create=create,
        api_version=api_version,  # type: ignore[arg-type]
        **overrides,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_manager_transitions_health_around_start(tmp_path: Path) -> None:
    quiet = _Module()
    noisy = _Module(report="hub unreachable")
    runtime = await build_test_runtime(
        tmp_path,
        ["quiet", "noisy"],
        installed={"quiet": _spec("quiet", quiet), "noisy": _spec("noisy", noisy)},
    )
    try:
        health = runtime.manager.health.snapshot()
        assert health["quiet"].state == "healthy"
        assert health["noisy"].state == "degraded"
        assert health["noisy"].detail == "hub unreachable"
        text = render_status(
            ("quiet", "noisy"), runtime.manager.specs, health, now=health["noisy"].updated_at
        )
        assert "✅ `quiet` 1.0.0 — healthy" in text
        assert "⚠️ `noisy` 1.0.0 — degraded (0s ago): hub unreachable" in text
        assert "queue=2" in text
    finally:
        await runtime.close()
    assert runtime.manager.health.snapshot() == {}


@pytest.mark.asyncio
async def test_failed_start_marks_failed_and_aborts(tmp_path: Path) -> None:
    ok = _Module()
    bad = _Module(fail=True)
    with pytest.raises(RuntimeError, match="cannot start"):
        await build_test_runtime(
            tmp_path,
            ["ok", "bad"],
            installed={"ok": _spec("ok", ok), "bad": _spec("bad", bad, dependencies=("ok",))},
        )
    assert ok.closed is True


def test_render_manifest_lists_declarations_and_escapes() -> None:
    spec = _spec(
        "guard",
        _Module(),
        activation_capabilities=("discord.message_content.v1",),
        permissions=ModulePermissions(
            discord_actions=frozenset({"ban", "send_message"}),
            event_topics=("discord.message",),
            raw_bot=True,
        ),
        provides=(ServiceDeclaration("records.cases", 1),),
    )
    text = render_manifest(
        {"guard": spec}, {"guard": ModuleHealth("healthy")}, lambda _n: ("tool_a",)
    )
    assert "provides: records.cases@1" in text
    assert "discord actions: ban, send_message" in text
    assert "activation capabilities: discord.message_content.v1" in text
    assert "escape hatches: raw_bot" in text
    assert "llm tools: tool_a" in text
    assert render_status((), {}, {}).startswith("No application modules")


def test_render_status_explains_soft_disabled_modules() -> None:
    text = render_status(
        ("discord_rag",),
        {},
        {},
        disabled={
            "discord_rag": (
                "0.1.0",
                "missing activation capability discord.message_content.v1",
            )
        },
    )

    assert text.splitlines()[1] == (
        "• ⏸️ `discord_rag` 0.1.0 — disabled: "
        "missing activation capability discord.message_content.v1"
    )


def test_keyed_reports_are_independent_concerns() -> None:
    registry = HealthRegistry(clock=lambda: 1.0)
    reporter = registry.reporter_for("m")

    reporter.report("degraded", "digest failed", {"digest_failures": 1}, key="digest")
    reporter.report("healthy", "", {"guilds": 2})

    health = registry.get("m")
    assert health is not None
    assert health.state == "degraded"
    assert health.detail == "digest failed"
    assert health.metrics == {"digest_failures": 1.0, "guilds": 2.0}

    # A keyed healthy report with nothing attached clears that concern only.
    reporter.report("healthy", key="digest")
    cleared = registry.get("m")
    assert cleared is not None and cleared.state == "healthy"
    assert cleared.metrics == {"guilds": 2.0}


def test_keyed_reports_cannot_clear_core_constraints() -> None:
    registry = HealthRegistry(clock=lambda: 1.0)
    registry.mark("m", "degraded", "paused", source="scheduler")

    registry.reporter_for("m").report("healthy", key="scheduler")

    health = registry.get("m")
    assert health is not None and health.state == "degraded"


def test_fake_health_state_is_the_worst_across_keys() -> None:
    from kimi_agent_module_api.testing import FakeHealth

    fake = FakeHealth()
    fake.report("healthy", "", {"guilds": 1})
    fake.report("degraded", "digest failed", key="digest")

    assert fake.state == "degraded"
    assert [key for key, _ in fake.history] == [None, "digest"]
    fake.report("healthy", key="digest")
    assert fake.state == "healthy"
