from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
from pathlib import Path
import tempfile
from typing import Any, cast

import pytest
from pydantic import SecretStr

from app.tools import CAPABILITY_PROBES, _register_video
from config.settings import Settings
from tools.config_spec import default_config
from tools.registry import BudgetName, MessageContext, ToolRegistry, TurnBudget
from tools.video import TOOL_NAME, canonicalize_youtube_url, init_video_tool
from tools.workspace.common import UserLocks
from trust.tiers import TrustTier
from video_understanding.client import (
    VideoEvidence,
    VideoInteractionError,
    VideoInteractionResult,
    VideoUsage,
)
from video_understanding.service import (
    VideoAnalysis,
    VideoInteractionCancelled,
    VideoResultCancelled,
    VideoSessionError,
)
from workspace import WorkspaceManager


@dataclass
class FakeVideoService:
    available: bool = True
    starts: list[dict[str, Any]] = field(default_factory=list)
    asks: list[dict[str, Any]] = field(default_factory=list)

    async def start(self, **kwargs: Any) -> VideoAnalysis:
        self.starts.append(kwargs)
        return _analysis("video_start")

    async def start_uploaded(self, **kwargs: Any) -> VideoAnalysis:
        self.starts.append(kwargs)
        source = kwargs["source"]
        return _analysis(
            "video_upload",
            source_kind=source.kind,
            source_display_name=source.display_name,
            youtube_url="",
        )

    async def ask(self, **kwargs: Any) -> VideoAnalysis:
        self.asks.append(kwargs)
        return _analysis(str(kwargs.get("session") or "video_active"))


@dataclass
class FakeVideoAttachment:
    filename: str
    size: int
    content_type: str | None

    def iter_video_chunks(self, *, chunk_size: int, max_bytes: int) -> Any:
        async def chunks() -> Any:
            yield b"video"

        return chunks()


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


class CancelledVideoService(FakeVideoService):
    async def start(self, **kwargs: Any) -> VideoAnalysis:
        raise VideoResultCancelled(
            result=VideoInteractionResult(
                interaction_id="remote",
                model="gemini-3.7-flash",
                answer="answer",
                evidence=(),
                limitations=(),
                usage=VideoUsage(input_tokens=50, cached_tokens=40, output_tokens=10),
            )
        )


@dataclass
class ZeroUsageVideoService(FakeVideoService):
    usage_present: bool = True

    async def start(self, **kwargs: Any) -> VideoAnalysis:
        return _analysis(
            "video_zero",
            usage=VideoUsage(),
            usage_present=self.usage_present,
        )


class MissingUsageErrorVideoService(FakeVideoService):
    async def start(self, **kwargs: Any) -> VideoAnalysis:
        raise VideoSessionError(
            "session persistence failed",
            result=_missing_usage_result(),
        )


class MissingUsageCancelledVideoService(FakeVideoService):
    async def start(self, **kwargs: Any) -> VideoAnalysis:
        raise VideoResultCancelled(result=_missing_usage_result())


def test_video_capability_probe_lists_every_registration_gate() -> None:
    probe = next(item for item in CAPABILITY_PROBES if item[0] == "video understanding")

    assert probe == (
        "video understanding",
        ("video",),
        "VIDEO_UNDERSTANDING_ENABLED + roles.video + GEMINI_API_KEY",
    )


@dataclass
class PinnedFollowupFailureVideoService(FakeVideoService):
    failure: str = "session"

    async def ask(self, **kwargs: Any) -> VideoAnalysis:
        result = VideoInteractionResult(
            interaction_id="remote-followup",
            model="old-upstream",
            answer="answer",
            evidence=(),
            limitations=(),
            usage=VideoUsage(input_tokens=50, cached_tokens=40, output_tokens=10),
        )
        if self.failure == "cancelled":
            raise VideoResultCancelled(result=result, catalog_model="old-catalog")
        if self.failure in {"interaction", "interaction_cancelled"}:
            error = VideoInteractionError(
                "malformed follow-up",
                interaction_id=result.interaction_id,
                model=result.model,
                usage=result.usage,
                catalog_model="old-catalog",
            )
            if self.failure == "interaction_cancelled":
                raise VideoInteractionCancelled(error=error)
            raise error
        raise VideoSessionError(
            "follow-up persistence failed",
            result=result,
            catalog_model="old-catalog",
        )


def _missing_usage_result() -> VideoInteractionResult:
    return VideoInteractionResult(
        interaction_id="remote",
        model="gemini-3.7-flash",
        answer="answer",
        evidence=(),
        limitations=(),
        usage=VideoUsage(),
        usage_present=False,
    )


def _analysis(
    session: str,
    *,
    source_kind: str = "youtube",
    source_display_name: str = "YouTube video",
    youtube_url: str = "https://www.youtube.com/watch?v=abcdefghijk",
    usage: VideoUsage | None = None,
    usage_present: bool = True,
    catalog_model: str = "gemini-video-flash",
    model: str = "gemini-3.7-flash",
) -> VideoAnalysis:
    return VideoAnalysis(
        session=session,
        source_kind=source_kind,
        source_display_name=source_display_name,
        source_locator=youtube_url or source_display_name,
        youtube_url=youtube_url,
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
        catalog_model=catalog_model,
        model=model,
        usage=(usage or VideoUsage(input_tokens=100, cached_tokens=80, output_tokens=20)),
        usage_present=usage_present,
    )


def _context(*, video_call_cap: int = 2, **overrides: Any) -> MessageContext:
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
        "budget": TurnBudget(caps={BudgetName.VIDEO_CALLS: video_call_cap}),
        "tool_configs": {
            TOOL_NAME: {
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


def _workspace_deps() -> tuple[WorkspaceManager, UserLocks]:
    manager = WorkspaceManager(Path(tempfile.mkdtemp(prefix="video-tool-tests-")))
    return manager, UserLocks()


def _registry_with_workspace(
    service: FakeVideoService,
    manager: WorkspaceManager,
    locks: UserLocks,
    *,
    catalog_model: str = "gemini-video-flash",
    model: str = "gemini-3.7-flash",
) -> ToolRegistry:
    registry = ToolRegistry()
    assert init_video_tool(
        registry,
        cast(Any, service),
        workspace_manager=manager,
        workspace_locks=locks,
        catalog_model=catalog_model,
        model=model,
    )
    return registry


def _registry(
    service: FakeVideoService,
    *,
    catalog_model: str = "gemini-video-flash",
    model: str = "gemini-3.7-flash",
) -> ToolRegistry:
    manager, locks = _workspace_deps()
    return _registry_with_workspace(
        service,
        manager,
        locks,
        catalog_model=catalog_model,
        model=model,
    )


def test_video_tool_is_searchable_member_surface_with_typed_config() -> None:
    registry = _registry(FakeVideoService())
    entry = next(item for item in registry.get_all_tools() if item.name == TOOL_NAME)

    assert entry.searchable is True
    assert entry.min_tier is TrustTier.MEMBER
    assert entry.untrusted is True
    assert default_config(registry.config_specs()[TOOL_NAME]) == {
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
    assert ctx.budget_used(BudgetName.VIDEO_CALLS) == 1
    assert ctx.usage_sink is not None
    assert ctx.usage_sink[0].usage.cached_read_tokens == 80
    assert ctx.usage_sink[0].pricing_model == "gemini-video-flash"
    assert ctx.usage_sink[0].model == "gemini-3.7-flash"
    assert ctx.usage_sink[0].est_cost_usd is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("usage_present", "expected_cost"),
    ((False, None), (True, 0.0)),
)
async def test_zero_video_usage_is_unpriced_only_when_provider_usage_is_missing(
    usage_present: bool,
    expected_cost: float | None,
) -> None:
    from config.model_config import parse_model_config_text
    from usage.pricing import price_usage_call

    model_config = parse_model_config_text("""
providers:
  gemini-video:
    type: gemini_interactions
    api_key_env: GEMINI_API_KEY
  main:
    type: openai_compat
    base_url: https://example.test/v1
    keyless: true
models:
  primary:
    provider: main
    model: test-chat
    capabilities: [text, tool_calling]
  gemini-video-flash:
    provider: gemini-video
    model: gemini-3.7-flash
    capabilities: [video_input]
    pricing: { input: 0.75, output: 3.75, cached_read: 0.075 }
roles:
  chat: primary
  compaction: primary
  video: gemini-video-flash
""")
    ctx = _context()

    await _registry(ZeroUsageVideoService(usage_present=usage_present)).dispatch(
        TOOL_NAME,
        {
            "action": "start",
            "url": "https://youtu.be/abcdefghijk",
            "question": "Question",
        },
        ctx,
    )

    assert ctx.usage_sink is not None
    [call] = ctx.usage_sink
    assert call.usage_present is usage_present
    assert call.est_cost_usd is None
    priced = price_usage_call(call, model_config)
    assert priced.est_cost_usd == expected_cost


@pytest.mark.asyncio
async def test_start_accepts_attachment_and_omits_youtube_links() -> None:
    service = FakeVideoService()
    attachment = FakeVideoAttachment("clip.mp4", 5, "video/mp4")
    ctx = _context(attachments=[attachment])

    payload = json.loads(
        await _registry(service).dispatch(
            TOOL_NAME,
            {
                "action": "start",
                "attachment": "clip.mp4",
                "question": "What happens?",
            },
            ctx,
        )
    )

    assert service.starts[0]["source"].kind == "attachment"
    assert payload["source"] == {
        "type": "uploaded_file",
        "filename": "clip.mp4",
        "origin": "attachment",
    }
    assert "video_url" not in payload
    assert "youtube_url" not in payload["evidence"][0]


@pytest.mark.asyncio
async def test_start_accepts_safe_workspace_video(tmp_path: Path) -> None:
    service = FakeVideoService()
    manager = WorkspaceManager(tmp_path / "workspaces")
    locks = UserLocks()
    ctx = _context()
    video = manager.resolve_user_file_path(ctx.workspace_key, "imports/clip.webm")
    video.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(video.write_bytes, b"video")

    payload = json.loads(
        await _registry_with_workspace(service, manager, locks).dispatch(
            TOOL_NAME,
            {
                "action": "start",
                "path": "imports/clip.webm",
                "question": "What happens?",
            },
            ctx,
        )
    )

    assert service.starts[0]["source"].kind == "workspace"
    assert service.starts[0]["source"].locator == "imports/clip.webm"
    assert payload["source"]["origin"] == "workspace"


@pytest.mark.asyncio
async def test_start_requires_exactly_one_video_source() -> None:
    ctx = _context()
    payload = json.loads(
        await _registry(FakeVideoService()).dispatch(
            TOOL_NAME,
            {
                "action": "start",
                "url": "https://youtu.be/abcdefghijk",
                "path": "clip.mp4",
                "question": "Question",
            },
            ctx,
        )
    )

    assert payload["error"] == "start requires exactly one of url, attachment, or path"
    assert ctx.budget_used(BudgetName.VIDEO_CALLS) == 0


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
    ctx = _context(
        video_call_cap=4,
        tool_configs={
            TOOL_NAME: {
                "thinking_level": "low",
                "max_output_tokens": 4096,
                "max_calls_per_turn": 4,
                "max_session_interactions": 10,
                "session_ttl_minutes": 60,
            }
        },
    )
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
    assert all(call.est_cost_usd is None for call in ctx.usage_sink)

    from config.model_config import parse_model_config_text
    from usage.pricing import price_usage_call

    model_config = parse_model_config_text("""
providers:
  gemini-video:
    type: gemini_interactions
    api_key_env: GEMINI_API_KEY
  main:
    type: openai_compat
    base_url: https://example.test/v1
    keyless: true
models:
  primary:
    provider: main
    model: test-chat
    capabilities: [text, tool_calling]
  gemini-video-flash:
    provider: gemini-video
    model: gemini-3.7-flash
    capabilities: [video_input]
    pricing: { input: 0.75, output: 3.75, cached_read: 0.075 }
roles:
  chat: primary
  compaction: primary
  video: gemini-video-flash
""")
    priced = [price_usage_call(c, model_config) for c in ctx.usage_sink]
    assert all(c.est_cost_usd is not None for c in priced)


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
    assert ctx.budget_used(BudgetName.VIDEO_CALLS) == 0
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
async def test_completed_call_usage_is_recorded_when_session_persistence_is_cancelled() -> None:
    ctx = _context()

    with pytest.raises(asyncio.CancelledError):
        await _registry(CancelledVideoService()).dispatch(
            TOOL_NAME,
            {
                "action": "start",
                "url": "https://youtu.be/abcdefghijk",
                "question": "Question",
            },
            ctx,
        )

    assert ctx.usage_sink is not None
    assert ctx.usage_sink[0].usage.input_tokens == 50
    assert ctx.usage_sink[0].usage.cached_read_tokens == 40


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    ["session", "interaction", "cancelled", "interaction_cancelled"],
)
async def test_failed_follow_up_prices_against_pinned_session_catalog(failure: str) -> None:
    from config.model_config import parse_model_config_text
    from usage.pricing import price_usage_call

    ctx = _context()
    operation = _registry(
        PinnedFollowupFailureVideoService(failure=failure),
        catalog_model="new-catalog",
        model="new-upstream",
    ).dispatch(
        TOOL_NAME,
        {"action": "ask", "session": "video_old", "question": "Follow up"},
        ctx,
    )
    if failure in {"cancelled", "interaction_cancelled"}:
        with pytest.raises(asyncio.CancelledError):
            await operation
    else:
        await operation

    assert ctx.usage_sink is not None
    [call] = ctx.usage_sink
    assert call.model == "old-upstream"
    assert call.pricing_model == "old-catalog"

    model_config = parse_model_config_text("""
providers:
  gemini-video:
    type: gemini_interactions
    api_key_env: GEMINI_API_KEY
  main:
    type: openai_compat
    base_url: https://example.test/v1
    keyless: true
models:
  primary:
    provider: main
    model: test-chat
    capabilities: [text, tool_calling]
  old-catalog:
    provider: gemini-video
    model: old-upstream
    capabilities: [video_input]
    pricing: { input: 1.0, cached_read: 0.5, output: 2.0 }
  new-catalog:
    provider: gemini-video
    model: new-upstream
    capabilities: [video_input]
    pricing: { input: 100.0, cached_read: 100.0, output: 100.0 }
roles:
  chat: primary
  compaction: primary
  video: new-catalog
""")
    priced = price_usage_call(call, model_config)
    assert priced.est_cost_usd == pytest.approx(0.00009)


@pytest.mark.asyncio
async def test_missing_usage_stays_unpriced_when_session_persistence_fails() -> None:
    ctx = _context()

    await _registry(MissingUsageErrorVideoService()).dispatch(
        TOOL_NAME,
        {
            "action": "start",
            "url": "https://youtu.be/abcdefghijk",
            "question": "Question",
        },
        ctx,
    )

    assert ctx.usage_sink is not None
    [call] = ctx.usage_sink
    assert call.usage_present is False
    assert call.est_cost_usd is None


@pytest.mark.asyncio
async def test_missing_usage_stays_unpriced_when_session_persistence_is_cancelled() -> None:
    ctx = _context()

    with pytest.raises(asyncio.CancelledError):
        await _registry(MissingUsageCancelledVideoService()).dispatch(
            TOOL_NAME,
            {
                "action": "start",
                "url": "https://youtu.be/abcdefghijk",
                "question": "Question",
            },
            ctx,
        )

    assert ctx.usage_sink is not None
    [call] = ctx.usage_sink
    assert call.usage_present is False
    assert call.est_cost_usd is None


@pytest.mark.asyncio
async def test_registration_requires_flag_secret_and_role() -> None:
    from config.model_config import parse_model_config_text

    model_config = parse_model_config_text("""
providers:
  gemini-video:
    type: gemini_interactions
    api_key_env: GEMINI_API_KEY
  main:
    type: openai_compat
    base_url: https://example.test/v1
    keyless: true
models:
  primary:
    provider: main
    model: test-chat
    capabilities: [text, tool_calling]
  gemini-video-flash:
    provider: gemini-video
    model: gemini-3.7-flash
    capabilities: [video_input]
roles:
  chat: primary
  compaction: primary
  video: gemini-video-flash
""")
    manager, locks = _workspace_deps()

    # 1. Disabled via VIDEO_UNDERSTANDING_ENABLED=False
    disabled = ToolRegistry()
    disabled_service = _register_video(
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            video_understanding_enabled=False,
            gemini_api_key=SecretStr("secret"),
        ),
        disabled,
        lambda: None,
        workspace_manager=manager,
        workspace_locks=locks,
        model_config=model_config,
    )
    assert not disabled.is_registered(TOOL_NAME)
    assert disabled_service.available is True  # Cleanup remains available!
    await disabled_service.close()

    # 2. Missing GEMINI_API_KEY
    missing = ToolRegistry()
    missing_service = _register_video(
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            video_understanding_enabled=True,
            gemini_api_key=SecretStr(""),
        ),
        missing,
        lambda: None,
        workspace_manager=manager,
        workspace_locks=locks,
        model_config=model_config,
    )
    assert not missing.is_registered(TOOL_NAME)
    assert missing_service.available is False
    await missing_service.close()

    # 3. Missing roles.video in model_config
    no_role_config = parse_model_config_text("""
providers:
  main:
    type: openai_compat
    base_url: https://example.test/v1
    keyless: true
models:
  primary:
    provider: main
    model: test-chat
    capabilities: [text, tool_calling]
roles:
  chat: primary
  compaction: primary
""")
    no_role_reg = ToolRegistry()
    no_role_service = _register_video(
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            video_understanding_enabled=True,
            gemini_api_key=SecretStr("secret"),
        ),
        no_role_reg,
        lambda: None,
        workspace_manager=manager,
        workspace_locks=locks,
        model_config=no_role_config,
    )
    assert not no_role_reg.is_registered(TOOL_NAME)
    await no_role_service.close()

    # 4. Fully configured (flag=True, secret set, roles.video configured)
    enabled = ToolRegistry()
    enabled_service = _register_video(
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            video_understanding_enabled=True,
            gemini_api_key=SecretStr("secret"),
        ),
        enabled,
        lambda: None,
        workspace_manager=manager,
        workspace_locks=locks,
        model_config=model_config,
    )
    assert enabled.is_registered(TOOL_NAME)
    await enabled_service.close()
