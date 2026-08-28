"""Discord SDK implementation of the module ``InteractionRouter`` port.

Modules describe commands with ``CommandSpec`` and handle ``ModuleInteraction``
values; this adapter builds the ``app_commands`` objects, gates them by the
declared minimum trust tier, converts option values to stable IDs, and routes
persistent components (``m:<module>:<key>:...`` custom IDs) through one
``DynamicItem`` class per kind so buttons survive restarts.

Ownership is per module: every command and component registration is tracked
and removed on close. Core performs one ``tree.sync`` after modules start.
"""

from __future__ import annotations

import inspect
import logging
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar

import discord
from discord import app_commands
from discord.ext import commands

from kimi_agent_module_api.contracts import (
    MessageRef,
    CUSTOM_ID_PREFIX,
    AutocompleteHandler,
    ButtonSpec,
    CommandHandler,
    CommandSpec,
    ModuleContractError,
    OutgoingEmbed,
    SelectSpec,
    TrustLookup,
    TrustTierName,
    build_custom_id,
    parse_custom_id,
)

log = logging.getLogger(__name__)

_TIER_ORDER: dict[TrustTierName, int] = {"member": 0, "regular": 1, "staff": 2}
type InteractionAvailability = Callable[[], bool]


def _always_available() -> bool:
    return True


_OPTION_TYPES: dict[str, Any] = {
    "string": str,
    "integer": int,
    "boolean": bool,
    "user": discord.User,
    "channel": discord.abc.GuildChannel,
    "role": discord.Role,
}
_BUTTON_STYLES = {
    "primary": discord.ButtonStyle.primary,
    "secondary": discord.ButtonStyle.secondary,
    "success": discord.ButtonStyle.success,
    "danger": discord.ButtonStyle.danger,
}
_COMPONENT_TEMPLATE = (
    rf"{CUSTOM_ID_PREFIX}:(?P<module>[a-z][a-z0-9_-]*):(?P<key>[a-z][a-z0-9_]*)(?::(?P<rest>.*))?"
)


def build_embed(spec: OutgoingEmbed) -> discord.Embed:
    embed = discord.Embed(title=spec.title, description=spec.description, color=spec.color)
    for name, value, inline in spec.fields:
        embed.add_field(name=name, value=value, inline=inline)
    if spec.footer:
        embed.set_footer(text=spec.footer)
    if spec.timestamp:
        embed.timestamp = discord.utils.utcnow()
    return embed


def build_view(components: Sequence[Any], module_name: str) -> discord.ui.View | None:
    """Turn ``ButtonSpec``/``SelectSpec`` values into a persistent view."""
    if not components:
        return None
    view = discord.ui.View(timeout=None)
    for component in components:
        if isinstance(component, ButtonSpec):
            view.add_item(
                discord.ui.Button(
                    label=component.label,
                    style=_BUTTON_STYLES[component.style],
                    custom_id=build_custom_id(module_name, component.key, *component.parts),
                    disabled=component.disabled,
                    emoji=component.emoji,
                )
            )
        elif isinstance(component, SelectSpec):
            view.add_item(
                discord.ui.Select(
                    custom_id=build_custom_id(module_name, component.key, *component.parts),
                    placeholder=component.placeholder,
                    min_values=component.min_values,
                    max_values=component.max_values,
                    options=[
                        discord.SelectOption(label=label, value=value, description=description)
                        for label, value, description in component.options
                    ],
                )
            )
        else:
            raise ModuleContractError(f"unsupported component {component!r}")
    return view


class ModuleInteractionAdapter:
    """``ModuleInteraction`` over a live ``discord.Interaction``."""

    def __init__(
        self,
        interaction: discord.Interaction,
        module_name: str,
        *,
        options: Mapping[str, Any] | None = None,
    ) -> None:
        self._interaction = interaction
        self._module_name = module_name
        self._options = dict(options or {})

    @property
    def guild_id(self) -> int:
        return int(self._interaction.guild_id or 0)

    @property
    def channel_id(self) -> int:
        return int(self._interaction.channel_id or 0)

    @property
    def user_id(self) -> int:
        return int(self._interaction.user.id)

    @property
    def guild_name(self) -> str | None:
        guild = getattr(self._interaction, "guild", None)
        return str(guild.name) if guild is not None else None

    @property
    def options(self) -> Mapping[str, Any]:
        return self._options

    @property
    def custom_id(self) -> str | None:
        data = getattr(self._interaction, "data", None) or {}
        value = data.get("custom_id") if isinstance(data, dict) else None
        return str(value) if value else None

    @property
    def values(self) -> tuple[str, ...]:
        data = getattr(self._interaction, "data", None) or {}
        values = data.get("values") if isinstance(data, dict) else None
        return tuple(str(v) for v in values) if values else ()

    @property
    def message(self) -> MessageRef | None:
        """The message a component lives on. Slash commands have none."""
        message = self._interaction.message
        if message is None or self._interaction.guild_id is None:
            return None
        channel = message.channel
        parent_id = channel.parent_id if isinstance(channel, discord.Thread) else None
        return MessageRef(
            guild_id=int(self._interaction.guild_id),
            channel_id=int(channel.id),
            message_id=int(message.id),
            parent_channel_id=int(parent_id) if parent_id is not None else None,
        )

    def _kwargs(
        self,
        content: str | None,
        embed: OutgoingEmbed | None,
        components: Sequence[Any],
        *,
        ephemeral: bool | None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"allowed_mentions": discord.AllowedMentions.none()}
        if content is not None:
            kwargs["content"] = content
        if embed is not None:
            kwargs["embed"] = build_embed(embed)
        view = build_view(components, self._module_name)
        if view is not None or components == ():
            kwargs["view"] = view
        if ephemeral is not None:
            kwargs["ephemeral"] = ephemeral
        return kwargs

    async def respond(
        self,
        content: str | None = None,
        *,
        embed: OutgoingEmbed | None = None,
        ephemeral: bool = False,
        components: Sequence[Any] = (),
    ) -> None:
        kwargs = self._kwargs(content, embed, components, ephemeral=ephemeral)
        if kwargs.get("view") is None:
            kwargs.pop("view", None)
        if self._interaction.response.is_done():
            await self._interaction.followup.send(**kwargs)
        else:
            await self._interaction.response.send_message(**kwargs)

    async def defer(self, *, ephemeral: bool = False) -> None:
        if not self._interaction.response.is_done():
            await self._interaction.response.defer(ephemeral=ephemeral)

    async def edit_original(
        self,
        content: str | None = None,
        *,
        embed: OutgoingEmbed | None = None,
        components: Sequence[Any] = (),
    ) -> None:
        kwargs = self._kwargs(content, embed, components, ephemeral=None)
        kwargs.setdefault("view", None)
        await self._interaction.edit_original_response(**kwargs)

    async def follow_up(
        self, content: str, *, embed: OutgoingEmbed | None = None, ephemeral: bool = False
    ) -> None:
        kwargs = self._kwargs(content, embed, (), ephemeral=ephemeral)
        kwargs.pop("view", None)
        await self._interaction.followup.send(**kwargs)


def _option_value(value: Any) -> Any:
    """Reduce SDK option objects to stable IDs; scalars pass through."""
    if isinstance(
        value,
        discord.Member | discord.User | discord.Role | discord.Thread | discord.abc.GuildChannel,
    ):
        return int(value.id)
    return value


@dataclass(slots=True)
class _ComponentRegistration:
    router: InteractionRouterImpl
    handler: CommandHandler
    expires_at: float | None
    min_tier: TrustTierName


@dataclass(slots=True)
class _Registration:
    router: InteractionRouterImpl
    close_fn: Callable[[], None]
    closed: bool = False

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.close_fn()


class ComponentDispatcher:
    """One process-wide table of (module, kind, key) -> handler."""

    def __init__(
        self,
        clock: Callable[[], float] = time.time,
        *,
        is_available: InteractionAvailability = _always_available,
    ) -> None:
        self._clock = clock
        self._is_available = is_available
        self._handlers: dict[tuple[str, str, str], _ComponentRegistration] = {}
        self._routers: dict[str, InteractionRouterImpl] = {}

    def register(
        self,
        router: InteractionRouterImpl,
        kind: str,
        key: str,
        handler: CommandHandler,
        expires_after_seconds: float | None,
        min_tier: TrustTierName,
    ) -> None:
        expires_at = self._clock() + expires_after_seconds if expires_after_seconds else None
        self._handlers[(router.module_name, kind, key)] = _ComponentRegistration(
            router, handler, expires_at, min_tier
        )

    def unregister(self, module_name: str, kind: str, key: str) -> None:
        self._handlers.pop((module_name, kind, key), None)

    def unregister_module(self, module_name: str) -> None:
        for entry in [k for k in self._handlers if k[0] == module_name]:
            self._handlers.pop(entry, None)

    def registered(self, module_name: str) -> tuple[tuple[str, str], ...]:
        return tuple((kind, key) for name, kind, key in self._handlers if name == module_name)

    async def dispatch(self, interaction: discord.Interaction, kind: str) -> bool:
        data = getattr(interaction, "data", None) or {}
        custom_id = data.get("custom_id") if isinstance(data, dict) else None
        parsed = parse_custom_id(str(custom_id or ""))
        if parsed is None:
            return False
        module_name, key, _parts = parsed
        registration = self._handlers.get((module_name, kind, key))
        if registration is None:
            await _quiet_reply(interaction, "This control is no longer active.")
            return True
        try:
            available = self._is_available()
        except Exception:
            log.exception("Dynamic component availability check failed")
            available = False
        if not available:
            await _quiet_reply(interaction, "This control is temporarily unavailable.")
            return True
        if registration.expires_at is not None and self._clock() > registration.expires_at:
            self._handlers.pop((module_name, kind, key), None)
            await _quiet_reply(interaction, "This control has expired.")
            return True
        if not await registration.router._allowed(interaction, registration.min_tier):
            await _quiet_reply(
                interaction,
                "Staff only." if registration.min_tier == "staff" else "Not allowed.",
            )
            return True
        try:
            await registration.handler(ModuleInteractionAdapter(interaction, module_name))
        except Exception:
            log.exception("Module %s component %s/%s failed", module_name, kind, key)
            await _quiet_reply(interaction, "Something went wrong handling that control.")
        return True


async def _quiet_reply(interaction: discord.Interaction, text: str) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(content=text, ephemeral=True)
        else:
            await interaction.response.send_message(content=text, ephemeral=True)
    except discord.HTTPException:
        pass


def make_dynamic_items(bound: ComponentDispatcher) -> tuple[type[Any], ...]:
    """Build the DynamicItem classes bound to one dispatcher (call once per bot)."""

    class ModuleButton(
        discord.ui.DynamicItem[discord.ui.Button[Any]], template=_COMPONENT_TEMPLATE
    ):
        dispatcher: ClassVar[ComponentDispatcher] = bound

        def __init__(self, custom_id: str) -> None:
            super().__init__(discord.ui.Button(label="​", custom_id=custom_id))

        @classmethod
        async def from_custom_id(
            cls,
            interaction: discord.Interaction,
            item: discord.ui.Item[Any],
            match: re.Match[str],
            /,
        ) -> ModuleButton:
            return cls(match.string)

        async def callback(self, interaction: discord.Interaction) -> None:
            await self.dispatcher.dispatch(interaction, "button")

    class ModuleSelect(
        discord.ui.DynamicItem[discord.ui.Select[Any]], template=_COMPONENT_TEMPLATE
    ):
        dispatcher: ClassVar[ComponentDispatcher] = bound

        def __init__(self, custom_id: str) -> None:
            super().__init__(discord.ui.Select(custom_id=custom_id))

        @classmethod
        async def from_custom_id(
            cls,
            interaction: discord.Interaction,
            item: discord.ui.Item[Any],
            match: re.Match[str],
            /,
        ) -> ModuleSelect:
            return cls(match.string)

        async def callback(self, interaction: discord.Interaction) -> None:
            await self.dispatcher.dispatch(interaction, "select")

    return (ModuleButton, ModuleSelect)


class InteractionRouterImpl:
    """The ``InteractionRouter`` port handed to one module."""

    def __init__(
        self,
        *,
        bot: commands.Bot,
        module_name: str,
        trust: TrustLookup,
        dispatcher: ComponentDispatcher,
        is_guild_active: Callable[[int], bool],
    ) -> None:
        self._bot = bot
        self._module_name = module_name
        self._trust = trust
        self._dispatcher = dispatcher
        self._is_guild_active = is_guild_active
        self._groups: dict[str, app_commands.Group] = {}
        self._commands: list[tuple[str | None, str]] = []
        self._closed = False

    @property
    def module_name(self) -> str:
        """Name owning commands and persistent component registrations."""
        return self._module_name

    # ---- commands ----------------------------------------------------------

    def add_command(
        self,
        spec: CommandSpec,
        handler: CommandHandler,
        *,
        autocomplete: AutocompleteHandler | None = None,
    ) -> _Registration:
        if self._closed:
            raise RuntimeError(f"module {self._module_name!r} interactions are closed")
        command = self._build_command(spec, handler, autocomplete)
        if spec.group:
            group = self._groups.get(spec.group)
            if group is None:
                if self._bot.tree.get_command(spec.group) is not None:
                    raise ModuleContractError(
                        f"module {self._module_name!r} command group {spec.group!r} "
                        "is already owned"
                    )
                group = app_commands.Group(
                    name=spec.group, description=spec.group_description or spec.group
                )
                self._groups[spec.group] = group
                self._bot.tree.add_command(group)
            group.add_command(command)
        else:
            if self._bot.tree.get_command(spec.name) is not None:
                raise ModuleContractError(
                    f"module {self._module_name!r} command {spec.name!r} is already owned"
                )
            self._bot.tree.add_command(command)
        self._commands.append((spec.group, spec.name))

        def close() -> None:
            self._remove_command(spec.group, spec.name)

        return _Registration(self, close)

    def _remove_command(self, group_name: str | None, name: str) -> None:
        if group_name:
            group = self._groups.get(group_name)
            if group is not None:
                group.remove_command(name)
                if not group.commands:
                    self._bot.tree.remove_command(group_name)
                    self._groups.pop(group_name, None)
        else:
            self._bot.tree.remove_command(name)
        if (group_name, name) in self._commands:
            self._commands.remove((group_name, name))

    def _build_command(
        self,
        spec: CommandSpec,
        handler: CommandHandler,
        autocomplete: AutocompleteHandler | None,
    ) -> app_commands.Command[Any, ..., None]:
        module_name = self._module_name
        option_names = [option.name for option in spec.options]

        async def callback(interaction: discord.Interaction, **kwargs: Any) -> None:
            options = {name: _option_value(kwargs.get(name)) for name in option_names}
            adapter = ModuleInteractionAdapter(interaction, module_name, options=options)
            if not await self._allowed(interaction, spec.min_tier):
                await adapter.respond(
                    "Staff only." if spec.min_tier == "staff" else "Not allowed.", ephemeral=True
                )
                return
            try:
                await handler(adapter)
            except Exception:
                log.exception("Module %s command %s failed", module_name, spec.name)
                await _quiet_reply(interaction, "Something went wrong running that command.")

        parameters = [
            inspect.Parameter(
                "interaction",
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                annotation=discord.Interaction,
            )
        ]
        for option in spec.options:
            annotation: Any = _OPTION_TYPES[option.kind]
            if option.kind == "user":
                annotation = discord.Member | discord.User
            if option.kind == "integer" and (
                option.min_value is not None or option.max_value is not None
            ):
                annotation = app_commands.Range[int, option.min_value, option.max_value]
            default = inspect.Parameter.empty if option.required else None
            if not option.required:
                annotation = annotation | None
            parameters.append(
                inspect.Parameter(
                    option.name,
                    inspect.Parameter.KEYWORD_ONLY,
                    annotation=annotation,
                    default=default,
                )
            )
        callback.__signature__ = inspect.Signature(parameters)  # type: ignore[attr-defined]
        callback.__annotations__ = {p.name: p.annotation for p in parameters}
        command: app_commands.Command[Any, ..., None] = app_commands.Command(
            name=spec.name, description=spec.description, callback=callback
        )
        for option in spec.options:
            parameter = command._params.get(option.name)
            if parameter is None:
                continue
            parameter.description = option.description
            if option.choices:
                parameter.choices = [
                    app_commands.Choice(name=name, value=value) for name, value in option.choices
                ]
            if option.autocomplete and autocomplete is not None:
                command.autocomplete(option.name)(
                    self._autocomplete(option.name, autocomplete, spec.min_tier)
                )
        return command

    def _autocomplete(
        self,
        option_name: str,
        handler: AutocompleteHandler,
        min_tier: TrustTierName,
    ) -> Any:
        module_name = self._module_name

        async def run(
            interaction: discord.Interaction, current: str
        ) -> list[app_commands.Choice[Any]]:
            if not await self._allowed(interaction, min_tier):
                return []
            try:
                results = await handler(
                    ModuleInteractionAdapter(interaction, module_name), option_name, current
                )
            except Exception:
                log.exception("Module %s autocomplete for %s failed", module_name, option_name)
                return []
            return [
                app_commands.Choice(name=name, value=value) for name, value in list(results)[:25]
            ]

        return run

    async def _allowed(self, interaction: discord.Interaction, min_tier: TrustTierName) -> bool:
        guild_id = int(interaction.guild_id or 0)
        if guild_id == 0 or not self._is_guild_active(guild_id):
            return False
        tier = await self._trust.tier(guild_id, int(interaction.user.id))
        return _TIER_ORDER[tier] >= _TIER_ORDER[min_tier]

    # ---- components ---------------------------------------------------------

    def register_component(
        self,
        kind: str,
        key: str,
        handler: CommandHandler,
        *,
        expires_after_seconds: float | None = None,
        min_tier: TrustTierName = "member",
    ) -> _Registration:
        if kind not in ("button", "select"):
            raise ModuleContractError(f"unsupported component kind {kind!r}")
        build_custom_id(self._module_name, key)  # validates the key
        self._dispatcher.register(self, kind, key, handler, expires_after_seconds, min_tier)
        return _Registration(
            self, lambda: self._dispatcher.unregister(self._module_name, kind, key)
        )

    def custom_id(self, key: str, *parts: str) -> str:
        return build_custom_id(self._module_name, key, *parts)

    # ---- ownership -----------------------------------------------------------

    def owned_commands(self) -> tuple[str, ...]:
        return tuple(f"{g}.{n}" if g else n for g, n in self._commands)

    def close(self) -> None:
        self._closed = True
        for group_name, name in list(self._commands):
            self._remove_command(group_name, name)
        self._dispatcher.unregister_module(self._module_name)


@dataclass(slots=True)
class InteractionRuntime:
    """Process-wide interaction state: the dispatcher and its dynamic items."""

    bot: commands.Bot
    is_available: InteractionAvailability = _always_available
    dispatcher: ComponentDispatcher = field(init=False)
    installed: bool = False

    def __post_init__(self) -> None:
        self.dispatcher = ComponentDispatcher(is_available=self.is_available)

    def install(self) -> None:
        if self.installed:
            return
        self.bot.add_dynamic_items(*make_dynamic_items(self.dispatcher))
        self.installed = True

    def router_for(
        self, module_name: str, *, trust: TrustLookup, is_guild_active: Callable[[int], bool]
    ) -> InteractionRouterImpl:
        return InteractionRouterImpl(
            bot=self.bot,
            module_name=module_name,
            trust=trust,
            dispatcher=self.dispatcher,
            is_guild_active=is_guild_active,
        )


__all__ = [
    "ComponentDispatcher",
    "InteractionRouterImpl",
    "InteractionRuntime",
    "ModuleInteractionAdapter",
    "build_embed",
    "build_view",
    "make_dynamic_items",
]
