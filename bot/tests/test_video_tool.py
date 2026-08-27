from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any, cast

import pytest
from pydantic import SecretStr

from app.tools import _register_video
from config.settings import Settings
from tools.config_spec import default_config
from tools.registry import MessageContext, ToolRegistry
from tools.video import TOOL_NAME, canonicalize_youtube_url, init_video_tool
from trust.tiers import TrustTier
from video_understanding.client import (
    VideoEvidence,
    VideoInteractionResult,
    VideoUsage,
)
from video_understanding.service import VideoAnalysis, VideoSessionError


@dataclass
class FakeVideoService:
    available: bool = True
    starts: list[dict[str, Any]] = field(default_factory=list)
    asks: list[dict[str, Any]] = field(default_factory=list)

    async def start(self, **kwargs: Any) -> VideoAnalysis:
        self.starts.append(kwargs)
        return _analysis("video_start")

    async def ask(self, **kwargs: Any) -> VideoAnalysis:
        self.asks.append(kwargs)
        return _analysis(str(kwargs.get("session") or "video_active"))


class ErrorVideoService(FakeVideoService):
    async def start(self, **kwargs: Any) -> VideoAnalysis:
        raise VideoSessionError(
            "session persistence failed",
            result=VideoInteractionResult(
                interaction_id="remote",
                model="gemini-3.7-flash",
                answer="answer",
                evidence=(),
                limitations=(),
                usage=VideoUsage(input_tokens=50, cached_tokens=40, output_tokens=10),
            ),
        )


def _analysis(session: str) -> VideoAnalysis:
    return VideoAnalysis(
        session=session,
        youtube_url="https://www.youtube.com/watch?v=abcdefghijk",
        answer="The speaker makes the point clearly.",
        evidence=(
            VideoEvidence(
                start_seconds=62,
                end_seconds=75,
                basis="audio_and_visual",
                claim="The claim is spoken while the slide is visible.",
            ),
        ),
        limitations=("Fast cuts may be missed.",),
        model="gemini-3.7-flash",
        usage=VideoUsage(input_tokens=100, cached_tokens=80, output_tokens=20),
    )


def _context(**overrides: Any) -> MessageContext:
    values: dict[str, Any] = {
        "user_id": "user",
        "user_name": "Tester",
        "guild_id": "guild",
        "channel_id": "channel",
        "thread_id": None,
        "trust_tier": TrustTier.MEMBER,
        "conversation_id": 42,
        "activated_tools": {TOOL_NAME},
        "usage_sink": [],
        "tool_configs": {
            TOOL_NAME: {
                "model": "gemini-3.7-flash",
                "thinking_level": "low",
                "max_output_tokens": 4096,
                "max_calls_per_turn": 2,
                "max_session_interactions": 10,
                "session_ttl_minutes": 60,
            }
        },
    }
    values.update(overrides)
    return MessageContext(**values)


def _registry(service: FakeVideoService) -> ToolRegistry:
    registry = ToolRegistry()
    assert init_video_tool(registry, service)  # type: ignore[arg-type]
    return registry


def test_video_tool_is_searchable_member_surface_with_typed_config() -> None:
    registry = _registry(FakeVideoService())
    entry = next(item for item in registry.get_all_tools() if item.name == TOOL_NAME)

    assert entry.searchable is True
    assert entry.min_tier is TrustTier.MEMBER
    assert default_config(registry.config_specs()[TOOL_NAME]) == {
        "model": "gemini-3.7-flash",
        "thinking_level": "low",
        "max_output_tokens": 8192,
        "max_calls_per_turn": 4,
        "max_session_interactions": 20,
        "session_ttl_minutes": 1440,
    }


@pytest.mark.parametrize(
    ("raw", "video_id"),
    [
        ("https://youtu.be/abcdefghijk", "abcdefghijk"),
        ("https://www.youtube.com/watch?v=abcdefghijk&t=62", "abcdefghijk"),
        ("https://youtube.com/shorts/abcdefghijk", "abcdefghijk"),
        ("https://m.youtube.com/live/abcdefghijk", "abcdefghijk"),
    ],
)
def test_canonicalizes_supported_youtube_urls(raw: str, video_id: str) -> None:
    assert canonicalize_youtube_url(raw) == (
        f"https://www.youtube.com/watch?v={video_id}",
        video_id,
    )


@pytest.mark.parametrize(
    "raw",
    [
        "http://youtube.com/watch?v=abcdefghijk",
        "https://youtube.com.evil.test/watch?v=abcdefghijk",
        "https://user@youtube.com/watch?v=abcdefghijk",
        "https://youtube.com:443/watch?v=abcdefghijk",
        "https://youtube.com/playlist?list=abcdefghijk",
        "https://youtu.be/too-short",
        "https://youtu.be/abcdefghijk/extra",
        "https://www.youtube.com/watch?v=abcdefghijk#fragment",
    ],
)
def test_rejects_noncanonical_or_unsafe_urls(raw: str) -> None:
    with pytest.raises(ValueError):
        canonicalize_youtube_url(raw)


@pytest.mark.asyncio
async def test_start_returns_untrusted_timestamped_result_and_records_usage() -> None:
    service = FakeVideoService()
    ctx = _context()
    raw = await _registry(service).dispatch(
        TOOL_NAME,
        {
            "action": "start",
            "url": "https://youtu.be/abcdefghijk?t=62",
            "question": "What point does the speaker make?",
        },
        ctx,
    )
    payload = json.loads(raw)

    assert service.starts[0]["youtube_url"] == ("https://www.youtube.com/watch?v=abcdefghijk")
    assert payload["session"] == "video_start"
    assert payload["context_is_untrusted"] is True
    assert payload["evidence"][0]["timestamp"] == "01:02–01:15"
    assert payload["evidence"][0]["youtube_url"].endswith("&t=62s")
    assert ctx.video_calls_this_turn == 1
    assert ctx.usage_sink is not None
    assert ctx.usage_sink[0].usage.cached_read_tokens == 80
    assert ctx.usage_sink[0].pricing_model == "gemini-3.7-flash"
    assert ctx.usage_sink[0].est_cost_usd is not None
    assert ctx.usage_sink[0].est_cost_usd > 0


@pytest.mark.asyncio
async def test_ask_resolves_active_session_and_enforces_per_turn_limit() -> None:
    service = FakeVideoService()
    registry = _registry(service)
    ctx = _context()

    first = json.loads(
        await registry.dispatch(
            TOOL_NAME,
            {"action": "ask", "question": "What evidence supports that?"},
            ctx,
        )
    )
    assert first["session"] == "video_active"
    assert service.asks[0]["session"] is None

    await registry.dispatch(
        TOOL_NAME,
        {"action": "ask", "session": "video_start", "question": "And afterward?"},
        ctx,
    )
    rejected = json.loads(
        await registry.dispatch(
            TOOL_NAME,
            {"action": "ask", "question": "One more?"},
            ctx,
        )
    )
    assert rejected["error"] == "Video-call limit reached for this turn"
    assert len(service.asks) == 2


@pytest.mark.asyncio
async def test_each_stateful_interaction_records_its_full_reported_usage() -> None:
    service = FakeVideoService()
    ctx = _context()
    configs = cast(dict[str, dict[str, Any]], ctx.tool_configs)
    configs[TOOL_NAME]["max_calls_per_turn"] = 4
    registry = _registry(service)

    for question in ("First?", "Second?", "Third?"):
        await registry.dispatch(
            TOOL_NAME,
            {"action": "ask", "question": question},
            ctx,
        )

    assert ctx.usage_sink is not None
    assert [call.usage.input_tokens for call in ctx.usage_sink] == [100, 100, 100]
    assert sum(call.usage.cached_read_tokens for call in ctx.usage_sink) == 240
    assert all(call.est_cost_usd is not None for call in ctx.usage_sink)


@pytest.mark.asyncio
async def test_local_validation_does_not_consume_call_allowance() -> None:
    service = FakeVideoService()
    ctx = _context()
    rejected = json.loads(
        await _registry(service).dispatch(
            TOOL_NAME,
            {
                "action": "start",
                "url": "https://example.com/video",
                "question": "Question",
            },
            ctx,
        )
    )

    assert "YouTube" in rejected["error"]
    assert ctx.video_calls_this_turn == 0
    assert not service.starts


@pytest.mark.asyncio
async def test_completed_call_usage_is_recorded_when_session_persistence_fails() -> None:
    ctx = _context()
    payload = json.loads(
        await _registry(ErrorVideoService()).dispatch(
            TOOL_NAME,
            {
                "action": "start",
                "url": "https://youtu.be/abcdefghijk",
                "question": "Question",
            },
            ctx,
        )
    )

    assert payload["error"] == "session persistence failed"
    assert ctx.usage_sink is not None
    assert ctx.usage_sink[0].usage.input_tokens == 50
    assert ctx.usage_sink[0].usage.cached_read_tokens == 40


@pytest.mark.asyncio
async def test_registration_requires_flag_and_secret() -> None:
    disabled = ToolRegistry()
    disabled_service = _register_video(
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            video_understanding_enabled=False,
            gemini_api_key=SecretStr("secret"),
        ),
        disabled,
        lambda: None,
    )
    assert not disabled.is_registered(TOOL_NAME)
    await disabled_service.close()

    missing = ToolRegistry()
    missing_service = _register_video(
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            video_understanding_enabled=True,
            gemini_api_key=SecretStr(""),
        ),
        missing,
        lambda: None,
    )
    assert not missing.is_registered(TOOL_NAME)
    await missing_service.close()

    enabled = ToolRegistry()
    enabled_service = _register_video(
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            video_understanding_enabled=True,
            gemini_api_key=SecretStr("secret"),
        ),
        enabled,
        lambda: None,
    )
    assert enabled.is_registered(TOOL_NAME)
    await enabled_service.close()
