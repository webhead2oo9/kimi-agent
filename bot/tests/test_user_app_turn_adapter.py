from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import discord
import pytest

from agent.activity import ActivityUpdate
from agent.turn import TurnPreparationInput, TurnResult
from app import user_app_turn_adapter
from app.foreground_turn import TurnSurfaceOutcome, TurnSurfaceOutcomeKind
from app.user_app_turn_adapter import UserAppInteractionTurnAdapter
from discord_adapter.interaction_io import PartialPublicDeliveryError
from tools.embeds import EmbedSpec


class FakeFollowup:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []

    async def send(self, content: object = None, **kwargs: object) -> None:
        kwargs["content"] = content
        self.messages.append(kwargs)


class FakeInteraction:
    def __init__(self, *, interaction_id: int = 73) -> None:
        self.id = interaction_id
        self.user = SimpleNamespace(id=42, display_name="Alice")
        self.channel = object()
        self.created_at = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)
        self.followup = FakeFollowup()
        self.edits: list[dict[str, object]] = []
        self.deleted = False

    async def edit_original_response(self, **kwargs: object) -> None:
        self.edits.append(kwargs)

    async def delete_original_response(self) -> None:
        self.deleted = True


def _adapter(
    interaction: FakeInteraction,
    *,
    requested_public: bool = False,
) -> UserAppInteractionTurnAdapter:
    return UserAppInteractionTurnAdapter(
        interaction=cast(discord.Interaction, interaction),
        requested_public=requested_public,
        context_channel_id="userapp",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("requested_public", [False, True], ids=["private", "public"])
async def test_delivery_receipt_preserves_visibility_and_synthetic_transcript_fields(
    monkeypatch: pytest.MonkeyPatch,
    requested_public: bool,
) -> None:
    interaction = FakeInteraction()
    calls: list[tuple[bool, bool]] = []

    async def send_result(
        _interaction: object,
        content: str,
        **kwargs: object,
    ) -> None:
        calls.append((bool(kwargs["ephemeral"]), bool(kwargs["original_ephemeral"])))
        callback = cast(
            Callable[[str], Awaitable[None]],
            kwargs["on_primary_delivered"],
        )
        await callback(f"delivered:{content}")

    monkeypatch.setattr(user_app_turn_adapter, "send_interaction_result", send_result)
    receipt = await _adapter(
        interaction,
        requested_public=requested_public,
    ).deliver(TurnResult(response_text="answer"), conversation_id=9)

    assert calls == [(not requested_public, not requested_public)]
    assert receipt.delivery_failed is False
    assert receipt.context_channel_id == "userapp"
    assert len(receipt.replies) == 1
    reply = receipt.replies[0]
    assert reply.discord_message_id == "userapp:73:assistant"
    assert reply.content == "delivered:answer"
    assert reply.source_created_at == interaction.created_at.timestamp()


@pytest.mark.asyncio
async def test_reporter_is_quiescent_before_final_interaction_edit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = FakeInteraction()
    adapter = _adapter(interaction)

    async def ignore_committed_message(_message_id: int) -> None:
        raise AssertionError("interaction activity must not commit a gateway message")

    reporter = adapter.make_activity_reporter(
        on_committed_message=ignore_committed_message,
    )
    await reporter(ActivityUpdate(label="Thinking..."))

    async def send_result(
        _interaction: object,
        content: str,
        **kwargs: object,
    ) -> None:
        assert cast(Any, reporter)._closed is True
        await interaction.edit_original_response(content=content)
        callback = cast(
            Callable[[str], Awaitable[None]],
            kwargs["on_primary_delivered"],
        )
        await callback(content)

    monkeypatch.setattr(user_app_turn_adapter, "send_interaction_result", send_result)

    assert adapter.activity_must_finish_before_delivery is True
    assert reporter.committed_message_id is None
    await reporter.finish()
    await adapter.deliver(TurnResult(response_text="answer"), conversation_id=9)

    assert [edit["content"] for edit in interaction.edits] == ["Thinking...", "answer"]


@pytest.mark.asyncio
async def test_partial_public_delivery_returns_delivered_prefix_and_private_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = FakeInteraction()

    async def send_partial(
        _interaction: object,
        _content: str,
        **kwargs: object,
    ) -> None:
        callback = cast(
            Callable[[str], Awaitable[None]],
            kwargs["on_primary_delivered"],
        )
        await callback("delivered prefix")
        raise PartialPublicDeliveryError

    monkeypatch.setattr(user_app_turn_adapter, "send_interaction_result", send_partial)
    receipt = await _adapter(interaction, requested_public=True).deliver(
        TurnResult(response_text="full answer"),
        conversation_id=9,
    )

    assert receipt.delivery_failed is True
    assert [reply.content for reply in receipt.replies] == ["delivered prefix"]
    assert len(interaction.followup.messages) == 1
    warning = interaction.followup.messages[0]
    assert warning["ephemeral"] is True
    assert isinstance(warning["allowed_mentions"], discord.AllowedMentions)
    assert warning["content"] == (
        "I posted the first part, but couldn't deliver the complete response."
    )


@pytest.mark.asyncio
async def test_partial_private_delivery_preserves_visible_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = FakeInteraction()
    status_calls: list[str] = []

    async def send_partial(
        _interaction: object,
        _content: str,
        **kwargs: object,
    ) -> None:
        callback = cast(
            Callable[[str], Awaitable[None]],
            kwargs["on_primary_delivered"],
        )
        await callback("delivered prefix")
        response = SimpleNamespace(status=500, reason="Server Error")
        raise discord.HTTPException(response, "followup failed")  # type: ignore[arg-type]

    async def replace_original(*_args: object, **_kwargs: object) -> None:
        status_calls.append("replaced")

    monkeypatch.setattr(user_app_turn_adapter, "send_interaction_result", send_partial)
    monkeypatch.setattr(user_app_turn_adapter, "send_interaction_status", replace_original)

    receipt = await _adapter(interaction).deliver(
        TurnResult(response_text="full answer"),
        conversation_id=9,
    )

    assert receipt.delivery_failed is True
    assert [reply.content for reply in receipt.replies] == ["delivered prefix"]
    assert status_calls == []
    assert interaction.edits == []
    assert len(interaction.followup.messages) == 1
    warning = interaction.followup.messages[0]
    assert warning["ephemeral"] is True
    assert warning["content"] == "I delivered part of the response, but couldn't send the rest."


@pytest.mark.asyncio
async def test_embed_only_delivery_receipt_uses_transcript_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = FakeInteraction()

    async def deliver_embed(
        _interaction: object,
        _content: str,
        **kwargs: object,
    ) -> None:
        callback = cast(
            Callable[[str], Awaitable[None]],
            kwargs["on_primary_delivered"],
        )
        await callback("")

    monkeypatch.setattr(user_app_turn_adapter, "send_interaction_result", deliver_embed)
    receipt = await _adapter(interaction).deliver(
        TurnResult(
            response_text="",
            embed=EmbedSpec(title="Forecast", description="Clear skies"),
        ),
        conversation_id=9,
    )

    assert len(receipt.replies) == 1
    assert receipt.replies[0].content == "[embed] Forecast: Clear skies"


@pytest.mark.asyncio
async def test_full_http_failure_returns_empty_receipt_and_sends_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = FakeInteraction()
    status_calls: list[tuple[str, bool, bool]] = []

    async def fail_result(*_args: object, **_kwargs: object) -> None:
        response = SimpleNamespace(status=500, reason="Server Error")
        raise discord.HTTPException(response, "delivery failed")  # type: ignore[arg-type]

    async def send_status(
        _interaction: object,
        content: str,
        *,
        ephemeral: bool,
        original_ephemeral: bool,
    ) -> None:
        status_calls.append((content, ephemeral, original_ephemeral))

    monkeypatch.setattr(user_app_turn_adapter, "send_interaction_result", fail_result)
    monkeypatch.setattr(user_app_turn_adapter, "send_interaction_status", send_status)
    receipt = await _adapter(interaction).deliver(
        TurnResult(response_text="answer"),
        conversation_id=9,
    )

    assert receipt.delivery_failed is True
    assert receipt.replies == ()
    assert status_calls == [
        (
            "I finished the turn but couldn't deliver the response here. Try again privately.",
            True,
            True,
        )
    ]


@pytest.mark.asyncio
async def test_no_result_finish_sends_selected_visibility_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = FakeInteraction()
    calls: list[tuple[str, bool, bool]] = []

    async def send_status(
        _interaction: object,
        content: str,
        *,
        ephemeral: bool,
        original_ephemeral: bool,
    ) -> None:
        calls.append((content, ephemeral, original_ephemeral))

    monkeypatch.setattr(user_app_turn_adapter, "send_interaction_status", send_status)
    await _adapter(interaction, requested_public=True).finish(
        TurnSurfaceOutcome(
            kind=TurnSurfaceOutcomeKind.NO_RESULT,
            conversation_id=9,
        )
    )

    assert calls == [
        (
            "There wasn't anything I could process in that request.",
            False,
            False,
        )
    ]


def test_bind_turn_source_is_a_noop_context_manager() -> None:
    adapter = _adapter(FakeInteraction())
    with adapter.bind_turn_source(cast(TurnPreparationInput, object())):
        pass
