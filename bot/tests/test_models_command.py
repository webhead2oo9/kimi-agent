from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import discord
import pytest
from discord.ext import commands

from app.providers import ProviderManager
from commands.models_cmd import ModelSelect, ModelsView, _status_text, register_models_command
from providers.circuit_breaker import CircuitTarget
from providers.failure_policy import CircuitScopeKind, FailureCategory, ProviderFailure
from config.model_config import parse_model_config_text
from config.settings import Settings


def _manager() -> ProviderManager:
    config = parse_model_config_text(
        """
providers:
  main: { type: openai_compat, base_url: https://example.test/v1, keyless: true }
models:
  default: { provider: main, model: default-id, capabilities: [text, tool_calling] }
  alternate: { provider: main, model: alternate-id, capabilities: [text, tool_calling] }
roles: { chat: default, compaction: default }
selectable_chat_models: [default, alternate]
"""
    )
    return ProviderManager(settings=Settings(), model_config=config)


def _interaction(user_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        user=SimpleNamespace(id=int(user_id)),
        response=SimpleNamespace(
            is_done=lambda: False,
            send_message=AsyncMock(),
            edit_message=AsyncMock(),
        ),
        followup=SimpleNamespace(send=AsyncMock()),
    )


@pytest.mark.asyncio
async def test_models_command_is_owner_only_and_ephemeral() -> None:
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    store = SimpleNamespace(set=AsyncMock())
    register_models_command(bot, _manager(), store, owner_user_id="42")  # type: ignore[arg-type]
    command = cast(Any, bot.tree.get_command("models"))
    assert command is not None

    denied = _interaction("41")
    await command.callback(denied)
    denied.response.send_message.assert_awaited_once()
    assert denied.response.send_message.await_args.args[0] == "Bot owner only."

    allowed = _interaction("42")
    await command.callback(allowed)
    allowed.response.send_message.assert_awaited_once()
    kwargs = allowed.response.send_message.await_args.kwargs
    assert kwargs["ephemeral"] is True
    assert isinstance(kwargs["view"], ModelsView)
    menu = cast(ModelSelect, kwargs["view"].children[0])
    assert [option.value for option in menu.options] == [
        "__config_default__",
        "default",
        "alternate",
    ]
    assert [option.label for option in menu.options] == [
        "Default · default",
        "default-id",
        "alternate-id",
    ]


def test_models_view_pages_choices_across_discord_selects() -> None:
    manager = _manager()
    model_config = manager.model_config
    assert model_config is not None
    template = model_config.models["alternate"]
    for index in range(25):
        name = f"extra-{index}"
        model_config.models[name] = template.model_copy(update={"model": f"model-{index}"})
        model_config.selectable_chat_models.append(name)
    store = SimpleNamespace(set=AsyncMock())

    first_view = ModelsView(manager, store, "42")  # type: ignore[arg-type]
    second_view = ModelsView(manager, store, "42", page_index=1)  # type: ignore[arg-type]
    menus = [
        cast(ModelSelect, child)
        for view in (first_view, second_view)
        for child in view.children
        if isinstance(child, ModelSelect)
    ]

    assert len(menus) == 2
    assert all(len(menu.options) <= 25 for menu in menus)
    assert menus[0].options[0].value == "__config_default__"
    assert "__config_default__" not in [option.value for option in menus[1].options]
    values = [
        option.value
        for menu in menus
        for option in menu.options
        if option.value != "__config_default__"
    ]
    assert values == list(manager.selectable_chat_models)


@pytest.mark.asyncio
async def test_model_select_persists_then_switches_live_routing() -> None:
    manager = _manager()
    store = SimpleNamespace(set=AsyncMock())
    select = ModelSelect(manager, store, "42")  # type: ignore[arg-type]
    select._values = ["alternate"]
    interaction = _interaction("42")

    await select.callback(interaction)  # type: ignore[arg-type]

    store.set.assert_awaited_once_with("alternate")
    assert manager.active_chat_model == "alternate"
    assert manager.resolved_chat_model_name() == "alternate"
    interaction.response.edit_message.assert_awaited_once()
    assert interaction.response.edit_message.await_args.kwargs["content"] == (
        "Active chat model: `alternate-id` (global override)."
    )


@pytest.mark.asyncio
async def test_model_select_rejects_non_owner_without_writing() -> None:
    manager = _manager()
    store = SimpleNamespace(set=AsyncMock())
    select = ModelSelect(manager, store, "42")  # type: ignore[arg-type]
    select._values = ["alternate"]
    interaction = _interaction("41")

    await select.callback(interaction)  # type: ignore[arg-type]

    store.set.assert_not_awaited()
    assert manager.active_chat_model is None
    interaction.response.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_models_status_shows_and_resets_provider_cooldown() -> None:
    manager = _manager()
    target = CircuitTarget.create(
        model_identity="main/default", account_identity="main/account", label="main/default-id"
    )
    permit = await manager.circuit_breaker.allow(target)
    assert permit is not None
    await manager.circuit_breaker.record_failure(
        target,
        ProviderFailure(
            "retry",
            FailureCategory.OUTAGE,
            CircuitScopeKind.MODEL,
            retry_at=2_000_000_000,
        ),
        permit,
    )

    status, count = await _status_text(manager)
    assert count == 1
    assert "main/default-id" in status
    assert "outage" in status

    await manager.reset_all_circuits()
    assert await manager.circuit_snapshots() == ()
