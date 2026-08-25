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


_DEFAULT_VALUE = "__config_default__"
_MODELS_PER_SELECT = 24


def _status_text(manager: ModelRouter) -> str:
    model_config = manager.model_config
    if model_config is None:
        return "Model routing is unavailable."
    selected = manager.active_chat_model
    configured_name = selected or model_config.roles.chat
    model_id = model_config.models[configured_name].model
    source = "global override" if selected is not None else "config default"
    return f"Active chat model: `{model_id}` ({source})."


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
    ) -> None:
        self._manager = manager
        self._store = store
        self._owner_user_id = owner_user_id
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
        await interaction.response.edit_message(
            content=_status_text(self._manager),
            view=ModelsView(self._manager, self._store, self._owner_user_id),
        )


class ModelsView(discord.ui.View):
    def __init__(
        self,
        manager: ModelRouter,
        store: ModelSelectionStore,
        owner_user_id: str,
    ) -> None:
        super().__init__(timeout=300)
        names = manager.selectable_chat_models
        pages = [
            names[offset : offset + _MODELS_PER_SELECT]
            for offset in range(0, len(names), _MODELS_PER_SELECT)
        ]
        for index, page in enumerate(pages):
            self.add_item(
                ModelSelect(
                    manager,
                    store,
                    owner_user_id,
                    model_names=page,
                    include_default=index == 0,
                    page_label=f"{index + 1}/{len(pages)}" if len(pages) > 1 else "",
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
        if not manager.selectable_chat_models:
            await send_message(
                interaction,
                "No selectable chat models are configured in `config/models.yaml`.",
            )
            return
        await interaction.response.send_message(
            _status_text(manager),
            view=ModelsView(manager, store, owner_user_id),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    bot.tree.add_command(models, override=True)
