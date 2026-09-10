from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from discord import app_commands

from app.runtime import KimiCommandTree
from tests.helpers import make_settings


@pytest.mark.asyncio
async def test_global_sync_keeps_primary_entrypoint_in_same_replacement():
    client = discord.Client(intents=discord.Intents.none(), application_id=42)
    client._agent_application = SimpleNamespace(settings=make_settings(dashboard_enabled=True))
    tree = KimiCommandTree(client)

    @tree.command(name="hello")
    async def hello(interaction: discord.Interaction):
        pass

    async def replacement(app_id, *, payload):
        assert app_id == 42
        return [
            {**item, "id": str(index + 1), "application_id": "42"}
            for index, item in enumerate(payload)
        ]

    tree._http.bulk_upsert_global_commands = AsyncMock(side_effect=replacement)
    result = await tree.sync()
    payload = tree._http.bulk_upsert_global_commands.call_args.kwargs["payload"]
    assert [item["name"] for item in payload] == ["hello", "Launch"]
    assert payload[-1] == {
        "name": "Launch",
        "description": "Open your private Kimi dashboard",
        "type": 4,
        "handler": 2,
        "integration_types": [0],
        "contexts": [0],
    }
    assert [item.type.value for item in result] == [1, 4]
    tree._http.bulk_upsert_global_commands.assert_awaited_once()


@pytest.mark.asyncio
async def test_disabled_and_guild_sync_use_normal_discord_py_path(monkeypatch):
    client = discord.Client(intents=discord.Intents.none(), application_id=42)
    client._agent_application = SimpleNamespace(settings=make_settings())
    tree = KimiCommandTree(client)
    original = AsyncMock(return_value=[])
    monkeypatch.setattr(app_commands.CommandTree, "sync", original)
    await tree.sync()
    original.assert_awaited_once_with(guild=None)
    client._agent_application.settings.dashboard_enabled = True
    guild = discord.Object(2)
    await tree.sync(guild=guild)
    assert original.call_args.kwargs == {"guild": guild}
