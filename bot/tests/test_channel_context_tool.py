from __future__ import annotations

import asyncio
import json

from agent.backfill import BackfilledMessage, ChannelContextImage
from discord_adapter.gateway import DiscordGatewayError
from tools.channel_context import MAX_CONTEXT_IMAGES, init_channel_context_tool
from tools.registry import UNTRUSTED_CONTEXT_NOTE, MessageContext, ToolRegistry
from trust.tiers import TrustTier


class _Gateway:
    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.result = result or []
        self.error = error
        self.limits: list[int] = []

    async def collect_recent_channel_context(
        self,
        ctx: MessageContext,
        *,
        limit: int = 15,
    ):
        self.limits.append(limit)
        if self.error is not None:
            raise self.error
        return self.result


def _ctx() -> MessageContext:
    return MessageContext(
        user_id="123",
        user_name="Alice",
        guild_id="999",
        channel_id="100",
        thread_id=None,
        trust_tier=TrustTier.MEMBER,
        context_key="guild:100:main",
        trigger_discord_message_id="555",
    )


def _message(line: str) -> BackfilledMessage:
    return BackfilledMessage(transcript_line=line)


def test_get_channel_context_defaults_limit_and_returns_untrusted_transcript() -> None:
    gateway = _Gateway(result=[_message("Alice: hello"), _message("Kimi: hi")])
    registry = ToolRegistry()
    init_channel_context_tool(registry, gateway)
    entry = next(item for item in registry.get_all_tools() if item.name == "get_channel_context")
    assert entry.untrusted is True

    raw = asyncio.run(registry.dispatch("get_channel_context", {}, _ctx()))

    result = json.loads(raw)
    assert gateway.limits == [15]
    assert result == {
        "count": 2,
        "limit": 15,
        "context_is_untrusted": True,
        "note": UNTRUSTED_CONTEXT_NOTE,
        "transcript": "Alice: hello\nKimi: hi",
    }


def test_get_channel_context_addresses_posted_images_by_id() -> None:
    # The transcript can only name a file. A visual tool needs somewhere to point,
    # so the ids ride alongside it, but only when the window actually has images.
    gateway = _Gateway(
        result=[
            BackfilledMessage(
                # A captioned image never has its filename in the transcript, so the
                # author is the only thing tying "the one Bob posted" to an id.
                transcript_line="Bob: check out my setup",
                images=(
                    ChannelContextImage(
                        message_id="777",
                        attachment_index=2,
                        filename="kickflip.png",
                        author_name="Bob",
                    ),
                ),
            ),
            _message("Kimi: nice"),
        ]
    )
    registry = ToolRegistry()
    init_channel_context_tool(registry, gateway)

    result = json.loads(asyncio.run(registry.dispatch("get_channel_context", {}, _ctx())))

    assert result["images_channel_id"] == "100"
    assert result["images"] == [
        {
            "message_id": "777",
            "attachment_index": 2,
            "filename": "kickflip.png",
            "posted_by": "Bob",
        }
    ]
    assert "images_omitted_older" not in result


def test_get_channel_context_caps_the_image_roster_and_reports_the_drop() -> None:
    gateway = _Gateway(
        result=[
            BackfilledMessage(
                transcript_line=f"Alice: shot {index}",
                images=(
                    ChannelContextImage(
                        message_id=str(index),
                        attachment_index=1,
                        filename=f"{index}.png",
                        author_name="Alice",
                    ),
                ),
            )
            for index in range(MAX_CONTEXT_IMAGES + 5)
        ]
    )
    registry = ToolRegistry()
    init_channel_context_tool(registry, gateway)

    result = json.loads(asyncio.run(registry.dispatch("get_channel_context", {}, _ctx())))

    assert len(result["images"]) == MAX_CONTEXT_IMAGES
    assert result["images_omitted_older"] == 5
    # The newest survive: context is chronological, so the tail is the recent end.
    assert result["images"][-1]["message_id"] == str(MAX_CONTEXT_IMAGES + 4)


def test_get_channel_context_bounds_requested_limit_to_100() -> None:
    gateway = _Gateway()
    registry = ToolRegistry()
    init_channel_context_tool(registry, gateway)

    raw = asyncio.run(registry.dispatch("get_channel_context", {"limit": 500}, _ctx()))

    assert gateway.limits == [100]
    assert json.loads(raw)["limit"] == 100


def test_get_channel_context_returns_safe_error_when_gateway_unavailable() -> None:
    gateway = _Gateway(error=DiscordGatewayError("Current Discord source is unavailable."))
    registry = ToolRegistry()
    init_channel_context_tool(registry, gateway)

    raw = asyncio.run(registry.dispatch("get_channel_context", {}, _ctx()))

    assert json.loads(raw) == {"error": "Current Discord source is unavailable."}
