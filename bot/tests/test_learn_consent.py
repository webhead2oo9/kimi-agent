"""Privacy-consent parity for the staff teaching context menu."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import discord
import pytest

import app.runtime as app_runtime
from app.user_app_consent import UserAppConsentView
from commands.learn_cmd import learn_menu_name
from tests.helpers import StubProviderManager, make_settings
from tools.learn import LearnTarget

USER_ID = 42
GUILD_ID = 999
REPORT = "Saved to community knowledge under events."


class _Response:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []
        self.deferred: list[dict[str, object]] = []

    def is_done(self) -> bool:
        return bool(self.sent or self.deferred)

    async def send_message(self, content: object = None, **kwargs: object) -> None:
        kwargs["content"] = content
        self.sent.append(kwargs)

    async def defer(self, **kwargs: object) -> None:
        self.deferred.append(kwargs)


class _Followup:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    async def send(self, content: object = None, **kwargs: object) -> None:
        kwargs["content"] = content
        self.sent.append(kwargs)


class _Interaction:
    def __init__(self, *, guild: bool = True) -> None:
        self.user = SimpleNamespace(id=USER_ID, display_name="Ada", mention=f"<@{USER_ID}>")
        self.guild_id = GUILD_ID if guild else None
        self.guild = SimpleNamespace(id=GUILD_ID, name="Test Guild") if guild else None
        self.channel_id = 100
        self.channel = SimpleNamespace(id=100, name="general", guild=self.guild)
        self.response = _Response()
        self.followup = _Followup()


class _ConsentLookupForbidden:
    async def has_consented(self, user_id: str) -> bool:
        raise AssertionError(f"consent was looked up for {user_id}")


class _BlockedStore:
    def __init__(self) -> None:
        self.asked: list[str] = []

    async def is_blocked(self, user_id: str) -> bool:
        self.asked.append(user_id)
        return True


def _message() -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        content="Raid night is Thursdays.",
        author=SimpleNamespace(id=7, display_name="Member", bot=False),
        channel=SimpleNamespace(id=100),
        attachments=[],
        jump_url="https://discord.com/channels/999/100/1",
    )


async def _build_learn_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    consent_enabled: bool,
) -> tuple[Any, list[tuple[LearnTarget, discord.Interaction]]]:
    monkeypatch.setattr(
        app_runtime,
        "build_provider_manager",
        lambda settings: StubProviderManager(settings),
    )
    calls: list[tuple[LearnTarget, discord.Interaction]] = []

    async def capture_learn(
        _self: object,
        target: LearnTarget,
        interaction: discord.Interaction,
    ) -> str:
        calls.append((target, interaction))
        return REPORT

    monkeypatch.setattr(app_runtime.KimiApplication, "_run_learn_turn", capture_learn)
    app = app_runtime.build_app(
        make_settings(
            discord_bot_token="token",
            model_api_key="key",
            hindsight_url="",
            moderation_enabled=False,
            allowed_guild_ids=str(GUILD_ID),
            config_dir=str(tmp_path / "config"),
            database_path=str(tmp_path / "runtime.db"),
            workspace_dir=str(tmp_path / "workspaces"),
            attachment_store_dir=str(tmp_path / "attachments"),
            user_app_chat_enabled=True,
            owner_user_id=str(USER_ID),
            staff_user_ids=str(USER_ID),
            privacy_consent_enabled=consent_enabled,
        )
    )
    try:
        await app._first_init_core()
    except BaseException:
        await app._close_resources()
        raise
    return app, calls


def _teach_menu(app: Any) -> Any:
    menu = app.bot.tree.get_command(
        learn_menu_name(app.settings.bot_name),
        type=discord.AppCommandType.message,
    )
    assert menu is not None
    return menu


@pytest.mark.asyncio
async def test_teach_prompts_unconsented_staff_before_defer_or_learn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, calls = await _build_learn_app(tmp_path, monkeypatch, consent_enabled=True)
    interaction = _Interaction()
    try:
        await _teach_menu(app).callback(cast(Any, interaction), cast(Any, _message()))

        assert interaction.response.deferred == []
        assert calls == []
        assert len(interaction.response.sent) == 1
        prompt = interaction.response.sent[0]
        assert prompt["ephemeral"] is True
        assert isinstance(prompt["view"], UserAppConsentView)
        embed = cast(discord.Embed, prompt["embed"])
        assert embed.title == app.settings.privacy_consent_title
        assert embed.description == app.settings.privacy_consent_text
    finally:
        await app._close_resources()


@pytest.mark.asyncio
async def test_teach_accept_stores_consent_and_resumes_with_button_interaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, calls = await _build_learn_app(tmp_path, monkeypatch, consent_enabled=True)
    original = _Interaction()
    accepted = _Interaction()
    try:
        await _teach_menu(app).callback(cast(Any, original), cast(Any, _message()))
        view = cast(UserAppConsentView, original.response.sent[0]["view"])
        accept_button = next(
            child
            for child in view.children
            if getattr(child, "label", None) == "Accept and continue"
        )

        await cast(Any, accept_button).callback(cast(discord.Interaction, accepted))

        assert app.preference_store is not None
        assert await app.preference_store.has_consented(str(USER_ID)) is True
        assert len(calls) == 1
        assert calls[0][1] is accepted
        assert accepted.response.deferred == [{"ephemeral": True, "thinking": True}]
        assert [message["content"] for message in accepted.followup.sent] == [REPORT]
    finally:
        await app._close_resources()


@pytest.mark.asyncio
async def test_teach_already_consented_defers_and_runs_directly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, calls = await _build_learn_app(tmp_path, monkeypatch, consent_enabled=True)
    interaction = _Interaction()
    try:
        assert app.preference_store is not None
        await app.preference_store.set_consent(str(USER_ID), True)

        await _teach_menu(app).callback(cast(Any, interaction), cast(Any, _message()))

        assert interaction.response.sent == []
        assert interaction.response.deferred == [{"ephemeral": True, "thinking": True}]
        assert len(calls) == 1
        assert calls[0][1] is interaction
        assert [message["content"] for message in interaction.followup.sent] == [REPORT]
    finally:
        await app._close_resources()


@pytest.mark.asyncio
async def test_teach_disabled_gate_never_looks_up_consent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, calls = await _build_learn_app(tmp_path, monkeypatch, consent_enabled=False)
    interaction = _Interaction()
    try:
        app.preference_store = cast(Any, _ConsentLookupForbidden())

        await _teach_menu(app).callback(cast(Any, interaction), cast(Any, _message()))

        assert interaction.response.sent == []
        assert interaction.response.deferred == [{"ephemeral": True, "thinking": True}]
        assert len(calls) == 1
    finally:
        await app._close_resources()


@pytest.mark.asyncio
async def test_teach_blocked_staff_is_refused_before_consent_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, calls = await _build_learn_app(tmp_path, monkeypatch, consent_enabled=True)
    interaction = _Interaction()
    blocked = _BlockedStore()
    try:
        app.blocked_user_store = cast(Any, blocked)
        app.preference_store = cast(Any, _ConsentLookupForbidden())

        await _teach_menu(app).callback(cast(Any, interaction), cast(Any, _message()))

        assert blocked.asked == [str(USER_ID)]
        assert interaction.response.sent[0]["content"] == "You can't use this right now."
        assert interaction.response.deferred == []
        assert calls == []
    finally:
        await app._close_resources()


@pytest.mark.asyncio
async def test_chat_still_prompts_through_extracted_consent_helper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, calls = await _build_learn_app(tmp_path, monkeypatch, consent_enabled=True)
    interaction = _Interaction(guild=False)
    try:
        command = app.bot.tree.get_command("chat")
        assert command is not None

        await cast(Any, command).callback(cast(Any, interaction), "hello")

        assert interaction.response.deferred == []
        assert calls == []
        assert len(interaction.response.sent) == 1
        prompt = interaction.response.sent[0]
        assert prompt["ephemeral"] is True
        assert isinstance(prompt["view"], UserAppConsentView)
    finally:
        await app._close_resources()
