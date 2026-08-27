from __future__ import annotations

import logging
from typing import Any, Protocol

import discord
from discord import app_commands
from discord.ext import commands

from commands._shared import send_message
from storage.model_selection import ModelSelectionStore

log = logging.getLogger(__name__)


class ModelRouter(Protocol):
    model_config: Any
    active_chat_model: str | None

    @property
    def selectable_chat_models(self) -> tuple[str, ...]: ...

    def validate_active_chat_model(self, model_name: str | None) -> None: ...

    def set_active_chat_model(self, model_name: str | None) -> None: ...

    async def circuit_snapshots(self) -> tuple[Any, ...]: ...

    async def reset_all_circuits(self) -> None: ...


_DEFAULT_VALUE = "__config_default__"
_MODELS_PER_SELECT = 24


async def _status_text(manager: ModelRouter) -> tuple[str, int]:
    model_config = manager.model_config
    if model_config is None:
        return "Model routing is unavailable.", 0
    selected = manager.active_chat_model
    configured_name = selected or model_config.roles.chat
    model_id = model_config.models[configured_name].model
    source = "global override" if selected is not None else "config default"
    lines = [f"Active chat model: `{model_id}` ({source})."]
    circuits = await manager.circuit_snapshots()
    if circuits:
        lines.append("\nProvider cooldowns:")
        for record in circuits[:10]:
            label = record.display_label[:80]
            lines.append(f"- `{label}` · {record.reason} · <t:{int(record.retry_at)}:R>")
        if len(circuits) > 10:
            lines.append(f"- …and {len(circuits) - 10} more")
    return "\n".join(lines), len(circuits)


class ModelSelect(discord.ui.Select):
    def __init__(
        self,
        manager: ModelRouter,
        store: ModelSelectionStore,
        owner_user_id: str,
        *,
        model_names: tuple[str, ...] | None = None,
        include_default: bool = True,
        page_label: str = "",
        page_index: int = 0,
    ) -> None:
        self._manager = manager
        self._store = store
        self._owner_user_id = owner_user_id
        self._page_index = page_index
        model_config = manager.model_config
        names = manager.selectable_chat_models if model_names is None else model_names
        options: list[discord.SelectOption] = []
        if include_default:
            default_name = model_config.roles.chat if model_config is not None else "unavailable"
            options.append(
                discord.SelectOption(
                    label=f"Default · {default_name}"[:100],
                    value=_DEFAULT_VALUE,
                    description="Use config and scope routing"[:100],
                    default=manager.active_chat_model is None,
                )
            )
        if model_config is not None:
            for name in names:
                entry = model_config.models[name]
                options.append(
                    discord.SelectOption(
                        label=entry.model[:100],
                        value=name,
                        description=f"{name} · {entry.provider}"[:100],
                        default=manager.active_chat_model == name,
                    )
                )
        placeholder = "Choose the global chat model"
        if page_label:
            placeholder = f"{placeholder} · {page_label}"
        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if str(interaction.user.id) != self._owner_user_id:
            await send_message(interaction, "Bot owner only.")
            return
        selected = None if self.values[0] == _DEFAULT_VALUE else self.values[0]
        try:
            self._manager.validate_active_chat_model(selected)
        except ValueError:
            await send_message(interaction, "That model is no longer available.")
            return
        try:
            # Persist before changing live routing so a database failure cannot
            # leave a restart-dependent split brain.
            await self._store.set(selected)
        except Exception:
            log.exception("Could not persist global chat model selection")
            await send_message(interaction, "Could not save the model selection.")
            return
        self._manager.set_active_chat_model(selected)
        status, circuit_count = await _status_text(self._manager)
        await interaction.response.edit_message(
            content=status,
            view=ModelsView(
                self._manager,
                self._store,
                self._owner_user_id,
                circuit_count=circuit_count,
                page_index=self._page_index,
            ),
        )


class ResetCircuitsButton(discord.ui.Button):
    def __init__(
        self,
        manager: ModelRouter,
        store: ModelSelectionStore,
        owner_user_id: str,
        *,
        disabled: bool,
    ) -> None:
        super().__init__(
            label="Reset all provider cooldowns",
            style=discord.ButtonStyle.secondary,
            disabled=disabled,
        )
        self._manager = manager
        self._store = store
        self._owner_user_id = owner_user_id

    async def callback(self, interaction: discord.Interaction) -> None:
        if str(interaction.user.id) != self._owner_user_id:
            await send_message(interaction, "Bot owner only.")
            return
        try:
            await self._manager.reset_all_circuits()
        except Exception:
            log.exception("Could not reset provider cooldowns")
            await send_message(interaction, "Could not reset provider cooldowns.")
            return
        status, circuit_count = await _status_text(self._manager)
        await interaction.response.edit_message(
            content=status,
            view=ModelsView(
                self._manager,
                self._store,
                self._owner_user_id,
                circuit_count=circuit_count,
            ),
        )


class ModelPageButton(discord.ui.Button):
    def __init__(
        self,
        manager: ModelRouter,
        store: ModelSelectionStore,
        owner_user_id: str,
        *,
        target_page: int,
        label: str,
    ) -> None:
        super().__init__(label=label, style=discord.ButtonStyle.secondary)
        self._manager = manager
        self._store = store
        self._owner_user_id = owner_user_id
        self._target_page = target_page

    async def callback(self, interaction: discord.Interaction) -> None:
        if str(interaction.user.id) != self._owner_user_id:
            await send_message(interaction, "Bot owner only.")
            return
        status, circuit_count = await _status_text(self._manager)
        await interaction.response.edit_message(
            content=status,
            view=ModelsView(
                self._manager,
                self._store,
                self._owner_user_id,
                circuit_count=circuit_count,
                page_index=self._target_page,
            ),
        )


class ModelsView(discord.ui.View):
    def __init__(
        self,
        manager: ModelRouter,
        store: ModelSelectionStore,
        owner_user_id: str,
        *,
        circuit_count: int = 0,
        page_index: int = 0,
    ) -> None:
        super().__init__(timeout=300)
        names = manager.selectable_chat_models
        pages = [
            names[offset : offset + _MODELS_PER_SELECT]
            for offset in range(0, len(names), _MODELS_PER_SELECT)
        ]
        page_index = min(max(page_index, 0), max(len(pages) - 1, 0))
        if pages:
            self.add_item(
                ModelSelect(
                    manager,
                    store,
                    owner_user_id,
                    model_names=pages[page_index],
                    include_default=page_index == 0,
                    page_label=(f"{page_index + 1}/{len(pages)}" if len(pages) > 1 else ""),
                    page_index=page_index,
                )
            )
        if page_index > 0:
            self.add_item(
                ModelPageButton(
                    manager,
                    store,
                    owner_user_id,
                    target_page=page_index - 1,
                    label="Previous models",
                )
            )
        if page_index + 1 < len(pages):
            self.add_item(
                ModelPageButton(
                    manager,
                    store,
                    owner_user_id,
                    target_page=page_index + 1,
                    label="More models",
                )
            )
        self.add_item(
            ResetCircuitsButton(
                manager,
                store,
                owner_user_id,
                disabled=circuit_count == 0,
            )
        )


def register_models_command(
    bot: commands.Bot,
    manager: ModelRouter,
    store: ModelSelectionStore,
    *,
    owner_user_id: str,
) -> None:
    @app_commands.command(
        name="models",
        description="Choose the global chat model",
    )
    async def models(interaction: discord.Interaction) -> None:
        if str(interaction.user.id) != owner_user_id:
            await send_message(interaction, "Bot owner only.")
            return
        status, circuit_count = await _status_text(manager)
        await interaction.response.send_message(
            status,
            view=ModelsView(
                manager,
                store,
                owner_user_id,
                circuit_count=circuit_count,
            ),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    bot.tree.add_command(models, override=True)
