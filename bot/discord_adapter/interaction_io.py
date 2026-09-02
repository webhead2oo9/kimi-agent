from __future__ import annotations

import io
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast

import discord

from discord_adapter.io import (
    apply_attachment_delivery_notice,
    build_embed,
    chunk_message,
    prepare_attachment_delivery,
    suppress_link_previews,
)

MAX_INTERACTION_FOLLOWUPS = 5


class PartialPublicDeliveryError(Exception):
    """A public primary response was sent, but a later chunk failed."""


async def send_interaction_result(
    interaction: discord.Interaction,
    content: str,
    *,
    ephemeral: bool,
    original_ephemeral: bool,
    output_files: tuple[str, ...] = (),
    output_file_descriptions: tuple[tuple[str, str], ...] = (),
    allowed_file_roots: tuple[str | Path, ...] = (),
    embed: Any | None = None,
    on_primary_delivered: Callable[[str], Awaitable[None]] | None = None,
) -> None:
    """Deliver a deferred response within Discord's user-install followup cap."""

    channel = interaction.channel
    if channel is None:
        await send_interaction_status(
            interaction,
            "I couldn't resolve where to deliver that response.",
            ephemeral=True,
            original_ephemeral=original_ephemeral,
        )
        return
    plan = prepare_attachment_delivery(
        cast(discord.abc.Messageable, channel),
        output_files=list(output_files),
        output_file_descriptions=dict(output_file_descriptions),
        allowed_file_roots=list(allowed_file_roots),
        embed=embed,
    )
    prepared = suppress_link_previews(apply_attachment_delivery_notice(content, plan))
    chunks = chunk_message(prepared)
    overflow_file: discord.File | None = None
    max_chunks = MAX_INTERACTION_FOLLOWUPS + int(ephemeral == original_ephemeral)
    if len(chunks) > max_chunks:
        overflow_file = discord.File(
            io.BytesIO(prepared.encode("utf-8")),
            filename="response.md",
            description="Full response",
        )
        chunks = ["The full response is attached as `response.md`."]

    files: list[discord.File] = []
    for path in plan.files:
        try:
            files.append(
                discord.File(
                    str(path),
                    description=dict(plan.file_descriptions).get(str(path)),
                )
            )
        except OSError:
            continue
    if overflow_file is not None:
        files = [overflow_file, *files[:9]]

    first = chunks[0] if chunks and chunks[0].strip() else None
    first_kwargs: dict[str, Any] = {
        "content": first,
        "allowed_mentions": discord.AllowedMentions.none(),
    }
    if files:
        first_kwargs["files"] = files
    if plan.embed is not None:
        first_kwargs["embed"] = build_embed(plan.embed)

    delivered_chunks = [first] if first is not None else []
    if ephemeral == original_ephemeral:
        edit_kwargs = dict(first_kwargs)
        edit_kwargs["attachments"] = edit_kwargs.pop("files", [])
        await interaction.edit_original_response(**edit_kwargs)
        remaining = chunks[1 : MAX_INTERACTION_FOLLOWUPS + 1]
    elif ephemeral:
        await interaction.delete_original_response()
        await interaction.followup.send(ephemeral=True, **first_kwargs)
        remaining = chunks[1:MAX_INTERACTION_FOLLOWUPS]
    else:
        await interaction.followup.send(ephemeral=False, **first_kwargs)
        # The public first message consumed one of the five user-install
        # followups; four remain after the private deferred acknowledgement.
        remaining = chunks[1:MAX_INTERACTION_FOLLOWUPS]

    try:
        if not ephemeral and ephemeral != original_ephemeral:
            await interaction.edit_original_response(
                content="Posted the response publicly.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        for chunk in remaining:
            await interaction.followup.send(
                chunk,
                ephemeral=ephemeral,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            delivered_chunks.append(chunk)
    except discord.HTTPException as exc:
        if on_primary_delivered is not None:
            # With an overflow file the first message already carried the whole
            # response; the visible chunk is only the placeholder pointing at it.
            delivered_text = prepared if overflow_file is not None else "\n".join(delivered_chunks)
            await on_primary_delivered(delivered_text)
        if not ephemeral:
            raise PartialPublicDeliveryError from exc
        raise
    if on_primary_delivered is not None:
        await on_primary_delivered(prepared)


async def send_interaction_status(
    interaction: discord.Interaction,
    content: str,
    *,
    ephemeral: bool,
    original_ephemeral: bool,
) -> None:
    """Replace the deferred response, changing visibility via a followup if needed."""

    if ephemeral == original_ephemeral:
        await interaction.edit_original_response(
            content=content,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return
    if not original_ephemeral:
        await interaction.delete_original_response()
    await interaction.followup.send(
        content,
        ephemeral=ephemeral,
        allowed_mentions=discord.AllowedMentions.none(),
    )
