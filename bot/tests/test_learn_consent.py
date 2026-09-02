"""Privacy-consent parity for the staff teaching context menu."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import discord
import pytest

import app.runtime as app_runtime
from app.user_app_consent import (
    UserAppConsentConfig,
    UserAppConsentPrompter,
    UserAppConsentView,
)
from commands.learn_cmd import learn_menu_name, register_learn_command
from tests.helpers import (
    LifecycleProbe,
    StubProviderManager,
    make_settings,
    replace_app_repositories,
)
from tools.learn import LearnTarget
from trust.resolver import TrustResolver

USER_ID = 42
GUILD_ID = 999
REPORT = "Saved to community knowledge under events."


class _Response:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []
        self.deferred: list[dict[str, object]] = []
        self.edited: list[dict[str, object]] = []

    def is_done(self) -> bool:
        return bool(self.sent or self.deferred)

    async def send_message(self, content: object = None, **kwargs: object) -> None:
        kwargs["content"] = content
        self.sent.append(kwargs)

    async def defer(self, **kwargs: object) -> None:
        self.deferred.append(kwargs)

    async def edit_message(self, **kwargs: object) -> None:
        self.edited.append(kwargs)


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


class _FailingConsentLookup:
    async def has_consented(self, _user_id: str) -> bool:
        raise RuntimeError("consent database unavailable")


class _PromptFailingResponse(_Response):
    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    async def send_message(self, content: object = None, **kwargs: object) -> None:
        self.attempts += 1
        if self.attempts == 1:
            raise RuntimeError("Discord prompt construction failed")
        await super().send_message(content, **kwargs)


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
        await LifecycleProbe(app).first_init_core()
        _bind_teach_consent(app, app.user_app_consent, calls)
    except BaseException:
        await app.close()
        raise
    return app, calls


def _consent_prompter(app: Any, store: object | None) -> UserAppConsentPrompter:
    return UserAppConsentPrompter(
        config=UserAppConsentConfig(
            enabled=app.settings.privacy_consent_enabled,
            title=app.settings.privacy_consent_title,
            text=app.settings.privacy_consent_text,
            timeout=app.settings.privacy_consent_timeout,
        ),
        preference_store=cast(Any, store),
    )


def _bind_teach_consent(
    app: Any,
    prompter: UserAppConsentPrompter,
    calls: list[tuple[LearnTarget, discord.Interaction]],
    *,
    trust_resolver: TrustResolver | None = None,
) -> None:
    async def capture_learn(
        target: LearnTarget,
        interaction: discord.Interaction,
    ) -> str:
        calls.append((target, interaction))
        return REPORT

    async def is_blocked(user_id: str) -> bool:
        if app.blocked_user_store is None:
            raise RuntimeError("blocked-user store is unavailable")
        return await app.blocked_user_store.is_blocked(user_id)

    register_learn_command(
        app.bot,
        trust_resolver or app.trust_resolver,
        run_learn=capture_learn,
        is_blocked=is_blocked,
        request_consent=lambda interaction, resume: prompter.prompt_if_needed(
            interaction,
            on_accept=resume,
            public_response=False,
        ),
        bot_name=app.settings.bot_name,
    )


def _teach_menu(app: Any) -> Any:
    menu = app.bot.tree.get_command(
        learn_menu_name(app.settings.bot_name),
        type=discord.AppCommandType.message,
    )
    assert menu is not None
    return menu


async def _assert_no_consent_state(app: Any) -> None:
    for table in ("messages", "conversations", "user_preferences"):
        async with app.database.conn.execute(f"SELECT COUNT(*) AS count FROM {table}") as cursor:
            row = await cursor.fetchone()
        assert row is not None and int(row["count"]) == 0


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
        await app.close()


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
        await app.close()


@pytest.mark.asyncio
async def test_teach_accept_rechecks_blocked_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, calls = await _build_learn_app(tmp_path, monkeypatch, consent_enabled=True)
    original = _Interaction()
    accepted = _Interaction()
    try:
        await _teach_menu(app).callback(cast(Any, original), cast(Any, _message()))
        assert app.blocked_user_store is not None
        await app.blocked_user_store.block_user(
            str(USER_ID),
            blocked_by="operator",
            reason="changed while consent was pending",
        )
        view = cast(UserAppConsentView, original.response.sent[0]["view"])
        accept_button = next(
            child
            for child in view.children
            if getattr(child, "label", None) == "Accept and continue"
        )

        await cast(Any, accept_button).callback(cast(discord.Interaction, accepted))

        assert calls == []
        assert [message["content"] for message in accepted.followup.sent] == [
            "You can't use this right now."
        ]
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_teach_accept_rechecks_staff_standing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, calls = await _build_learn_app(tmp_path, monkeypatch, consent_enabled=True)
    staff_ids = {str(USER_ID)}
    resolver = TrustResolver(set(), set(), staff_ids)
    _bind_teach_consent(app, app.user_app_consent, calls, trust_resolver=resolver)
    original = _Interaction()
    accepted = _Interaction()
    try:
        await _teach_menu(app).callback(cast(Any, original), cast(Any, _message()))
        staff_ids.clear()
        view = cast(UserAppConsentView, original.response.sent[0]["view"])
        accept_button = next(
            child
            for child in view.children
            if getattr(child, "label", None) == "Accept and continue"
        )

        await cast(Any, accept_button).callback(cast(discord.Interaction, accepted))

        assert calls == []
        assert [message["content"] for message in accepted.followup.sent] == ["Staff only."]
    finally:
        await app.close()


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
        await app.close()


@pytest.mark.asyncio
async def test_teach_disabled_gate_never_looks_up_consent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, calls = await _build_learn_app(tmp_path, monkeypatch, consent_enabled=False)
    interaction = _Interaction()
    try:
        _bind_teach_consent(
            app,
            _consent_prompter(app, _ConsentLookupForbidden()),
            calls,
        )

        await _teach_menu(app).callback(cast(Any, interaction), cast(Any, _message()))

        assert interaction.response.sent == []
        assert interaction.response.deferred == [{"ephemeral": True, "thinking": True}]
        assert len(calls) == 1
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_teach_blocked_staff_is_refused_before_consent_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, calls = await _build_learn_app(tmp_path, monkeypatch, consent_enabled=True)
    interaction = _Interaction()
    blocked = _BlockedStore()
    try:
        replace_app_repositories(app, blocked_user_store=cast(Any, blocked))
        _bind_teach_consent(
            app,
            _consent_prompter(app, _ConsentLookupForbidden()),
            calls,
        )

        await _teach_menu(app).callback(cast(Any, interaction), cast(Any, _message()))

        assert blocked.asked == [str(USER_ID)]
        assert interaction.response.sent[0]["content"] == "You can't use this right now."
        assert interaction.response.deferred == []
        assert calls == []
    finally:
        await app.close()


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
        await app.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "lookup", "prompt"])
async def test_teach_consent_failures_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    app, learn_calls = await _build_learn_app(tmp_path, monkeypatch, consent_enabled=True)
    interaction = _Interaction()

    if failure == "missing":
        prompter = _consent_prompter(app, None)
    elif failure == "lookup":
        prompter = _consent_prompter(app, _FailingConsentLookup())
    else:
        assert app.preference_store is not None
        prompter = _consent_prompter(app, app.preference_store)
        interaction.response = _PromptFailingResponse()
    _bind_teach_consent(app, prompter, learn_calls)

    try:
        await _teach_menu(app).callback(cast(Any, interaction), cast(Any, _message()))

        assert interaction.response.deferred == []
        assert learn_calls == []
        assert interaction.response.sent == [
            {
                "content": "I couldn't verify your privacy consent. Please try again.",
                "ephemeral": True,
            }
        ]
        await _assert_no_consent_state(app)
    finally:
        await app.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "lookup", "prompt"])
async def test_chat_consent_failures_stop_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """Both surfaces share the consent helper, but /chat is asserted directly
    rather than inferred from the teach menu."""

    app, learn_calls = await _build_learn_app(tmp_path, monkeypatch, consent_enabled=True)
    interaction = _Interaction(guild=False)
    execute_calls: list[discord.Interaction] = []

    async def capture_chat_execute(
        resume_interaction: discord.Interaction,
        **_kwargs: object,
    ) -> None:
        execute_calls.append(resume_interaction)

    if failure == "missing":
        prompter = _consent_prompter(app, None)
    elif failure == "lookup":
        prompter = _consent_prompter(app, _FailingConsentLookup())
    else:
        assert app.preference_store is not None
        prompter = _consent_prompter(app, app.preference_store)
        interaction.response = _PromptFailingResponse()
    monkeypatch.setattr(app.user_app_chat, "_consent", prompter)
    monkeypatch.setattr(app.user_app_chat, "run", capture_chat_execute)

    try:
        command = app.bot.tree.get_command("chat")
        assert command is not None
        await cast(Any, command).callback(cast(Any, interaction), "hello")

        assert interaction.response.deferred == []
        assert execute_calls == []
        assert learn_calls == []
        assert interaction.response.sent == [
            {
                "content": "I couldn't verify your privacy consent. Please try again.",
                "ephemeral": True,
            }
        ]
        await _assert_no_consent_state(app)
    finally:
        await app.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["write", "defer"])
async def test_user_app_consent_accept_failure_does_not_resume_request(failure: str) -> None:
    class FailingConsentStore:
        async def set_consent(self, _user_id: str, _granted: bool) -> bool:
            if failure == "write":
                raise RuntimeError("consent database unavailable")
            return True

    class FailingDeferResponse(_Response):
        async def defer(self, **_kwargs: object) -> None:
            raise RuntimeError("interaction defer failed")

    accepted: list[discord.Interaction] = []

    async def on_accept(interaction: discord.Interaction) -> None:
        accepted.append(interaction)

    view = UserAppConsentView(
        author_id=USER_ID,
        store=cast(Any, FailingConsentStore()),
        on_accept=on_accept,
        timeout=60.0,
    )
    button = next(
        child for child in view.children if getattr(child, "label", None) == "Accept and continue"
    )
    interaction = _Interaction()
    if failure == "defer":
        interaction.response = FailingDeferResponse()

    await cast(Any, button).callback(cast(discord.Interaction, interaction))

    assert accepted == []
    assert interaction.response.deferred == []
    expected = (
        "I couldn't save your privacy choice. Please try again."
        if failure == "write"
        # The write succeeded, so the user must not be told to retry a prompt
        # that will no longer be shown.
        else "Your privacy choice was saved, but I couldn't continue. Run the command again."
    )
    assert interaction.response.edited == [{"content": expected, "embed": None, "view": None}]


@pytest.mark.asyncio
async def test_teach_replies_when_blocked_user_lookup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing block lookup stays fail-closed but must still answer the click."""

    app, calls = await _build_learn_app(tmp_path, monkeypatch, consent_enabled=False)
    interaction = _Interaction()
    try:

        async def failing_is_blocked(_user_id: str) -> bool:
            raise RuntimeError("database is locked")

        assert app.blocked_user_store is not None
        monkeypatch.setattr(app.blocked_user_store, "is_blocked", failing_is_blocked)

        await _teach_menu(app).callback(cast(Any, interaction), cast(Any, _message()))

        assert calls == []
        replies = [message["content"] for message in interaction.response.sent] + [
            message["content"] for message in interaction.followup.sent
        ]
        assert replies == ["You can't use this right now."]
    finally:
        await app.close()
