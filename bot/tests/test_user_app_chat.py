from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import discord
import pytest
from discord.ext import commands
from pydantic import ValidationError

from agent.activity import ActivityUpdate
from agent.core import ConversationRunRequest, ConversationRunResult
from agent.turn import TurnResult
from app.admission import TURN_ADMISSION_BUSY_MESSAGE, TurnAdmissionController
from app import user_app_turn_adapter
from commands.chat_cmd import register_user_app_chat_commands
from app import runtime as app_runtime
from config.fragments.prompt import resolve_template_path
from config.settings import Settings
from discord_adapter.interaction_io import (
    PartialPublicDeliveryError,
    send_interaction_result,
    send_interaction_status,
)
from storage.conversations import OWNER_ONLY, ConversationStore
from storage.db import Database
from providers.types import ContentPartType, ProviderCapability
from trust.tiers import TrustTier
from trust.user_app import UserAppAccess
from tools.registry import MessageContext
from workspace import user_app_workspace_key
from tests.helpers import StubProviderManager, make_settings


_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9aw"
    "AAAABJRU5ErkJggg=="
)


def test_user_app_access_uses_highest_overlapping_tier() -> None:
    access = UserAppAccess(
        member_ids=frozenset({"1", "2", "3"}),
        regular_ids=frozenset({"2", "3"}),
        staff_ids=frozenset({"3"}),
    )
    assert access.resolve("1") is TrustTier.MEMBER
    assert access.resolve("2") is TrustTier.REGULAR
    assert access.resolve("3") is TrustTier.STAFF
    assert access.resolve("4") is None


def test_user_app_settings_are_off_by_default_and_require_access() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        user_app_chat_enabled=False,
        user_app_dm_enabled=False,
        owner_user_id="",
        user_app_member_ids="",
        user_app_regular_ids="",
        user_app_staff_ids="",
    )
    assert settings.user_app_chat_enabled is False
    with pytest.raises(ValidationError, match="USER_APP_CHAT_ENABLED requires"):
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            user_app_chat_enabled=True,
            owner_user_id="",
            user_app_member_ids="",
            user_app_regular_ids="",
            user_app_staff_ids="",
        )


def test_owner_is_automatically_user_app_staff() -> None:
    settings = make_settings(
        user_app_chat_enabled=True,
        owner_user_id="42",
        user_app_member_ids="",
        user_app_regular_ids="",
        user_app_staff_ids="",
    )
    assert settings.user_app_staff_id_set == {"42"}


def test_owner_user_id_rejects_multiple_ids() -> None:
    with pytest.raises(ValidationError, match="OWNER_USER_ID must be one numeric"):
        Settings(_env_file=None, owner_user_id="1,2")  # type: ignore[call-arg]
    settings = Settings(_env_file=None, owner_user_id=" 42 ")  # type: ignore[call-arg]
    assert settings.owner_user_id == "42"


def test_user_app_workspace_is_global_per_user() -> None:
    assert user_app_workspace_key("123") == "123__userapp"
    assert user_app_workspace_key("123") != user_app_workspace_key("456")


def test_personal_tool_context_splits_location_from_data_scope() -> None:
    workspace = user_app_workspace_key("123")
    context = MessageContext(
        user_id="123",
        user_name="Alice",
        guild_id=None,
        channel_id="physical-channel",
        thread_id="physical-thread",
        trust_tier=TrustTier.REGULAR,
        workspace_key_override=workspace,
        personal_chat=True,
        platform_guild_id="physical-guild",
    )
    # The logical scope is guild-less; the physical location is metadata only.
    assert context.guild_id is None
    assert context.platform_guild_id == "physical-guild"
    assert context.channel_id == "physical-channel"
    assert context.thread_id == "physical-thread"
    assert context.workspace_key == workspace
    assert context.conversation_channel_id == "userapp"


def test_local_command_prompt_wins_over_tracked_default(tmp_path: Path) -> None:
    command_dir = tmp_path / "prompts" / "commands"
    command_dir.mkdir(parents=True)
    (tmp_path / "prompt.md").write_text("base", encoding="utf-8")
    tracked = command_dir / "chat.md"
    local = command_dir / "chat.local.md"
    tracked.write_text("tracked", encoding="utf-8")
    local.write_text("local", encoding="utf-8")
    assert (
        resolve_template_path(
            tmp_path,
            channel_id="",
            guild_id="",
            command_template="chat",
        )
        == local
    )


def test_chat_commands_are_user_install_only() -> None:
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())

    async def run_chat(*_args: object) -> None:
        return None

    async def reset_chat(_interaction: discord.Interaction) -> str:
        return "ok"

    register_user_app_chat_commands(
        bot,
        run_chat=run_chat,
        reset_chat=reset_chat,
        bot_name="Kimi",
    )
    chat_command = cast(Any, bot.tree.get_command("chat"))
    assert chat_command is not None
    assert chat_command.description == "Chat with Kimi"
    parameters = {parameter.name: parameter for parameter in chat_command.parameters}
    assert set(parameters) == {"message", "attachment", "visibility"}
    assert parameters["visibility"].required is False
    assert parameters["visibility"].default == "private"
    assert [(choice.name, choice.value) for choice in parameters["visibility"].choices] == [
        ("Only me", "private"),
        ("Everyone", "public"),
    ]
    for name in ("chat", "chat-reset"):
        command = bot.tree.get_command(name)
        assert command is not None
        installs = command.allowed_installs
        contexts = command.allowed_contexts
        assert installs is not None and installs.guild is False and installs.user is True
        assert contexts is not None
        assert contexts.guild is True
        assert contexts.dm_channel is True
        assert contexts.private_channel is True


def test_chat_command_description_respects_discord_limit() -> None:
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())

    async def run_chat(*_args: object) -> None:
        return None

    async def reset_chat(_interaction: discord.Interaction) -> str:
        return "ok"

    register_user_app_chat_commands(
        bot,
        run_chat=run_chat,
        reset_chat=reset_chat,
        bot_name="K" * 100,
    )

    command = cast(Any, bot.tree.get_command("chat"))
    assert command is not None
    assert command.description == f"Chat with {'K' * 90}"
    assert len(command.description) == 100


def test_user_app_public_post_uses_invoking_members_permissions() -> None:
    interaction = SimpleNamespace(
        guild_id=777,
        channel=object(),
        permissions=SimpleNamespace(
            send_messages=True,
            use_external_apps=True,
        ),
        app_permissions=SimpleNamespace(send_messages=False),
        is_user_integration=lambda: True,
        is_guild_integration=lambda: False,
    )

    assert (
        app_runtime._interaction_can_post_publicly(cast(discord.Interaction, interaction)) is True
    )


@pytest.mark.parametrize(
    ("send_messages", "use_external_apps"),
    [(False, True), (True, False)],
)
def test_user_app_public_post_requires_member_channel_and_external_app_permissions(
    send_messages: bool,
    use_external_apps: bool,
) -> None:
    interaction = SimpleNamespace(
        guild_id=777,
        channel=object(),
        permissions=SimpleNamespace(
            send_messages=send_messages,
            use_external_apps=use_external_apps,
        ),
        app_permissions=SimpleNamespace(send_messages=True),
        is_user_integration=lambda: True,
        is_guild_integration=lambda: False,
    )

    assert (
        app_runtime._interaction_can_post_publicly(cast(discord.Interaction, interaction)) is False
    )


@pytest.mark.parametrize(
    ("send_messages_in_threads", "expected"),
    [(True, True), (False, False)],
)
def test_user_app_public_post_uses_thread_permission(
    monkeypatch: pytest.MonkeyPatch,
    send_messages_in_threads: bool,
    expected: bool,
) -> None:
    class FakeThread:
        pass

    monkeypatch.setattr(discord, "Thread", FakeThread)
    interaction = SimpleNamespace(
        guild_id=777,
        channel=FakeThread(),
        permissions=SimpleNamespace(
            send_messages=False,
            send_messages_in_threads=send_messages_in_threads,
            use_external_apps=True,
        ),
        app_permissions=SimpleNamespace(send_messages_in_threads=False),
        is_user_integration=lambda: True,
        is_guild_integration=lambda: False,
    )

    assert (
        app_runtime._interaction_can_post_publicly(cast(discord.Interaction, interaction))
        is expected
    )


def test_dual_installed_app_public_post_uses_application_permissions() -> None:
    interaction = SimpleNamespace(
        guild_id=777,
        channel=object(),
        permissions=SimpleNamespace(
            send_messages=False,
            use_external_apps=False,
        ),
        app_permissions=SimpleNamespace(send_messages=True),
        is_user_integration=lambda: True,
        is_guild_integration=lambda: True,
    )

    assert (
        app_runtime._interaction_can_post_publicly(cast(discord.Interaction, interaction)) is True
    )


def test_user_app_public_post_is_allowed_outside_guilds() -> None:
    interaction = SimpleNamespace(guild_id=None)

    assert (
        app_runtime._interaction_can_post_publicly(cast(discord.Interaction, interaction)) is True
    )


@pytest.mark.asyncio
async def test_chat_visibility_choice_maps_to_internal_public_flag() -> None:
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    calls: list[bool] = []

    async def run_chat(
        _interaction: discord.Interaction,
        _message: str,
        _attachment: discord.Attachment | None,
        public: bool,
    ) -> None:
        calls.append(public)

    async def reset_chat(_interaction: discord.Interaction) -> str:
        return "ok"

    register_user_app_chat_commands(
        bot,
        run_chat=run_chat,
        reset_chat=reset_chat,
        bot_name="Kimi",
    )
    command = cast(Any, bot.tree.get_command("chat"))
    assert command is not None
    interaction = cast(discord.Interaction, object())

    await command.callback(interaction, "private")
    await command.callback(
        interaction,
        "public",
        visibility="public",
    )

    assert calls == [False, True]


class _UserAppImageAttachment:
    def __init__(self, *, unreadable: bool = False) -> None:
        self.filename = "photo.png"
        self.content_type = "application/octet-stream"
        self._payload = _PNG_1X1
        self.size = len(self._payload)
        self.unreadable = unreadable
        self.read_count = 0

    async def read(self) -> bytes:
        self.read_count += 1
        if self.unreadable:
            raise OSError("attachment expired")
        return self._payload


class _UserAppInteractionResponse:
    def __init__(self) -> None:
        self.deferred: list[dict[str, object]] = []
        self.sent: list[dict[str, object]] = []

    async def defer(self, **kwargs: object) -> None:
        self.deferred.append(kwargs)

    async def send_message(self, content: object = None, **kwargs: object) -> None:
        kwargs["content"] = content
        self.sent.append(kwargs)


class _UserAppImageInteraction:
    def __init__(self, interaction_id: int = 7, *, file_size_limit: int | None = None) -> None:
        self.id = interaction_id
        self.user = SimpleNamespace(id=42, display_name="Alice")
        guild = (
            SimpleNamespace(filesize_limit=file_size_limit) if file_size_limit is not None else None
        )
        self.channel = SimpleNamespace(guild=guild)
        self.channel_id = 99
        self.guild = None
        self.guild_id = None
        self.created_at = datetime.now(UTC)
        self.response = _UserAppInteractionResponse()
        self.edits: list[dict[str, object]] = []

    async def edit_original_response(self, **kwargs: object) -> None:
        self.edits.append(kwargs)


async def _user_app_chat_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    **setting_overrides: object,
) -> Any:
    monkeypatch.setattr(
        app_runtime,
        "build_provider_manager",
        lambda settings: StubProviderManager(settings),
    )
    settings_values: dict[str, object] = {
        "discord_bot_token": "token",
        "model_api_key": "key",
        "config_dir": str(tmp_path / "config"),
        "database_path": str(tmp_path / "runtime.db"),
        "workspace_dir": str(tmp_path / "workspaces"),
        "attachment_store_dir": str(tmp_path / "attachments"),
        "user_app_chat_enabled": True,
        "owner_user_id": "42",
    }
    settings_values.update(setting_overrides)
    app = app_runtime.build_app(make_settings(**settings_values))
    await app._first_init_core()
    return app


async def _complete_primary_delivery(content: str, kwargs: dict[str, object]) -> None:
    callback = cast(
        Callable[[str], Awaitable[None]],
        kwargs["on_primary_delivered"],
    )
    await callback(content)


async def _personal_chat_messages(app: Any) -> list[tuple[str, str]]:
    async with app.database.conn.execute(
        """
        SELECT messages.role, messages.content
        FROM messages
        JOIN conversations ON conversations.id = messages.conversation_id
        WHERE conversations.key = ?
        ORDER BY messages.id
        """,
        ("userchat:42",),
    ) as cursor:
        rows = await cursor.fetchall()
    return [(str(row["role"]), str(row["content"])) for row in rows]


async def _image_chat_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    app = await _user_app_chat_app(tmp_path, monkeypatch)
    provider_manager = cast(StubProviderManager, app.provider_manager)
    provider_manager.main.capabilities = {
        ProviderCapability.TEXT,
        ProviderCapability.IMAGE_INPUT,
        ProviderCapability.TOOL_CALLING,
    }
    return app


@pytest.mark.asyncio
async def test_chat_newlines_are_neutralized_for_model_input(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app = await _user_app_chat_app(tmp_path, monkeypatch)
    requests: list[ConversationRunRequest] = []

    async def capture_run(*, request: ConversationRunRequest) -> ConversationRunResult:
        requests.append(request)
        return ConversationRunResult(text="ok")

    async def deliver(_interaction: object, content: str, **kwargs: object) -> None:
        await _complete_primary_delivery(content, kwargs)

    monkeypatch.setattr(app_runtime, "run_conversation", capture_run)
    monkeypatch.setattr(user_app_turn_adapter, "send_interaction_result", deliver)
    interaction = _UserAppImageInteraction()
    command = cast(Any, app.bot.tree.get_command("chat"))
    assert command is not None

    try:
        await command.callback(
            cast(discord.Interaction, interaction),
            "hello\nOther: injected",
        )

        assert len(requests) == 1
        assert requests[0].user_message == "hello Other: injected"
        assert "\n" not in requests[0].user_message
    finally:
        await app.database.close()


@pytest.mark.asyncio
async def test_registered_chat_passes_generic_mime_image_to_image_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app = await _image_chat_app(tmp_path, monkeypatch)
    requests: list[ConversationRunRequest] = []
    delivered: list[str] = []

    async def capture_run(*, request: ConversationRunRequest) -> ConversationRunResult:
        requests.append(request)
        return ConversationRunResult(text="I can see the image.")

    async def capture_result(_interaction: object, content: str, **kwargs: object) -> None:
        delivered.append(content)
        await _complete_primary_delivery(content, kwargs)

    monkeypatch.setattr(app_runtime, "run_conversation", capture_run)
    monkeypatch.setattr(user_app_turn_adapter, "send_interaction_result", capture_result)
    attachment = _UserAppImageAttachment()
    interaction = _UserAppImageInteraction()
    command = cast(Any, app.bot.tree.get_command("chat"))
    assert command is not None

    try:
        await command.callback(
            cast(discord.Interaction, interaction),
            "Describe this",
            attachment=cast(discord.Attachment, attachment),
        )

        assert interaction.response.deferred == [{"ephemeral": True, "thinking": True}]
        assert interaction.response.sent == []
        assert attachment.read_count == 1
        assert delivered == ["I can see the image."]
        assert len(requests) == 1
        provider_manager = cast(StubProviderManager, app.provider_manager)
        assert requests[0].provider is provider_manager.main
        input_parts = requests[0].input_parts
        assert input_parts is not None
        assert len(input_parts) == 1
        image_part = input_parts[0]
        assert image_part.type is ContentPartType.IMAGE
        assert image_part.media_type == "image/png"
        assert image_part.image_url is not None
        header, encoded = image_part.image_url.split(",", maxsplit=1)
        assert header == "data:image/png;base64"
        assert base64.b64decode(encoded, validate=True) == _PNG_1X1
        assert not list((tmp_path / "attachments").rglob("photo.png"))
    finally:
        await app.database.close()


@pytest.mark.asyncio
async def test_registered_chat_reports_unreadable_image_without_provider_or_transcript(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app = await _image_chat_app(tmp_path, monkeypatch)
    requests: list[ConversationRunRequest] = []
    delivered: list[str] = []

    async def capture_run(*, request: ConversationRunRequest) -> ConversationRunResult:
        requests.append(request)
        return ConversationRunResult(text="unexpected")

    async def capture_result(_interaction: object, content: str, **kwargs: object) -> None:
        delivered.append(content)
        await _complete_primary_delivery(content, kwargs)

    monkeypatch.setattr(app_runtime, "run_conversation", capture_run)
    monkeypatch.setattr(user_app_turn_adapter, "send_interaction_result", capture_result)
    attachment = _UserAppImageAttachment(unreadable=True)
    interaction = _UserAppImageInteraction()
    command = cast(Any, app.bot.tree.get_command("chat"))
    assert command is not None

    try:
        await command.callback(
            cast(discord.Interaction, interaction),
            "Describe this",
            attachment=cast(discord.Attachment, attachment),
        )

        assert attachment.read_count == 1
        assert requests == []
        assert delivered == [
            (
                "I couldn't read the attached image. Re-upload it as a valid PNG, JPEG, GIF, "
                "or WebP within the attachment size limit."
            )
        ]
        async with app.database.conn.execute(
            """
            SELECT messages.role
            FROM messages
            JOIN conversations ON conversations.id = messages.conversation_id
            WHERE conversations.key = ?
            """,
            ("userchat:42",),
        ) as cursor:
            rows = await cursor.fetchall()
        assert rows == []
        assert not list((tmp_path / "attachments").rglob("photo.png"))
    finally:
        await app.database.close()


@pytest.mark.asyncio
async def test_public_result_edits_original_then_uses_public_followups() -> None:
    class Followup:
        def __init__(self) -> None:
            self.messages: list[dict[str, object]] = []

        async def send(self, content: object = None, **kwargs: object) -> None:
            kwargs["content"] = content
            self.messages.append(kwargs)

    class FakeInteraction:
        def __init__(self) -> None:
            self.channel = object()
            self.followup = Followup()
            self.edits: list[dict[str, object]] = []
            self.deleted = False

        async def edit_original_response(self, **kwargs: object) -> None:
            self.edits.append(kwargs)

        async def delete_original_response(self) -> None:
            self.deleted = True

    interaction = FakeInteraction()
    await send_interaction_result(
        interaction,  # type: ignore[arg-type]
        "A" * 2500,
        ephemeral=False,
        original_ephemeral=False,
    )

    assert interaction.deleted is False
    assert len(interaction.edits) == 1
    assert str(interaction.edits[0]["content"]).startswith("A")
    assert interaction.edits[0]["content"] != "Posted the response publicly."
    assert len(interaction.followup.messages) == 1
    assert interaction.followup.messages[0]["ephemeral"] is False


@pytest.mark.asyncio
async def test_partial_public_delivery_keeps_primary_response() -> None:
    class Followup:
        def __init__(self) -> None:
            self.calls = 0

        async def send(self, _content: object = None, **_kwargs: object) -> None:
            self.calls += 1
            if self.calls == 2:
                response = SimpleNamespace(status=500, reason="Server Error")
                raise discord.HTTPException(response, "followup failed")  # type: ignore[arg-type]

    class FakeInteraction:
        def __init__(self) -> None:
            self.channel = object()
            self.followup = Followup()
            self.edits: list[dict[str, object]] = []
            self.deleted = False

        async def edit_original_response(self, **kwargs: object) -> None:
            self.edits.append(kwargs)

        async def delete_original_response(self) -> None:
            self.deleted = True

    interaction = FakeInteraction()
    with pytest.raises(PartialPublicDeliveryError):
        await send_interaction_result(
            interaction,  # type: ignore[arg-type]
            "A" * 4500,
            ephemeral=False,
            original_ephemeral=False,
        )

    assert interaction.deleted is False
    assert len(interaction.edits) == 1
    assert str(interaction.edits[0]["content"]).startswith("A")


@pytest.mark.asyncio
async def test_private_status_replaces_public_placeholder_with_followup() -> None:
    class Followup:
        def __init__(self) -> None:
            self.messages: list[dict[str, object]] = []

        async def send(self, content: object = None, **kwargs: object) -> None:
            kwargs["content"] = content
            self.messages.append(kwargs)

    class FakeInteraction:
        def __init__(self) -> None:
            self.followup = Followup()
            self.edits: list[dict[str, object]] = []
            self.deleted = False

        async def edit_original_response(self, **kwargs: object) -> None:
            self.edits.append(kwargs)

        async def delete_original_response(self) -> None:
            self.deleted = True

    interaction = FakeInteraction()
    await send_interaction_status(
        interaction,  # type: ignore[arg-type]
        "Private failure",
        ephemeral=True,
        original_ephemeral=False,
    )

    assert interaction.deleted is True
    assert interaction.edits == []
    assert interaction.followup.messages[0]["content"] == "Private failure"
    assert interaction.followup.messages[0]["ephemeral"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("requested_public", [False, True])
async def test_user_app_status_keeps_selected_visibility(
    monkeypatch: pytest.MonkeyPatch,
    requested_public: bool,
) -> None:
    calls: list[tuple[bool, bool]] = []

    async def send_status(
        _interaction: object,
        _content: str,
        *,
        ephemeral: bool,
        original_ephemeral: bool,
    ) -> None:
        calls.append((ephemeral, original_ephemeral))

    monkeypatch.setattr(app_runtime, "send_interaction_status", send_status)

    await app_runtime._send_user_app_status(
        cast(discord.Interaction, object()),
        "Turn failed.",
        requested_public=requested_public,
    )

    assert calls == [(not requested_public, not requested_public)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "expected", "sends_result"),
    [
        ("result", "The provider failed after I started.", True),
        ("exception", "I couldn't complete that chat turn. Please try again.", False),
        ("timeout", "That personal chat turn timed out. Run `/chat` again to retry.", False),
        ("cancel", "Stopped.", False),
    ],
)
async def test_public_personal_chat_finishes_activity_before_every_outcome(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    outcome: str,
    expected: str,
    sends_result: bool,
) -> None:
    monkeypatch.setattr(
        app_runtime,
        "build_provider_manager",
        lambda settings: StubProviderManager(settings),
    )
    app = app_runtime.build_app(
        Settings.model_validate(
            {
                "discord_bot_token": "token",
                "model_api_key": "key",
                "config_dir": str(tmp_path / "config"),
                "database_path": str(tmp_path / "runtime.db"),
                "user_app_chat_enabled": True,
                "user_app_chat_timeout_seconds": 1 if outcome == "timeout" else 840,
                "owner_user_id": "42",
            }
        )
    )
    await app._first_init_core()

    class FakeInteraction:
        def __init__(self) -> None:
            self.id = 7
            self.user = SimpleNamespace(id=42, display_name="Alice")
            self.channel = SimpleNamespace(guild=None)
            self.channel_id = 99
            self.guild = None
            self.guild_id = None
            self.created_at = datetime.now(UTC)
            self.edits: list[str] = []

        async def edit_original_response(self, **kwargs: object) -> None:
            self.edits.append(str(kwargs.get("content", "")))

    reporters: list[Any] = []
    turn_started = asyncio.Event()

    async def failed_turn(
        _turn_input: object,
        *,
        dependencies: object,
        **_kwargs: object,
    ) -> TurnResult:
        reporter = dependencies.activity_reporter  # type: ignore[attr-defined]
        assert reporter is not None
        reporters.append(reporter)
        await reporter(ActivityUpdate(label="Thinking..."))
        turn_started.set()
        if outcome == "result":
            return TurnResult(
                response_text=expected,
                termination_reason="provider_error",
            )
        if outcome == "exception":
            raise RuntimeError("provider exploded")
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    delivered: list[tuple[bool, bool]] = []
    real_send_result = user_app_turn_adapter.send_interaction_result

    async def record_result(*args: object, **kwargs: object) -> None:
        delivered.append((bool(kwargs["ephemeral"]), bool(kwargs["original_ephemeral"])))
        await real_send_result(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(app_runtime, "handle_turn", failed_turn)
    monkeypatch.setattr(user_app_turn_adapter, "send_interaction_result", record_result)
    interaction = FakeInteraction()

    try:
        task = asyncio.create_task(
            app._execute_user_app_chat(
                interaction,  # type: ignore[arg-type]
                message="hello",
                attachment=None,
                public=True,
                request_generation=app._user_app_chat_generation("42"),
            )
        )
        if outcome == "cancel":
            await turn_started.wait()
            task.cancel()
        await task
        assert reporters
        await reporters[0](ActivityUpdate(label="Late update"))

        assert interaction.edits == [
            "Thinking...",
            expected,
        ]
        assert delivered == ([(False, False)] if sends_result else [])
    finally:
        await app.database.close()


@pytest.mark.asyncio
async def test_same_root_chat_delivery_finishes_before_next_turn_starts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app = await _user_app_chat_app(tmp_path, monkeypatch)
    events: list[str] = []
    first_body_started = asyncio.Event()
    release_first_body = asyncio.Event()
    first_delivery_started = asyncio.Event()
    release_first_delivery = asyncio.Event()
    turn_count = 0

    async def run_turn(*_args: object, **_kwargs: object) -> TurnResult:
        nonlocal turn_count
        turn_count += 1
        turn_number = turn_count
        events.append(f"turn:{turn_number}:start")
        if turn_number == 1:
            first_body_started.set()
            await release_first_body.wait()
        events.append(f"turn:{turn_number}:end")
        return TurnResult(response_text=f"reply-{turn_number}")

    async def deliver(_interaction: object, content: str, **kwargs: object) -> None:
        events.append(f"deliver:{content}:start")
        if content == "reply-1":
            first_delivery_started.set()
            await release_first_delivery.wait()
        await _complete_primary_delivery(content, kwargs)
        events.append(f"deliver:{content}:end")

    monkeypatch.setattr(app_runtime, "handle_turn", run_turn)
    monkeypatch.setattr(user_app_turn_adapter, "send_interaction_result", deliver)
    first = asyncio.create_task(
        app._execute_user_app_chat(
            _UserAppImageInteraction(7),  # type: ignore[arg-type]
            message="first",
            attachment=None,
            public=False,
            request_generation=app._user_app_chat_generation("42"),
        )
    )
    await asyncio.wait_for(first_body_started.wait(), timeout=0.5)
    second = asyncio.create_task(
        app._execute_user_app_chat(
            _UserAppImageInteraction(8),  # type: ignore[arg-type]
            message="second",
            attachment=None,
            public=False,
            request_generation=app._user_app_chat_generation("42"),
        )
    )

    try:
        release_first_body.set()
        await asyncio.wait_for(first_delivery_started.wait(), timeout=0.5)
        await asyncio.sleep(0)
        assert events == [
            "turn:1:start",
            "turn:1:end",
            "deliver:reply-1:start",
        ]

        release_first_delivery.set()
        await asyncio.wait_for(asyncio.gather(first, second), timeout=0.5)
        assert events == [
            "turn:1:start",
            "turn:1:end",
            "deliver:reply-1:start",
            "deliver:reply-1:end",
            "turn:2:start",
            "turn:2:end",
            "deliver:reply-2:start",
            "deliver:reply-2:end",
        ]
    finally:
        release_first_body.set()
        release_first_delivery.set()
        for task in (first, second):
            if not task.done():
                task.cancel()
        await asyncio.gather(first, second, return_exceptions=True)
        await app.database.close()


@pytest.mark.asyncio
async def test_chat_admission_stays_active_through_delivery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app = await _user_app_chat_app(tmp_path, monkeypatch)
    delivery_started = asyncio.Event()
    release_delivery = asyncio.Event()

    async def run_turn(*_args: object, **_kwargs: object) -> TurnResult:
        return TurnResult(response_text="reply")

    async def deliver(_interaction: object, content: str, **kwargs: object) -> None:
        delivery_started.set()
        await release_delivery.wait()
        await _complete_primary_delivery(content, kwargs)

    monkeypatch.setattr(app_runtime, "handle_turn", run_turn)
    monkeypatch.setattr(user_app_turn_adapter, "send_interaction_result", deliver)
    task = asyncio.create_task(
        app._execute_user_app_chat(
            _UserAppImageInteraction(),  # type: ignore[arg-type]
            message="hello",
            attachment=None,
            public=False,
            request_generation=app._user_app_chat_generation("42"),
        )
    )

    try:
        await asyncio.wait_for(delivery_started.wait(), timeout=0.5)
        snapshot = await app.turn_admission.snapshot()
        assert snapshot.active_total == 1
        assert snapshot.active_by_user == {"42": 1}

        release_delivery.set()
        await asyncio.wait_for(task, timeout=0.5)
        assert (await app.turn_admission.snapshot()).active_total == 0
    finally:
        release_delivery.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await app.database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown", [False, True], ids=["capacity", "shutdown"])
async def test_chat_admission_maps_shutdown_without_busy_status(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    shutdown: bool,
) -> None:
    app = await _user_app_chat_app(tmp_path, monkeypatch)
    app.turn_admission = TurnAdmissionController(max_active=1, max_active_per_user=1)
    held_lease = None
    if shutdown:
        await app.turn_admission.close()
    else:
        admission = await app.turn_admission.try_acquire("42")
        assert admission.lease is not None
        held_lease = admission.lease

    provider_calls: list[object] = []

    async def run_turn(*args: object, **_kwargs: object) -> TurnResult:
        provider_calls.extend(args)
        return TurnResult(response_text="unexpected")

    monkeypatch.setattr(app_runtime, "handle_turn", run_turn)
    interaction = _UserAppImageInteraction()

    try:
        with caplog.at_level("INFO", logger=app_runtime.__name__):
            result = await app._execute_user_app_chat(
                interaction,  # type: ignore[arg-type]
                message="hello",
                attachment=None,
                public=False,
                request_generation=app._user_app_chat_generation("42"),
            )

        assert result is None
        assert provider_calls == []
        if shutdown:
            assert interaction.edits == []
            assert "during shutdown" in caplog.text
        else:
            assert interaction.edits[-1]["content"] == TURN_ADMISSION_BUSY_MESSAGE
    finally:
        if held_lease is not None:
            await held_lease.release()
        await app.database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("gate_change", "expected_status"),
    [
        ("access", "You no longer have access to this app's personal chat."),
        ("block", "You can't use personal chat right now."),
    ],
)
async def test_chat_rechecks_access_and_block_after_waiting_for_root_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    gate_change: str,
    expected_status: str,
) -> None:
    app = await _user_app_chat_app(tmp_path, monkeypatch)
    provider_calls: list[object] = []

    async def run_turn(*args: object, **_kwargs: object) -> TurnResult:
        provider_calls.extend(args)
        return TurnResult(response_text="unexpected")

    class BlockedNow:
        async def is_blocked(self, _user_id: str) -> bool:
            return True

    monkeypatch.setattr(app_runtime, "handle_turn", run_turn)
    interaction = _UserAppImageInteraction()
    root_key = "userchat:42"
    task: asyncio.Task[TurnResult | None] | None = None

    try:
        async with app._root_lock(root_key):
            task = asyncio.create_task(
                app._execute_user_app_chat(
                    interaction,  # type: ignore[arg-type]
                    message="hello",
                    attachment=None,
                    public=False,
                    request_generation=app._user_app_chat_generation("42"),
                )
            )
            deadline = asyncio.get_running_loop().time() + 0.5
            while app._lock_refcounts.get(root_key, 0) < 2:
                if asyncio.get_running_loop().time() > deadline:
                    raise AssertionError("personal chat turn never queued on the root lock")
                await asyncio.sleep(0.005)

            if gate_change == "access":
                app.user_app_access = UserAppAccess(
                    member_ids=frozenset(),
                    regular_ids=frozenset(),
                    staff_ids=frozenset(),
                )
            else:
                app.blocked_user_store = cast(Any, BlockedNow())

        assert task is not None
        result = await asyncio.wait_for(task, timeout=0.5)
        assert result is None
        assert provider_calls == []
        assert interaction.edits[-1]["content"] == expected_status
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await app.database.close()


@pytest.mark.asyncio
async def test_chat_delivery_failure_does_not_persist_assistant(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app = await _user_app_chat_app(tmp_path, monkeypatch)

    async def run_turn(*_args: object, **_kwargs: object) -> TurnResult:
        return TurnResult(response_text="undelivered reply")

    async def fail_delivery(*_args: object, **_kwargs: object) -> None:
        response = SimpleNamespace(status=500, reason="Server Error")
        raise discord.HTTPException(response, "delivery failed")  # type: ignore[arg-type]

    monkeypatch.setattr(app_runtime, "handle_turn", run_turn)
    monkeypatch.setattr(user_app_turn_adapter, "send_interaction_result", fail_delivery)
    interaction = _UserAppImageInteraction()

    try:
        result = await app._execute_user_app_chat(
            interaction,  # type: ignore[arg-type]
            message="hello",
            attachment=None,
            public=False,
            request_generation=app._user_app_chat_generation("42"),
        )

        assert result is not None
        assert result.delivery_failed is True
        assert not any(role == "assistant" for role, _content in await _personal_chat_messages(app))
        assert interaction.edits[-1]["content"] == (
            "I finished the turn but couldn't deliver the response here. Try again privately."
        )
    finally:
        await app.database.close()


@pytest.mark.asyncio
async def test_file_only_chat_persists_delivered_attachment_notice(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app = await _user_app_chat_app(tmp_path, monkeypatch)
    output = tmp_path / "artifact.txt"
    output.write_text("too large", encoding="utf-8")
    workspace_key = user_app_workspace_key("42")

    async def run_turn(*_args: object, **_kwargs: object) -> TurnResult:
        return TurnResult(
            response_text="",
            output_files=(str(output),),
            allowed_file_roots=(tmp_path,),
            workspace_key=workspace_key,
        )

    monkeypatch.setattr(app_runtime, "handle_turn", run_turn)
    interaction = _UserAppImageInteraction(file_size_limit=1)

    try:
        result = await app._execute_user_app_chat(
            interaction,  # type: ignore[arg-type]
            message="make a file",
            attachment=None,
            public=False,
            request_generation=app._user_app_chat_generation("42"),
        )

        assert result is not None
        assert result.delivery_failed is False
        delivered_content = str(interaction.edits[-1]["content"])
        assert delivered_content.startswith("Delivery notice:")
        assert await _personal_chat_messages(app) == [("assistant", delivered_content)]
    finally:
        await app.database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("output_files", "waits_for_writer"),
    [((), False), (("artifact.txt",), True)],
    ids=["text-only", "file"],
)
async def test_chat_file_delivery_uses_workspace_activity_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    output_files: tuple[str, ...],
    waits_for_writer: bool,
) -> None:
    app = await _user_app_chat_app(tmp_path, monkeypatch)
    workspace_key = user_app_workspace_key("42")
    turn_finished = asyncio.Event()
    delivery_started = asyncio.Event()

    async def run_turn(*_args: object, **_kwargs: object) -> TurnResult:
        turn_finished.set()
        return TurnResult(
            response_text="reply",
            output_files=output_files,
            workspace_key=workspace_key,
        )

    async def deliver(_interaction: object, content: str, **kwargs: object) -> None:
        delivery_started.set()
        await _complete_primary_delivery(content, kwargs)

    monkeypatch.setattr(app_runtime, "handle_turn", run_turn)
    monkeypatch.setattr(user_app_turn_adapter, "send_interaction_result", deliver)
    task: asyncio.Task[TurnResult | None] | None = None

    try:
        async with app.tools.workspace_locks.writer(workspace_key):
            task = asyncio.create_task(
                app._execute_user_app_chat(
                    _UserAppImageInteraction(),  # type: ignore[arg-type]
                    message="hello",
                    attachment=None,
                    public=False,
                    request_generation=app._user_app_chat_generation("42"),
                )
            )
            await asyncio.wait_for(turn_finished.wait(), timeout=0.5)
            if waits_for_writer:
                await asyncio.sleep(0)
                assert delivery_started.is_set() is False
                assert task.done() is False
            else:
                await asyncio.wait_for(delivery_started.wait(), timeout=0.5)

        assert task is not None
        await asyncio.wait_for(task, timeout=0.5)
        assert delivery_started.is_set() is True
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await app.database.close()


@pytest.mark.asyncio
async def test_chat_reset_deletes_only_exact_owner_root(tmp_path: Path) -> None:
    db = Database(tmp_path / "chat-reset.db")
    await db.connect()
    try:
        store = ConversationStore(db)
        mine = await store.get_or_create(
            "userchat:1",
            "Personal chat",
            owner_user_id="1",
            access_scope=OWNER_ONLY,
        )
        theirs = await store.get_or_create(
            "userchat:2",
            "Personal chat",
            owner_user_id="2",
            access_scope=OWNER_ONLY,
        )
        assert mine != theirs
        assert await store.delete_owner_conversation("userchat:1", "2") is False
        assert await store.delete_owner_conversation("userchat:1", "1") is True
        async with db.conn.execute("SELECT key FROM conversations ORDER BY key") as cursor:
            keys = [str(row["key"]) for row in await cursor.fetchall()]
        assert keys == ["userchat:2"]
        assert await store.delete_owner_conversation("userchat:1", "1") is False
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_runtime_registers_user_commands_only_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        app_runtime,
        "build_provider_manager",
        lambda settings: StubProviderManager(settings),
    )

    off = app_runtime.build_app(
        Settings.model_validate(
            {
                "discord_bot_token": "token",
                "model_api_key": "key",
                "config_dir": str(tmp_path / "off-config"),
                "database_path": str(tmp_path / "off.db"),
                "user_app_chat_enabled": False,
                "user_app_dm_enabled": False,
            }
        )
    )
    await off._first_init_core()
    assert off.bot.tree.get_command("chat") is None
    assert off.bot.tree.get_command("chat-reset") is None
    assert off.bot.tree.allowed_installs.guild is True
    assert off.bot.tree.allowed_installs.user is False
    await off.database.close()

    on = app_runtime.build_app(
        Settings.model_validate(
            {
                "discord_bot_token": "token",
                "model_api_key": "key",
                "config_dir": str(tmp_path / "on-config"),
                "database_path": str(tmp_path / "on.db"),
                "user_app_chat_enabled": True,
                "owner_user_id": "42",
            }
        )
    )
    await on._first_init_core()
    for name in ("chat", "chat-reset", "privacy", "memory", "stop"):
        command = on.bot.tree.get_command(name)
        assert command is not None
        assert command.allowed_installs is not None
        assert command.allowed_installs.user is True
    await on.database.close()


@pytest.mark.asyncio
async def test_reset_drains_full_chat_lifecycle_and_invalidates_older_requests(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        app_runtime,
        "build_provider_manager",
        lambda settings: StubProviderManager(settings),
    )
    app = app_runtime.build_app(
        Settings.model_validate(
            {
                "discord_bot_token": "token",
                "model_api_key": "key",
                "config_dir": str(tmp_path / "config"),
                "database_path": str(tmp_path / "runtime.db"),
                "user_app_chat_enabled": True,
                "owner_user_id": "42",
            }
        )
    )
    await app._first_init_core()

    class FakeInteraction:
        def __init__(self, interaction_id: int) -> None:
            self.id = interaction_id
            self.user = type("User", (), {"id": 42, "display_name": "Alice"})()
            self.edits: list[str] = []
            self.followups: list[str] = []
            self.deleted = False

            class Followup:
                async def send(
                    _self,
                    content: object = None,
                    **_kwargs: object,
                ) -> None:
                    self.followups.append(str(content or ""))

            self.followup = Followup()

        async def edit_original_response(self, **kwargs: object) -> None:
            self.edits.append(str(kwargs.get("content", "")))

        async def delete_original_response(self) -> None:
            self.deleted = True

    entered_delivery = asyncio.Event()
    calls = 0

    async def wait_in_delivery(_interaction: object, **_kwargs: object) -> None:
        nonlocal calls
        calls += 1
        entered_delivery.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(app, "_run_user_app_chat_turn", wait_in_delivery)
    interaction = FakeInteraction(1)
    generation = app._user_app_chat_generation("42")
    task = asyncio.create_task(
        app._execute_user_app_chat(
            interaction,  # type: ignore[arg-type]
            message="hello",
            attachment=None,
            public=True,
            request_generation=generation,
        )
    )
    await entered_delivery.wait()

    summary = await app._handle_user_app_chat_reset(interaction)  # type: ignore[arg-type]
    await task

    assert summary == "Your personal chat thread is already clear."
    assert interaction.deleted is False
    assert interaction.edits == ["Stopped."]
    assert interaction.followups == []
    assert calls == 1

    stale = FakeInteraction(2)
    await app._execute_user_app_chat(
        stale,  # type: ignore[arg-type]
        message="old retained request",
        attachment=None,
        public=False,
        request_generation=generation,
    )
    assert calls == 1
    assert "expired because your personal thread was reset or deleted" in stale.edits[-1]
    await app.database.close()


@pytest.mark.asyncio
async def test_personal_chat_timeout_covers_wait_before_turn_execution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        app_runtime,
        "build_provider_manager",
        lambda settings: StubProviderManager(settings),
    )
    app = app_runtime.build_app(
        Settings.model_validate(
            {
                "discord_bot_token": "token",
                "model_api_key": "key",
                "config_dir": str(tmp_path / "config"),
                "database_path": str(tmp_path / "runtime.db"),
                "user_app_chat_enabled": True,
                "user_app_chat_timeout_seconds": 1,
                "owner_user_id": "42",
            }
        )
    )
    await app._first_init_core()

    class FakeInteraction:
        def __init__(self) -> None:
            self.id = 1
            self.user = type("User", (), {"id": 42, "display_name": "Alice"})()
            self.edits: list[str] = []

        async def edit_original_response(self, **kwargs: object) -> None:
            self.edits.append(str(kwargs.get("content", "")))

    async def wait_forever(_interaction: object, **_kwargs: object) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(app, "_run_user_app_chat_turn", wait_forever)
    interaction = FakeInteraction()
    await app._execute_user_app_chat(
        interaction,  # type: ignore[arg-type]
        message="hello",
        attachment=None,
        public=False,
        request_generation=app._user_app_chat_generation("42"),
    )

    assert interaction.edits == ["That personal chat turn timed out. Run `/chat` again to retry."]
    await app.database.close()


@pytest.mark.asyncio
async def test_stop_current_reaches_personal_chat_for_dual_installed_app(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A guild+user installed app reports both integration owners, so /stop must
    still reach the personal root instead of only the invoking channel."""
    monkeypatch.setattr(
        app_runtime,
        "build_provider_manager",
        lambda settings: StubProviderManager(settings),
    )
    app = app_runtime.build_app(
        Settings.model_validate(
            {
                "discord_bot_token": "token",
                "model_api_key": "key",
                "config_dir": str(tmp_path / "config"),
                "database_path": str(tmp_path / "runtime.db"),
                "user_app_chat_enabled": True,
                "owner_user_id": "42",
            }
        )
    )
    await app._first_init_core()

    recorded: list[tuple[str, str | None, bool]] = []

    async def record_cancel(
        *,
        user_id: str,
        root_key: str | None,
        channel_id: str,
        all_operations: bool,
        wait_seconds: float,
    ) -> tuple[int, bool]:
        recorded.append((channel_id, root_key, all_operations))
        return 0, True

    monkeypatch.setattr(app.active_operations, "cancel", record_cancel)

    class FakeInteraction:
        def __init__(self, *, user_install: bool, guild_install: bool) -> None:
            self.user = type("User", (), {"id": 42, "display_name": "Alice"})()
            self.channel_id = 555
            self.guild_id = 777
            self._user_install = user_install
            self._guild_install = guild_install

        def is_user_integration(self) -> bool:
            return self._user_install

        def is_guild_integration(self) -> bool:
            return self._guild_install

    dual = FakeInteraction(user_install=True, guild_install=True)
    await app._handle_stop_interaction(dual, False, None)  # type: ignore[arg-type]
    assert ("userapp", "userchat:42", False) in recorded
    assert ("555", None, False) in recorded

    recorded.clear()
    user_only = FakeInteraction(user_install=True, guild_install=False)
    await app._handle_stop_interaction(user_only, False, None)  # type: ignore[arg-type]
    assert recorded == [("userapp", "userchat:42", False)]

    recorded.clear()
    guild_only = FakeInteraction(user_install=False, guild_install=True)
    await app._handle_stop_interaction(guild_only, False, None)  # type: ignore[arg-type]
    assert recorded == [("555", None, False)]

    await app.database.close()


@pytest.mark.asyncio
async def test_personal_chat_cancellation_propagates_during_shutdown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Shutdown cancels every active operation; the personal chat handler must not
    absorb that cancellation or await another edit on the closing client."""
    monkeypatch.setattr(
        app_runtime,
        "build_provider_manager",
        lambda settings: StubProviderManager(settings),
    )
    app = app_runtime.build_app(
        Settings.model_validate(
            {
                "discord_bot_token": "token",
                "model_api_key": "key",
                "config_dir": str(tmp_path / "config"),
                "database_path": str(tmp_path / "runtime.db"),
                "user_app_chat_enabled": True,
                "owner_user_id": "42",
            }
        )
    )
    await app._first_init_core()

    class FakeInteraction:
        def __init__(self) -> None:
            self.id = 1
            self.user = type("User", (), {"id": 42, "display_name": "Alice"})()
            self.edits: list[str] = []

        async def edit_original_response(self, **kwargs: object) -> None:
            self.edits.append(str(kwargs.get("content", "")))

    entered = asyncio.Event()

    async def wait_forever(_interaction: object, **_kwargs: object) -> None:
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(app, "_run_user_app_chat_turn", wait_forever)
    interaction = FakeInteraction()
    task = asyncio.create_task(
        app._execute_user_app_chat(
            interaction,  # type: ignore[arg-type]
            message="hello",
            attachment=None,
            public=False,
            request_generation=app._user_app_chat_generation("42"),
        )
    )
    await entered.wait()

    app._closed = True
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()
    assert interaction.edits == []

    await app.database.close()


def _personal_context(**overrides: object) -> MessageContext:
    base: dict[str, object] = {
        "user_id": "123",
        "user_name": "Alice",
        # Guild-less by design; the physical location travels separately.
        "guild_id": None,
        "channel_id": "physical-channel",
        "thread_id": None,
        "trust_tier": TrustTier.STAFF,
        "workspace_key_override": user_app_workspace_key("123"),
        "personal_chat": True,
        "platform_guild_id": "physical-guild",
    }
    base.update(overrides)
    return MessageContext(**base)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_personal_chat_cannot_dispatch_a_guild_scoped_tool() -> None:
    """Trust from USER_APP_* is granted outside any guild, so it must not unlock
    a tool scoped to the guild the slash command happened to be invoked from."""
    from tools.registry import ToolRegistry

    registry = ToolRegistry()

    async def handler(_args: dict, _ctx: MessageContext) -> str:
        return "ran"

    registry.register(
        name="guild_scoped_tool",
        description="scoped",
        parameters={},
        handler=handler,
        min_tier=TrustTier.MEMBER,
        guild_ids=frozenset({"physical-guild"}),
    )

    personal = _personal_context()
    # Masked as unknown so a scoped tool's existence never leaks.
    assert "Unknown tool: guild_scoped_tool" in await registry.dispatch(
        "guild_scoped_tool", {}, personal
    )

    # The same user in that guild for real still reaches it.
    in_guild = _personal_context(
        guild_id="physical-guild",
        personal_chat=False,
        workspace_key_override=None,
    )
    assert await registry.dispatch("guild_scoped_tool", {}, in_guild) == "ran"


def test_personal_chat_blocks_guild_artifact_tools() -> None:
    """Community memory and shared skills belong to a guild or the deployment.
    A guild-less personal turn has no coherent target for them."""
    from app.turn_entry import _PERSONAL_CHAT_BLOCKED_TOOLS

    for name in ("teach", "recall_community", "reflect_community"):
        assert name in _PERSONAL_CHAT_BLOCKED_TOOLS
    for name in ("skill_create", "skill_edit", "skill_delete"):
        assert name in _PERSONAL_CHAT_BLOCKED_TOOLS


@pytest.mark.asyncio
async def test_skill_create_refuses_personal_chat_instead_of_going_global() -> None:
    """Fail-closed second layer behind the denylist: a guild-less context must
    never fall into the "no guild means every guild" branch."""
    from tools import skills as skills_tools

    result = await skills_tools._skill_create(
        {"name": "x", "description": "d", "content": "c"},
        _personal_context(),
    )
    assert "only be managed from a server conversation" in result


def test_dm_surface_requires_the_chat_commands() -> None:
    """A DM-only deployment would hand out a personal transcript with no way to
    clear, cancel, or delete it: those commands ride on the /chat surface."""
    with pytest.raises(ValidationError, match="USER_APP_DM_ENABLED requires"):
        make_settings(
            user_app_chat_enabled=False,
            user_app_dm_enabled=True,
            owner_user_id="42",
            user_app_member_ids="",
            user_app_regular_ids="",
            user_app_staff_ids="",
        )
    ok = make_settings(
        user_app_chat_enabled=True,
        user_app_dm_enabled=True,
        owner_user_id="42",
        user_app_member_ids="",
        user_app_regular_ids="",
        user_app_staff_ids="",
    )
    assert ok.user_app_dm_enabled is True


def _dm_message(user_id: int, *, content: str = "hello", message_id: int = 7) -> object:
    channel = discord.DMChannel.__new__(discord.DMChannel)
    channel.id = 4242

    class _DMMessage:
        def __init__(self) -> None:
            self.id = message_id
            self.content = content
            self.channel = channel
            self.guild = None
            self.author = type(
                "User",
                (),
                {"id": user_id, "display_name": "Alice", "bot": False},
            )()
            self.type = discord.MessageType.default

    return _DMMessage()


async def _dm_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **overrides: object):
    monkeypatch.setattr(
        app_runtime,
        "build_provider_manager",
        lambda settings: StubProviderManager(settings),
    )
    config: dict[str, object] = {
        "discord_bot_token": "token",
        "model_api_key": "key",
        "config_dir": str(tmp_path / "config"),
        "database_path": str(tmp_path / "runtime.db"),
        "user_app_chat_enabled": True,
        "user_app_dm_enabled": True,
        "owner_user_id": "42",
    }
    config.update(overrides)
    app = app_runtime.build_app(Settings.model_validate(config))
    await app._first_init_core()
    return app


@pytest.mark.asyncio
async def test_dm_personal_chat_tier_gates_on_setting_and_allowlist(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app = await _dm_app(tmp_path, monkeypatch)
    # Owner is automatically user-app staff; nobody else is allowlisted here.
    assert app._dm_personal_chat_tier(_dm_message(42)) is TrustTier.STAFF
    assert app._dm_personal_chat_tier(_dm_message(999)) is None
    await app.database.close()

    off = await _dm_app(tmp_path / "off", monkeypatch, user_app_dm_enabled=False)
    assert off._dm_personal_chat_tier(_dm_message(42)) is None
    await off.database.close()


@pytest.mark.asyncio
async def test_dm_continues_the_owner_only_chat_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A DM after a /chat turn must resolve the existing owner-only root.

    ConversationStore rejects resolving an owner-only conversation under any
    other scope, so a DM that let access_scope default to channel-shared would
    raise PermissionError on every message after the first /chat turn.
    """
    app = await _dm_app(tmp_path, monkeypatch)
    store = app.conversation_store
    assert store is not None

    # Stand in for a prior /chat turn creating the personal root.
    await store.get_or_create(
        "userchat:42",
        "Personal chat",
        guild_id=None,
        channel_id="userapp",
        thread_id=None,
        root_discord_message_id="1",
        owner_user_id="42",
        access_scope=OWNER_ONLY,
    )

    resolved = await app._resolve_personal_dm_conversation(_dm_message(42))
    assert resolved is not None
    assert resolved.key == "userchat:42"
    assert resolved.access_scope == OWNER_ONLY
    assert resolved.owner_user_id == "42"

    # The default scope is exactly what the resolver must not fall back to.
    with pytest.raises(PermissionError):
        await store.get_or_create(
            "userchat:42",
            "Personal chat",
            guild_id=None,
            channel_id="userapp",
            thread_id=None,
            root_discord_message_id="2",
            owner_user_id="42",
        )
    await app.database.close()


@pytest.mark.asyncio
async def test_personal_dm_uses_chat_execution_policy_without_onboarding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app = await _dm_app(tmp_path, monkeypatch, new_user_onboarding_turns=9)
    captured: dict[str, Any] = {}

    async def capture_turn(
        turn_input: object,
        *,
        dependencies: object,
        preparation_config: object,
        execution_config: object,
    ) -> None:
        captured.update(
            turn_input=turn_input,
            dependencies=dependencies,
            preparation_config=preparation_config,
            execution_config=execution_config,
        )

    monkeypatch.setattr(app_runtime, "handle_turn", capture_turn)

    try:
        result = await app.handle_message(
            _dm_message(42),  # type: ignore[arg-type]
            lock_acquired=True,
        )

        assert result is None
        assert captured["execution_config"].command_template == "chat"
        assert captured["preparation_config"].new_user_onboarding_turns == 0
        assert captured["dependencies"].count_user_prior_messages is None
    finally:
        await app.database.close()


@pytest.mark.asyncio
async def test_dm_stop_targets_shared_personal_scope(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app = await _dm_app(tmp_path, monkeypatch)
    captured: dict[str, object] = {}

    async def add_status_reaction(_message: object, _emoji: str) -> None:
        return None

    async def cancel_user_work(**kwargs: object) -> str:
        captured.update(kwargs)
        return "Stopped."

    async def send_response(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(app.discord_gateway, "add_status_reaction", add_status_reaction)
    monkeypatch.setattr(app, "_cancel_user_work", cancel_user_work)
    monkeypatch.setattr(app, "send_response", send_response)

    await app._handle_stop_message(_dm_message(42, content="stop"))  # type: ignore[arg-type]

    assert captured == {
        "user_id": "42",
        "scopes": [("userapp", "userchat:42")],
        "all_work": False,
    }
    await app.database.close()


@pytest.mark.asyncio
async def test_dm_registers_provisional_work_in_personal_scope(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app = await _dm_app(tmp_path, monkeypatch)
    app.gateway_ready = True
    channels: list[str] = []
    original_register = app.active_operations.register_provisional

    @contextmanager
    def record_registration(**kwargs: object):
        channels.append(str(kwargs["channel_id"]))
        with original_register(**kwargs):  # type: ignore[arg-type]
            yield

    async def stop_after_registration(_message: object) -> None:
        return None

    monkeypatch.setattr(app.active_operations, "register_provisional", record_registration)
    monkeypatch.setattr(app, "_on_message_for_user", stop_after_registration)

    await app.on_message(_dm_message(42))  # type: ignore[arg-type]

    assert channels == ["userapp"]
    await app.database.close()
