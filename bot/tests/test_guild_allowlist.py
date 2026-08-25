"""Guild activation gate.

The active-guild set is the security boundary that keeps a publicly-invitable
bot silent in pending guilds: an inactive guild must be rejected at
``is_eligible_to_respond`` time, before trust resolution or tool dispatch.
"""

from __future__ import annotations
from pathlib import Path

from types import SimpleNamespace

import discord
import pytest
from config import paths

from discord_adapter.io import is_allowed_guild_interaction, is_eligible_to_respond
from config.fragments.guild_config import server_setup_activation


def _message(guild_id: int | None):
    return SimpleNamespace(
        author=SimpleNamespace(bot=False),
        type=discord.MessageType.default,
        guild=SimpleNamespace(id=guild_id) if guild_id is not None else None,
    )


def _interaction(guild_id: int | None, *, user_install: bool, guild_install: bool):
    return SimpleNamespace(
        guild_id=guild_id,
        is_user_integration=lambda: user_install,
        is_guild_integration=lambda: guild_install,
    )


_BOT = SimpleNamespace()
_ALLOW = {111, 222}


def test_approved_guild_is_eligible():
    assert is_eligible_to_respond(_message(111), bot_user=_BOT, allowed_guilds=_ALLOW) is True


def test_unapproved_guild_is_rejected():
    assert is_eligible_to_respond(_message(999), bot_user=_BOT, allowed_guilds=_ALLOW) is False


def test_none_disables_the_guild_gate_for_isolated_callers():
    assert is_eligible_to_respond(_message(999), bot_user=_BOT, allowed_guilds=None) is True


def test_empty_active_set_rejects_every_guild():
    assert is_eligible_to_respond(_message(999), bot_user=_BOT, allowed_guilds=set()) is False


def test_dm_without_guild_is_not_filtered_by_allowlist():
    # DMs (no guild) are rejected elsewhere; the guild gate must not depend on
    # a guild being present, mirroring the allowed_channels convention.
    assert is_eligible_to_respond(_message(None), bot_user=_BOT, allowed_guilds=_ALLOW) is True


def test_interaction_guild_install_in_unapproved_guild_is_rejected():
    # A guild-installed command in a guild outside the allowlist is gated: the
    # bot was added to that server and must not operate there.
    interaction = _interaction(999, user_install=False, guild_install=True)
    assert is_allowed_guild_interaction(interaction, allowed_guilds=_ALLOW) is False


def test_interaction_guild_install_in_approved_guild_is_allowed():
    interaction = _interaction(111, user_install=False, guild_install=True)
    assert is_allowed_guild_interaction(interaction, allowed_guilds=_ALLOW) is True


def test_user_install_in_unapproved_guild_is_allowed():
    # The user carried their personal app into a foreign server; the guild
    # allowlist (a guild-membership boundary) must not apply to user installs.
    interaction = _interaction(999, user_install=True, guild_install=False)
    assert is_allowed_guild_interaction(interaction, allowed_guilds=_ALLOW) is True


def test_dual_install_in_unapproved_guild_is_rejected():
    # When the same guild also has the app guild-installed, the bot is a member
    # there and the allowlist still applies; only user-*only* installs bypass.
    interaction = _interaction(999, user_install=True, guild_install=True)
    assert is_allowed_guild_interaction(interaction, allowed_guilds=_ALLOW) is False


def test_interaction_without_integration_markers_falls_back_to_allowlist():
    # Fail closed: a stub/older interaction without integration markers is
    # treated as guild-gated, preserving the stricter pre-change behavior.
    interaction = SimpleNamespace(guild_id=999)
    assert is_allowed_guild_interaction(interaction, allowed_guilds=_ALLOW) is False


def test_interaction_with_partial_integration_markers_falls_back_to_allowlist():
    # Fail closed: a user-integration marker without its guild-integration
    # counterpart is incomplete information; require BOTH before exempting.
    interaction = SimpleNamespace(guild_id=999, is_user_integration=lambda: True)
    assert is_allowed_guild_interaction(interaction, allowed_guilds=_ALLOW) is False


def test_none_allows_user_and_guild_installs():
    interaction = _interaction(999, user_install=False, guild_install=True)
    assert is_allowed_guild_interaction(interaction, allowed_guilds=None) is True


def test_empty_active_set_rejects_a_guild_install():
    interaction = _interaction(999, user_install=False, guild_install=True)
    assert is_allowed_guild_interaction(interaction, allowed_guilds=set()) is False


def test_server_setup_activation_cache_requires_explicit_valid_config(tmp_path: Path):
    servers = tmp_path / "servers"
    servers.mkdir()
    (servers / "111.md").write_text("---\nbot_active: true\n---\n", encoding="utf-8")
    (servers / "112.md").write_text("---\nbot_active: false\n---\n", encoding="utf-8")
    (servers / "113.md").write_text("---\n---\n", encoding="utf-8")
    (servers / "114.md").write_text(
        "---\nbot_active: true\nstaff_role_ids: nope\n---\n", encoding="utf-8"
    )
    (servers / "example.md").write_text("Example.\n", encoding="utf-8")
    (servers / "222.txt").write_text("Wrong suffix.\n", encoding="utf-8")
    (servers / "guild.md").write_text("Wrong name.\n", encoding="utf-8")
    (servers / "000111.md").write_text("---\nbot_active: false\n---\n", encoding="utf-8")

    cache = paths.GuildActivationCache(tmp_path, server_setup_activation)
    snapshot = cache.refresh()

    assert snapshot.active == frozenset({111})
    assert snapshot.deactivated == frozenset({112})
    assert snapshot.invalid == frozenset({113, 114})


def test_single_guild_refresh_removes_a_deleted_setup(tmp_path: Path) -> None:
    servers = tmp_path / "servers"
    servers.mkdir()
    path = servers / "111.md"
    path.write_text("---\nbot_active: true\n---\n", encoding="utf-8")
    cache = paths.GuildActivationCache(tmp_path, server_setup_activation)
    assert cache.refresh().active == frozenset({111})

    path.unlink()
    snapshot = cache.refresh_guild(111)

    assert snapshot.active == frozenset()
    assert snapshot.invalid == frozenset()


def test_activation_snapshot_changes_only_after_refresh(tmp_path: Path) -> None:
    servers = tmp_path / "servers"
    servers.mkdir()
    path = servers / "111.md"
    path.write_text("---\nbot_active: true\n---\n", encoding="utf-8")
    cache = paths.GuildActivationCache(tmp_path, server_setup_activation)
    cache.refresh()

    path.write_text("---\nbot_active: false\n---\n", encoding="utf-8")
    assert cache.snapshot().active == frozenset({111})
    assert cache.refresh_guild(111).deactivated == frozenset({111})


def test_symlinked_server_setup_cannot_activate(tmp_path: Path) -> None:
    servers = tmp_path / "servers"
    servers.mkdir()
    target = tmp_path / "target.md"
    target.write_text("---\nbot_active: true\n---\n", encoding="utf-8")
    link = servers / "111.md"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    snapshot = paths.GuildActivationCache(tmp_path, server_setup_activation).refresh()

    assert snapshot.active == frozenset()
    assert snapshot.invalid == frozenset({111})


def test_symlinked_servers_directory_cannot_activate_on_single_refresh(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "servers"
    directory.mkdir()
    for guild_id in (111, 222):
        (directory / f"{guild_id}.md").write_text("---\nbot_active: true\n---\n", encoding="utf-8")
    cache = paths.GuildActivationCache(tmp_path, server_setup_activation)
    assert cache.refresh().active == frozenset({111, 222})

    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "111.md").write_text("---\nbot_active: true\n---\n", encoding="utf-8")
    directory.rename(tmp_path / "original-servers")
    try:
        directory.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is unavailable")

    assert cache.refresh_guild(111).active == frozenset()
