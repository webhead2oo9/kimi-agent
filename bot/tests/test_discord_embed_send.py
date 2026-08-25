from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import discord
import pytest

from discord_adapter.io import build_embed, send_response
from tools.embeds import EmbedSpec


class RecordingChannel:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.fail_next_file_upload = False

    async def send(self, content: str | None, **kwargs: object) -> object:
        self.calls.append({"content": content, **kwargs})
        if kwargs.get("files") and self.fail_next_file_upload:
            self.fail_next_file_upload = False
            raise discord.HTTPException(
                response=cast(Any, type("R", (), {"status": 413, "reason": "x"})()),
                message="file too large",
            )
        return object()


def _channel() -> RecordingChannel:
    channel = RecordingChannel()
    channel.fail_next_file_upload = False
    return channel


def test_build_embed_maps_all_fields() -> None:
    spec = EmbedSpec(
        title="T",
        description="D",
        url="https://e.com",
        color=0x5865F2,
        author_name="A",
        footer_text="F",
        image="attachment://c.png",
        thumbnail_url="https://e.com/t.png",
        fields=(("n", "v", True),),
        timestamp=True,
    )
    embed = build_embed(spec)
    assert embed.title == "T"
    assert embed.description == "D"
    assert embed.url == "https://e.com"
    assert embed.color is not None and embed.color.value == 0x5865F2
    assert embed.author.name == "A"
    assert embed.footer.text == "F"
    assert embed.image.url == "attachment://c.png"
    assert embed.thumbnail.url == "https://e.com/t.png"
    assert embed.fields[0].name == "n"
    assert embed.fields[0].value == "v"
    assert embed.fields[0].inline is True
    assert embed.timestamp is not None


@pytest.mark.asyncio
async def test_embed_rides_on_first_chunk() -> None:
    channel = _channel()
    await send_response(
        cast(discord.abc.Messageable, channel), "caption", embed=EmbedSpec(title="Hi")
    )
    assert len(channel.calls) == 1
    assert channel.calls[0]["content"] == "caption"
    assert len(channel.calls[0]["embeds"]) == 1


@pytest.mark.asyncio
async def test_link_suppression_preserves_explicit_embed() -> None:
    channel = _channel()

    await send_response(
        cast(discord.abc.Messageable, channel),
        "Read https://example.com",
        embed=EmbedSpec(title="Hi"),
    )

    assert channel.calls[0]["content"] == "Read <https://example.com>"
    assert len(channel.calls[0]["embeds"]) == 1
    assert "suppress_embeds" not in channel.calls[0]


@pytest.mark.asyncio
async def test_embed_only_empty_content_still_sends() -> None:
    channel = _channel()
    await send_response(cast(discord.abc.Messageable, channel), "", embed=EmbedSpec(title="Hi"))
    assert len(channel.calls) == 1
    assert channel.calls[0]["content"] in (None, "")
    assert "embeds" in channel.calls[0]


@pytest.mark.asyncio
async def test_files_and_embed_in_same_message(tmp_path: Path) -> None:
    output = tmp_path / "artifact.txt"
    output.write_text("x", encoding="utf-8")
    channel = _channel()
    spec = EmbedSpec(title="Hi", image="attachment://artifact.txt")
    await send_response(
        cast(discord.abc.Messageable, channel),
        "cap",
        output_files=[str(output)],
        allowed_file_roots=[tmp_path],
        embed=spec,
    )
    assert len(channel.calls) == 1
    assert channel.calls[0]["files"]
    assert "embeds" in channel.calls[0]


@pytest.mark.asyncio
async def test_attachment_backed_embed_file_is_prioritized_with_many_files(
    tmp_path: Path,
) -> None:
    regular_outputs: list[str] = []
    for index in range(10):
        output = tmp_path / f"artifact-{index}.txt"
        output.write_text("x", encoding="utf-8")
        regular_outputs.append(str(output))
    embed_image = tmp_path / "embed-image.png"
    embed_image.write_bytes(b"\x89PNG\r\n")

    channel = _channel()
    spec = EmbedSpec(title="Hi", image="attachment://embed-image.png")
    await send_response(
        cast(discord.abc.Messageable, channel),
        "cap",
        output_files=[*regular_outputs, str(embed_image)],
        allowed_file_roots=[tmp_path],
        embed=spec,
    )

    filenames = [file.filename for file in channel.calls[0]["files"]]
    assert len(filenames) == 10
    assert "embed-image.png" in filenames


@pytest.mark.asyncio
async def test_attachment_backed_embed_retry_preserves_file_and_embed(
    tmp_path: Path,
) -> None:
    output = tmp_path / "embed-image.png"
    output.write_bytes(b"\x89PNG\r\n")
    channel = _channel()
    channel.fail_next_file_upload = True
    spec = EmbedSpec(title="Hi", image="attachment://embed-image.png")

    await send_response(
        cast(discord.abc.Messageable, channel),
        "cap",
        output_files=[str(output)],
        allowed_file_roots=[tmp_path],
        embed=spec,
    )

    assert len(channel.calls) == 2
    assert "files" in channel.calls[0]
    assert "embeds" in channel.calls[0]
    assert "files" in channel.calls[1]
    assert "embeds" in channel.calls[1]


@pytest.mark.asyncio
async def test_embed_only_attachment_failure_retries_exact_payload(
    tmp_path: Path,
) -> None:
    output = tmp_path / "embed-image.png"
    output.write_bytes(b"\x89PNG\r\n")
    channel = _channel()
    channel.fail_next_file_upload = True
    spec = EmbedSpec(title="Hi", image="attachment://embed-image.png")

    await send_response(
        cast(discord.abc.Messageable, channel),
        "",
        output_files=[str(output)],
        allowed_file_roots=[tmp_path],
        embed=spec,
    )

    assert len(channel.calls) == 2
    assert "files" in channel.calls[1]
    assert "embeds" in channel.calls[1]


@pytest.mark.asyncio
async def test_embed_only_on_first_of_multiple_chunks() -> None:
    channel = _channel()
    await send_response(
        cast(discord.abc.Messageable, channel), "a" * 2500, embed=EmbedSpec(title="Hi")
    )
    assert len(channel.calls) >= 2
    assert "embeds" in channel.calls[0]
    assert "embeds" not in channel.calls[1]


@pytest.mark.asyncio
async def test_permanent_middle_chunk_failure_stops_later_delivery() -> None:
    class FailingMiddleChannel(RecordingChannel):
        async def send(self, content: str | None, **kwargs: object) -> object:
            self.calls.append({"content": content, **kwargs})
            if len(self.calls) in {2, 3}:
                raise discord.HTTPException(
                    response=cast(Any, type("R", (), {"status": 500, "reason": "x"})()),
                    message="temporary failure",
                )
            return object()

    channel = FailingMiddleChannel()
    sent = await send_response(
        cast(discord.abc.Messageable, channel),
        "x" * 4500,
    )

    assert len(channel.calls) == 3  # first chunk + two attempts for chunk two
    assert len(sent) == 1
    assert sent.delivery_failed is True


@pytest.mark.asyncio
async def test_nothing_to_send_sends_no_message() -> None:
    channel = _channel()
    await send_response(cast(discord.abc.Messageable, channel), "")
    assert channel.calls == []
