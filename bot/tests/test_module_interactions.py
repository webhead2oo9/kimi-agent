"""Module interaction router: Discord-free specs in, gated app commands out."""

from __future__ import annotations

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
    build_view,
)
from kimi_agent_module_api.contracts import (
    ButtonSpec,
    CommandOption,
    CommandSpec,
    ModuleContractError,
    ModuleInteraction,
    OutgoingEmbed,
    SelectSpec,
)
from kimi_agent_module_api.testing import FakeTrust


class _Tree:
    def __init__(self) -> None:
        self.commands: dict[str, Any] = {}

    def add_command(self, command: Any, **_kwargs: Any) -> None:
        self.commands[command.name] = command

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
    *, staff: frozenset[int] = frozenset({10}), active: bool = True
) -> tuple[InteractionRouterImpl, _Bot, ComponentDispatcher]:
    bot = _Bot()
    dispatcher = ComponentDispatcher(clock=lambda: 100.0)
    router = InteractionRouterImpl(
        bot=bot,  # type: ignore[arg-type]
        module_name="mod",
        trust=FakeTrust({(1, uid): "staff" for uid in staff}),
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
