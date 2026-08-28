"""The module integration harness composes real core the way the bot does."""

from __future__ import annotations

from pathlib import Path

import pytest

from community_agent_module_api import (
    ModuleLoadContext,
    ModulePermissions,
    ModuleRuntimeContext,
    ModuleSpec,
)
from community_agent_module_api.contracts import (
    MigrationContext,
    ScopedModuleMigration,
    UndeclaredDiscordAction,
)
from community_agent_module_api.testing import FakeInteraction, FakeScheduler
from modules.testing import build_test_runtime, write_guild_config


class RecordingModule:
    def __init__(self, name: str, log: list[str]) -> None:
        self.name = name
        self.log = log
        self.ctx: ModuleRuntimeContext | None = None
        self.scoped_migrations: tuple[ScopedModuleMigration, ...] = (("init", self._migrate),)

    async def _migrate(self, ctx: MigrationContext) -> None:
        await ctx.connection.execute(
            f"CREATE TABLE IF NOT EXISTS {self.name}_rows (id INTEGER PRIMARY KEY)"
        )

    async def start(self, ctx: ModuleRuntimeContext) -> None:
        self.ctx = ctx
        self.log.append(f"start:{self.name}")

    async def close(self) -> None:
        self.log.append(f"close:{self.name}")


def _spec(name: str, module: RecordingModule, **overrides: object) -> ModuleSpec:
    def create(_ctx: ModuleLoadContext) -> RecordingModule:
        return module

    return ModuleSpec(name=name, version="0.0.1", create=create, **overrides)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_harness_starts_modules_with_per_module_contexts(tmp_path: Path) -> None:
    log: list[str] = []
    alpha = RecordingModule("alpha", log)
    beta = RecordingModule("beta", log)
    runtime = await build_test_runtime(
        tmp_path,
        ["beta", "alpha"],
        installed={
            "alpha": _spec("alpha", alpha),
            "beta": _spec("beta", beta, dependencies=("alpha",)),
        },
    )
    try:
        assert log == ["start:alpha", "start:beta"]
        assert alpha.ctx is not None and beta.ctx is not None
        assert alpha.ctx.module_name == "alpha"
        assert beta.ctx.module_name == "beta"
        assert alpha.ctx is runtime.ctx_for("alpha")
        assert alpha.ctx.events is not beta.ctx.events
        assert alpha.ctx.storage.table("x") == '"alpha_x"'
        assert runtime.manager.health.get("alpha") is not None
        assert alpha.ctx.current_config_dir() == runtime.config_dir

        cursor = await runtime.database.conn.execute(
            "SELECT module_name, version FROM module_schema_versions ORDER BY module_name"
        )
        assert [tuple(row) for row in await cursor.fetchall()] == [("alpha", 1), ("beta", 1)]
    finally:
        await runtime.close()
    assert log[-2:] == ["close:beta", "close:alpha"]


@pytest.mark.asyncio
async def test_harness_fakes_enforce_declared_discord_actions(tmp_path: Path) -> None:
    module = RecordingModule("guarded", [])
    runtime = await build_test_runtime(
        tmp_path,
        ["guarded"],
        installed={
            "guarded": _spec(
                "guarded",
                module,
                permissions=ModulePermissions(discord_actions=frozenset({"send_message"})),
            )
        },
    )
    try:
        ctx = runtime.ctx_for("guarded")
        await ctx.discord.send_message(42, "hello")
        assert runtime.ports["guarded"].discord.calls_for("send_message")[0].args[0] == 42
        with pytest.raises(UndeclaredDiscordAction):
            await ctx.discord.ban(1, 2, actor_id=3, reason="nope")
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_harness_writes_guild_config_and_exposes_fakes(tmp_path: Path) -> None:
    module = RecordingModule("cfg", [])
    runtime = await build_test_runtime(
        tmp_path,
        ["cfg"],
        installed={"cfg": _spec("cfg", module)},
        guild_config={7: {"mod_log_channel_id": 99, "mod_log_events": ["ban", "kick"]}},
    )
    try:
        text = (runtime.config_dir / "servers" / "7.md").read_text(encoding="utf-8")
        assert "mod_log_channel_id: 99" in text
        assert "mod_log_events: [ban, kick]" in text
        write_guild_config(runtime.config_dir, 8, {"x": 1})
        assert (runtime.config_dir / "servers" / "8.md").exists()

        scheduler = runtime.ports["cfg"].scheduler
        assert isinstance(scheduler, FakeScheduler)
        ran: list[str] = []

        async def handler(run: object) -> None:
            ran.append("ran")

        scheduler.register("tick", handler)
        await scheduler.run_every("k", 60, "tick")
        assert await scheduler.run_due(now=0) == 1
        assert await scheduler.run_due(now=30) == 0
        assert await scheduler.run_due(now=60) == 1
        assert ran == ["ran", "ran"]

        interaction = FakeInteraction(user_id=5)
        await interaction.respond("ok", ephemeral=True)
        assert interaction.last.ephemeral is True
    finally:
        await runtime.close()
