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
import itertools
import logging
import re
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal, Protocol

import discord
from discord import app_commands
from discord.ext import commands

from kimi_agent_module_api.contracts import (
    MessageRef,
    CUSTOM_ID_PREFIX,
    MODAL_CUSTOM_ID_MAX_LENGTH,
    MODAL_NONCE_CHARS,
    AutocompleteHandler,
    ButtonSpec,
    CommandHandler,
    CommandSpec,
    CommandSyncError,
    GuildCommand,
    HealthState,
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
    validate_command_spec,
    validate_component_spec,
    validate_modal_spec,
    validate_layout_components,
    validate_outgoing_layout,
)

log = logging.getLogger(__name__)

_TIER_ORDER: dict[TrustTierName, int] = {"member": 0, "regular": 1, "staff": 2}
type InteractionAvailability = Callable[[], bool]
type GuildCommandSyncHealth = Callable[[str, HealthState, str], None]
type _GuildTopCommand = app_commands.Command[Any, ..., None] | app_commands.Group
type _GuildSyncPhase = Literal["track", "publish", "forget"]


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
_VIEW_CACHE_TIMEOUT_SECONDS = 180.0
INTERACTION_DRAIN_SECONDS = 5.0
INTERACTION_CANCEL_GRACE_SECONDS = 1.0
COMMAND_SYNC_CANCEL_GRACE_SECONDS = 1.0
GUILD_COMMAND_SYNC_RETRY_DELAYS = (1.0, 5.0, 30.0)


def _consume_task_result(task: asyncio.Task[Any]) -> None:
    """Retrieve a detached task's outcome so it cannot warn at collection."""

    with suppress(BaseException):
        task.result()


async def _cancel_tasks_bounded(
    tasks: set[asyncio.Task[Any]],
    *,
    timeout: float,
    what: str,
) -> set[asyncio.Task[Any]]:
    """Cancel tasks, but never let cancellation-resistant work block teardown."""

    current = asyncio.current_task()
    pending = {task for task in tasks if task is not current and not task.done()}
    for task in pending:
        task.cancel()
    if not pending:
        return set()
    done, still_running = await asyncio.wait(pending, timeout=max(0.0, timeout))
    for task in done:
        _consume_task_result(task)
    for task in still_running:
        task.add_done_callback(_consume_task_result)
    if still_running:
        log.error(
            "Timed out cancelling %d %s task(s); continuing bounded shutdown",
            len(still_running),
            what,
        )
    return still_running


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
    validate_component_spec(component)
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
    """Turn ``ButtonSpec``/``SelectSpec`` values into a Discord view.

    The process-wide dynamic item supplies persistence. A finite local timeout
    lets discord.py release its per-message ViewStore entry after sending.
    """
    if not components:
        return None
    view = discord.ui.View(timeout=_VIEW_CACHE_TIMEOUT_SECONDS)
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

    view = discord.ui.LayoutView(timeout=_VIEW_CACHE_TIMEOUT_SECONDS)
    # Discord.py drops a falsy accent, so a raw 0 would silently lose a black
    # accent the contract explicitly permits. A Color instance is always truthy.
    accent = None if layout.accent_color is None else discord.Color(layout.accent_color)
    view.add_item(discord.ui.Container(*children, accent_color=accent))

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


# Discord.py keys open modals by custom_id alone, so two people opening the same
# module action would share one entry: the second open evicts the first, and
# whichever submits first removes the survivor. A per-open suffix keeps them
# apart. A counter, not randomness: uniqueness within the process is exactly
# what is needed, and the ID is not a secret. The table now holds one entry per
# open until it is submitted or times out, rather than one per module action.
_modal_opens = itertools.count()
# Masked so the suffix is always MODAL_NONCE_CHARS wide. The budget a module gets
# must not depend on how long the host has been running, and wrapping needs 2**32
# opens inside one modal's 30-minute lifetime to collide.
_MODAL_NONCE_MASK = (1 << (4 * MODAL_NONCE_CHARS)) - 1


def _mint_modal_custom_id(module_name: str, spec: ModalSpec) -> str:
    """Build a modal ID, reserving the fixed-width per-open suffix."""

    declared = build_custom_id(module_name, spec.key, *spec.parts)
    if len(declared) > MODAL_CUSTOM_ID_MAX_LENGTH:
        raise ModuleContractError(
            f"modal custom_id exceeds {MODAL_CUSTOM_ID_MAX_LENGTH} characters; "
            f"a modal reserves {MODAL_NONCE_CHARS} more for its per-open suffix"
        )
    return f"{declared}:{format(next(_modal_opens) & _MODAL_NONCE_MASK, f'0{MODAL_NONCE_CHARS}x')}"


def _strip_modal_nonce(custom_id: str) -> str:
    """Return the ID the module described, without the per-open suffix.

    An ID we did not mint has no suffix to remove and is passed through.
    """

    parsed = parse_custom_id(custom_id)
    if parsed is None:
        return custom_id
    module_name, key, parts = parsed
    if not parts:
        return custom_id
    return build_custom_id(module_name, key, *parts[:-1])


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
            custom_id=_mint_modal_custom_id(module_name, spec),
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
        if not value:
            return None
        if getattr(self._interaction, "type", None) is discord.InteractionType.modal_submit:
            # The per-open suffix is ours; the module gets back the ID it described.
            return _strip_modal_nonce(str(value))
        return str(value)

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


@dataclass(frozen=True, slots=True)
class _GuildCommandState:
    top_names: frozenset[str]
    commands: tuple[_GuildTopCommand, ...]


@dataclass(frozen=True, slots=True)
class _GuildSyncOutcome:
    error: Exception | None = None
    published: bool = False
    phase: _GuildSyncPhase = "publish"


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
        self._in_flight: set[asyncio.Task[Any]] = set()
        self._admitting = True

    @property
    def admitting(self) -> bool:
        """Whether a new interaction may still enter a module handler."""

        return self._admitting

    def stop_admitting(self) -> None:
        self._admitting = False

    @contextmanager
    def track_in_flight(self) -> Iterator[None]:
        """Register the running handler so shutdown can wait for it.

        Discord.py dispatches interactions in tasks it owns and never cancels,
        so without this the application would close its modules and database
        underneath a handler that is still running.
        """

        task = asyncio.current_task()
        if task is None:
            yield
            return
        self._in_flight.add(task)
        try:
            yield
        finally:
            self._in_flight.discard(task)

    @contextmanager
    def admit_in_flight(self) -> Iterator[bool]:
        """Atomically admit and track one interaction task.

        There is deliberately no await between checking ``_admitting`` and
        adding the current task to ``_in_flight``. Once shutdown closes the
        admission gate, it can therefore account for every task that may still
        await a trust lookup or enter module code.
        """

        if not self._admitting:
            yield False
            return
        with self.track_in_flight():
            yield True

    async def drain(
        self,
        timeout: float = INTERACTION_DRAIN_SECONDS,
        *,
        cancel_timeout: float = INTERACTION_CANCEL_GRACE_SECONDS,
    ) -> None:
        """Let admitted handlers finish, then cancel whatever outlasts the bound."""

        pending = {task for task in self._in_flight if task is not asyncio.current_task()}
        if not pending:
            return
        done, running = await asyncio.wait(pending, timeout=timeout)
        for task in done:
            with suppress(BaseException):
                task.result()
        for task in running:
            task.cancel()
        if running:
            log.warning("Timed out draining %d module interaction handler(s)", len(running))
            # Cancellation is cooperative. Give finally blocks a second bounded
            # window, then detach a task that refuses cancellation rather than
            # holding module/database teardown forever.
            await _cancel_tasks_bounded(
                set(running),
                timeout=cancel_timeout,
                what="module interaction handler",
            )

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
        try:
            with self.admit_in_flight() as admitted:
                if not admitted:
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
                if not self._admitting:
                    await _quiet_reply(interaction, "This control is temporarily unavailable.")
                    return True
                if self._handlers.get((module_name, kind, key)) is not registration:
                    await _quiet_reply(interaction, "This control is no longer active.")
                    return True
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
        validate_command_spec(spec)
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
    ) -> _GuildCommandState:
        if self._closed:
            raise RuntimeError(f"module {self._module_name!r} interactions are closed")
        if guild_id <= 0:
            raise ModuleContractError("guild_id must be a positive Discord snowflake")
        if commands and not self._is_guild_active(guild_id):
            raise ModuleContractError(
                f"module {self._module_name!r} is not active in guild {guild_id}"
            )

        guild = discord.Object(id=guild_id)
        old_top_names = set(self._guild_top_names.get(guild_id, set()))
        prepared: dict[str, _GuildTopCommand] = {}
        qualified_names: set[str] = set()
        # Validate the whole desired set before touching the tree: one malformed
        # command would otherwise reject this guild's entire bulk sync.
        for binding in commands:
            validate_command_spec(binding.spec)
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
            command.name: command
            for command in existing_chat_commands
            if command.name in old_top_names
        }
        previous = _GuildCommandState(
            top_names=frozenset(old_top_names),
            commands=tuple(old_commands.values()),
        )
        for name in old_top_names:
            self._bot.tree.remove_command(name, guild=guild)
        try:
            for top_command in prepared.values():
                self._bot.tree.add_command(top_command, guild=guild)
        except Exception as exc:
            try:
                self._restore_guild_commands_local(guild_id, previous, set(prepared))
            except Exception as rollback_error:
                raise ExceptionGroup(
                    f"failed to stage and restore guild {guild_id} commands",
                    [exc, rollback_error],
                ) from None
            if isinstance(exc, app_commands.CommandLimitReached):
                raise ModuleContractError(
                    f"guild {guild_id} slash commands exceed Discord's limit of 100"
                ) from exc
            raise
        if prepared:
            self._guild_top_names[guild_id] = set(prepared)
        else:
            self._guild_top_names.pop(guild_id, None)
        return previous

    def _restore_guild_commands_local(
        self,
        guild_id: int,
        state: _GuildCommandState,
        remove_names: set[str] | frozenset[str],
    ) -> None:
        """Restore an opaque local command snapshot after staging/publication fails."""

        guild = discord.Object(id=guild_id)
        for name in set(remove_names) | set(self._guild_top_names.get(guild_id, set())):
            self._bot.tree.remove_command(name, guild=guild)

        restored_names: set[str] = set()
        try:
            for command in state.commands:
                self._bot.tree.add_command(command, guild=guild)
                restored_names.add(command.name)
        except Exception:
            if restored_names:
                self._guild_top_names[guild_id] = restored_names
            else:
                self._guild_top_names.pop(guild_id, None)
            raise

        if state.top_names:
            self._guild_top_names[guild_id] = set(state.top_names)
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
        if autocomplete is None and any(option.autocomplete for option in spec.options):
            # Otherwise Discord would offer a suggestion box nothing answers.
            raise ModuleContractError(
                f"module {module_name!r} command {spec.name!r} declares an autocomplete "
                "option but supplied no autocomplete handler"
            )

        async def callback(interaction: discord.Interaction, **kwargs: Any) -> None:
            options = {name: _option_value(kwargs.get(name)) for name in option_names}
            adapter = ModuleInteractionAdapter(
                interaction, module_name, options=options, dispatcher=self._dispatcher
            )
            try:
                with self._dispatcher.admit_in_flight() as admitted:
                    if not admitted:
                        await _quiet_reply(interaction, "This command is temporarily unavailable.")
                        return
                    if not await self._allowed(interaction, spec.min_tier):
                        await adapter.respond(
                            "Staff only." if spec.min_tier == "staff" else "Not allowed.",
                            ephemeral=True,
                        )
                        return
                    if not self._dispatcher.admitting:
                        await _quiet_reply(interaction, "This command is temporarily unavailable.")
                        return
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
            try:
                with self._dispatcher.admit_in_flight() as admitted:
                    if not admitted:
                        return []
                    if not await self._allowed(interaction, min_tier):
                        return []
                    if not self._dispatcher.admitting:
                        return []
                    results = await handler(
                        ModuleInteractionAdapter(
                            interaction, module_name, dispatcher=self._dispatcher
                        ),
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
        if self._closed or guild_id == 0 or not self._is_guild_active(guild_id):
            return False
        tier = await self._trust.tier(guild_id, int(interaction.user.id))
        return (
            not self._closed
            and self._is_guild_active(guild_id)
            and _TIER_ORDER[tier] >= _TIER_ORDER[min_tier]
        )

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
    on_sync_health: GuildCommandSyncHealth | None = None
    sync_retry_delays: tuple[float, ...] = GUILD_COMMAND_SYNC_RETRY_DELAYS
    dispatcher: ComponentDispatcher = field(init=False)
    installed: bool = False
    _live: bool = False
    _closed: bool = False
    _sync_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _routers: list[InteractionRouterImpl] = field(default_factory=list, init=False)
    _sync_failures: dict[int, str] = field(default_factory=dict, init=False)
    _sync_failure_phases: dict[int, _GuildSyncPhase] = field(default_factory=dict, init=False)
    _sync_failure_modules: dict[int, frozenset[str]] = field(default_factory=dict, init=False)
    _sync_retry_attempts: dict[int, int] = field(default_factory=dict, init=False)
    _sync_exhausted: set[int] = field(default_factory=set, init=False)
    _sync_retry_tasks: dict[int, asyncio.Task[None]] = field(default_factory=dict, init=False)
    _retired_sync_retry_tasks: set[asyncio.Task[None]] = field(default_factory=set, init=False)
    _sync_operations: set[asyncio.Task[Any]] = field(default_factory=set, init=False)
    _scope_discovery_failure: str | None = field(default=None, init=False)
    _scope_discovery_failure_modules: frozenset[str] = field(default_factory=frozenset, init=False)
    _scope_discovery_exhausted: bool = field(default=False, init=False)
    _scope_discovery_retry_task: asyncio.Task[None] | None = field(default=None, init=False)
    _reported_sync_health: dict[str, tuple[HealthState, str]] = field(
        default_factory=dict, init=False
    )

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
        for guild_id in self._sync_failures:
            self._sync_failure_modules[guild_id] = self._modules_for_guild(guild_id)
        self._publish_sync_health()

    @contextmanager
    def _track_sync_operation(self) -> Iterator[None]:
        task = asyncio.current_task()
        if task is None:
            yield
            return
        self._sync_operations.add(task)
        try:
            yield
        finally:
            self._sync_operations.discard(task)

    async def drain(
        self,
        *,
        interaction_timeout: float = INTERACTION_DRAIN_SECONDS,
        cancel_timeout: float = INTERACTION_CANCEL_GRACE_SECONDS,
        sync_cancel_timeout: float = COMMAND_SYNC_CANCEL_GRACE_SECONDS,
    ) -> None:
        """Stop admitting interactions and wait out the handlers already running.

        The caller runs this while the Discord HTTP session is still open, so a
        handler can finish its reply before its module and the database close.
        """

        # These assignments contain no await, so a waiter that has not entered
        # the sync lock yet observes closure before it can mutate the tree.
        self.dispatcher.stop_admitting()
        self._closed = True
        self._live = False
        await self._cancel_sync_retries()
        # A handler that calls replace_guild_commands is both an in-flight
        # interaction and a sync operation. Drain first so it keeps the full
        # interaction window; cancelling sync work up front would cut it off
        # after the shorter sync grace and leave the interaction unanswered.
        await self.dispatcher.drain(
            timeout=interaction_timeout,
            cancel_timeout=cancel_timeout,
        )
        await _cancel_tasks_bounded(
            set(self._sync_operations),
            timeout=sync_cancel_timeout,
            what="guild command synchronization",
        )

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
        with self._track_sync_operation():
            await self._replace_guild_commands(router, guild_id, commands)

    async def _replace_guild_commands(
        self,
        router: InteractionRouterImpl,
        guild_id: int,
        commands: Sequence[GuildCommand],
    ) -> None:
        async with self._sync_lock:
            if self._closed:
                raise RuntimeError("module interactions are shutting down")
            prior_failure_phase = self._sync_failure_phases.get(guild_id)
            try:
                previous = router._replace_guild_commands_local(guild_id, commands)
            except ExceptionGroup as exc:
                self._record_sync_failure(
                    guild_id,
                    exc,
                    {router.module_name},
                    reset_attempts=True,
                )
                self._sync_exhausted.add(guild_id)
                self._publish_sync_health()
                raise
            staged_names = frozenset(router._guild_top_names.get(guild_id, set()))
            if not self._live:
                try:
                    await self._track(guild_id)
                except Exception as exc:
                    router._restore_guild_commands_local(guild_id, previous, staged_names)
                    self._record_sync_failure(
                        guild_id,
                        exc,
                        {router.module_name},
                        phase=prior_failure_phase or "track",
                        reset_attempts=True,
                    )
                    raise CommandSyncError(
                        f"could not persist guild command scope for guild {guild_id}"
                    ) from exc
                return

            outcome = await self._sync_one(guild_id)
            if self._closed:
                return
            if outcome.error is None:
                self._clear_sync_failure(guild_id)
                return
            if outcome.published:
                # Discord accepted the desired set. A subsequent persistence
                # cleanup failed, so rolling back handlers would itself create
                # the remote/local mismatch this transaction prevents.
                self._record_sync_failure(
                    guild_id,
                    outcome.error,
                    {router.module_name},
                    phase=outcome.phase,
                    reset_attempts=True,
                )
                self._schedule_sync_retry(guild_id, restart=True)
                return

            try:
                router._restore_guild_commands_local(guild_id, previous, staged_names)
            except Exception as rollback_error:
                combined = ExceptionGroup(
                    f"guild {guild_id} publication and local rollback both failed",
                    [outcome.error, rollback_error],
                )
                self._record_sync_failure(
                    guild_id,
                    combined,
                    {router.module_name},
                    reset_attempts=True,
                )
                # The tree is now partial/unknown. Automatically publishing it
                # could destroy the last coherent remote scope, so require an
                # explicit replacement/READY generation to repair it.
                self._sync_exhausted.add(guild_id)
                self._publish_sync_health()
                raise CommandSyncError(
                    f"guild command publication and local rollback failed for guild {guild_id}"
                ) from combined

            if outcome.phase == "track":
                # Discord was never touched, so the restored tree already
                # matches remote state. Retry only the failed persistence marker.
                self._record_sync_failure(
                    guild_id,
                    outcome.error,
                    {router.module_name},
                    phase=prior_failure_phase or "track",
                    reset_attempts=True,
                )
                self._schedule_sync_retry(guild_id, restart=True)
                raise CommandSyncError(
                    f"could not persist guild command scope for guild {guild_id}"
                ) from outcome.error

            # A transport exception is ambiguous: Discord may have accepted the
            # PUT even though the client never received its response. Republish
            # the restored tree once so old handlers and the remote schema agree.
            compensation = await self._sync_one(guild_id)
            if compensation.error is None:
                self._clear_sync_failure(guild_id)
            else:
                self._record_sync_failure(
                    guild_id,
                    compensation.error,
                    {router.module_name},
                    # If compensation could not even persist its marker, the
                    # original PUT is still transport-ambiguous: a later retry
                    # must perform the restored PUT, not stop after tracking.
                    phase="forget" if compensation.phase == "forget" else "publish",
                    reset_attempts=True,
                )
                self._schedule_sync_retry(guild_id, restart=True)
            raise CommandSyncError(
                f"could not synchronize guild commands for guild {guild_id}"
            ) from outcome.error

    async def sync_ready(
        self,
        *,
        is_current: Callable[[], bool] = _always_available,
    ) -> None:
        """Publish staged commands and retry every persisted guild scope."""

        with self._track_sync_operation():
            async with self._sync_lock:
                # The generation predicate is evaluated while holding the same
                # lock as pause_sync. If a disconnect won first, stale READY work
                # cannot reopen publication; if sync won, pause runs last.
                if self._closed or not is_current():
                    return
                self._live = True
                local = {
                    guild_id for router in self._routers for guild_id in router.guild_command_ids()
                }
                connected = {int(guild.id) for guild in self.bot.guilds}
                try:
                    tracked = set(await self._tracked_guild_ids())
                except Exception as exc:
                    log.warning("Failed to discover persisted guild command scopes", exc_info=True)
                    self._record_scope_discovery_failure(exc, reset_attempts=True)
                    self._schedule_scope_discovery_retry(restart=True)
                    # Scope discovery and local publication are independent. A
                    # read-side database failure must not strand commands whose
                    # guild IDs are already known from the local tree.
                    tracked = set()
                else:
                    self._clear_scope_discovery_failure()
                await self._reconcile_scopes_locked(tracked, local, connected)

    async def _reconcile_scopes_locked(
        self,
        tracked: set[int],
        local: set[int],
        connected: set[int],
    ) -> None:
        for guild_id in sorted(tracked - connected):
            try:
                await self._forget(guild_id)
            except Exception as exc:
                log.warning(
                    "Failed to forget disconnected guild command scope %s",
                    guild_id,
                    exc_info=True,
                )
                self._record_sync_failure(
                    guild_id,
                    exc,
                    phase="forget",
                    reset_attempts=True,
                )
                self._schedule_sync_retry(guild_id, restart=True)
            else:
                self._clear_sync_failure(guild_id)

        for guild_id in sorted((tracked | local | set(self._sync_failures)) & connected):
            outcome = await self._sync_one(guild_id)
            if self._closed:
                return
            if outcome.error is None:
                self._clear_sync_failure(guild_id)
            else:
                self._record_sync_failure(
                    guild_id,
                    outcome.error,
                    # A track failure happened before the desired startup PUT.
                    # Retrying only the marker would report healthy while leaving
                    # the command tree unpublished.
                    phase="forget" if outcome.phase == "forget" else "publish",
                    reset_attempts=True,
                )
                self._schedule_sync_retry(guild_id, restart=True)

    async def pause_sync(self) -> None:
        """Pause publication retries while the gateway is disconnected."""

        # A generation-guarded sync that has not entered the lock will refuse
        # itself; one already inside set _live before this synchronous write, so
        # this write wins without waiting behind Discord or database I/O.
        self._live = False
        await self._cancel_sync_retries()

    async def resume_sync(
        self,
        *,
        is_current: Callable[[], bool] = _always_available,
    ) -> None:
        """Publish work staged during a disconnect and rearm failed scopes."""

        if self._closed:
            return
        await self.sync_ready(is_current=is_current)

    async def _sync_one(self, guild_id: int) -> _GuildSyncOutcome:
        has_commands = any(router.has_guild_commands(guild_id) for router in self._routers)
        try:
            # Track before every PUT, including an empty cleanup. A failed or
            # transport-ambiguous publication must remain discoverable after a
            # restart; a successful empty publication forgets it below.
            await self._track(guild_id)
        except Exception as exc:
            log.warning(
                "Failed to persist slash-command scope for guild %s", guild_id, exc_info=True
            )
            return _GuildSyncOutcome(error=exc, phase="track")
        try:
            synced = await self.bot.tree.sync(guild=discord.Object(id=guild_id))
        except Exception as exc:
            log.warning("Failed to sync slash commands for guild %s", guild_id, exc_info=True)
            return _GuildSyncOutcome(error=exc)

        if not has_commands:
            try:
                await self._forget(guild_id)
            except Exception as exc:
                log.warning(
                    "Published empty slash-command scope for guild %s but could not "
                    "forget its cleanup marker",
                    guild_id,
                    exc_info=True,
                )
                return _GuildSyncOutcome(error=exc, published=True, phase="forget")
        log.info("Synced %d guild slash command(s) for guild %s", len(synced), guild_id)
        return _GuildSyncOutcome(published=True)

    def _modules_for_guild(self, guild_id: int) -> frozenset[str]:
        return frozenset(
            router.module_name for router in self._routers if router.has_guild_commands(guild_id)
        )

    def _modules_with_guild_commands(self) -> frozenset[str]:
        return frozenset(
            router.module_name for router in self._routers if router.guild_command_ids()
        )

    def _record_scope_discovery_failure(
        self,
        error: Exception,
        *,
        reset_attempts: bool = False,
    ) -> None:
        self._scope_discovery_failure = type(error).__name__
        self._scope_discovery_failure_modules = (
            self._scope_discovery_failure_modules | self._modules_with_guild_commands()
        )
        if reset_attempts:
            self._scope_discovery_exhausted = False
        self._publish_sync_health()

    def _clear_scope_discovery_failure(self) -> None:
        self._scope_discovery_failure = None
        self._scope_discovery_failure_modules = frozenset()
        self._scope_discovery_exhausted = False
        task = self._scope_discovery_retry_task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            self._retired_sync_retry_tasks.add(task)
        self._publish_sync_health()

    def _record_sync_failure(
        self,
        guild_id: int,
        error: Exception,
        modules: set[str] | frozenset[str] | None = None,
        *,
        phase: _GuildSyncPhase = "publish",
        reset_attempts: bool = False,
    ) -> None:
        self._sync_failures[guild_id] = type(error).__name__
        self._sync_failure_phases[guild_id] = phase
        if reset_attempts:
            self._sync_retry_attempts.pop(guild_id, None)
            self._sync_exhausted.discard(guild_id)
        affected = set(self._sync_failure_modules.get(guild_id, frozenset()))
        affected.update(self._modules_for_guild(guild_id))
        if modules is not None:
            affected.update(modules)
        self._sync_failure_modules[guild_id] = frozenset(affected)
        self._publish_sync_health()

    def _clear_sync_failure(self, guild_id: int) -> None:
        self._sync_failures.pop(guild_id, None)
        self._sync_failure_phases.pop(guild_id, None)
        self._sync_failure_modules.pop(guild_id, None)
        self._sync_retry_attempts.pop(guild_id, None)
        self._sync_exhausted.discard(guild_id)
        task = self._sync_retry_tasks.get(guild_id)
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
        self._publish_sync_health()

    def _publish_sync_health(self) -> None:
        if self.on_sync_health is None:
            return
        failed_guilds_by_module: dict[str, list[int]] = {}
        for guild_id, modules in self._sync_failure_modules.items():
            for module_name in modules:
                failed_guilds_by_module.setdefault(module_name, []).append(guild_id)

        affected_modules = (
            set(self._reported_sync_health)
            | set(failed_guilds_by_module)
            | set(self._scope_discovery_failure_modules)
        )
        for module_name in affected_modules:
            guild_ids = sorted(failed_guilds_by_module.get(module_name, []))
            exhausted = [guild_id for guild_id in guild_ids if guild_id in self._sync_exhausted]
            discovery_pending = module_name in self._scope_discovery_failure_modules
            if exhausted or (discovery_pending and self._scope_discovery_exhausted):
                state: HealthState = "failed"
                details: list[str] = []
                if exhausted:
                    details.append(
                        "Discord guild command sync retries exhausted for guild(s) "
                        + ", ".join(str(guild_id) for guild_id in exhausted)
                    )
                if discovery_pending and self._scope_discovery_exhausted:
                    details.append("Discord guild command scope discovery retries exhausted")
                detail = "; ".join(details)
            elif guild_ids or discovery_pending:
                state = "degraded"
                details = []
                if guild_ids:
                    details.append(
                        "Discord guild command sync pending for guild(s) "
                        + ", ".join(str(guild_id) for guild_id in guild_ids)
                    )
                if discovery_pending:
                    details.append("Discord guild command scope discovery pending")
                detail = "; ".join(details)
            else:
                state = "healthy"
                detail = ""
            reported = (state, detail)
            if self._reported_sync_health.get(module_name) == reported:
                continue
            try:
                self.on_sync_health(module_name, state, detail)
            except Exception:
                log.exception("Guild command sync health observer failed for %s", module_name)
                continue
            if detail:
                self._reported_sync_health[module_name] = reported
            else:
                self._reported_sync_health.pop(module_name, None)

    def _schedule_sync_retry(self, guild_id: int, *, restart: bool = False) -> None:
        if self._closed or not self._live:
            return
        if not self.sync_retry_delays:
            self._sync_exhausted.add(guild_id)
            self._publish_sync_health()
            return
        existing = self._sync_retry_tasks.get(guild_id)
        if existing is not None and not existing.done():
            if not restart:
                return
            existing.cancel()
            self._retired_sync_retry_tasks.add(existing)
        task = asyncio.create_task(
            self._retry_guild_sync(guild_id),
            name=f"module-guild-command-sync-{guild_id}",
        )
        self._sync_retry_tasks[guild_id] = task

        def done(completed: asyncio.Task[None]) -> None:
            self._sync_retry_done(guild_id, completed)

        task.add_done_callback(done)

    def _schedule_scope_discovery_retry(self, *, restart: bool = False) -> None:
        if self._closed or not self._live:
            return
        if not self.sync_retry_delays:
            self._scope_discovery_exhausted = True
            self._publish_sync_health()
            return
        existing = self._scope_discovery_retry_task
        if existing is not None and not existing.done():
            if not restart:
                return
            existing.cancel()
            self._retired_sync_retry_tasks.add(existing)
        task = asyncio.create_task(
            self._retry_scope_discovery(),
            name="module-guild-command-scope-discovery",
        )
        self._scope_discovery_retry_task = task
        task.add_done_callback(self._scope_discovery_retry_done)

    async def _retry_scope_discovery(self) -> None:
        for delay in self.sync_retry_delays:
            await asyncio.sleep(max(0.0, delay))
            async with self._sync_lock:
                if self._closed or not self._live or self._scope_discovery_failure is None:
                    return
                try:
                    tracked = set(await self._tracked_guild_ids())
                except Exception as exc:
                    self._record_scope_discovery_failure(exc)
                    continue
                if self._closed or not self._live:
                    return
                local = {
                    guild_id for router in self._routers for guild_id in router.guild_command_ids()
                }
                connected = {int(guild.id) for guild in self.bot.guilds}
                self._clear_scope_discovery_failure()
                await self._reconcile_scopes_locked(tracked, local, connected)
                return
        if self._scope_discovery_failure is not None:
            self._scope_discovery_exhausted = True
            self._publish_sync_health()
            log.error(
                "Exhausted %d automatic guild-command scope discovery retries",
                len(self.sync_retry_delays),
            )

    def _scope_discovery_retry_done(self, task: asyncio.Task[None]) -> None:
        self._retired_sync_retry_tasks.discard(task)
        if self._scope_discovery_retry_task is task:
            self._scope_discovery_retry_task = None
        with suppress(asyncio.CancelledError):
            error = task.exception()
            if error is not None:
                self._scope_discovery_exhausted = True
                self._publish_sync_health()
                log.error(
                    "Guild command scope discovery retry task failed",
                    exc_info=(type(error), error, error.__traceback__),
                )

    async def _retry_guild_sync(self, guild_id: int) -> None:
        for attempt, delay in enumerate(self.sync_retry_delays, start=1):
            await asyncio.sleep(max(0.0, delay))
            async with self._sync_lock:
                if self._closed or not self._live or guild_id not in self._sync_failures:
                    return
                self._sync_retry_attempts[guild_id] = attempt
                phase = self._sync_failure_phases.get(guild_id, "publish")
                if phase == "track":
                    try:
                        await self._track(guild_id)
                    except Exception as exc:
                        outcome = _GuildSyncOutcome(error=exc, phase="track")
                    else:
                        outcome = _GuildSyncOutcome(phase="track")
                elif phase == "forget":
                    try:
                        await self._forget(guild_id)
                    except Exception as exc:
                        outcome = _GuildSyncOutcome(error=exc, published=True, phase="forget")
                    else:
                        outcome = _GuildSyncOutcome(published=True, phase="forget")
                else:
                    outcome = await self._sync_one(guild_id)
                if self._closed or not self._live:
                    return
                if outcome.error is None:
                    self._clear_sync_failure(guild_id)
                    return
                self._record_sync_failure(
                    guild_id,
                    outcome.error,
                    # A publish retry that fails in its prerequisite track step
                    # must remain a publish retry. Only a successful empty PUT
                    # followed by failed cleanup narrows the remaining work.
                    phase="forget" if outcome.phase == "forget" else phase,
                )
        if guild_id in self._sync_failures:
            self._sync_exhausted.add(guild_id)
            self._publish_sync_health()
            log.error(
                "Exhausted %d automatic slash-command sync retries for guild %s",
                len(self.sync_retry_delays),
                guild_id,
            )

    def _sync_retry_done(self, guild_id: int, task: asyncio.Task[None]) -> None:
        self._retired_sync_retry_tasks.discard(task)
        if self._sync_retry_tasks.get(guild_id) is task:
            self._sync_retry_tasks.pop(guild_id, None)
        with suppress(asyncio.CancelledError):
            error = task.exception()
            if error is not None:
                log.error(
                    "Guild command retry task failed for guild %s",
                    guild_id,
                    exc_info=(type(error), error, error.__traceback__),
                )

    async def _cancel_sync_retries(self) -> None:
        current = asyncio.current_task()
        scope_task = self._scope_discovery_retry_task
        tasks = {
            task
            for task in (
                *self._sync_retry_tasks.values(),
                *self._retired_sync_retry_tasks,
                *((scope_task,) if scope_task is not None else ()),
            )
            if task is not current and not task.done()
        }
        still_running = await _cancel_tasks_bounded(
            tasks,
            timeout=COMMAND_SYNC_CANCEL_GRACE_SECONDS,
            what="guild command retry",
        )
        self._sync_retry_tasks.clear()
        self._scope_discovery_retry_task = None
        self._retired_sync_retry_tasks = {task for task in still_running if not task.done()}

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
