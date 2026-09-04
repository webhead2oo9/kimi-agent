"""The module integration harness composes real core the way the bot does."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace

import pytest

from kimi_agent_module_api import (
    MODULE_API_VERSION,
    ModuleCapabilities,
    ModuleLoadContext,
    ModulePermissions,
    ModuleRuntimeContext,
    ModuleSpec,
)
from kimi_agent_module_api.contracts import (
    ButtonSpec,
    CommandOption,
    CommandSpec,
    GuildCommand,
    MigrationContext,
    ModalSpec,
    ModuleContractError,
    ScopedModuleMigration,
    TextInputSpec,
    UndeclaredDiscordAction,
)
from kimi_agent_module_api.testing import (
    FakeInteraction,
    FakeInteractionOwnership,
    FakeInteractions,
    FakeScheduler,
)
from modules.testing import build_test_runtime, fake_ports, write_guild_config


class RecordingModule:
    def __init__(self, name: str, log: list[str]) -> None:
        self.name = name
        self.log = log
        self.ctx: ModuleRuntimeContext | None = None
        self.scoped_migrations: Sequence[ScopedModuleMigration] = (("init", self._migrate),)

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

    api_version = overrides.pop("api_version", MODULE_API_VERSION)
    return ModuleSpec(
        name=name,
        version="0.0.1",
        create=create,
        api_version=api_version,  # type: ignore[arg-type]
        **overrides,  # type: ignore[arg-type]
    )


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


@pytest.mark.asyncio
async def test_fake_interactions_replace_guild_command_sets() -> None:
    interactions = FakeInteractions("demo")

    async def handler(_interaction: object) -> None:
        pass

    await interactions.replace_guild_commands(
        7,
        (GuildCommand(CommandSpec(name="first", description="first"), handler),),  # type: ignore[arg-type]
    )
    assert set(interactions.guild_commands[7]) == {"first"}

    await interactions.replace_guild_commands(
        7,
        (GuildCommand(CommandSpec(name="second", description="second"), handler),),  # type: ignore[arg-type]
    )
    assert set(interactions.guild_commands[7]) == {"second"}

    await interactions.replace_guild_commands(7, ())
    assert 7 not in interactions.guild_commands


@pytest.mark.asyncio
async def test_fake_interactions_reject_the_top_name_collisions_production_rejects() -> None:
    # Global commands are stored by qualified name, but Discord owns the top-level
    # name. A fake that only compares qualified names accepts registrations the
    # real command tree refuses, so a module passes its tests and fails at boot.
    async def handler(_interaction: object) -> None:
        pass

    grouped = FakeInteractions("demo")
    grouped.add_command(
        CommandSpec(name="run", description="run", group="tools"),
        handler,  # type: ignore[arg-type]
    )
    with pytest.raises(ModuleContractError, match="shadow a global command"):
        await grouped.replace_guild_commands(
            7,
            (
                GuildCommand(  # type: ignore[arg-type]
                    CommandSpec(name="go", description="go", group="tools"), handler
                ),
            ),
        )
    with pytest.raises(ModuleContractError, match="both a command and a group"):
        grouped.add_command(
            CommandSpec(name="tools", description="tools"),
            handler,  # type: ignore[arg-type]
        )

    guild_first = FakeInteractions("demo")
    await guild_first.replace_guild_commands(
        7,
        (
            GuildCommand(  # type: ignore[arg-type]
                CommandSpec(name="go", description="go", group="tools"), handler
            ),
        ),
    )
    with pytest.raises(ModuleContractError, match="shadow a guild command"):
        guild_first.add_command(
            CommandSpec(name="run", description="run", group="tools"),
            handler,  # type: ignore[arg-type]
        )


def test_fake_interactions_share_top_level_ownership_across_modules() -> None:
    ownership = FakeInteractionOwnership()
    alpha = FakeInteractions("alpha", ownership=ownership)
    beta = FakeInteractions("beta", ownership=ownership)

    async def handler(_interaction: object) -> None:
        pass

    alpha.add_command(
        CommandSpec(name="run", description="run", group="tools"),
        handler,  # type: ignore[arg-type]
    )
    with pytest.raises(ModuleContractError, match="already owned"):
        beta.add_command(
            CommandSpec(name="other", description="other", group="tools"),
            handler,  # type: ignore[arg-type]
        )


def test_fake_ports_share_ownership_for_one_manager_runtime() -> None:
    database = SimpleNamespace()
    base_ports = {"storage": SimpleNamespace(database=database)}
    alpha = fake_ports(_spec("alpha", RecordingModule("alpha", [])), dict(base_ports))[
        "interactions"
    ]
    beta = fake_ports(_spec("beta", RecordingModule("beta", [])), dict(base_ports))["interactions"]

    async def handler(_interaction: object) -> None:
        pass

    alpha.add_command(
        CommandSpec(name="run", description="run", group="tools"),
        handler,
    )
    with pytest.raises(ModuleContractError, match="already owned"):
        beta.add_command(
            CommandSpec(name="other", description="other", group="tools"),
            handler,
        )

    alpha.close()
    beta.add_command(
        CommandSpec(name="other", description="other", group="tools"),
        handler,
    )


@pytest.mark.asyncio
async def test_fake_ports_preserve_the_scoped_guild_activity_predicate() -> None:
    router = fake_ports(
        _spec("demo", RecordingModule("demo", [])),
        {
            "storage": SimpleNamespace(database=SimpleNamespace()),
            "is_guild_active": lambda _guild_id: False,
        },
    )["interactions"]

    async def handler(_interaction: object) -> None:
        pass

    with pytest.raises(ModuleContractError, match="not active in guild 7"):
        await router.replace_guild_commands(
            7,
            (GuildCommand(CommandSpec(name="run", description="run"), handler),),
        )
    await router.replace_guild_commands(7, ())


@pytest.mark.asyncio
async def test_fake_interactions_enforce_command_tree_limits() -> None:
    interactions = FakeInteractions("demo")

    async def handler(_interaction: object) -> None:
        pass

    for index in range(25):
        interactions.add_command(
            CommandSpec(name=f"c{index}", description="command", group="tools"),
            handler,
        )
    with pytest.raises(ModuleContractError, match="more than 25"):
        interactions.add_command(
            CommandSpec(name="overflow", description="command", group="tools"),
            handler,
        )

    guild_commands = tuple(
        GuildCommand(CommandSpec(name=f"c{index}", description="command"), handler)
        for index in range(101)
    )
    with pytest.raises(ModuleContractError, match="more than 100"):
        await interactions.replace_guild_commands(7, guild_commands)


@pytest.mark.asyncio
async def test_fake_interactions_match_live_binding_and_custom_id_validation() -> None:
    interactions = FakeInteractions("demo")

    async def handler(_interaction: object) -> None:
        pass

    autocomplete_spec = CommandSpec(
        name="search",
        description="search",
        options=(CommandOption("query", "string", "query", autocomplete=True),),
    )
    with pytest.raises(ModuleContractError, match="no autocomplete handler"):
        interactions.add_command(autocomplete_spec, handler)  # type: ignore[arg-type]

    with pytest.raises(ModuleContractError, match="needs module_name"):
        await FakeInteraction().respond(components=(ButtonSpec(key="go", label="Go"),))
    bound_interaction = interactions.interaction()
    await bound_interaction.respond(components=(ButtonSpec(key="go", label="Go"),))

    interaction = FakeInteraction(module_name="m")
    with pytest.raises(ModuleContractError, match="custom_id exceeds"):
        await interaction.respond(
            components=(ButtonSpec(key="go", label="Go", parts=("x" * 100,)),)
        )
    with pytest.raises(ModuleContractError, match="modal custom_id exceeds"):
        await interaction.show_modal(
            ModalSpec(
                key="edit",
                title="Edit",
                inputs=(TextInputSpec("value", "Value"),),
                parts=("x" * 90,),
            )
        )


@pytest.mark.asyncio
async def test_harness_shares_interaction_ownership_across_modules(tmp_path: Path) -> None:
    class CommandModule(RecordingModule):
        async def start(self, ctx: ModuleRuntimeContext) -> None:
            await super().start(ctx)

            async def handler(_interaction: object) -> None:
                pass

            ctx.interactions.add_command(
                CommandSpec(name=self.name, description=self.name, group="tools"),
                handler,  # type: ignore[arg-type]
            )

    alpha = CommandModule("alpha", [])
    beta = CommandModule("beta", [])
    with pytest.raises(ModuleContractError, match="already owned"):
        await build_test_runtime(
            tmp_path,
            ("alpha", "beta"),
            installed={"alpha": _spec("alpha", alpha), "beta": _spec("beta", beta)},
        )


@pytest.mark.asyncio
async def test_harness_rejects_guild_commands_for_an_inactive_guild(tmp_path: Path) -> None:
    class GuildCommandModule(RecordingModule):
        async def start(self, ctx: ModuleRuntimeContext) -> None:
            await super().start(ctx)

            async def handler(_interaction: object) -> None:
                pass

            await ctx.interactions.replace_guild_commands(
                7,
                (GuildCommand(CommandSpec(name="run", description="run"), handler),),
            )

    module = GuildCommandModule("demo", [])
    with pytest.raises(ModuleContractError, match="not active in guild 7"):
        await build_test_runtime(
            tmp_path,
            ("demo",),
            installed={"demo": _spec("demo", module)},
            active_guilds=lambda _guild_id: False,
        )


@pytest.mark.asyncio
async def test_harness_capability_override_is_used_for_create_and_start(tmp_path: Path) -> None:
    empty = ModuleCapabilities(frozenset(), False, False)
    seen: list[ModuleCapabilities] = []
    module = RecordingModule("demo", [])

    def create(ctx: ModuleLoadContext) -> RecordingModule:
        seen.append(ctx.capabilities)
        return module

    async def start(ctx: ModuleRuntimeContext) -> None:
        seen.append(ctx.capabilities)

    module.start = start  # type: ignore[method-assign]
    spec = ModuleSpec(name="demo", version="0.0.1", create=create, api_version=MODULE_API_VERSION)
    runtime = await build_test_runtime(
        tmp_path,
        ("demo",),
        installed={"demo": spec},
        capabilities=empty,
    )
    try:
        assert seen == [empty, empty]
    finally:
        await runtime.close()

    unavailable = ModuleSpec(
        name="required",
        version="0.0.1",
        create=lambda _ctx: RecordingModule("required", []),
        api_version=MODULE_API_VERSION,
        requires_capabilities=("discord.modals.v1",),
    )
    missing_root = tmp_path / "missing"
    missing_root.mkdir()
    with pytest.raises(RuntimeError, match="requires unavailable capability"):
        await build_test_runtime(
            missing_root,
            ("required",),
            installed={"required": unavailable},
            capabilities=empty,
        )
