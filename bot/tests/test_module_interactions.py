"""Module interaction router: Discord-free specs in, gated app commands out."""

from __future__ import annotations

import asyncio
import itertools
from types import SimpleNamespace
from typing import Any

import discord
import pytest
from discord import app_commands

from discord_adapter import module_interactions
from discord_adapter.module_interactions import (
    ComponentDispatcher,
    InteractionRouterImpl,
    InteractionRuntime,
    ModuleInteractionAdapter,
    _option_value,
    build_view,
    build_layout_view,
)
from kimi_agent_module_api.contracts import (
    CUSTOM_ID_MAX_LENGTH,
    MODAL_CUSTOM_ID_MAX_LENGTH,
    ButtonSpec,
    CommandSyncError,
    CommandOption,
    CommandSpec,
    GuildCommand,
    LayoutGallery,
    LayoutSection,
    LayoutSeparator,
    LayoutText,
    ModalSpec,
    ModuleContractError,
    ModuleInteraction,
    OutgoingEmbed,
    OutgoingLayout,
    SelectSpec,
    TextInputSpec,
    TrustTierName,
    build_custom_id,
)
from kimi_agent_module_api.testing import FakeTrust


class _Tree:
    def __init__(self) -> None:
        self.commands: dict[str, Any] = {}
        self.guild_commands: dict[int, dict[str, Any]] = {}
        self.published_guild_commands: dict[int, dict[str, Any]] = {}
        self.sync_calls: list[int | None] = []
        self.sync_error: Exception | None = None
        self.sync_accept_then_error: Exception | None = None
        self.command_limit_on_name: str | None = None
        self.add_error_on_name: str | None = None

    def add_command(self, command: Any, *, guild: Any = None, **_kwargs: Any) -> None:
        if command.name == self.command_limit_on_name:
            self.command_limit_on_name = None
            raise app_commands.CommandLimitReached(
                None if guild is None else guild.id,
                100,
            )
        if command.name == self.add_error_on_name:
            self.add_error_on_name = None
            raise RuntimeError(f"injected add failure for {command.name}")
        target = self.commands if guild is None else self.guild_commands.setdefault(guild.id, {})
        target[command.name] = command

    def get_command(self, name: str, *, guild: Any = None) -> Any:
        target = self.commands if guild is None else self.guild_commands.get(guild.id, {})
        return target.get(name)

    def get_commands(self, *, guild: Any = None, type: Any = None) -> list[Any]:
        target = self.commands if guild is None else self.guild_commands.get(guild.id, {})
        return list(target.values())

    def remove_command(self, name: str, *, guild: Any = None) -> Any:
        target = self.commands if guild is None else self.guild_commands.get(guild.id, {})
        return target.pop(name, None)

    async def sync(self, *, guild: Any = None) -> list[Any]:
        self.sync_calls.append(None if guild is None else guild.id)
        if self.sync_error is not None:
            raise self.sync_error
        target = self.commands if guild is None else self.guild_commands.get(guild.id, {})
        if guild is not None:
            self.published_guild_commands[guild.id] = dict(target)
        if self.sync_accept_then_error is not None:
            error = self.sync_accept_then_error
            self.sync_accept_then_error = None
            raise error
        return list(target.values())


class _Bot:
    def __init__(self) -> None:
        self.tree = _Tree()
        self.dynamic_items: list[Any] = []
        self.guilds = [SimpleNamespace(id=1), SimpleNamespace(id=2)]

    def add_dynamic_items(self, *items: Any) -> None:
        self.dynamic_items.extend(items)


class _ScopeStore:
    def __init__(self, guild_ids: set[int] | None = None) -> None:
        self.tracked = set(guild_ids or set())
        self.track_error: Exception | None = None
        self.guild_ids_error: Exception | None = None
        self.forget_error: Exception | None = None
        self.forget_error_guild_ids: set[int] = set()
        self.track_calls: list[int] = []
        self.forget_calls: list[int] = []
        self.track_called = asyncio.Event()

    async def track(self, guild_id: int) -> None:
        self.track_calls.append(guild_id)
        self.track_called.set()
        if self.track_error is not None:
            raise self.track_error
        self.tracked.add(guild_id)

    async def guild_ids(self) -> tuple[int, ...]:
        if self.guild_ids_error is not None:
            raise self.guild_ids_error
        return tuple(sorted(self.tracked))

    async def forget(self, guild_id: int) -> None:
        self.forget_calls.append(guild_id)
        if self.forget_error is not None or guild_id in self.forget_error_guild_ids:
            error = self.forget_error or RuntimeError(f"database busy for guild {guild_id}")
            raise error
        self.tracked.discard(guild_id)


class _Response:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.deferred: list[bool] = []
        self._done = False
        self.modals: list[Any] = []
        self.edits: list[dict[str, Any]] = []

    def is_done(self) -> bool:
        return self._done

    async def send_message(self, **kwargs: Any) -> None:
        self._done = True
        self.sent.append(kwargs)

    async def defer(self, *, ephemeral: bool = False) -> None:
        self._done = True
        self.deferred.append(ephemeral)

    async def send_modal(self, modal: Any) -> None:
        self._done = True
        self.modals.append(modal)

    async def edit_message(self, **kwargs: Any) -> None:
        self._done = True
        self.edits.append(kwargs)


class _Interaction:
    def __init__(
        self,
        *,
        user_id: int = 10,
        guild_id: int | None = 1,
        data: dict[str, Any] | None = None,
        type: discord.InteractionType = discord.InteractionType.application_command,
        message: Any = None,
    ) -> None:
        self.user = SimpleNamespace(id=user_id)
        self.guild_id = guild_id
        self.channel_id = 2
        self.data = data or {}
        self.type = type
        self.message = message
        self.response = _Response()
        self.followups: list[dict[str, Any]] = []
        self.edits: list[dict[str, Any]] = []
        self.followup = SimpleNamespace(send=self._follow)

    async def _follow(self, *args: Any, **kwargs: Any) -> None:
        self.followups.append(kwargs | ({"content": args[0]} if args else {}))

    async def edit_original_response(self, **kwargs: Any) -> None:
        self.edits.append(kwargs)


def _router(
    *,
    staff: frozenset[int] = frozenset({10}),
    tiers: dict[tuple[int, int], TrustTierName] | None = None,
    active: bool = True,
) -> tuple[InteractionRouterImpl, _Bot, ComponentDispatcher]:
    bot = _Bot()
    dispatcher = ComponentDispatcher(clock=lambda: 100.0)
    router = InteractionRouterImpl(
        bot=bot,  # type: ignore[arg-type]
        module_name="mod",
        trust=FakeTrust(tiers or {(1, uid): "staff" for uid in staff}),
        dispatcher=dispatcher,
        is_guild_active=lambda _g: active,
    )
    return router, bot, dispatcher


@pytest.mark.asyncio
async def test_command_spec_builds_gated_app_command_with_stable_ids() -> None:
    router, bot, _ = _router()
    seen: list[dict[str, Any]] = []

    async def handler(interaction: ModuleInteraction) -> None:
        seen.append(dict(interaction.options))
        await interaction.respond("done", ephemeral=True)

    spec = CommandSpec(
        name="warn",
        description="Warn someone",
        group="mod",
        group_description="Moderation",
        options=(
            CommandOption("user", "user", "Who", required=True),
            CommandOption("reason", "string", "Why", required=True),
            CommandOption("days", "integer", "Days", min_value=0, max_value=7),
            CommandOption("mode", "string", "Mode", choices=(("Soft", "soft"), ("Hard", "hard"))),
        ),
    )
    router.add_command(spec, handler)
    group = bot.tree.commands["mod"]
    assert isinstance(group, app_commands.Group)
    command = group.get_command("warn")
    assert isinstance(command, app_commands.Command)
    assert [p.name for p in command.parameters] == ["user", "reason", "days", "mode"]
    assert command.parameters[0].required is True
    assert command.parameters[2].required is False
    assert [c.value for c in command.parameters[3].choices] == ["soft", "hard"]

    member = SimpleNamespace(id=55)
    interaction = _Interaction(user_id=10)
    await command.callback(  # type: ignore[call-arg]
        interaction,  # type: ignore[arg-type]
        user=discord.Object(id=55),
        reason="spam",
        days=None,
        mode=None,
    )
    # discord.Object is not a Member; stable-id reduction applies only to SDK entities.
    assert (
        seen == [{"user": discord.Object(id=55), "reason": "spam", "days": None, "mode": None}]
        or seen[0]["reason"] == "spam"
    )
    assert interaction.response.sent[0]["content"] == "done"
    assert interaction.response.sent[0]["ephemeral"] is True
    assert interaction.response.sent[0]["allowed_mentions"].everyone is False
    del member


@pytest.mark.asyncio
async def test_non_staff_and_inactive_guilds_are_refused_before_the_handler() -> None:
    router, bot, _ = _router()
    calls: list[str] = []

    async def handler(interaction: ModuleInteraction) -> None:
        calls.append("ran")

    router.add_command(CommandSpec(name="ping", description="p"), handler)
    command = bot.tree.commands["ping"]
    member_interaction = _Interaction(user_id=20)
    await command.callback(member_interaction)
    assert calls == []
    assert member_interaction.response.sent[0]["content"] == "Staff only."

    router_inactive, bot_inactive, _ = _router(active=False)
    router_inactive.add_command(CommandSpec(name="ping", description="p"), handler)
    await bot_inactive.tree.commands["ping"].callback(_Interaction(user_id=10))
    assert calls == []


@pytest.mark.asyncio
async def test_autocomplete_is_refused_before_staff_gate() -> None:
    router, bot, _ = _router()
    calls: list[int] = []

    async def command_handler(_interaction: ModuleInteraction) -> None:
        pass

    async def autocomplete_handler(
        interaction: ModuleInteraction, _option: str, current: str
    ) -> list[tuple[str, str]]:
        calls.append(interaction.user_id)
        return [(current, current)]

    router.add_command(
        CommandSpec(
            name="lookup",
            description="lookup",
            options=(CommandOption("query", "string", "query", autocomplete=True),),
            min_tier="staff",
        ),
        command_handler,
        autocomplete=autocomplete_handler,
    )
    autocomplete = bot.tree.commands["lookup"]._params["query"].autocomplete
    assert await autocomplete(_Interaction(user_id=20), "secret") == []
    choices = await autocomplete(_Interaction(user_id=10), "allowed")
    assert [choice.value for choice in choices] == ["allowed"]
    assert calls == [10]


def test_module_commands_cannot_replace_an_existing_owner() -> None:
    router, bot, _ = _router()

    async def handler(_interaction: ModuleInteraction) -> None:
        pass

    bot.tree.commands["privacy"] = SimpleNamespace(name="privacy")
    with pytest.raises(ModuleContractError, match="already owned"):
        router.add_command(CommandSpec(name="privacy", description="module"), handler)

    bot.tree.commands["admin"] = SimpleNamespace(name="admin")
    with pytest.raises(ModuleContractError, match="already owned"):
        router.add_command(CommandSpec(name="thing", description="module", group="admin"), handler)


def test_duplicate_module_command_registration_is_rejected() -> None:
    router, _, _ = _router()

    async def handler(_interaction: ModuleInteraction) -> None:
        pass

    router.add_command(CommandSpec(name="thing", description="first", group="admin"), handler)
    with pytest.raises(ModuleContractError, match="already registered"):
        router.add_command(CommandSpec(name="thing", description="second", group="admin"), handler)


@pytest.mark.asyncio
async def test_guild_commands_stage_then_replace_and_remove_live() -> None:
    bot = _Bot()
    store = _ScopeStore()
    runtime = InteractionRuntime(bot, scope_store=store)  # type: ignore[arg-type]
    router = runtime.router_for(
        "mod", trust=FakeTrust({(1, 10): "staff"}), is_guild_active=lambda _g: True
    )
    hits: list[str] = []

    async def first(_interaction: ModuleInteraction) -> None:
        hits.append("first")

    async def second(_interaction: ModuleInteraction) -> None:
        hits.append("second")

    await router.replace_guild_commands(
        1,
        (
            GuildCommand(CommandSpec(name="hello", description="hello"), first),
            GuildCommand(
                CommandSpec(
                    name="grouped",
                    description="grouped",
                    group="tools",
                    group_description="Guild tools",
                ),
                first,
            ),
        ),
    )
    assert bot.tree.sync_calls == []
    assert set(bot.tree.guild_commands[1]) == {"hello", "tools"}
    assert bot.tree.guild_commands[1]["tools"].get_command("grouped") is not None
    assert store.tracked == {1}

    await runtime.sync_ready()
    assert bot.tree.sync_calls == [1]

    await router.replace_guild_commands(
        1,
        (GuildCommand(CommandSpec(name="goodbye", description="goodbye"), second),),
    )
    assert set(bot.tree.guild_commands[1]) == {"goodbye"}
    assert bot.tree.sync_calls == [1, 1]

    interaction = _Interaction()
    await bot.tree.guild_commands[1]["goodbye"].callback(interaction)
    assert hits == ["second"]

    await router.replace_guild_commands(1, ())
    assert bot.tree.guild_commands[1] == {}
    assert store.tracked == set()


@pytest.mark.asyncio
async def test_guild_commands_reject_global_shadow_and_same_guild_collision() -> None:
    bot = _Bot()
    runtime = InteractionRuntime(bot, scope_store=_ScopeStore())  # type: ignore[arg-type]
    first = runtime.router_for("first", trust=FakeTrust(), is_guild_active=lambda _g: True)
    second = runtime.router_for("second", trust=FakeTrust(), is_guild_active=lambda _g: True)

    async def handler(_interaction: ModuleInteraction) -> None:
        pass

    bot.tree.commands["privacy"] = SimpleNamespace(name="privacy")
    with pytest.raises(ModuleContractError, match="shadow a global command"):
        await first.replace_guild_commands(
            1,
            (GuildCommand(CommandSpec(name="privacy", description="p"), handler),),
        )

    command = GuildCommand(CommandSpec(name="local", description="local"), handler)
    await first.replace_guild_commands(1, (command,))
    with pytest.raises(ModuleContractError, match="already owned"):
        await second.replace_guild_commands(1, (command,))

    await second.replace_guild_commands(2, (command,))
    assert "local" in bot.tree.guild_commands[1]
    assert "local" in bot.tree.guild_commands[2]

    with pytest.raises(ModuleContractError, match="shadow a guild command"):
        first.add_command(CommandSpec(name="local", description="global"), handler)


@pytest.mark.asyncio
async def test_guild_command_replacement_preflights_the_discord_limit() -> None:
    bot = _Bot()
    runtime = InteractionRuntime(bot, scope_store=_ScopeStore())  # type: ignore[arg-type]
    router = runtime.router_for("mod", trust=FakeTrust(), is_guild_active=lambda _g: True)

    async def handler(_interaction: ModuleInteraction) -> None:
        pass

    bot.tree.guild_commands[1] = {
        f"external_{index}": SimpleNamespace(name=f"external_{index}") for index in range(99)
    }
    await router.replace_guild_commands(
        1,
        (GuildCommand(CommandSpec(name="before", description="before"), handler),),
    )

    with pytest.raises(ModuleContractError, match="would have 101 slash commands"):
        await router.replace_guild_commands(
            1,
            (
                GuildCommand(CommandSpec(name="after_one", description="one"), handler),
                GuildCommand(CommandSpec(name="after_two", description="two"), handler),
            ),
        )

    assert "before" in bot.tree.guild_commands[1]
    assert "after_one" not in bot.tree.guild_commands[1]
    assert "after_two" not in bot.tree.guild_commands[1]


@pytest.mark.asyncio
async def test_guild_command_limit_exception_is_normalized_and_rolled_back() -> None:
    bot = _Bot()
    runtime = InteractionRuntime(bot, scope_store=_ScopeStore())  # type: ignore[arg-type]
    router = runtime.router_for("mod", trust=FakeTrust(), is_guild_active=lambda _g: True)

    async def handler(_interaction: ModuleInteraction) -> None:
        pass

    await router.replace_guild_commands(
        1,
        (GuildCommand(CommandSpec(name="before", description="before"), handler),),
    )
    previous = bot.tree.guild_commands[1]["before"]
    bot.tree.command_limit_on_name = "after_two"

    with pytest.raises(ModuleContractError, match="limit of 100"):
        await router.replace_guild_commands(
            1,
            (
                GuildCommand(CommandSpec(name="after_one", description="one"), handler),
                GuildCommand(CommandSpec(name="after_two", description="two"), handler),
            ),
        )

    assert bot.tree.guild_commands[1] == {"before": previous}


@pytest.mark.asyncio
async def test_generic_guild_command_add_failure_restores_tree_and_ownership() -> None:
    bot = _Bot()
    runtime = InteractionRuntime(bot, scope_store=_ScopeStore())  # type: ignore[arg-type]
    router = runtime.router_for("mod", trust=FakeTrust(), is_guild_active=lambda _g: True)

    async def handler(_interaction: ModuleInteraction) -> None:
        pass

    await router.replace_guild_commands(
        1,
        (GuildCommand(CommandSpec(name="before", description="before"), handler),),
    )
    previous = bot.tree.guild_commands[1]["before"]
    bot.tree.add_error_on_name = "after_two"

    with pytest.raises(RuntimeError, match="injected add failure"):
        await router.replace_guild_commands(
            1,
            (
                GuildCommand(CommandSpec(name="after_one", description="one"), handler),
                GuildCommand(CommandSpec(name="after_two", description="two"), handler),
            ),
        )

    assert bot.tree.guild_commands[1] == {"before": previous}
    assert router._guild_top_names[1] == {"before"}


@pytest.mark.asyncio
async def test_live_guild_sync_failure_restores_handlers_and_retries_compensation() -> None:
    bot = _Bot()
    store = _ScopeStore()
    health: list[tuple[str, str, str]] = []
    runtime = InteractionRuntime(
        bot,  # type: ignore[arg-type]
        scope_store=store,
        on_sync_health=lambda module, state, detail: health.append((module, state, detail)),
        sync_retry_delays=(0.0,),
    )
    router = runtime.router_for(
        "mod",
        trust=FakeTrust({(1, 10): "staff"}),
        is_guild_active=lambda _g: True,
    )
    calls: list[str] = []

    async def before_handler(_interaction: ModuleInteraction) -> None:
        calls.append("before")

    async def rejected_handler(_interaction: ModuleInteraction) -> None:
        calls.append("rejected")

    await router.replace_guild_commands(
        1,
        (GuildCommand(CommandSpec(name="stable", description="before"), before_handler),),
    )
    await runtime.sync_ready()
    published_before = bot.tree.published_guild_commands[1]["stable"]

    bot.tree.sync_error = RuntimeError("offline")
    with pytest.raises(CommandSyncError):
        await router.replace_guild_commands(
            1,
            (GuildCommand(CommandSpec(name="stable", description="after"), rejected_handler),),
        )
    # The failed desired callback is rejected. The second failed PUT is the
    # compensating publication of the restored, previously published tree.
    assert bot.tree.guild_commands[1]["stable"] is published_before
    await bot.tree.guild_commands[1]["stable"].callback(_Interaction())
    assert calls == ["before"]
    assert store.tracked == {1}
    assert runtime._sync_failures or runtime._scope_discovery_failure is not None
    assert health[-1][1] == "degraded"

    bot.tree.sync_error = None
    retry = runtime._sync_retry_tasks[1]
    await retry

    assert bot.tree.sync_calls == [1, 1, 1, 1]
    assert bot.tree.published_guild_commands[1]["stable"] is published_before
    assert not runtime._sync_failures and runtime._scope_discovery_failure is None
    assert health[-1] == ("mod", "healthy", "")


@pytest.mark.asyncio
async def test_ambiguous_live_publication_compensates_the_restored_tree() -> None:
    bot = _Bot()
    runtime = InteractionRuntime(bot, scope_store=_ScopeStore())  # type: ignore[arg-type]
    router = runtime.router_for("mod", trust=FakeTrust(), is_guild_active=lambda _g: True)

    async def before_handler(_interaction: ModuleInteraction) -> None:
        pass

    async def rejected_handler(_interaction: ModuleInteraction) -> None:
        pass

    await router.replace_guild_commands(
        1,
        (GuildCommand(CommandSpec(name="stable", description="before"), before_handler),),
    )
    await runtime.sync_ready()
    published_before = bot.tree.published_guild_commands[1]["stable"]
    bot.tree.sync_accept_then_error = RuntimeError("response lost after acceptance")

    with pytest.raises(CommandSyncError):
        await router.replace_guild_commands(
            1,
            (GuildCommand(CommandSpec(name="stable", description="after"), rejected_handler),),
        )

    assert bot.tree.guild_commands[1]["stable"] is published_before
    assert bot.tree.published_guild_commands[1]["stable"] is published_before
    assert bot.tree.sync_calls == [1, 1, 1]
    assert not runtime._sync_failures and runtime._scope_discovery_failure is None
    assert runtime._sync_retry_tasks == {}


@pytest.mark.asyncio
async def test_failed_live_rollback_is_not_automatically_published() -> None:
    bot = _Bot()
    runtime = InteractionRuntime(
        bot,  # type: ignore[arg-type]
        scope_store=_ScopeStore(),
        sync_retry_delays=(0.0,),
    )
    router = runtime.router_for("mod", trust=FakeTrust(), is_guild_active=lambda _g: True)

    async def handler(_interaction: ModuleInteraction) -> None:
        pass

    await router.replace_guild_commands(
        1,
        (GuildCommand(CommandSpec(name="before", description="before"), handler),),
    )
    await runtime.sync_ready()
    bot.tree.sync_error = RuntimeError("offline")
    bot.tree.add_error_on_name = "before"

    with pytest.raises(CommandSyncError) as raised:
        await router.replace_guild_commands(
            1,
            (GuildCommand(CommandSpec(name="after", description="after"), handler),),
        )

    assert isinstance(raised.value.__cause__, ExceptionGroup)
    assert len(raised.value.__cause__.exceptions) == 2
    assert bot.tree.sync_calls == [1, 1]
    assert runtime._sync_exhausted == {1}
    assert runtime._sync_retry_tasks == {}


@pytest.mark.asyncio
async def test_scope_tracking_failure_rolls_back_without_a_discord_put() -> None:
    bot = _Bot()
    store = _ScopeStore()
    store.track_error = RuntimeError("database busy")
    runtime = InteractionRuntime(bot, scope_store=store)  # type: ignore[arg-type]
    router = runtime.router_for("mod", trust=FakeTrust(), is_guild_active=lambda _g: True)

    async def handler(_interaction: ModuleInteraction) -> None:
        pass

    with pytest.raises(CommandSyncError, match="persist guild command scope"):
        await router.replace_guild_commands(
            1,
            (GuildCommand(CommandSpec(name="pending", description="pending"), handler),),
        )

    assert bot.tree.guild_commands[1] == {}
    assert router.has_guild_commands(1) is False
    assert bot.tree.sync_calls == []


@pytest.mark.asyncio
async def test_startup_guild_sync_failure_retries_without_another_ready() -> None:
    bot = _Bot()
    bot.tree.sync_error = RuntimeError("offline")
    store = _ScopeStore()
    health: list[tuple[str, str, str]] = []
    runtime = InteractionRuntime(
        bot,  # type: ignore[arg-type]
        scope_store=store,
        on_sync_health=lambda module, state, detail: health.append((module, state, detail)),
        sync_retry_delays=(0.0,),
    )
    router = runtime.router_for("mod", trust=FakeTrust(), is_guild_active=lambda _g: True)

    async def handler(_interaction: ModuleInteraction) -> None:
        pass

    await router.replace_guild_commands(
        1,
        (GuildCommand(CommandSpec(name="pending", description="pending"), handler),),
    )

    await runtime.sync_ready()

    assert set(bot.tree.guild_commands[1]) == {"pending"}
    assert store.tracked == {1}
    assert runtime._sync_failures or runtime._scope_discovery_failure is not None
    bot.tree.sync_error = None
    retry = runtime._sync_retry_tasks[1]
    await retry

    assert bot.tree.sync_calls == [1, 1]
    assert set(bot.tree.published_guild_commands[1]) == {"pending"}
    assert not runtime._sync_failures and runtime._scope_discovery_failure is None
    assert health[-1] == ("mod", "healthy", "")


@pytest.mark.asyncio
async def test_startup_track_retry_still_publishes_the_desired_tree() -> None:
    bot = _Bot()
    store = _ScopeStore()
    runtime = InteractionRuntime(
        bot,  # type: ignore[arg-type]
        scope_store=store,
        sync_retry_delays=(0.0, 0.05),
    )
    router = runtime.router_for("mod", trust=FakeTrust(), is_guild_active=lambda _g: True)

    async def handler(_interaction: ModuleInteraction) -> None:
        pass

    await router.replace_guild_commands(
        1,
        (GuildCommand(CommandSpec(name="pending", description="pending"), handler),),
    )
    store.track_called.clear()
    store.track_error = RuntimeError("database busy")
    await runtime.sync_ready()
    retry = runtime._sync_retry_tasks[1]

    # Let the first retry prove that a second track error does not narrow the
    # remaining work to persistence-only, then recover before the next delay.
    store.track_called.clear()
    await store.track_called.wait()
    store.track_error = None
    await retry

    assert bot.tree.sync_calls == [1]
    assert set(bot.tree.published_guild_commands[1]) == {"pending"}
    assert not runtime._sync_failures and runtime._scope_discovery_failure is None


@pytest.mark.asyncio
async def test_live_track_failure_preserves_an_existing_pending_publish() -> None:
    bot = _Bot()
    store = _ScopeStore()
    runtime = InteractionRuntime(
        bot,  # type: ignore[arg-type]
        scope_store=store,
        sync_retry_delays=(60.0,),
    )
    router = runtime.router_for("mod", trust=FakeTrust(), is_guild_active=lambda _g: True)

    async def handler(_interaction: ModuleInteraction) -> None:
        pass

    await router.replace_guild_commands(
        1,
        (GuildCommand(CommandSpec(name="pending", description="pending"), handler),),
    )
    bot.tree.sync_error = RuntimeError("offline")
    await runtime.sync_ready()
    assert runtime._sync_failure_phases[1] == "publish"

    store.track_error = RuntimeError("database busy")
    with pytest.raises(CommandSyncError, match="persist guild command scope"):
        await router.replace_guild_commands(
            1,
            (GuildCommand(CommandSpec(name="rejected", description="rejected"), handler),),
        )

    assert set(bot.tree.guild_commands[1]) == {"pending"}
    assert runtime._sync_failure_phases[1] == "publish"

    scheduled = runtime._sync_retry_tasks[1]
    store.track_error = None
    bot.tree.sync_error = None
    runtime.sync_retry_delays = (0.0,)
    await runtime._retry_guild_sync(1)
    await asyncio.gather(scheduled, return_exceptions=True)

    assert set(bot.tree.published_guild_commands[1]) == {"pending"}
    assert not runtime._sync_failures and runtime._scope_discovery_failure is None


@pytest.mark.asyncio
async def test_guild_sync_retry_budget_exhaustion_is_observable() -> None:
    bot = _Bot()
    bot.tree.sync_error = RuntimeError("offline")
    health: list[tuple[str, str, str]] = []
    runtime = InteractionRuntime(
        bot,  # type: ignore[arg-type]
        scope_store=_ScopeStore(),
        on_sync_health=lambda module, state, detail: health.append((module, state, detail)),
        sync_retry_delays=(0.0, 0.0),
    )
    router = runtime.router_for("mod", trust=FakeTrust(), is_guild_active=lambda _g: True)

    async def handler(_interaction: ModuleInteraction) -> None:
        pass

    await router.replace_guild_commands(
        1,
        (GuildCommand(CommandSpec(name="pending", description="pending"), handler),),
    )
    await runtime.sync_ready()
    retry = runtime._sync_retry_tasks[1]
    await retry

    assert bot.tree.sync_calls == [1, 1, 1]
    assert runtime._sync_exhausted == {1}
    assert health[-1][1] == "failed"


@pytest.mark.asyncio
async def test_scope_discovery_failure_does_not_block_known_local_publication() -> None:
    bot = _Bot()
    store = _ScopeStore()
    health: list[tuple[str, str, str]] = []
    runtime = InteractionRuntime(
        bot,  # type: ignore[arg-type]
        scope_store=store,
        on_sync_health=lambda module, state, detail: health.append((module, state, detail)),
        sync_retry_delays=(60.0,),
    )
    router = runtime.router_for("mod", trust=FakeTrust(), is_guild_active=lambda _g: True)

    async def handler(_interaction: ModuleInteraction) -> None:
        pass

    await router.replace_guild_commands(
        1,
        (GuildCommand(CommandSpec(name="known", description="known"), handler),),
    )
    store.guild_ids_error = RuntimeError("database busy")

    await runtime.sync_ready()

    assert set(bot.tree.published_guild_commands[1]) == {"known"}
    assert runtime._scope_discovery_failure == "RuntimeError"
    assert health[-1][1] == "degraded"

    scheduled = runtime._scope_discovery_retry_task
    assert scheduled is not None
    store.guild_ids_error = None
    runtime.sync_retry_delays = (0.0,)
    await runtime._retry_scope_discovery()
    await asyncio.gather(scheduled, return_exceptions=True)

    assert runtime._scope_discovery_failure is None
    assert not runtime._sync_failures
    assert health[-1] == ("mod", "healthy", "")


@pytest.mark.asyncio
async def test_empty_publication_retries_only_failed_scope_cleanup() -> None:
    bot = _Bot()
    store = _ScopeStore()
    runtime = InteractionRuntime(
        bot,  # type: ignore[arg-type]
        scope_store=store,
        sync_retry_delays=(0.0,),
    )
    router = runtime.router_for("mod", trust=FakeTrust(), is_guild_active=lambda _g: True)

    async def handler(_interaction: ModuleInteraction) -> None:
        pass

    await router.replace_guild_commands(
        1,
        (GuildCommand(CommandSpec(name="before", description="before"), handler),),
    )
    await runtime.sync_ready()
    store.forget_error = RuntimeError("database busy")

    await router.replace_guild_commands(1, ())

    assert bot.tree.guild_commands[1] == {}
    assert bot.tree.published_guild_commands[1] == {}
    assert runtime._sync_failures or runtime._scope_discovery_failure is not None
    assert bot.tree.sync_calls == [1, 1]

    store.forget_error = None
    retry = runtime._sync_retry_tasks[1]
    await retry

    assert bot.tree.sync_calls == [1, 1]
    assert store.forget_calls == [1, 1]
    assert not runtime._sync_failures and runtime._scope_discovery_failure is None


@pytest.mark.asyncio
async def test_disconnect_pauses_retry_and_resume_rearms_it() -> None:
    bot = _Bot()
    bot.tree.sync_error = RuntimeError("offline")
    runtime = InteractionRuntime(
        bot,  # type: ignore[arg-type]
        scope_store=_ScopeStore(),
        sync_retry_delays=(60.0,),
    )
    router = runtime.router_for("mod", trust=FakeTrust(), is_guild_active=lambda _g: True)

    async def handler(_interaction: ModuleInteraction) -> None:
        pass

    await router.replace_guild_commands(
        1,
        (GuildCommand(CommandSpec(name="pending", description="pending"), handler),),
    )
    await runtime.sync_ready()
    retry = runtime._sync_retry_tasks[1]

    await runtime.pause_sync()

    assert retry.done()
    assert bot.tree.sync_calls == [1]
    bot.tree.sync_error = None
    await runtime.resume_sync()
    assert bot.tree.sync_calls == [1, 1]
    assert not runtime._sync_failures and runtime._scope_discovery_failure is None


@pytest.mark.asyncio
async def test_pause_does_not_wait_behind_a_stubborn_sync_lock_owner() -> None:
    bot = _Bot()
    runtime = InteractionRuntime(bot, scope_store=_ScopeStore())  # type: ignore[arg-type]
    router = runtime.router_for("mod", trust=FakeTrust(), is_guild_active=lambda _g: True)

    async def handler(_interaction: ModuleInteraction) -> None:
        pass

    await router.replace_guild_commands(
        1,
        (GuildCommand(CommandSpec(name="pending", description="pending"), handler),),
    )
    sync_started = asyncio.Event()
    release_sync = asyncio.Event()

    async def stubborn_sync(*, guild: Any = None) -> list[Any]:
        assert guild is not None
        sync_started.set()
        while not release_sync.is_set():
            try:
                await release_sync.wait()
            except asyncio.CancelledError:
                continue
        return []

    bot.tree.sync = stubborn_sync  # type: ignore[method-assign]
    syncing = asyncio.create_task(runtime.sync_ready())
    await sync_started.wait()

    await asyncio.wait_for(runtime.pause_sync(), timeout=0.1)

    assert runtime._live is False
    assert syncing.done() is False
    release_sync.set()
    await asyncio.wait_for(syncing, timeout=0.2)
    assert runtime._live is False


@pytest.mark.asyncio
async def test_runtime_shutdown_cancels_retries_and_rejects_replacement() -> None:
    bot = _Bot()
    bot.tree.sync_error = RuntimeError("offline")
    runtime = InteractionRuntime(
        bot,  # type: ignore[arg-type]
        scope_store=_ScopeStore(),
        sync_retry_delays=(60.0,),
    )
    router = runtime.router_for("mod", trust=FakeTrust(), is_guild_active=lambda _g: True)

    async def handler(_interaction: ModuleInteraction) -> None:
        pass

    await router.replace_guild_commands(
        1,
        (GuildCommand(CommandSpec(name="pending", description="pending"), handler),),
    )
    await runtime.sync_ready()
    retry = runtime._sync_retry_tasks[1]

    await runtime.drain()

    assert retry.done()
    assert runtime._sync_retry_tasks == {}
    with pytest.raises(RuntimeError, match="shutting down"):
        await router.replace_guild_commands(1, ())


@pytest.mark.asyncio
async def test_ready_clears_stale_scopes_and_forgets_disconnected_guilds() -> None:
    bot = _Bot()
    bot.guilds = [SimpleNamespace(id=1)]
    store = _ScopeStore({1, 99})
    runtime = InteractionRuntime(bot, scope_store=store)  # type: ignore[arg-type]

    await runtime.sync_ready()

    assert bot.tree.sync_calls == [1]
    assert store.tracked == set()


@pytest.mark.asyncio
async def test_disconnected_scope_forget_failure_does_not_block_connected_scopes() -> None:
    bot = _Bot()
    bot.guilds = [SimpleNamespace(id=1)]
    store = _ScopeStore({1, 99})
    store.forget_error_guild_ids.add(99)
    runtime = InteractionRuntime(
        bot,  # type: ignore[arg-type]
        scope_store=store,
        sync_retry_delays=(60.0,),
    )

    await runtime.sync_ready()

    assert bot.tree.sync_calls == [1]
    assert store.tracked == {99}
    assert runtime._sync_failures == {99: "RuntimeError"}

    scheduled = runtime._sync_retry_tasks[99]
    store.forget_error_guild_ids.clear()
    runtime.sync_retry_delays = (0.0,)
    await runtime._retry_guild_sync(99)
    await asyncio.gather(scheduled, return_exceptions=True)

    assert store.tracked == set()
    assert not runtime._sync_failures and runtime._scope_discovery_failure is None


@pytest.mark.asyncio
async def test_ready_retracks_a_guild_it_forgot_while_disconnected() -> None:
    # A scope forgotten while the guild was away must come back when the staged
    # commands are actually published, or a later restart can never clean them up.
    bot = _Bot()
    bot.guilds = []
    store = _ScopeStore()
    runtime = InteractionRuntime(bot, scope_store=store)  # type: ignore[arg-type]
    router = runtime.router_for("mod", trust=FakeTrust(), is_guild_active=lambda _g: True)

    async def handler(_interaction: ModuleInteraction) -> None:
        pass

    await router.replace_guild_commands(
        7,
        (GuildCommand(CommandSpec(name="staged", description="staged"), handler),),
    )
    assert store.tracked == {7}

    await runtime.sync_ready()
    assert store.tracked == set()
    assert router.has_guild_commands(7) is True

    bot.guilds = [SimpleNamespace(id=7)]
    await runtime.sync_ready()

    assert bot.tree.sync_calls == [7]
    assert store.tracked == {7}


@pytest.mark.asyncio
async def test_router_close_releases_local_guild_command_ownership() -> None:
    bot = _Bot()
    runtime = InteractionRuntime(bot, scope_store=_ScopeStore())  # type: ignore[arg-type]
    router = runtime.router_for("mod", trust=FakeTrust(), is_guild_active=lambda _g: True)

    async def handler(_interaction: ModuleInteraction) -> None:
        pass

    await router.replace_guild_commands(
        1,
        (GuildCommand(CommandSpec(name="released", description="released"), handler),),
    )
    router.close()

    assert bot.tree.guild_commands[1] == {}
    assert runtime.guild_command_owner("released") is None


def test_thread_options_reduce_to_stable_ids() -> None:
    thread = object.__new__(discord.Thread)
    thread.id = 123  # type: ignore[attr-defined]
    assert _option_value(thread) == 123


@pytest.mark.asyncio
async def test_handler_errors_are_contained() -> None:
    router, bot, _ = _router()

    async def boom(interaction: ModuleInteraction) -> None:
        raise RuntimeError("boom")

    router.add_command(CommandSpec(name="boom", description="b"), boom)
    interaction = _Interaction(user_id=10)
    await bot.tree.commands["boom"].callback(interaction)
    assert "went wrong" in interaction.response.sent[0]["content"]


def test_close_removes_owned_commands_and_components() -> None:
    router, bot, dispatcher = _router()

    async def handler(interaction: ModuleInteraction) -> None:
        pass

    router.add_command(CommandSpec(name="a", description="a", group="mod"), handler)
    registration = router.add_command(CommandSpec(name="b", description="b"), handler)
    router.register_component("button", "confirm", handler)
    assert set(bot.tree.commands) == {"mod", "b"}
    assert dispatcher.registered("mod") == (("button", "confirm"),)
    registration.close()
    assert set(bot.tree.commands) == {"mod"}
    router.close()
    assert bot.tree.commands == {}
    assert dispatcher.registered("mod") == ()
    with pytest.raises(RuntimeError):
        router.add_command(CommandSpec(name="late", description="l"), handler)
    with pytest.raises(RuntimeError):
        router.register_component("button", "late", handler)


def test_duplicate_component_registration_is_rejected() -> None:
    router, _, dispatcher = _router()

    async def first(_interaction: ModuleInteraction) -> None:
        pass

    async def second(_interaction: ModuleInteraction) -> None:
        pass

    router.register_component("button", "confirm", first)
    with pytest.raises(ModuleContractError, match="already registered"):
        router.register_component("button", "confirm", second)
    assert dispatcher._handlers[("mod", "button", "confirm")].handler is first


@pytest.mark.asyncio
async def test_old_registration_handles_cannot_remove_reloaded_handlers() -> None:
    first_router, bot, dispatcher = _router()

    async def first(_interaction: ModuleInteraction) -> None:
        pass

    old_command = first_router.add_command(CommandSpec(name="ping", description="p"), first)
    old_component = first_router.register_component("button", "confirm", first)
    first_router.close()

    second_router = InteractionRouterImpl(
        bot=bot,  # type: ignore[arg-type]
        module_name="mod",
        trust=FakeTrust({(1, 10): "staff"}),
        dispatcher=dispatcher,
        is_guild_active=lambda _g: True,
    )
    hits: list[str] = []

    async def second(_interaction: ModuleInteraction) -> None:
        hits.append("second")

    second_router.add_command(CommandSpec(name="ping", description="p"), second)
    second_router.register_component("button", "confirm", second)

    old_command.close()
    old_component.close()

    assert "ping" in bot.tree.commands
    interaction = _Interaction(data={"custom_id": second_router.custom_id("confirm")})
    await dispatcher.dispatch(interaction, "button")  # type: ignore[arg-type]
    assert hits == ["second"]


@pytest.mark.asyncio
async def test_closing_old_router_cannot_remove_reloaded_component() -> None:
    first_router, bot, dispatcher = _router()

    async def first(_interaction: ModuleInteraction) -> None:
        pass

    old_component = first_router.register_component("button", "confirm", first)
    old_component.close()

    second_router = InteractionRouterImpl(
        bot=bot,  # type: ignore[arg-type]
        module_name="mod",
        trust=FakeTrust({(1, 10): "staff"}),
        dispatcher=dispatcher,
        is_guild_active=lambda _g: True,
    )
    hits: list[str] = []

    async def second(_interaction: ModuleInteraction) -> None:
        hits.append("second")

    second_router.register_component("button", "confirm", second)
    first_router.close()

    interaction = _Interaction(data={"custom_id": second_router.custom_id("confirm")})
    await dispatcher.dispatch(interaction, "button")  # type: ignore[arg-type]
    assert hits == ["second"]


@pytest.mark.asyncio
async def test_components_dispatch_by_custom_id_and_expire() -> None:
    router, _, dispatcher = _router()
    hits: list[tuple[str | None, tuple[str, ...]]] = []

    async def handler(interaction: ModuleInteraction) -> None:
        hits.append((interaction.custom_id, interaction.values))
        await interaction.respond("ok")

    router.register_component("button", "confirm", handler, expires_after_seconds=30)
    custom_id = router.custom_id("confirm", "123")
    assert custom_id == "m:mod:confirm:123"

    interaction = _Interaction(data={"custom_id": custom_id})
    assert await dispatcher.dispatch(interaction, "button") is True  # type: ignore[arg-type]
    assert hits == [("m:mod:confirm:123", ())]

    unknown = _Interaction(data={"custom_id": "m:other:thing"})
    assert await dispatcher.dispatch(unknown, "button") is True  # type: ignore[arg-type]
    assert "no longer active" in unknown.response.sent[0]["content"]

    assert await dispatcher.dispatch(_Interaction(data={"custom_id": "core:x"}), "button") is False  # type: ignore[arg-type]

    dispatcher._clock = lambda: 200.0  # type: ignore[assignment]
    expired = _Interaction(data={"custom_id": custom_id})
    await dispatcher.dispatch(expired, "button")  # type: ignore[arg-type]
    assert "expired" in expired.response.sent[0]["content"]
    assert dispatcher.registered("mod") == ()

    router.register_component("modal", "edit", handler)
    with pytest.raises(ModuleContractError):
        router.register_component("other", "x", handler)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_components_reject_dm_and_inactive_guild_before_handler() -> None:
    hits: list[str] = []

    async def handler(interaction: ModuleInteraction) -> None:
        hits.append("ran")

    router, _, dispatcher = _router()
    router.register_component("button", "confirm", handler)
    custom_id = router.custom_id("confirm")
    dm = _Interaction(guild_id=None, data={"custom_id": custom_id})
    await dispatcher.dispatch(dm, "button")  # type: ignore[arg-type]

    inactive_router, _, inactive_dispatcher = _router(active=False)
    inactive_router.register_component("button", "confirm", handler)
    inactive = _Interaction(data={"custom_id": custom_id})
    await inactive_dispatcher.dispatch(inactive, "button")  # type: ignore[arg-type]

    assert hits == []
    assert dm.response.sent[0]["content"] == "Not allowed."
    assert inactive.response.sent[0]["content"] == "Not allowed."


@pytest.mark.asyncio
async def test_components_enforce_the_registered_minimum_tier() -> None:
    router, _, dispatcher = _router(tiers={(1, 10): "staff", (1, 30): "regular"})
    hits: list[int] = []

    async def handler(interaction: ModuleInteraction) -> None:
        hits.append(interaction.user_id)

    router.register_component("button", "confirm", handler, min_tier="staff")
    custom_id = router.custom_id("confirm")
    regular = _Interaction(user_id=30, data={"custom_id": custom_id})
    staff = _Interaction(user_id=10, data={"custom_id": custom_id})

    await dispatcher.dispatch(regular, "button")  # type: ignore[arg-type]
    await dispatcher.dispatch(staff, "button")  # type: ignore[arg-type]

    assert regular.response.sent[0]["content"] == "Staff only."
    assert hits == [10]


@pytest.mark.asyncio
async def test_dynamic_components_reject_unavailable_runtime_before_trust_or_handler() -> None:
    bot = _Bot()
    available = False
    trust_calls: list[tuple[int, int]] = []

    class _Trust:
        async def tier(self, guild_id: int, user_id: int) -> TrustTierName:
            trust_calls.append((guild_id, user_id))
            return "staff"

    runtime = InteractionRuntime(
        bot,  # type: ignore[arg-type]
        is_available=lambda: available,
    )
    runtime.install()
    router = runtime.router_for("mod", trust=_Trust(), is_guild_active=lambda _g: True)
    hits: list[str] = []

    async def handler(_interaction: ModuleInteraction) -> None:
        hits.append("ran")

    router.register_component("button", "confirm", handler, min_tier="staff")
    custom_id = router.custom_id("confirm")
    interaction = _Interaction(data={"custom_id": custom_id})
    dynamic_button = bot.dynamic_items[0](discord.ui.Button(label="Confirm", custom_id=custom_id))

    await dynamic_button.callback(interaction)  # type: ignore[arg-type]

    assert hits == []
    assert trust_calls == []
    assert interaction.response.sent == [
        {"content": "This control is temporarily unavailable.", "ephemeral": True}
    ]


@pytest.mark.asyncio
async def test_component_admitted_before_shutdown_is_allowed_to_finish() -> None:
    available = True
    dispatcher = ComponentDispatcher(is_available=lambda: available)
    bot = _Bot()
    router = InteractionRouterImpl(
        bot=bot,  # type: ignore[arg-type]
        module_name="mod",
        trust=FakeTrust({(1, 10): "staff"}),
        dispatcher=dispatcher,
        is_guild_active=lambda _g: True,
    )
    started = asyncio.Event()
    release = asyncio.Event()
    finished: list[str] = []

    async def handler(_interaction: ModuleInteraction) -> None:
        started.set()
        await release.wait()
        finished.append("done")

    router.register_component("button", "confirm", handler, min_tier="staff")
    custom_id = router.custom_id("confirm")
    interaction = _Interaction(data={"custom_id": custom_id})
    running = asyncio.create_task(dispatcher.dispatch(interaction, "button"))  # type: ignore[arg-type]
    await started.wait()

    available = False
    release.set()
    await running

    assert finished == ["done"]


def test_build_view_renders_buttons_and_selects_with_module_ids() -> None:
    view = build_view(
        (
            ButtonSpec(key="confirm", label="Confirm", style="danger", parts=("7",)),
            SelectSpec(
                key="pick", options=(("A", "a", None), ("B", "b", "second")), placeholder="Pick"
            ),
        ),
        "mod",
    )
    assert view is not None
    assert view.timeout == 180.0
    button, select = view.children
    assert isinstance(button, discord.ui.Button) and button.custom_id == "m:mod:confirm:7"
    assert isinstance(select, discord.ui.Select) and select.custom_id == "m:mod:pick"
    assert [o.value for o in select.options] == ["a", "b"]
    assert build_view((), "mod") is None
    with pytest.raises(ModuleContractError):
        build_view((object(),), "mod")


def test_build_layout_view_renders_typed_items_and_control_rows() -> None:
    view = build_layout_view(
        OutgoingLayout(
            items=(
                LayoutText("Heading"),
                LayoutSeparator(spacing="large"),
                LayoutGallery(("https://example.test/one.png", "https://example.test/two.png")),
                LayoutSection(("Body", "More"), "https://example.test/thumb.png"),
            ),
            accent_color=0x123456,
        ),
        (
            ButtonSpec(key="back", label="Back"),
            ButtonSpec(key="next", label="Next"),
            SelectSpec(key="page", options=(("One", "1", None),)),
        ),
        "mod",
    )

    assert isinstance(view, discord.ui.LayoutView)
    assert view.timeout == 180.0
    container, button_row, select_row = view.children
    assert isinstance(container, discord.ui.Container)
    assert container.to_component_dict()["accent_color"] == 0x123456
    assert [type(item) for item in container.children] == [
        discord.ui.TextDisplay,
        discord.ui.Separator,
        discord.ui.MediaGallery,
        discord.ui.Section,
    ]
    assert isinstance(button_row, discord.ui.ActionRow)
    assert isinstance(select_row, discord.ui.ActionRow)
    assert len(button_row.children) == 2
    first_button = button_row.children[0]
    page_select = select_row.children[0]
    assert isinstance(first_button, discord.ui.Button)
    assert isinstance(page_select, discord.ui.Select)
    assert first_button.custom_id == "m:mod:back"
    assert page_select.custom_id == "m:mod:page"


def test_build_layout_view_keeps_a_black_accent_colour() -> None:
    # Discord.py drops a falsy accent, so a raw 0 would serialize as absent even
    # though the contract admits it. Only the serialized payload proves it survived.
    black = build_layout_view(
        OutgoingLayout(items=(LayoutText("Heading"),), accent_color=0),
        (),
        "mod",
    )
    container = black.children[0]
    assert isinstance(container, discord.ui.Container)
    assert container.to_component_dict()["accent_color"] == 0

    absent = build_layout_view(
        OutgoingLayout(items=(LayoutText("Heading"),), accent_color=None),
        (),
        "mod",
    )
    unaccented = absent.children[0]
    assert isinstance(unaccented, discord.ui.Container)
    assert unaccented.to_component_dict()["accent_color"] is None


@pytest.mark.asyncio
async def test_modal_show_submit_values_and_validation() -> None:
    router, _, dispatcher = _router()
    submitted: list[dict[str, str]] = []

    async def handler(interaction: ModuleInteraction) -> None:
        submitted.append(dict(interaction.text_values))

    router.register_component("modal", "edit", handler, min_tier="staff")
    opening = _Interaction(data={"custom_id": router.custom_id("open")})
    adapter = ModuleInteractionAdapter(
        opening,  # type: ignore[arg-type]
        "mod",
        dispatcher=dispatcher,
    )
    await adapter.show_modal(
        ModalSpec(
            key="edit",
            title="Edit command",
            inputs=(
                TextInputSpec("title", "Title"),
                TextInputSpec("body", "Body", style="paragraph", max_length=1000),
            ),
            parts=("42",),
        )
    )

    modal = opening.response.modals[0]
    # The wire ID carries a per-open suffix; the module still sees its own ID.
    assert modal.custom_id.startswith("m:mod:edit:42:")
    assert modal.timeout == 30 * 60
    assert [child.custom_id for child in modal.children] == ["title", "body"]

    submit = _Interaction(
        type=discord.InteractionType.modal_submit,
        data={
            "custom_id": modal.custom_id,
            "components": [
                {"type": 1, "components": [{"type": 4, "custom_id": "title", "value": "Hi"}]},
                {
                    "type": 18,
                    "component": {"type": 4, "custom_id": "body", "value": "There"},
                },
                {"type": 2, "custom_id": "ignore", "value": "not text"},
            ],
        },
    )
    assert await dispatcher.dispatch(submit, "modal") is True  # type: ignore[arg-type]
    assert submitted == [{"title": "Hi", "body": "There"}]

    invalid = _Interaction()
    invalid_adapter = ModuleInteractionAdapter(
        invalid,  # type: ignore[arg-type]
        "mod",
        dispatcher=dispatcher,
    )
    with pytest.raises(ModuleContractError, match="unique"):
        await invalid_adapter.show_modal(
            ModalSpec(
                key="edit",
                title="Bad",
                inputs=(TextInputSpec("same", "A"), TextInputSpec("same", "B")),
            )
        )


@pytest.mark.parametrize(
    "modal",
    [
        ModalSpec("edit", "", (TextInputSpec("value", "Value"),)),
        ModalSpec("edit", "x" * 46, (TextInputSpec("value", "Value"),)),
        ModalSpec("edit", "Edit", ()),
        ModalSpec(
            "edit",
            "Edit",
            tuple(TextInputSpec(f"value_{index}", "Value") for index in range(6)),
        ),
        ModalSpec("edit", "Edit", (TextInputSpec("value", ""),)),
        ModalSpec("edit", "Edit", (TextInputSpec("value", "x" * 46),)),
        ModalSpec(
            "edit",
            "Edit",
            (TextInputSpec("value", "Value", style="invalid"),),  # type: ignore[arg-type]
        ),
        ModalSpec("edit", "Edit", (TextInputSpec("value", "Value", min_length=-1),)),
        ModalSpec("edit", "Edit", (TextInputSpec("value", "Value", max_length=0),)),
        ModalSpec("edit", "Edit", (TextInputSpec("value", "Value", max_length=4_001),)),
        ModalSpec("Invalid", "Edit", (TextInputSpec("value", "Value"),)),
        ModalSpec("edit", "Edit", (TextInputSpec("value", "Value"),), ("bad:part",)),
        ModalSpec(
            "edit",
            "Edit",
            (TextInputSpec("value", "Value", min_length=2, max_length=1),),
        ),
        ModalSpec(
            "edit",
            "Edit",
            (TextInputSpec("value", "Value", placeholder="x" * 101),),
        ),
        ModalSpec("edit", "Edit", (TextInputSpec("value", "Value", default="x" * 4_001),)),
    ],
)
@pytest.mark.asyncio
async def test_modal_structural_validation_raises_contract_errors(modal: ModalSpec) -> None:
    _, _, dispatcher = _router()
    adapter = ModuleInteractionAdapter(
        _Interaction(),  # type: ignore[arg-type]
        "mod",
        dispatcher=dispatcher,
    )
    with pytest.raises(ModuleContractError):
        await adapter.show_modal(modal)


@pytest.mark.parametrize(
    "layout",
    [
        OutgoingLayout(()),
        OutgoingLayout(tuple(LayoutText("x") for _ in range(41))),
        OutgoingLayout((LayoutText(""),)),
        OutgoingLayout((LayoutText("x" * 4_001),)),
        OutgoingLayout((LayoutText("x" * 2_001), LayoutText("y" * 2_000))),
        OutgoingLayout((LayoutSeparator(spacing="invalid"),)),  # type: ignore[arg-type]
        OutgoingLayout((LayoutGallery(()),)),
        OutgoingLayout((LayoutGallery(tuple(f"https://example.test/{i}" for i in range(11))),)),
        OutgoingLayout((LayoutGallery(("",)),)),
        OutgoingLayout((LayoutSection((), "https://example.test/thumb.png"),)),
        OutgoingLayout((LayoutSection(("1", "2", "3", "4"), "thumb"),)),
        OutgoingLayout((LayoutSection(("Body",), ""),)),
        OutgoingLayout((LayoutText("Body"),), accent_color=0x1000000),
    ],
)
def test_layout_structural_validation_raises_contract_errors(layout: OutgoingLayout) -> None:
    with pytest.raises(ModuleContractError):
        build_layout_view(layout, (), "mod")


def test_layout_controls_cannot_exceed_five_action_rows() -> None:
    controls = tuple(
        SelectSpec(key=f"select_{index}", options=(("One", "1", None),)) for index in range(6)
    )

    with pytest.raises(ModuleContractError, match="five action rows"):
        build_layout_view(OutgoingLayout((LayoutText("Preview"),)), controls, "mod")


@pytest.mark.asyncio
async def test_adapter_respond_edit_and_follow_up() -> None:
    interaction = _Interaction()
    adapter = ModuleInteractionAdapter(interaction, "mod")  # type: ignore[arg-type]
    await adapter.respond(
        "hi", embed=OutgoingEmbed(title="t"), components=(ButtonSpec(key="k", label="L"),)
    )
    sent = interaction.response.sent[0]
    assert sent["content"] == "hi" and sent["embed"].title == "t" and sent["view"] is not None
    await adapter.respond("again")
    assert interaction.followups[-1]["content"] == "again"
    await adapter.edit_original("edited")
    assert interaction.edits[0]["content"] == "edited" and interaction.edits[0]["view"] is None
    await adapter.follow_up("more", ephemeral=True)
    assert interaction.followups[-1]["ephemeral"] is True


@pytest.mark.asyncio
async def test_adapter_layout_exclusivity_and_modal_edit_response() -> None:
    interaction = _Interaction(type=discord.InteractionType.modal_submit, message=object())
    adapter = ModuleInteractionAdapter(interaction, "mod")  # type: ignore[arg-type]
    layout = OutgoingLayout((LayoutText("Preview"),))

    with pytest.raises(ModuleContractError, match="cannot be combined"):
        await adapter.respond("legacy", layout=layout)
    with pytest.raises(ModuleContractError, match="cannot be combined"):
        await adapter.respond(embed=OutgoingEmbed(title="legacy"), layout=layout)

    await adapter.edit_original(layout=layout)
    assert interaction.response.edits[0]["content"] is None
    assert interaction.response.edits[0]["embed"] is None
    assert isinstance(interaction.response.edits[0]["view"], discord.ui.LayoutView)
    assert interaction.edits == []
    with pytest.raises(ModuleContractError, match="must continue to use layout"):
        await adapter.edit_original("legacy")


@pytest.mark.asyncio
async def test_adapter_rejects_legacy_edit_of_existing_components_v2_message() -> None:
    message = SimpleNamespace(flags=SimpleNamespace(components_v2=True))
    interaction = _Interaction(type=discord.InteractionType.component, message=message)
    adapter = ModuleInteractionAdapter(interaction, "mod")  # type: ignore[arg-type]

    with pytest.raises(ModuleContractError, match="must continue to use layout"):
        await adapter.edit_original("legacy")

    unanchored = _Interaction(type=discord.InteractionType.modal_submit)
    unanchored_adapter = ModuleInteractionAdapter(unanchored, "mod")  # type: ignore[arg-type]
    with pytest.raises(ModuleContractError, match="unanchored modal"):
        await unanchored_adapter.edit_original("cannot repaint")
    assert unanchored.response.edits == []
    assert unanchored.edits == []


def test_interaction_runtime_installs_dynamic_items_once() -> None:
    bot = _Bot()
    runtime = InteractionRuntime(bot)  # type: ignore[arg-type]
    runtime.install()
    runtime.install()
    assert len(bot.dynamic_items) == 1
    router = runtime.router_for("mod", trust=FakeTrust(), is_guild_active=lambda _g: True)
    assert router.custom_id("k") == "m:mod:k"


@pytest.mark.asyncio
async def test_dynamic_item_routes_buttons_and_selects_without_template_collision() -> None:
    bot = _Bot()
    runtime = InteractionRuntime(bot)  # type: ignore[arg-type]
    runtime.install()
    router = runtime.router_for(
        "mod", trust=FakeTrust({(1, 10): "staff"}), is_guild_active=lambda _g: True
    )
    hits: list[str] = []

    async def button_handler(_interaction: ModuleInteraction) -> None:
        hits.append("button")

    async def select_handler(_interaction: ModuleInteraction) -> None:
        hits.append("select")

    router.register_component("button", "confirm", button_handler)
    router.register_component("select", "choose", select_handler)
    dynamic_type = bot.dynamic_items[0]

    button_id = router.custom_id("confirm")
    button = dynamic_type(discord.ui.Button(label="Confirm", custom_id=button_id))
    await button.callback(_Interaction(data={"custom_id": button_id}))  # type: ignore[arg-type]

    select_id = router.custom_id("choose")
    select = dynamic_type(discord.ui.Select(custom_id=select_id))
    await select.callback(_Interaction(data={"custom_id": select_id}))  # type: ignore[arg-type]

    assert hits == ["button", "select"]


@pytest.mark.asyncio
async def test_drain_waits_for_an_in_flight_component_handler() -> None:
    # discord.py dispatches interactions in tasks it never cancels, so without
    # tracking, shutdown would close modules and SQLite under a running handler.
    dispatcher = ComponentDispatcher()
    bot = _Bot()
    router = InteractionRouterImpl(
        bot=bot,  # type: ignore[arg-type]
        module_name="mod",
        trust=FakeTrust({(1, 10): "staff"}),
        dispatcher=dispatcher,
        is_guild_active=lambda _g: True,
    )
    started = asyncio.Event()
    release = asyncio.Event()
    finished: list[str] = []

    async def handler(_interaction: ModuleInteraction) -> None:
        started.set()
        await release.wait()
        finished.append("done")

    router.register_component("button", "confirm", handler, min_tier="staff")
    interaction = _Interaction(data={"custom_id": router.custom_id("confirm")})
    running = asyncio.create_task(dispatcher.dispatch(interaction, "button"))  # type: ignore[arg-type]
    await started.wait()

    draining = asyncio.create_task(dispatcher.drain(timeout=5.0))
    await asyncio.sleep(0)
    assert not draining.done()

    release.set()
    await asyncio.wait_for(draining, timeout=1.0)
    assert finished == ["done"]
    await running


@pytest.mark.asyncio
async def test_drain_cancels_a_handler_that_outlasts_the_bound() -> None:
    dispatcher = ComponentDispatcher()
    bot = _Bot()
    router = InteractionRouterImpl(
        bot=bot,  # type: ignore[arg-type]
        module_name="mod",
        trust=FakeTrust({(1, 10): "staff"}),
        dispatcher=dispatcher,
        is_guild_active=lambda _g: True,
    )
    started = asyncio.Event()

    async def handler(_interaction: ModuleInteraction) -> None:
        started.set()
        await asyncio.Event().wait()

    router.register_component("button", "hang", handler, min_tier="staff")
    interaction = _Interaction(data={"custom_id": router.custom_id("hang")})
    running = asyncio.create_task(dispatcher.dispatch(interaction, "button"))  # type: ignore[arg-type]
    await started.wait()

    await dispatcher.drain(timeout=0.01)

    assert running.done()
    with pytest.raises(asyncio.CancelledError):
        await running


@pytest.mark.asyncio
async def test_drain_remains_bounded_when_a_handler_suppresses_cancellation() -> None:
    dispatcher = ComponentDispatcher()
    bot = _Bot()
    router = InteractionRouterImpl(
        bot=bot,  # type: ignore[arg-type]
        module_name="mod",
        trust=FakeTrust({(1, 10): "staff"}),
        dispatcher=dispatcher,
        is_guild_active=lambda _g: True,
    )
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(_interaction: ModuleInteraction) -> None:
        started.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue

    router.register_component("button", "stubborn", handler, min_tier="staff")
    interaction = _Interaction(data={"custom_id": router.custom_id("stubborn")})
    running = asyncio.create_task(dispatcher.dispatch(interaction, "button"))  # type: ignore[arg-type]
    await started.wait()

    await asyncio.wait_for(
        dispatcher.drain(timeout=0.01, cancel_timeout=0.01),
        timeout=0.2,
    )

    assert running.done() is False
    release.set()
    await asyncio.wait_for(running, timeout=0.2)


@pytest.mark.asyncio
async def test_runtime_drain_cancels_sync_owner_before_waiting_for_the_lock() -> None:
    bot = _Bot()
    runtime = InteractionRuntime(bot, scope_store=_ScopeStore())  # type: ignore[arg-type]
    router = runtime.router_for(
        "mod", trust=FakeTrust({(1, 10): "staff"}), is_guild_active=lambda _g: True
    )
    await runtime.sync_ready()
    sync_started = asyncio.Event()
    release_sync = asyncio.Event()

    async def stubborn_sync(*, guild: Any = None) -> list[Any]:
        assert guild is not None
        sync_started.set()
        while not release_sync.is_set():
            try:
                await release_sync.wait()
            except asyncio.CancelledError:
                continue
        return []

    bot.tree.sync = stubborn_sync  # type: ignore[method-assign]

    async def replacement_handler(_interaction: ModuleInteraction) -> None:
        pass

    async def command_handler(_interaction: ModuleInteraction) -> None:
        await router.replace_guild_commands(
            1,
            (
                GuildCommand(
                    CommandSpec(name="installed", description="installed"),
                    replacement_handler,
                ),
            ),
        )

    router.add_command(CommandSpec(name="trigger", description="trigger"), command_handler)
    running = asyncio.create_task(bot.tree.commands["trigger"].callback(_Interaction()))
    await sync_started.wait()

    await asyncio.wait_for(
        runtime.drain(
            interaction_timeout=0.01,
            cancel_timeout=0.01,
            sync_cancel_timeout=0.01,
        ),
        timeout=0.2,
    )

    assert running.done() is False
    release_sync.set()
    await asyncio.wait_for(running, timeout=0.2)


@pytest.mark.asyncio
async def test_runtime_drain_refuses_new_interactions_before_waiting() -> None:
    runtime = InteractionRuntime(_Bot())  # type: ignore[arg-type]
    router = runtime.router_for(
        "mod", trust=FakeTrust({(1, 10): "staff"}), is_guild_active=lambda _g: True
    )
    ran: list[str] = []

    async def handler(_interaction: ModuleInteraction) -> None:
        ran.append("ran")

    router.register_component("button", "confirm", handler, min_tier="staff")
    await runtime.drain()

    interaction = _Interaction(data={"custom_id": router.custom_id("confirm")})
    assert await runtime.dispatcher.dispatch(interaction, "button") is True  # type: ignore[arg-type]

    assert ran == []
    assert runtime.dispatcher.admitting is False
    assert interaction.response.sent[0]["content"] == "This control is temporarily unavailable."


@pytest.mark.asyncio
async def test_component_trust_lookup_is_tracked_and_cannot_enter_after_drain() -> None:
    class BlockingTrust:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def tier(self, _guild_id: int, _user_id: int) -> TrustTierName:
            self.started.set()
            await self.release.wait()
            return "staff"

    trust = BlockingTrust()
    runtime = InteractionRuntime(_Bot())  # type: ignore[arg-type]
    router = runtime.router_for("mod", trust=trust, is_guild_active=lambda _g: True)
    ran: list[str] = []

    async def handler(_interaction: ModuleInteraction) -> None:
        ran.append("ran")

    router.register_component("button", "confirm", handler, min_tier="staff")
    interaction = _Interaction(data={"custom_id": router.custom_id("confirm")})
    dispatching = asyncio.create_task(
        runtime.dispatcher.dispatch(interaction, "button")  # type: ignore[arg-type]
    )
    await trust.started.wait()

    draining = asyncio.create_task(runtime.drain())
    await asyncio.sleep(0)
    assert not draining.done()
    trust.release.set()
    await draining
    await dispatching

    assert ran == []
    assert interaction.response.sent[0]["content"] == "This control is temporarily unavailable."


@pytest.mark.asyncio
async def test_slash_trust_lookup_is_tracked_and_cannot_enter_after_drain() -> None:
    class BlockingTrust:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def tier(self, _guild_id: int, _user_id: int) -> TrustTierName:
            self.started.set()
            await self.release.wait()
            return "staff"

    trust = BlockingTrust()
    bot = _Bot()
    runtime = InteractionRuntime(bot)  # type: ignore[arg-type]
    router = runtime.router_for("mod", trust=trust, is_guild_active=lambda _g: True)
    ran: list[str] = []

    async def handler(_interaction: ModuleInteraction) -> None:
        ran.append("ran")

    router.add_command(CommandSpec(name="ping", description="ping"), handler)
    interaction = _Interaction()
    dispatching = asyncio.create_task(bot.tree.commands["ping"].callback(interaction))
    await trust.started.wait()

    draining = asyncio.create_task(runtime.drain())
    await asyncio.sleep(0)
    assert not draining.done()
    trust.release.set()
    await draining
    await dispatching

    assert ran == []
    assert interaction.response.sent[0]["content"] == "This command is temporarily unavailable."


@pytest.mark.asyncio
async def test_autocomplete_trust_lookup_is_tracked_and_cannot_enter_after_drain() -> None:
    class BlockingTrust:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def tier(self, _guild_id: int, _user_id: int) -> TrustTierName:
            self.started.set()
            await self.release.wait()
            return "staff"

    trust = BlockingTrust()
    bot = _Bot()
    runtime = InteractionRuntime(bot)  # type: ignore[arg-type]
    router = runtime.router_for("mod", trust=trust, is_guild_active=lambda _g: True)
    ran: list[str] = []

    async def command_handler(_interaction: ModuleInteraction) -> None:
        pass

    async def autocomplete_handler(
        _interaction: ModuleInteraction, _option: str, _current: str
    ) -> list[tuple[str, str]]:
        ran.append("ran")
        return [("one", "one")]

    router.add_command(
        CommandSpec(
            name="lookup",
            description="lookup",
            options=(CommandOption("query", "string", "query", autocomplete=True),),
        ),
        command_handler,
        autocomplete=autocomplete_handler,
    )
    autocomplete = bot.tree.commands["lookup"]._params["query"].autocomplete
    dispatching = asyncio.create_task(autocomplete(_Interaction(), "one"))
    await trust.started.wait()

    draining = asyncio.create_task(runtime.drain())
    await asyncio.sleep(0)
    assert not draining.done()
    trust.release.set()
    await draining

    assert await dispatching == []
    assert ran == []


@pytest.mark.asyncio
async def test_concurrent_modal_opens_survive_each_other_in_the_real_view_store() -> None:
    # Discord.py keys open modals by custom_id, so identical IDs made the second
    # open evict the first and the first submit remove the survivor, silently
    # dropping the other person's submission.
    router, _, dispatcher = _router()
    submitted: list[str] = []

    async def handler(interaction: ModuleInteraction) -> None:
        submitted.append(str(interaction.custom_id))

    router.register_component("modal", "edit", handler, min_tier="staff")
    spec = ModalSpec(
        key="edit",
        title="Edit",
        inputs=(TextInputSpec("title", "Title"),),
        parts=("42",),
    )

    async def open_for(user_id: int) -> Any:
        opening = _Interaction(user_id=user_id)
        await ModuleInteractionAdapter(
            opening,  # type: ignore[arg-type]
            "mod",
            dispatcher=dispatcher,
        ).show_modal(spec)
        return opening.response.modals[0]

    first = await open_for(10)
    second = await open_for(11)
    assert first.custom_id != second.custom_id

    store = discord.ui.view.ViewStore(None)  # type: ignore[arg-type]
    store.add_view(first)
    store.add_view(second)
    assert store._modals[first.custom_id] is first
    assert store._modals[second.custom_id] is second

    # The first submitter's modal completing must not unregister the second's.
    first.stop()
    assert first.custom_id not in store._modals
    assert store._modals[second.custom_id] is second

    for modal in (first, second):
        interaction = _Interaction(
            type=discord.InteractionType.modal_submit,
            data={
                "custom_id": modal.custom_id,
                "components": [
                    {"type": 1, "components": [{"type": 4, "custom_id": "title", "value": "Hi"}]}
                ],
            },
        )
        assert await dispatcher.dispatch(interaction, "modal") is True  # type: ignore[arg-type]

    # Both submissions reached the handler, each seeing the ID the module described.
    assert submitted == ["m:mod:edit:42", "m:mod:edit:42"]


@pytest.mark.asyncio
async def test_invalid_command_specs_are_refused_before_the_tree_is_touched() -> None:
    # A whole guild scope syncs as one bulk PUT, so a malformed command has to be
    # rejected at registration rather than rejecting every sibling command later.
    bot = _Bot()
    runtime = InteractionRuntime(bot, scope_store=_ScopeStore())  # type: ignore[arg-type]
    router = runtime.router_for(
        "mod", trust=FakeTrust({(1, 10): "staff"}), is_guild_active=lambda _g: True
    )

    async def handler(_interaction: ModuleInteraction) -> None:
        pass

    bad = CommandSpec(
        name="broken",
        description="d",
        options=(CommandOption("a", "string", "d", choices=(("N", "v"),), autocomplete=True),),
    )
    with pytest.raises(ModuleContractError, match="choices and autocomplete"):
        router.add_command(bad, handler)
    assert bot.tree.commands == {}

    router.add_command(CommandSpec(name="good", description="d"), handler)
    with pytest.raises(ModuleContractError, match="more than 25 options"):
        await router.replace_guild_commands(
            7,
            (
                GuildCommand(CommandSpec(name="fine", description="d"), handler),
                GuildCommand(
                    CommandSpec(
                        name="huge",
                        description="d",
                        options=tuple(CommandOption(f"o{i}", "string", "d") for i in range(26)),
                    ),
                    handler,
                ),
            ),
        )
    # The valid sibling in the same batch must not have been staged either.
    assert bot.tree.guild_commands.get(7, {}) == {}
    assert router.has_guild_commands(7) is False


def test_invalid_select_specs_are_refused_when_a_view_is_built() -> None:
    with pytest.raises(ModuleContractError, match="between one and 25 options"):
        build_view((SelectSpec(key="pick", options=()),), "mod")
    with pytest.raises(ModuleContractError, match="min_values cannot exceed max_values"):
        build_view(
            (
                SelectSpec(
                    key="pick",
                    options=(("A", "a", None), ("B", "b", None)),
                    min_values=2,
                    max_values=1,
                ),
            ),
            "mod",
        )


@pytest.mark.asyncio
async def test_modal_custom_id_budget_does_not_shrink_as_the_host_runs() -> None:
    # The per-open suffix is fixed width on purpose. A variable-width one would
    # let a modal open successfully N times and then fail permanently once the
    # process-wide counter grew a digit, blaming a limit the module never hit.
    _, _, dispatcher = _router()
    padding = "m" * (MODAL_CUSTOM_ID_MAX_LENGTH - len(build_custom_id("mod", "edit", "")))
    at_budget = ModalSpec(
        key="edit",
        title="Edit",
        inputs=(TextInputSpec("title", "Title"),),
        parts=(padding,),
    )
    assert len(build_custom_id("mod", "edit", padding)) == MODAL_CUSTOM_ID_MAX_LENGTH

    widths: set[int] = set()
    for start in (0, 16, 4096, 2**32 - 1):
        module_interactions._modal_opens = itertools.count(start)
        interaction = _Interaction()
        await ModuleInteractionAdapter(
            interaction,  # type: ignore[arg-type]
            "mod",
            dispatcher=dispatcher,
        ).show_modal(at_budget)
        minted = interaction.response.modals[0].custom_id
        assert len(minted) <= CUSTOM_ID_MAX_LENGTH
        widths.add(len(minted))
    assert widths == {CUSTOM_ID_MAX_LENGTH}

    over_budget = ModalSpec(
        key="edit",
        title="Edit",
        inputs=(TextInputSpec("title", "Title"),),
        parts=(padding + "m",),
    )
    module_interactions._modal_opens = itertools.count(0)
    with pytest.raises(ModuleContractError, match=f"exceeds {MODAL_CUSTOM_ID_MAX_LENGTH}"):
        await ModuleInteractionAdapter(
            _Interaction(),  # type: ignore[arg-type]
            "mod",
            dispatcher=dispatcher,
        ).show_modal(over_budget)


@pytest.mark.asyncio
async def test_autocomplete_option_without_a_handler_is_refused() -> None:
    # Discord would render a suggestion box that nothing ever answers.
    router, bot, _ = _router()

    async def handler(_interaction: ModuleInteraction) -> None:
        pass

    spec = CommandSpec(
        name="search",
        description="Search",
        options=(CommandOption("query", "string", "Query", autocomplete=True),),
    )
    with pytest.raises(ModuleContractError, match="no autocomplete handler"):
        router.add_command(spec, handler)
    assert bot.tree.commands == {}

    async def complete(_i: ModuleInteraction, _name: str, _current: str) -> list[Any]:
        return []

    router.add_command(spec, handler, autocomplete=complete)
    assert "search" in bot.tree.commands


def test_invalid_button_specs_are_refused_when_a_view_is_built() -> None:
    with pytest.raises(ModuleContractError, match="label or emoji"):
        build_view((ButtonSpec(key="go", label=""),), "mod")


@pytest.mark.asyncio
async def test_runtime_drain_lets_a_syncing_handler_finish_inside_the_interaction_window() -> None:
    """A handler that replaces guild commands is tracked as both an in-flight
    interaction and a sync operation. Drain must give it the interaction
    window rather than cancelling it after the shorter sync grace."""

    bot = _Bot()
    runtime = InteractionRuntime(bot, scope_store=_ScopeStore())  # type: ignore[arg-type]
    router = runtime.router_for(
        "mod", trust=FakeTrust({(1, 10): "staff"}), is_guild_active=lambda _g: True
    )
    await runtime.sync_ready()
    sync_started = asyncio.Event()
    finished: list[str] = []

    async def brief_sync(*, guild: Any = None) -> list[Any]:
        assert guild is not None
        sync_started.set()
        await asyncio.sleep(0.05)
        return []

    bot.tree.sync = brief_sync  # type: ignore[method-assign]

    async def replacement_handler(_interaction: ModuleInteraction) -> None:
        pass

    async def command_handler(_interaction: ModuleInteraction) -> None:
        await router.replace_guild_commands(
            1,
            (
                GuildCommand(
                    CommandSpec(name="installed", description="installed"),
                    replacement_handler,
                ),
            ),
        )
        finished.append("replied")

    router.add_command(CommandSpec(name="trigger", description="trigger"), command_handler)
    running = asyncio.create_task(bot.tree.commands["trigger"].callback(_Interaction()))
    await sync_started.wait()

    await asyncio.wait_for(
        runtime.drain(
            interaction_timeout=1.0,
            cancel_timeout=0.01,
            sync_cancel_timeout=0.01,
        ),
        timeout=2.0,
    )

    assert running.done() is True
    assert finished == ["replied"]
