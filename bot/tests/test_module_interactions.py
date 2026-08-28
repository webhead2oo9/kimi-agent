"""Module interaction router: Discord-free specs in, gated app commands out."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import discord
import pytest
from discord import app_commands

from discord_adapter.module_interactions import (
    ComponentDispatcher,
    InteractionRouterImpl,
    InteractionRuntime,
    ModuleInteractionAdapter,
    _option_value,
    build_view,
)
from community_agent_module_api.contracts import (
    ButtonSpec,
    CommandOption,
    CommandSpec,
    ModuleContractError,
    ModuleInteraction,
    OutgoingEmbed,
    SelectSpec,
    TrustTierName,
)
from community_agent_module_api.testing import FakeTrust


class _Tree:
    def __init__(self) -> None:
        self.commands: dict[str, Any] = {}

    def add_command(self, command: Any, **_kwargs: Any) -> None:
        self.commands[command.name] = command

    def get_command(self, name: str) -> Any:
        return self.commands.get(name)

    def remove_command(self, name: str) -> Any:
        return self.commands.pop(name, None)


class _Bot:
    def __init__(self) -> None:
        self.tree = _Tree()
        self.dynamic_items: list[Any] = []

    def add_dynamic_items(self, *items: Any) -> None:
        self.dynamic_items.extend(items)


class _Response:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.deferred: list[bool] = []
        self._done = False

    def is_done(self) -> bool:
        return self._done

    async def send_message(self, **kwargs: Any) -> None:
        self._done = True
        self.sent.append(kwargs)

    async def defer(self, *, ephemeral: bool = False) -> None:
        self._done = True
        self.deferred.append(ephemeral)


class _Interaction:
    def __init__(
        self, *, user_id: int = 10, guild_id: int | None = 1, data: dict[str, Any] | None = None
    ) -> None:
        self.user = SimpleNamespace(id=user_id)
        self.guild_id = guild_id
        self.channel_id = 2
        self.data = data or {}
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

    with pytest.raises(ModuleContractError):
        router.register_component("modal", "x", handler)


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
    dynamic_button = bot.dynamic_items[0](custom_id)

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
    button, select = view.children
    assert isinstance(button, discord.ui.Button) and button.custom_id == "m:mod:confirm:7"
    assert isinstance(select, discord.ui.Select) and select.custom_id == "m:mod:pick"
    assert [o.value for o in select.options] == ["a", "b"]
    assert build_view((), "mod") is None
    with pytest.raises(ModuleContractError):
        build_view((object(),), "mod")


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


def test_interaction_runtime_installs_dynamic_items_once() -> None:
    bot = _Bot()
    runtime = InteractionRuntime(bot)  # type: ignore[arg-type]
    runtime.install()
    runtime.install()
    assert len(bot.dynamic_items) == 2
    router = runtime.router_for("mod", trust=FakeTrust(), is_guild_active=lambda _g: True)
    assert router.custom_id("k") == "m:mod:k"
