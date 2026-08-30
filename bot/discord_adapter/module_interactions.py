"""Discord SDK implementation of the module ``InteractionRouter`` port.

Modules describe commands with ``CommandSpec`` and handle ``ModuleInteraction``
values; this adapter builds the ``app_commands`` objects, gates them by the
declared minimum trust tier, converts option values to stable IDs, and routes
persistent components (``m:<module>:<key>:...`` custom IDs) through one
``DynamicItem`` class per kind so buttons survive restarts.

Ownership is per module: every command and component registration is tracked
and removed on close. Core syncs global commands and the tracked guild command
sets after modules start.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol

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
    CommandSyncError,
    GuildCommand,
    LayoutGallery,
    LayoutSection,
    LayoutSeparator,
    LayoutText,
    ModalSpec,
    ModuleContractError,
    OutgoingEmbed,
    OutgoingLayout,
    SelectSpec,
    TextInputSpec,
    TrustLookup,
    TrustTierName,
    build_custom_id,
    parse_custom_id,
    validate_modal_spec,
    validate_layout_components,
    validate_outgoing_layout,
)

log = logging.getLogger(__name__)

_TIER_ORDER: dict[TrustTierName, int] = {"member": 0, "regular": 1, "staff": 2}
type InteractionAvailability = Callable[[], bool]


class GuildCommandScopeStore(Protocol):
    async def track(self, guild_id: int) -> None: ...

    async def guild_ids(self) -> tuple[int, ...]: ...

    async def forget(self, guild_id: int) -> None: ...


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


def _build_control(component: Any, module_name: str) -> discord.ui.Item[Any]:
    if isinstance(component, ButtonSpec):
        return discord.ui.Button(
            label=component.label,
            style=_BUTTON_STYLES[component.style],
            custom_id=build_custom_id(module_name, component.key, *component.parts),
            disabled=component.disabled,
            emoji=component.emoji,
        )
    if isinstance(component, SelectSpec):
        return discord.ui.Select(
            custom_id=build_custom_id(module_name, component.key, *component.parts),
            placeholder=component.placeholder,
            min_values=component.min_values,
            max_values=component.max_values,
            options=[
                discord.SelectOption(label=label, value=value, description=description)
                for label, value, description in component.options
            ],
        )
    raise ModuleContractError(f"unsupported component {component!r}")


def build_view(components: Sequence[Any], module_name: str) -> discord.ui.View | None:
    """Turn ``ButtonSpec``/``SelectSpec`` values into a persistent view."""
    if not components:
        return None
    view = discord.ui.View(timeout=None)
    for component in components:
        view.add_item(_build_control(component, module_name))
    return view


def build_layout_view(
    layout: OutgoingLayout, components: Sequence[Any], module_name: str
) -> discord.ui.LayoutView:
    """Turn the narrow public layout model into one Components V2 container."""
    validate_outgoing_layout(layout)
    validate_layout_components(components, layout=layout)
    children: list[discord.ui.Item[Any]] = []
    for item in layout.items:
        if isinstance(item, LayoutText):
            children.append(discord.ui.TextDisplay(item.content))
        elif isinstance(item, LayoutSeparator):
            spacing = (
                discord.SeparatorSpacing.large
                if item.spacing == "large"
                else discord.SeparatorSpacing.small
            )
            children.append(discord.ui.Separator(visible=item.visible, spacing=spacing))
        elif isinstance(item, LayoutGallery):
            children.append(
                discord.ui.MediaGallery(*(discord.MediaGalleryItem(url) for url in item.urls))
            )
        elif isinstance(item, LayoutSection):
            children.append(
                discord.ui.Section(
                    *item.texts,
                    accessory=discord.ui.Thumbnail(item.thumbnail_url),
                )
            )
        else:
            raise ModuleContractError(f"unsupported layout item {item!r}")

    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(*children, accent_color=layout.accent_color))

    row_items: list[discord.ui.Item[Any]] = []
    for component in components:
        control = _build_control(component, module_name)
        if isinstance(control, discord.ui.Select):
            if row_items:
                view.add_item(discord.ui.ActionRow(*row_items))
                row_items = []
            view.add_item(discord.ui.ActionRow(control))
            continue
        if len(row_items) == 5:
            view.add_item(discord.ui.ActionRow(*row_items))
            row_items = []
        row_items.append(control)
    if row_items:
        view.add_item(discord.ui.ActionRow(*row_items))
    return view


def _text_values(data: Any) -> dict[str, str]:
    found: dict[str, str] = {}

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            key = value.get("custom_id")
            text = value.get("value")
            if value.get("type") == int(discord.ComponentType.text_input) and key is not None:
                found[str(key)] = str(text)
            for child in value.get("components", ()) or ():
                visit(child)
            component = value.get("component")
            if component is not None:
                visit(component)
        elif isinstance(value, Sequence) and not isinstance(value, str | bytes):
            for child in value:
                visit(child)

    visit(data)
    return found


class _ModuleModal(discord.ui.Modal):
    def __init__(
        self,
        spec: ModalSpec,
        module_name: str,
        dispatcher: ComponentDispatcher,
    ) -> None:
        validate_modal_spec(spec)
        super().__init__(
            title=spec.title,
            timeout=30 * 60,
            custom_id=build_custom_id(module_name, spec.key, *spec.parts),
        )
        self._dispatcher = dispatcher
        for input_spec in spec.inputs:
            self.add_item(_build_text_input(input_spec))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._dispatcher.dispatch(interaction, "modal")


def _build_text_input(spec: TextInputSpec) -> discord.ui.TextInput[Any]:
    style = discord.TextStyle.paragraph if spec.style == "paragraph" else discord.TextStyle.short
    return discord.ui.TextInput(
        label=spec.label,
        style=style,
        custom_id=spec.key,
        placeholder=spec.placeholder,
        default=spec.default,
        required=spec.required,
        min_length=spec.min_length,
        max_length=spec.max_length,
    )


class ModuleInteractionAdapter:
    """``ModuleInteraction`` over a live ``discord.Interaction``."""

    def __init__(
        self,
        interaction: discord.Interaction,
        module_name: str,
        *,
        options: Mapping[str, Any] | None = None,
        dispatcher: ComponentDispatcher | None = None,
    ) -> None:
        self._interaction = interaction
        self._module_name = module_name
        self._options = dict(options or {})
        self._dispatcher = dispatcher
        message = getattr(interaction, "message", None)
        flags = getattr(message, "flags", None)
        self._original_uses_layout = bool(
            flags is not None and getattr(flags, "components_v2", False)
        )

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
    def text_values(self) -> Mapping[str, str]:
        data = getattr(self._interaction, "data", None) or {}
        return _text_values(data)

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
        layout: OutgoingLayout | None,
        components: Sequence[Any],
        *,
        ephemeral: bool | None,
    ) -> dict[str, Any]:
        if layout is not None and (content is not None or embed is not None):
            raise ModuleContractError("layout cannot be combined with content or embed")
        kwargs: dict[str, Any] = {"allowed_mentions": discord.AllowedMentions.none()}
        if content is not None:
            kwargs["content"] = content
        if embed is not None:
            kwargs["embed"] = build_embed(embed)
        view = (
            build_layout_view(layout, components, self._module_name)
            if layout is not None
            else build_view(components, self._module_name)
        )
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
        layout: OutgoingLayout | None = None,
        ephemeral: bool = False,
        components: Sequence[Any] = (),
    ) -> None:
        kwargs = self._kwargs(content, embed, layout, components, ephemeral=ephemeral)
        if kwargs.get("view") is None:
            kwargs.pop("view", None)
        if self._interaction.response.is_done():
            await self._interaction.followup.send(**kwargs)
        else:
            await self._interaction.response.send_message(**kwargs)
            self._original_uses_layout = layout is not None

    async def defer(self, *, ephemeral: bool = False) -> None:
        if not self._interaction.response.is_done():
            await self._interaction.response.defer(ephemeral=ephemeral)

    async def show_modal(self, modal: ModalSpec) -> None:
        if self._dispatcher is None:
            raise RuntimeError("modal support requires a module interaction router")
        if self._interaction.response.is_done():
            raise ModuleContractError("a modal must be the interaction's initial response")
        await self._interaction.response.send_modal(
            _ModuleModal(modal, self._module_name, self._dispatcher)
        )

    async def edit_original(
        self,
        content: str | None = None,
        *,
        embed: OutgoingEmbed | None = None,
        layout: OutgoingLayout | None = None,
        components: Sequence[Any] = (),
    ) -> None:
        if self._original_uses_layout and layout is None:
            raise ModuleContractError(
                "a Components V2 message must continue to use layout when edited"
            )
        kwargs = self._kwargs(content, embed, layout, components, ephemeral=None)
        if layout is not None:
            # Discord requires explicit nulls when replacing a legacy message
            # body with Components V2.
            kwargs.setdefault("content", None)
            kwargs.setdefault("embed", None)
        kwargs.setdefault("view", None)
        interaction_type = getattr(self._interaction, "type", None)
        if not self._interaction.response.is_done() and interaction_type in (
            discord.InteractionType.component,
            discord.InteractionType.modal_submit,
        ):
            if getattr(self._interaction, "message", None) is not None:
                await self._interaction.response.edit_message(**kwargs)
            elif interaction_type is discord.InteractionType.modal_submit:
                raise ModuleContractError(
                    "cannot edit the original message from an unanchored modal submission"
                )
            else:
                await self._interaction.edit_original_response(**kwargs)
        else:
            await self._interaction.edit_original_response(**kwargs)
        if layout is not None:
            self._original_uses_layout = True

    async def follow_up(
        self, content: str, *, embed: OutgoingEmbed | None = None, ephemeral: bool = False
    ) -> None:
        kwargs = self._kwargs(content, embed, None, (), ephemeral=ephemeral)
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
            try:
                self.close_fn()
            finally:
                self.router._forget_registration(self)


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
    ) -> _ComponentRegistration:
        identity = (router.module_name, kind, key)
        if identity in self._handlers:
            raise ModuleContractError(
                f"module {router.module_name!r} component {kind}/{key!r} is already registered"
            )
        expires_at = self._clock() + expires_after_seconds if expires_after_seconds else None
        registration = _ComponentRegistration(router, handler, expires_at, min_tier)
        self._handlers[identity] = registration
        return registration

    def unregister(
        self,
        module_name: str,
        kind: str,
        key: str,
        registration: _ComponentRegistration,
    ) -> None:
        identity = (module_name, kind, key)
        if self._handlers.get(identity) is registration:
            self._handlers.pop(identity)

    def unregister_module(self, router: InteractionRouterImpl) -> None:
        for entry in [
            key for key, registration in self._handlers.items() if registration.router is router
        ]:
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
            await registration.handler(
                ModuleInteractionAdapter(interaction, module_name, dispatcher=self)
            )
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
    """Build the DynamicItem class bound to one dispatcher (call once per bot).

    Discord.py keys dynamic items by their compiled regex. Buttons and selects
    share our custom-id format, so registering separate classes with the same
    template makes the latter silently replace the former. One wrapper keeps a
    single template registered and chooses the dispatcher kind from the item
    Discord reconstructed for the interaction.
    """

    class ModuleComponent(
        discord.ui.DynamicItem[discord.ui.Item[Any]], template=_COMPONENT_TEMPLATE
    ):
        dispatcher: ClassVar[ComponentDispatcher] = bound

        def __init__(self, item: discord.ui.Item[Any]) -> None:
            if not isinstance(item, discord.ui.Button | discord.ui.Select):
                raise TypeError("module dynamic components must be buttons or selects")
            super().__init__(item)

        @classmethod
        async def from_custom_id(
            cls,
            interaction: discord.Interaction,
            item: discord.ui.Item[Any],
            match: re.Match[str],
            /,
        ) -> ModuleComponent:
            return cls(item)

        async def callback(self, interaction: discord.Interaction) -> None:
            kind = "button" if isinstance(self.item, discord.ui.Button) else "select"
            await self.dispatcher.dispatch(interaction, kind)

    return (ModuleComponent,)


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
        runtime: InteractionRuntime | None = None,
    ) -> None:
        self._bot = bot
        self._module_name = module_name
        self._trust = trust
        self._dispatcher = dispatcher
        self._is_guild_active = is_guild_active
        self._runtime = runtime
        self._groups: dict[str, app_commands.Group] = {}
        self._commands: list[tuple[str | None, str]] = []
        self._guild_top_names: dict[int, set[str]] = {}
        self._registrations: list[_Registration] = []
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
        qualified = f"{spec.group}.{spec.name}" if spec.group else spec.name
        if (spec.group, spec.name) in self._commands:
            raise ModuleContractError(
                f"module {self._module_name!r} command {qualified!r} is already registered"
            )
        top_name = spec.group or spec.name
        if self._runtime is not None and self._runtime.guild_command_owner(top_name) is not None:
            raise ModuleContractError(
                f"module {self._module_name!r} global command {top_name!r} "
                "would shadow a guild command"
            )
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

        registration = _Registration(self, close)
        self._registrations.append(registration)
        return registration

    async def replace_guild_commands(
        self,
        guild_id: int,
        commands: Sequence[GuildCommand],
    ) -> None:
        if self._runtime is None:
            raise RuntimeError("guild command replacement requires an interaction runtime")
        await self._runtime.replace_guild_commands(self, guild_id, commands)

    def _replace_guild_commands_local(
        self,
        guild_id: int,
        commands: Sequence[GuildCommand],
    ) -> None:
        if self._closed:
            raise RuntimeError(f"module {self._module_name!r} interactions are closed")
        if guild_id <= 0:
            raise ModuleContractError("guild_id must be a positive Discord snowflake")
        if commands and not self._is_guild_active(guild_id):
            raise ModuleContractError(
                f"module {self._module_name!r} is not active in guild {guild_id}"
            )

        guild = discord.Object(id=guild_id)
        old_top_names = self._guild_top_names.get(guild_id, set())
        prepared: dict[str, app_commands.Command[Any, ..., None] | app_commands.Group] = {}
        qualified_names: set[str] = set()
        for binding in commands:
            spec = binding.spec
            qualified = f"{spec.group}.{spec.name}" if spec.group else spec.name
            if qualified in qualified_names:
                raise ModuleContractError(
                    f"module {self._module_name!r} guild command {qualified!r} "
                    "is registered more than once"
                )
            qualified_names.add(qualified)
            top_name = spec.group or spec.name
            if self._bot.tree.get_command(top_name) is not None:
                raise ModuleContractError(
                    f"module {self._module_name!r} guild command {top_name!r} "
                    "would shadow a global command"
                )
            existing = self._bot.tree.get_command(top_name, guild=guild)
            if existing is not None and top_name not in old_top_names:
                raise ModuleContractError(
                    f"module {self._module_name!r} guild command {top_name!r} is already owned"
                )

            command = self._build_command(spec, binding.handler, binding.autocomplete)
            if spec.group:
                top = prepared.get(spec.group)
                if top is None:
                    top = app_commands.Group(
                        name=spec.group,
                        description=spec.group_description or spec.group,
                    )
                    prepared[spec.group] = top
                if not isinstance(top, app_commands.Group):
                    raise ModuleContractError(
                        f"module {self._module_name!r} guild command {spec.group!r} "
                        "is both a command and a group"
                    )
                top.add_command(command)
            else:
                if spec.name in prepared:
                    raise ModuleContractError(
                        f"module {self._module_name!r} guild command {spec.name!r} "
                        "is both a command and a group"
                    )
                prepared[spec.name] = command

        existing_chat_commands = self._bot.tree.get_commands(
            guild=guild, type=discord.AppCommandType.chat_input
        )
        other_command_count = sum(
            command.name not in old_top_names for command in existing_chat_commands
        )
        projected_count = other_command_count + len(prepared)
        if projected_count > 100:
            raise ModuleContractError(
                f"guild {guild_id} would have {projected_count} slash commands; "
                "Discord allows at most 100"
            )

        old_commands = {
            name: existing_command
            for name in old_top_names
            if (existing_command := self._bot.tree.get_command(name, guild=guild)) is not None
        }
        for name in old_top_names:
            self._bot.tree.remove_command(name, guild=guild)
        try:
            for top_command in prepared.values():
                self._bot.tree.add_command(top_command, guild=guild)
        except app_commands.CommandLimitReached as exc:
            for name in prepared:
                self._bot.tree.remove_command(name, guild=guild)
            for old_command in old_commands.values():
                self._bot.tree.add_command(old_command, guild=guild)
            raise ModuleContractError(
                f"guild {guild_id} slash commands exceed Discord's limit of 100"
            ) from exc
        if prepared:
            self._guild_top_names[guild_id] = set(prepared)
        else:
            self._guild_top_names.pop(guild_id, None)

    def _forget_registration(self, registration: _Registration) -> None:
        if registration in self._registrations:
            self._registrations.remove(registration)

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
            adapter = ModuleInteractionAdapter(
                interaction, module_name, options=options, dispatcher=self._dispatcher
            )
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
                    ModuleInteractionAdapter(interaction, module_name, dispatcher=self._dispatcher),
                    option_name,
                    current,
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
        if self._closed:
            raise RuntimeError(f"module {self._module_name!r} interactions are closed")
        if kind not in ("button", "select", "modal"):
            raise ModuleContractError(f"unsupported component kind {kind!r}")
        build_custom_id(self._module_name, key)  # validates the key
        component = self._dispatcher.register(
            self, kind, key, handler, expires_after_seconds, min_tier
        )
        registration = _Registration(
            self,
            lambda: self._dispatcher.unregister(self._module_name, kind, key, component),
        )
        self._registrations.append(registration)
        return registration

    def custom_id(self, key: str, *parts: str) -> str:
        return build_custom_id(self._module_name, key, *parts)

    # ---- ownership -----------------------------------------------------------

    def owned_commands(self) -> tuple[str, ...]:
        return tuple(f"{g}.{n}" if g else n for g, n in self._commands)

    def guild_command_ids(self) -> tuple[int, ...]:
        return tuple(self._guild_top_names)

    def has_guild_commands(self, guild_id: int) -> bool:
        return bool(self._guild_top_names.get(guild_id))

    def owns_guild_top_name(self, top_name: str) -> bool:
        return any(top_name in names for names in self._guild_top_names.values())

    def close(self) -> None:
        self._closed = True
        for registration in list(self._registrations):
            registration.close()
        for guild_id, names in list(self._guild_top_names.items()):
            guild = discord.Object(id=guild_id)
            for name in names:
                self._bot.tree.remove_command(name, guild=guild)
        self._guild_top_names.clear()
        self._dispatcher.unregister_module(self)
        if self._runtime is not None:
            self._runtime.unregister_router(self)


@dataclass(slots=True)
class InteractionRuntime:
    """Process-wide interaction state: the dispatcher and its dynamic items."""

    bot: commands.Bot
    is_available: InteractionAvailability = _always_available
    scope_store: GuildCommandScopeStore | None = None
    dispatcher: ComponentDispatcher = field(init=False)
    installed: bool = False
    _live: bool = False
    _sync_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _routers: list[InteractionRouterImpl] = field(default_factory=list, init=False)

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
        router = InteractionRouterImpl(
            bot=self.bot,
            module_name=module_name,
            trust=trust,
            dispatcher=self.dispatcher,
            is_guild_active=is_guild_active,
            runtime=self,
        )
        self._routers.append(router)
        return router

    def unregister_router(self, router: InteractionRouterImpl) -> None:
        if router in self._routers:
            self._routers.remove(router)

    def guild_command_owner(self, top_name: str) -> str | None:
        for router in self._routers:
            if router.owns_guild_top_name(top_name):
                return router.module_name
        return None

    async def replace_guild_commands(
        self,
        router: InteractionRouterImpl,
        guild_id: int,
        commands: Sequence[GuildCommand],
    ) -> None:
        async with self._sync_lock:
            router._replace_guild_commands_local(guild_id, commands)
            await self._track(guild_id)
            if not self._live:
                return
            error = await self._sync_one(guild_id)
            if error is not None:
                raise CommandSyncError(
                    f"could not synchronize guild commands for guild {guild_id}"
                ) from error

    async def sync_ready(self) -> None:
        """Publish staged commands and retry every persisted guild scope."""
        async with self._sync_lock:
            self._live = True
            tracked = set(await self._tracked_guild_ids())
            local = {
                guild_id for router in self._routers for guild_id in router.guild_command_ids()
            }
            connected = {int(guild.id) for guild in self.bot.guilds}
            for guild_id in tracked - connected:
                await self._forget(guild_id)
            for guild_id in sorted((tracked | local) & connected):
                await self._sync_one(guild_id)

    async def _sync_one(self, guild_id: int) -> Exception | None:
        has_commands = any(router.has_guild_commands(guild_id) for router in self._routers)
        try:
            synced = await self.bot.tree.sync(guild=discord.Object(id=guild_id))
        except Exception as exc:
            log.warning("Failed to sync slash commands for guild %s", guild_id, exc_info=True)
            return exc

        if not has_commands:
            await self._forget(guild_id)
        log.info("Synced %d guild slash command(s) for guild %s", len(synced), guild_id)
        return None

    async def _track(self, guild_id: int) -> None:
        if self.scope_store is not None:
            await self.scope_store.track(guild_id)

    async def _tracked_guild_ids(self) -> tuple[int, ...]:
        if self.scope_store is None:
            return ()
        return await self.scope_store.guild_ids()

    async def _forget(self, guild_id: int) -> None:
        if self.scope_store is not None:
            await self.scope_store.forget(guild_id)


__all__ = [
    "ComponentDispatcher",
    "InteractionRouterImpl",
    "InteractionRuntime",
    "ModuleInteractionAdapter",
    "build_embed",
    "build_layout_view",
    "build_view",
    "make_dynamic_items",
]
