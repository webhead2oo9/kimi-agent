"""One blocked user, every interactive entry point, the same refusal.

The guild message path, personal ``/chat``, and the teach context menu each
have their own Discord boundary code, so a block honoured on one and missed on
another fails silently. This file builds the real application once per case,
blocks one user in a shared store, and drives each entry point with that user,
asserting that no privacy lease is taken, no turn starts, and no provider is
called. The coding-task scheduler's claim-time check is covered in
test_coding_tasks.py because it needs no Discord surface.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import discord
import pytest

from app import message_runtime

import app.runtime as app_runtime
from commands.learn_cmd import learn_menu_name
from config.settings import Settings
from pydantic import SecretStr
from tests.helpers import (
    LifecycleProbe,
    StubProviderManager,
    replace_app_repositories,
    replace_lifecycle_resources,
)

BLOCKED_USER_ID = 4242
GUILD_ID = 999


class _BlockedStore:
    def __init__(self, blocked: set[str]) -> None:
        self._blocked = blocked
        self.asked: list[str] = []

    async def is_blocked(self, user_id: str) -> bool:
        self.asked.append(user_id)
        return user_id in self._blocked


class _NeverLeased:
    """A blocked user must be refused before any privacy lease is taken."""

    def activity(self, user_id: str) -> Any:
        raise AssertionError(f"blocked user {user_id} acquired a privacy lease")


class _Response:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.deferred = False

    def is_done(self) -> bool:
        return self.deferred or bool(self.sent)

    async def send_message(self, content: object = None, **_kwargs: object) -> None:
        self.sent.append(str(content))

    async def defer(self, **_kwargs: object) -> None:
        self.deferred = True


class _Followup:
    def __init__(self, sink: list[str]) -> None:
        self._sink = sink

    async def send(self, content: object = None, **_kwargs: object) -> None:
        self._sink.append(str(content))


class _Interaction:
    def __init__(self, *, guild: bool) -> None:
        self.id = 7
        self.user = SimpleNamespace(
            id=BLOCKED_USER_ID, display_name="Blocked", mention=f"<@{BLOCKED_USER_ID}>"
        )
        self.guild_id = GUILD_ID if guild else None
        self.guild = SimpleNamespace(id=GUILD_ID, name="Test Guild") if guild else None
        self.channel_id = 100
        self.channel = SimpleNamespace(name="general", guild=self.guild)
        self.created_at = datetime.now(UTC)
        self.response = _Response()
        self.followup = _Followup(self.response.sent)

    async def edit_original_response(self, **_kwargs: object) -> None:
        raise AssertionError("a refused interaction has nothing to edit")


def _guild_message() -> Any:
    channel = SimpleNamespace(id=100, guild=SimpleNamespace(id=GUILD_ID), type=None)
    reactions: list[str] = []

    async def add_reaction(emoji: object) -> None:
        reactions.append(str(emoji))

    return SimpleNamespace(
        id=555,
        content="<@1> hello",
        author=SimpleNamespace(id=BLOCKED_USER_ID, display_name="Blocked", bot=False, name="b"),
        guild=channel.guild,
        channel=channel,
        type=discord.MessageType.default,
        reference=None,
        attachments=[],
        created_at=datetime.now(UTC),
        add_reaction=add_reaction,
        reactions=reactions,
    )


async def _blocked_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Any, _BlockedStore]:
    monkeypatch.setattr(
        app_runtime, "build_provider_manager", lambda settings: StubProviderManager(settings)
    )
    app = app_runtime.build_app(
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            discord_bot_token=SecretStr("token"),
            model_api_key=SecretStr("key"),
            hindsight_url="",
            moderation_enabled=False,
            allowed_guild_ids=str(GUILD_ID),
            config_dir=str(tmp_path / "config"),
            database_path=str(tmp_path / "runtime.db"),
            workspace_dir=str(tmp_path / "workspaces"),
            attachment_store_dir=str(tmp_path / "attachments"),
            user_app_chat_enabled=True,
            owner_user_id=str(BLOCKED_USER_ID),
            staff_user_ids=str(BLOCKED_USER_ID),
        )
    )
    try:
        await LifecycleProbe(app).first_init_core()
        LifecycleProbe(app).set_gateway_ready()
        store = _BlockedStore({str(BLOCKED_USER_ID)})
        replace_app_repositories(app, blocked_user_store=cast(Any, store))
        replace_lifecycle_resources(app, privacy_barrier=cast(Any, _NeverLeased()))

        async def never_runs(**_kwargs: object) -> None:
            raise AssertionError("a blocked user reached the provider")

        monkeypatch.setattr(message_runtime, "run_conversation", never_runs)
    except BaseException:
        await app.close()
        raise
    return app, store


class _ReachedPastGate(Exception):
    """Raised by the patched next step after the block gate on the guild path."""


async def _via_guild_message(app: Any, monkeypatch: pytest.MonkeyPatch) -> str | None:
    monkeypatch.setattr(
        message_runtime,
        "should_respond",
        lambda message, **_kwargs: True,
    )

    async def handled(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("handle_message ran for a blocked user")

    monkeypatch.setattr(app, "handle_message", handled)

    # The stop-message check is the statement directly after the block gate, so
    # reaching it means the gate passed; a blocked user must return before it.
    def reached(*_args: object, **_kwargs: object) -> bool:
        raise _ReachedPastGate

    # The stop check is the first thing after the block gate in on_message.
    monkeypatch.setattr(app.work_cancellation, "is_stop_message", reached)
    message = _guild_message()
    await app.on_message(message)
    assert message.reactions == []
    return None


async def _via_personal_chat(app: Any, _monkeypatch: pytest.MonkeyPatch) -> str | None:
    command = cast(Any, app.bot.tree.get_command("chat"))
    assert command is not None
    interaction = _Interaction(guild=False)
    await command.callback(cast(discord.Interaction, interaction), "hello")
    assert not interaction.response.deferred
    return interaction.response.sent[-1]


async def _via_teach_menu(app: Any, _monkeypatch: pytest.MonkeyPatch) -> str | None:
    menu = cast(
        Any,
        app.bot.tree.get_command(
            learn_menu_name(app.settings.bot_name), type=discord.AppCommandType.message
        ),
    )
    assert menu is not None
    interaction = _Interaction(guild=True)
    message = SimpleNamespace(
        id=1,
        content="Raid night is Thursdays.",
        author=SimpleNamespace(id=7, display_name="Member", bot=False),
        channel=SimpleNamespace(id=100),
        attachments=[],
        jump_url="https://discord.com/channels/999/100/1",
    )
    await menu.callback(cast(Any, interaction), cast(Any, message))
    assert not interaction.response.deferred
    return interaction.response.sent[-1]


EntryPoint = Callable[[Any, pytest.MonkeyPatch], Awaitable[str | None]]

ENTRY_POINTS = [
    pytest.param(_via_guild_message, None, id="guild-message"),
    pytest.param(_via_personal_chat, "You can't use personal chat right now.", id="personal-chat"),
    pytest.param(_via_teach_menu, "You can't use this right now.", id="teach-menu"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("entry_point", "refusal"), ENTRY_POINTS)
async def test_every_entry_point_refuses_a_blocked_user_before_any_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry_point: EntryPoint,
    refusal: str | None,
) -> None:
    app, store = await _blocked_app(tmp_path, monkeypatch)
    try:
        reply = await entry_point(app, monkeypatch)
    finally:
        await app.close()

    assert reply == refusal
    assert store.asked == [str(BLOCKED_USER_ID)]


@pytest.mark.asyncio
async def test_the_guild_gate_sits_directly_before_the_stop_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Positive control: an unblocked user passes the gate and reaches the
    very next statement, so the refusal above is the gate and not some other
    failure of the fake message."""
    app, _ = await _blocked_app(tmp_path, monkeypatch)
    control_store = _BlockedStore(set())
    replace_app_repositories(app, blocked_user_store=cast(Any, control_store))
    try:
        with pytest.raises(_ReachedPastGate):
            await _via_guild_message(app, monkeypatch)
    finally:
        await app.close()

    # The sentinel alone proves the stop check ran; this proves the gate was
    # the thing consulted immediately before it.
    assert control_store.asked == [str(BLOCKED_USER_ID)]
