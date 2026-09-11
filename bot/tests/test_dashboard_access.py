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
@pytest.mark.parametrize(
    "ids",
    [
        {"user_id": "²", "guild_id": "2", "channel_id": "3"},
        {"user_id": "1", "guild_id": "²", "channel_id": "3"},
        {"user_id": "1", "guild_id": "2", "channel_id": "²"},
    ],
)
async def test_non_ascii_request_ids_fail_closed(tmp_path, ids):
    access, _, _, _, _ = setup(tmp_path)

    with pytest.raises(web.HTTPForbidden, match="disabled in this server"):
        await access.resolve(**ids)


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


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 404, 429, 500, 503])
async def test_discord_lookup_distinguishes_revocation_from_temporary_failure(tmp_path, status):
    access, guild, _, _, _ = setup(tmp_path)
    guild.fetch_member.side_effect = discord.HTTPException(
        SimpleNamespace(status=status, reason="Discord unavailable"), "failed"
    )
    expected = web.HTTPServiceUnavailable if status == 429 or status >= 500 else web.HTTPForbidden
    with pytest.raises(expected):
        await access.resolve(user_id="1", guild_id="2", channel_id="3")
    guild.fetch_channel.assert_not_awaited()
    guild.fetch_member.side_effect = None
    await access.resolve(user_id="1", guild_id="2", channel_id="3")


@pytest.mark.asyncio
@pytest.mark.parametrize("tier", list(TrustTier))
async def test_unlisted_users_cannot_access_dashboard_even_as_owner_or_staff(tmp_path, tier):
    access, guild, _, _, _ = setup(tmp_path)
    access.settings.dashboard_allowed_user_ids = "9"
    access.settings.owner_user_id = "1"
    access.trust.resolve = lambda *_: tier
    with pytest.raises(web.HTTPForbidden, match="limited to invited users"):
        await access.resolve(user_id="1", guild_id="2", channel_id="3")
    guild.fetch_member.assert_not_awaited()


@pytest.mark.asyncio
async def test_listed_user_remains_subject_to_channel_permissions_and_blocks(tmp_path):
    access, _, _, _, permissions = setup(tmp_path)
    access.settings.dashboard_allowed_user_ids = "9, 1"
    await access.resolve(user_id="1", guild_id="2", channel_id="3", continuing=True)
    permissions.view_channel = False
    with pytest.raises(web.HTTPForbidden):
        await access.resolve(user_id="1", guild_id="2", channel_id="3")
    permissions.view_channel = True
    access.user_blocked.return_value = True
    with pytest.raises(web.HTTPForbidden):
        await access.resolve(user_id="1", guild_id="2", channel_id="3")


@pytest.mark.asyncio
async def test_guild_dashboard_role_admits_an_unlisted_member_without_changing_trust(tmp_path):
    access, guild, _, _, _ = setup(tmp_path)
    access.settings.dashboard_allowed_user_ids = "9"
    guild.fetch_member.return_value.roles = [SimpleNamespace(id=77)]
    (tmp_path / "servers" / "2.md").write_text(
        "---\ndashboard:\n  enabled: true\n  allowed_role_ids: [77]\n---\n"
    )

    ctx = await access.resolve(user_id="1", guild_id="2", channel_id="3")

    assert ctx.tier == TrustTier.MEMBER


@pytest.mark.asyncio
async def test_guild_dashboard_role_allowlist_denies_other_roles_even_at_staff_tier(tmp_path):
    access, guild, _, _, _ = setup(tmp_path)
    access.trust.resolve = lambda *_: TrustTier.STAFF
    guild.fetch_member.return_value.roles = [SimpleNamespace(id=88)]
    (tmp_path / "servers" / "2.md").write_text(
        "---\ndashboard:\n  enabled: true\n  allowed_role_ids: [77]\n---\n"
    )

    with pytest.raises(web.HTTPForbidden, match="limited to invited users or roles"):
        await access.resolve(user_id="1", guild_id="2", channel_id="3")


@pytest.mark.asyncio
async def test_global_dashboard_invitee_bypasses_guild_role_allowlist(tmp_path):
    access, guild, _, _, _ = setup(tmp_path)
    access.settings.dashboard_allowed_user_ids = "1"
    guild.fetch_member.return_value.roles = []
    (tmp_path / "servers" / "2.md").write_text(
        "---\ndashboard:\n  enabled: true\n  allowed_role_ids: [77]\n---\n"
    )

    await access.resolve(user_id="1", guild_id="2", channel_id="3")


@pytest.mark.asyncio
@pytest.mark.parametrize("roles", ["77", [True], ["staff"], ["²"], [77, 77], list(range(101))])
async def test_invalid_guild_dashboard_role_allowlist_fails_closed(tmp_path, roles):
    access, _, _, _, _ = setup(tmp_path)
    (tmp_path / "servers" / "2.md").write_text(
        "---\ndashboard:\n  enabled: true\n  allowed_role_ids: " + repr(roles) + "\n---\n"
    )

    with pytest.raises(web.HTTPForbidden, match="disabled in this server"):
        await access.resolve(user_id="1", guild_id="2", channel_id="3")


@pytest.mark.asyncio
@pytest.mark.parametrize("allowlist", ["", "1"])
@pytest.mark.parametrize("minimum", list(TrustTier))
@pytest.mark.parametrize("tier", list(TrustTier))
async def test_dashboard_minimum_tier_applies_with_or_without_an_allowlist(
    tmp_path, allowlist, minimum, tier
):
    access, _, _, _, _ = setup(tmp_path)
    access.settings.dashboard_allowed_user_ids = allowlist
    access.settings.dashboard_min_tier = minimum.value
    access.trust.resolve = lambda *_: tier
    if tier < minimum:
        with pytest.raises(web.HTTPForbidden, match="trust tier"):
            await access.resolve(user_id="1", guild_id="2", channel_id="3")
    else:
        ctx = await access.resolve(user_id="1", guild_id="2", channel_id="3")
        assert ctx.tier == tier


@pytest.mark.parametrize("value", ["someone", "1,staff", ", ,", "1;2", "\u00b2"])
def test_invalid_dashboard_allowlist_fails_closed(value):
    with pytest.raises(ValueError, match="DASHBOARD_ALLOWED_USER_IDS"):
        make_settings(dashboard_allowed_user_ids=value)


def test_dashboard_access_settings_read_environment(monkeypatch):
    monkeypatch.setenv("DASHBOARD_ALLOWED_USER_IDS", " 1, 9,1 ")
    monkeypatch.setenv("DASHBOARD_MIN_TIER", " REGULAR ")
    settings = make_settings()
    assert settings.dashboard_allowed_user_id_set == {"1", "9"}
    assert settings.dashboard_min_tier == "regular"


def test_invalid_dashboard_minimum_tier_fails_closed():
    with pytest.raises(ValueError, match="DASHBOARD_MIN_TIER"):
        make_settings(dashboard_min_tier="everyone")
