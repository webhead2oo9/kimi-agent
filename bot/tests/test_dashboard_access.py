from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from aiohttp import web

from app.dashboard_access import DashboardAccess
from tests.helpers import make_settings
from trust.tiers import TrustTier


def setup(tmp_path, *, thread=False):
    directory = tmp_path / "servers"
    directory.mkdir()
    (directory / "2.md").write_text("---\ndashboard:\n  enabled: true\n---\n")
    member, bot_member = SimpleNamespace(id=1), SimpleNamespace(id=42)
    channel = MagicMock(spec=discord.Thread if thread else discord.TextChannel)
    channel.id, channel.parent_id = 3, 4
    channel.type = discord.ChannelType.private_thread if thread else discord.ChannelType.text
    channel.archived = channel.locked = False
    channel.fetch_member = AsyncMock(return_value=SimpleNamespace(id=1))
    parent = MagicMock(spec=discord.TextChannel)
    permissions = SimpleNamespace(
        view_channel=True,
        read_message_history=True,
        send_messages=True,
        send_messages_in_threads=True,
        manage_threads=False,
    )
    parent.permissions_for.return_value = permissions
    channel.permissions_for.return_value = permissions
    guild = SimpleNamespace(
        id=2,
        me=bot_member,
        fetch_member=AsyncMock(return_value=member),
        fetch_channel=AsyncMock(side_effect=lambda id: parent if id == 4 else channel),
    )
    channel.guild = guild
    access = DashboardAccess(
        bot=SimpleNamespace(get_guild=lambda _: guild),
        settings=make_settings(dashboard_enabled=True, config_dir=str(tmp_path)),
        trust=SimpleNamespace(resolve=lambda *_: TrustTier.MEMBER),
        active_guilds=lambda: {2},
        user_blocked=AsyncMock(return_value=False),
        channel_access_allowed=lambda *_: True,
        preferences=None,
    )
    return access, guild, channel, parent, permissions


@pytest.mark.asyncio
async def test_access_fetches_member_channel_and_requires_strict_guild_opt_in(tmp_path):
    access, guild, _, _, _ = setup(tmp_path)
    ctx = await access.resolve(user_id="1", guild_id="2", channel_id="3", continuing=True)
    assert ctx.tier == TrustTier.MEMBER
    guild.fetch_member.assert_awaited_once_with(1)
    guild.fetch_channel.assert_awaited_once_with(3)
    (tmp_path / "servers" / "2.md").write_text('---\ndashboard:\n  enabled: "true"\n---\n')
    with pytest.raises(web.HTTPForbidden):
        await access.resolve(user_id="1", guild_id="2", channel_id="3")


@pytest.mark.asyncio
async def test_thread_access_uses_fresh_parent_overwrites_and_membership(tmp_path):
    access, guild, channel, _, permissions = setup(tmp_path, thread=True)
    await access.resolve(user_id="1", guild_id="2", channel_id="3", continuing=True)
    assert [call.args[0] for call in guild.fetch_channel.call_args_list] == [3, 4]
    assert channel.fetch_member.await_count == 2
    # Cached Thread.permissions_for could still grant access after the parent changed.
    channel.permissions_for.return_value = SimpleNamespace(
        view_channel=True, read_message_history=True, manage_threads=True
    )
    permissions.view_channel = False
    with pytest.raises(web.HTTPForbidden):
        await access.resolve(user_id="1", guild_id="2", channel_id="3")


@pytest.mark.asyncio
async def test_continuing_rechecks_posting_permission_and_blocked_user(tmp_path):
    access, _, _, _, permissions = setup(tmp_path)
    permissions.send_messages = False
    await access.resolve(user_id="1", guild_id="2", channel_id="3")
    with pytest.raises(web.HTTPForbidden):
        await access.resolve(user_id="1", guild_id="2", channel_id="3", continuing=True)
    access.user_blocked.return_value = True
    with pytest.raises(web.HTTPForbidden):
        await access.resolve(user_id="1", guild_id="2", channel_id="3")
