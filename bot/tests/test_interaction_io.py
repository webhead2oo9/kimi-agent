"""Deferred interaction delivery: transcript text under partial failure."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import discord
import pytest

from discord_adapter.interaction_io import (
    MAX_INTERACTION_FOLLOWUPS,
    PartialPublicDeliveryError,
    send_interaction_result,
)


def _http_error() -> discord.HTTPException:
    response = SimpleNamespace(status=500, reason="Server Error")
    return discord.HTTPException(response, "delivery failed")  # type: ignore[arg-type]


class _Followup:
    def __init__(self, fail_after: int | None) -> None:
        self.sent: list[dict[str, Any]] = []
        self._fail_after = fail_after

    async def send(self, content: str | None = None, **kwargs: Any) -> None:
        if self._fail_after is not None and len(self.sent) >= self._fail_after:
            raise _http_error()
        self.sent.append({"content": content, **kwargs})


class _Interaction:
    def __init__(self, *, fail_followups_after: int | None = None, fail_edit: bool = False) -> None:
        self.channel = SimpleNamespace(id=1)
        self.edits: list[dict[str, Any]] = []
        self.followup = _Followup(fail_followups_after)
        self._fail_edit = fail_edit

    async def edit_original_response(self, **kwargs: Any) -> None:
        if self._fail_edit:
            raise _http_error()
        self.edits.append(kwargs)


@pytest.mark.asyncio
async def test_partial_failure_after_an_overflow_file_records_the_full_text() -> None:
    """A public reply that overflowed is one followup carrying the whole text as
    response.md. If the private acknowledgement edit then fails, the persisted
    transcript must be that text, not the placeholder chunk."""

    interaction = _Interaction(fail_edit=True)
    content = "\n".join(f"line {index} " + "x" * 90 for index in range(400))
    recorded: list[str] = []

    async def on_primary_delivered(text: str) -> None:
        recorded.append(text)

    with pytest.raises(PartialPublicDeliveryError):
        await send_interaction_result(
            interaction,  # type: ignore[arg-type]
            content,
            ephemeral=False,
            original_ephemeral=True,
            on_primary_delivered=on_primary_delivered,
        )

    assert len(interaction.followup.sent) == 1
    first = interaction.followup.sent[0]
    assert first["content"].startswith("The full response is attached")
    assert [file.filename for file in first["files"]] == ["response.md"]
    assert recorded == [content]


@pytest.mark.asyncio
async def test_partial_failure_without_overflow_records_the_delivered_prefix() -> None:
    interaction = _Interaction(fail_followups_after=0)
    content = "\n".join("y" * 1900 for _ in range(MAX_INTERACTION_FOLLOWUPS))
    recorded: list[str] = []

    async def on_primary_delivered(text: str) -> None:
        recorded.append(text)

    with pytest.raises(discord.HTTPException):
        await send_interaction_result(
            interaction,  # type: ignore[arg-type]
            content,
            ephemeral=True,
            original_ephemeral=True,
            on_primary_delivered=on_primary_delivered,
        )

    assert recorded == [interaction.edits[0]["content"]]
    assert recorded[0] != content
