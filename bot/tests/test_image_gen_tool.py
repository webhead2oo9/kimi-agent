"""Behavioral tests for the model-invoked image generation tool."""

from __future__ import annotations

import asyncio
import base64
import json
import threading
from dataclasses import asdict
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

import tools.image_gen as image_gen_tool
from app.tools import _register_image_gen
from config.settings import Settings
from image_gen.types import (
    ImageEditRequest,
    ImageGenError,
    ImageGenRequest,
    ImageQuotaError,
    ImageResult,
    ImageProviderRejectedError,
)
from storage.db import Database
from storage.usage import ImageUsageRequest, ImageUsageReservation, UsageStore
from tools.image_gen import TOOL_NAME, init_image_gen_tool
from tools.registry import BudgetName, MessageContext, ToolRegistry, TurnBudget
from tools.workspace.common import UserLocks
from tools.workspace.config import WorkspaceToolConfig
from trust.tiers import TrustTier
from usage.normalization import LLMUsageCall
from workspace import WorkspaceKey, WorkspaceManager

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"generated"
PNG_BASE64 = base64.b64encode(PNG_BYTES).decode("ascii")


class StubService:
    def __init__(self) -> None:
        self.generate_requests: list[ImageGenRequest] = []
        self.edit_requests: list[ImageEditRequest] = []
        self.failure: Exception | None = None
        self.usage: dict[str, object] | None = None
        self.image_bytes: bytes | None = PNG_BYTES
        self.result_size: str | None = "1024x1024"
        self.result_quality: str | None = "medium"
        self.result_background: str | None = "opaque"
        self.result_output_format: str | None = "png"
        self.actual_size: str | None = "1536x1024"
        self.requires_persistent_usage_reservation = False
        self.backend_name = "openai_codex"
        self.provider = "openai"
        self.estimated_cost_usd = 0.0
        self.events: list[str] = []
        self.validation_failure: Exception | None = None
        self.provider_reported_model: str | None = None
        self.model_verified = False
        self.request_id: str | None = None
        self.model_caveat: str | None = None

    async def generate(self, request: ImageGenRequest) -> ImageResult:
        self.events.append("provider")
        self.generate_requests.append(request)
        if self.failure is not None:
            raise self.failure
        return ImageResult(
            image_base64=PNG_BASE64,
            size=self.result_size,
            quality=self.result_quality,
            background=self.result_background,
            output_format=self.result_output_format,
            usage=self.usage,
            image_bytes=self.image_bytes,
            actual_size=self.actual_size,
            backend=self.backend_name,
            provider=self.provider,
            requested_model=request.model,
            provider_reported_model=self.provider_reported_model,
            model_verified=self.model_verified,
            request_id=self.request_id,
            model_caveat=self.model_caveat,
        )

    async def edit(self, request: ImageEditRequest) -> ImageResult:
        self.events.append("provider")
        self.edit_requests.append(request)
        if self.failure is not None:
            raise self.failure
        return ImageResult(
            image_base64=PNG_BASE64,
            size=self.result_size,
            quality=self.result_quality,
            background=self.result_background,
            output_format=self.result_output_format,
            usage=self.usage,
            image_bytes=self.image_bytes,
            actual_size=self.actual_size,
            backend=self.backend_name,
            provider=self.provider,
            requested_model=request.model,
            provider_reported_model=self.provider_reported_model,
            model_verified=self.model_verified,
            request_id=self.request_id,
            model_caveat=self.model_caveat,
        )

    def estimate_cost(self, request: ImageGenRequest | ImageEditRequest) -> float:
        del request
        return self.estimated_cost_usd

    def validate_generate(self, request: ImageGenRequest) -> None:
        del request
        if self.validation_failure is not None:
            raise self.validation_failure

    def validate_edit(self, request: ImageEditRequest) -> None:
        del request
        if self.validation_failure is not None:
            raise self.validation_failure


class RecordingImageUsageStore:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.requests: list[object] = []
        self.finalized: list[tuple[str, str]] = []

    async def reserve_image_usage(self, **kwargs: object) -> ImageUsageReservation:
        self.events.append("reserve")
        self.requests.append(kwargs["request"])
        return ImageUsageReservation("reservation-1", 0.25)

    async def finalize_image_usage(
        self, reservation_id: str, *, state: str, **_kwargs: object
    ) -> None:
        self.finalized.append((reservation_id, state))


def _context(
    *,
    tier: TrustTier = TrustTier.REGULAR,
    context_key: str = "guild:channel:root",
    tool_config: dict[str, object] | None = None,
) -> MessageContext:
    configured = tool_config or {}
    max_calls = configured.get("max_calls_per_turn", 2)
    budget_cap = max_calls if isinstance(max_calls, int) and not isinstance(max_calls, bool) else 2
    return MessageContext(
        user_id="user-1",
        user_name="Regular",
        guild_id="guild-1",
        channel_id="channel-1",
        thread_id=None,
        trust_tier=tier,
        context_key=context_key,
        tool_configs={TOOL_NAME: configured},
        budget=TurnBudget(caps={BudgetName.IMAGE_GEN_CALLS: budget_cap}),
    )


def _registered(
    tmp_path: Path,
) -> tuple[ToolRegistry, StubService, WorkspaceManager]:
    registry = ToolRegistry()
    service = StubService()
    manager = WorkspaceManager(tmp_path / "workspaces")
    init_image_gen_tool(
        registry,
        service,
        manager,
        UserLocks(),
        WorkspaceToolConfig(),
    )
    return registry, service, manager


def _args(**extra: object) -> dict[str, object]:
    return {
        "prompt": "A moonlit cabin in a pine forest",
        "attachment_description": "A moonlit cabin surrounded by pine trees.",
        **extra,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["gpt-image-2.5-flare", "gpt-image-2.5-sunburst"])
async def test_image_2_5_config_is_selectable_and_reaches_backend(
    tmp_path: Path,
    model: str,
) -> None:
    registry, service, _manager = _registered(tmp_path)
    entry = next(t for t in registry.get_tools_for_tier(TrustTier.REGULAR) if t.name == TOOL_NAME)
    model_field = next(field for field in entry.config_spec if field.field == "model")
    assert model_field.default == "gpt-image-2"
    assert model_field.choices == (
        "gpt-image-2",
        "gpt-image-2.5-flare",
        "gpt-image-2.5-sunburst",
    )
    ctx = _context(tool_config={"model": model, "size": "1536x1024"})
    result = json.loads(await registry.dispatch(TOOL_NAME, _args(), ctx))
    assert result["ok"] is True
    assert service.generate_requests[0].model == model
    assert service.generate_requests[0].size == "1536x1024"


@pytest.mark.asyncio
async def test_paid_image_fails_closed_without_persistent_usage_store(tmp_path: Path) -> None:
    registry, service, _manager = _registered(tmp_path)
    service.requires_persistent_usage_reservation = True
    ctx = _context()

    result = json.loads(await registry.dispatch(TOOL_NAME, _args(), ctx))

    assert "persistent usage accounting is unavailable" in result["error"]
    assert service.generate_requests == []


@pytest.mark.asyncio
async def test_paid_image_reserves_before_provider_without_storing_prompt(tmp_path: Path) -> None:
    registry, service, _manager = _registered(tmp_path)
    service.requires_persistent_usage_reservation = True
    service.backend_name = "openai_api"
    service.estimated_cost_usd = 0.25
    usage = RecordingImageUsageStore(service.events)
    ctx = _context()
    ctx.usage_store = usage  # type: ignore[assignment]

    result = json.loads(await registry.dispatch(TOOL_NAME, _args(), ctx))

    assert result["ok"] is True
    assert service.events == ["reserve", "provider"]
    usage_request = usage.requests[0]
    assert isinstance(usage_request, ImageUsageRequest)
    assert asdict(usage_request) == {
        "backend": "openai_api",
        "provider": "openai",
        "model": "gpt-image-2",
        "operation": "generate",
        "size": "auto",
        "quality": "auto",
        "estimated_cost_usd": 0.25,
    }
    assert usage.finalized == [("reservation-1", "succeeded")]


@pytest.mark.asyncio
async def test_paid_image_closes_database_transaction_before_provider_http(tmp_path: Path) -> None:
    db = Database(tmp_path / "usage.db")
    await db.connect()

    class TransactionCheckingService(StubService):
        async def generate(self, request: ImageGenRequest) -> ImageResult:
            assert db.conn.in_transaction is False
            return await super().generate(request)

    registry = ToolRegistry()
    service = TransactionCheckingService()
    service.requires_persistent_usage_reservation = True
    service.backend_name = "openai_api"
    service.estimated_cost_usd = 0.25
    manager = WorkspaceManager(tmp_path / "workspaces")
    init_image_gen_tool(
        registry,
        service,
        manager,
        UserLocks(),
        WorkspaceToolConfig(),
    )
    ctx = _context()
    ctx.usage_store = UsageStore(db)
    try:
        result = json.loads(await registry.dispatch(TOOL_NAME, _args(), ctx))

        assert result["ok"] is True
        assert db.conn.in_transaction is False
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_paid_provider_failure_keeps_reservation_as_uncertain(tmp_path: Path) -> None:
    registry, service, _manager = _registered(tmp_path)
    service.requires_persistent_usage_reservation = True
    service.backend_name = "openai_api"
    service.estimated_cost_usd = 0.25
    service.failure = ImageGenError("transport failed")
    usage = RecordingImageUsageStore(service.events)
    ctx = _context()
    ctx.usage_store = usage  # type: ignore[assignment]

    result = json.loads(await registry.dispatch(TOOL_NAME, _args(), ctx))

    assert "transport failed" in result["error"]
    assert usage.finalized == [("reservation-1", "failed_uncertain")]


@pytest.mark.asyncio
async def test_paid_provider_rejection_is_distinct_but_not_refunded(tmp_path: Path) -> None:
    registry, service, _manager = _registered(tmp_path)
    service.requires_persistent_usage_reservation = True
    service.backend_name = "openai_api"
    service.estimated_cost_usd = 0.25
    service.failure = ImageProviderRejectedError("rejected", "req_rejected")
    usage = RecordingImageUsageStore(service.events)
    ctx = _context()
    ctx.usage_store = usage  # type: ignore[assignment]

    result = json.loads(await registry.dispatch(TOOL_NAME, _args(), ctx))

    assert "rejected" in result["error"]
    assert usage.finalized == [("reservation-1", "provider_rejected")]


@pytest.mark.asyncio
async def test_paid_cancellation_keeps_reservation_as_uncertain(tmp_path: Path) -> None:
    registry, service, _manager = _registered(tmp_path)
    service.requires_persistent_usage_reservation = True
    service.backend_name = "openai_api"
    service.estimated_cost_usd = 0.25
    service.failure = asyncio.CancelledError()  # type: ignore[assignment]
    usage = RecordingImageUsageStore(service.events)
    ctx = _context()
    ctx.usage_store = usage  # type: ignore[assignment]

    with pytest.raises(asyncio.CancelledError):
        await registry.dispatch(TOOL_NAME, _args(), ctx)

    assert usage.finalized == [("reservation-1", "cancelled_uncertain")]


@pytest.mark.asyncio
async def test_invalid_local_options_do_not_consume_persistent_allowance(tmp_path: Path) -> None:
    registry, service, _manager = _registered(tmp_path)
    service.requires_persistent_usage_reservation = True
    service.backend_name = "openai_api"
    service.validation_failure = ImageGenError("model is not allowed for openai_api")
    usage = RecordingImageUsageStore(service.events)
    ctx = _context(tool_config={"model": "invalid-model"})
    ctx.usage_store = usage  # type: ignore[assignment]

    result = json.loads(await registry.dispatch(TOOL_NAME, _args(), ctx))

    assert "not allowed" in result["error"]
    assert usage.requests == []
    assert service.generate_requests == []


def test_tool_is_core_and_regular_tier(tmp_path: Path) -> None:
    registry, _service, _manager = _registered(tmp_path)

    member_names = {entry.name for entry in registry.get_tools_for_tier(TrustTier.MEMBER)}
    regular_tools = registry.get_tools_for_tier(TrustTier.REGULAR)
    regular_entry = next(entry for entry in regular_tools if entry.name == TOOL_NAME)

    assert TOOL_NAME not in member_names
    assert regular_entry.searchable is False
    assert regular_entry.category == "Media"
    assert "best-effort provider hints, not guarantees" in regular_entry.description
    assert regular_entry.parameters["required"] == ["prompt", "attachment_description"]
    assert regular_entry.parameters["properties"]["size"]["enum"] == [
        "auto",
        "1024x1024",
        "1024x1536",
        "1536x1024",
    ]
    assert regular_entry.parameters["properties"]["quality"]["enum"] == [
        "auto",
        "low",
        "medium",
        "high",
    ]
    assert regular_entry.parameters["properties"]["background"]["enum"] == [
        "auto",
        "opaque",
        "transparent",
    ]
    for field in ("size", "quality", "background"):
        assert "best-effort" in regular_entry.parameters["properties"][field]["description"]
    assert {field.field for field in regular_entry.config_spec} == {
        "model",
        "size",
        "quality",
        "background",
        "max_calls_per_turn",
        "max_reference_images",
        "max_attachments",
    }


@pytest.mark.asyncio
async def test_member_dispatch_masks_tool_existence(tmp_path: Path) -> None:
    registry, _service, _manager = _registered(tmp_path)

    result = json.loads(
        await registry.dispatch(TOOL_NAME, _args(), _context(tier=TrustTier.MEMBER))
    )

    assert result == {"error": "Unknown tool: generate_image"}


@pytest.mark.asyncio
async def test_generation_saves_reusable_workspace_png_and_queues_it(tmp_path: Path) -> None:
    registry, service, manager = _registered(tmp_path)
    ctx = _context(
        tool_config={
            "model": "gpt-image-2",
            "size": "1536x1024",
            "quality": "low",
            "background": "transparent",
            "max_calls_per_turn": 2,
            "max_reference_images": 5,
            "max_attachments": 5,
        }
    )

    result = json.loads(
        await registry.dispatch(
            TOOL_NAME,
            _args(size="1024x1024", quality="high", background="opaque"),
            ctx,
        )
    )

    assert result["ok"] is True
    assert result["operation"] == "generate"
    assert result["path"].startswith("generated_images/image-")
    assert result["path"].endswith(".png")
    assert result["bytes"] == len(PNG_BYTES)
    assert result["attached_to_reply"] is True
    assert ctx.budget_used(BudgetName.IMAGE_GEN_CALLS) == 1
    assert len(service.generate_requests) == 1
    assert service.generate_requests[0] == ImageGenRequest(
        prompt="A moonlit cabin in a pine forest",
        model="gpt-image-2",
        size="1024x1024",
        quality="high",
        background="opaque",
    )
    assert not service.edit_requests
    saved = manager.resolve_user_file_path(ctx.workspace_key, result["path"], must_exist=True)
    assert saved.read_bytes() == PNG_BYTES
    assert ctx.outbox.output_files == (str(saved.resolve()),)
    assert ctx.outbox.output_file_descriptions[str(saved.resolve())] == (
        "A moonlit cabin surrounded by pine trees."
    )


@pytest.mark.asyncio
async def test_generation_uses_operator_defaults_when_options_are_omitted(tmp_path: Path) -> None:
    registry, service, _manager = _registered(tmp_path)
    ctx = _context(
        tool_config={
            "model": "gpt-image-2.5-sunburst",
            "size": "1024x1536",
            "quality": "medium",
            "background": "transparent",
        }
    )

    result = json.loads(await registry.dispatch(TOOL_NAME, _args(), ctx))

    assert result["ok"] is True
    assert service.generate_requests[0] == ImageGenRequest(
        prompt="A moonlit cabin in a pine forest",
        model="gpt-image-2.5-sunburst",
        size="1024x1536",
        quality="medium",
        background="transparent",
    )


@pytest.mark.asyncio
async def test_generation_records_provider_reported_usage(tmp_path: Path) -> None:
    registry, service, _manager = _registered(tmp_path)
    service.usage = {"input_tokens": 17, "output_tokens": 5}
    ctx = _context()
    ctx.usage_sink = []

    result = json.loads(await registry.dispatch(TOOL_NAME, _args(), ctx))

    assert result["ok"] is True
    assert ctx.usage_sink is not None
    assert len(ctx.usage_sink) == 1
    call = ctx.usage_sink[0]
    assert call.model == "gpt-image-2"
    assert call.role == "image_generation"
    assert call.usage.input_tokens == 17
    assert call.usage.output_tokens == 5
    assert call.usage_present is True
    assert call.est_cost_usd == 0.0


@pytest.mark.asyncio
async def test_generation_records_missing_usage_without_fabricated_cost(tmp_path: Path) -> None:
    registry, _service, _manager = _registered(tmp_path)
    ctx = _context()
    ctx.usage_sink = []

    result = json.loads(await registry.dispatch(TOOL_NAME, _args(), ctx))

    assert result["ok"] is True
    assert ctx.usage_sink is not None
    assert len(ctx.usage_sink) == 1
    call = ctx.usage_sink[0]
    assert call.usage_present is False
    assert call.usage.input_tokens == 0
    assert call.usage.output_tokens == 0
    assert call.est_cost_usd == 0.0


@pytest.mark.asyncio
async def test_edit_loads_workspace_references_as_typed_data_urls(tmp_path: Path) -> None:
    registry, service, manager = _registered(tmp_path)
    ctx = _context(tool_config={"size": "1024x1024", "quality": "low", "background": "opaque"})
    ctx.usage_sink = []
    service.usage = {"input_tokens": 23, "output_tokens": 7}
    png = manager.resolve_user_file_path(ctx.workspace_key, "references/source.png")
    jpg = manager.resolve_user_file_path(ctx.workspace_key, "references/source.jpg")
    png.parent.mkdir(parents=True, exist_ok=True)
    png.write_bytes(b"\x89PNG\r\n\x1a\nsource")
    jpg.write_bytes(b"\xff\xd8\xffsource")

    result = json.loads(
        await registry.dispatch(
            TOOL_NAME,
            _args(
                reference_paths=["references/source.png", "references/source.jpg"],
                size="1024x1536",
                quality="high",
                background="transparent",
            ),
            ctx,
        )
    )

    assert result["ok"] is True
    assert result["operation"] == "edit"
    assert not service.generate_requests
    assert len(service.edit_requests) == 1
    request = service.edit_requests[0]
    assert request.prompt == "A moonlit cabin in a pine forest"
    assert request.model == "gpt-image-2"
    assert request.size == "1024x1536"
    assert request.quality == "high"
    assert request.background == "transparent"
    assert request.images[0].data_url.startswith("data:image/png;base64,")
    assert request.images[1].data_url.startswith("data:image/jpeg;base64,")
    assert ctx.usage_sink is not None
    assert len(ctx.usage_sink) == 1
    assert ctx.usage_sink[0].usage.input_tokens == 23
    assert ctx.usage_sink[0].usage.output_tokens == 7


@pytest.mark.asyncio
async def test_edit_uses_operator_defaults_when_options_are_omitted(tmp_path: Path) -> None:
    registry, service, manager = _registered(tmp_path)
    ctx = _context(tool_config={"size": "1536x1024", "quality": "medium", "background": "opaque"})
    image = manager.resolve_user_file_path(ctx.workspace_key, "references/source.png")
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(b"\x89PNG\r\n\x1a\nsource")

    result = json.loads(
        await registry.dispatch(
            TOOL_NAME,
            _args(reference_paths=["references/source.png"]),
            ctx,
        )
    )

    assert result["ok"] is True
    request = service.edit_requests[0]
    assert request.size == "1536x1024"
    assert request.quality == "medium"
    assert request.background == "opaque"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("option", "value", "error"),
    [
        ("size", None, "size must be one of"),
        ("size", 1024, "size must be one of"),
        ("size", "1536x864", "size must be one of"),
        ("quality", None, "quality must be one of"),
        ("quality", "xhigh", "quality must be one of"),
        ("background", False, "background must be one of"),
        ("background", "white", "background must be one of"),
        ("model", "gpt-image-2.5-sunburst", "unknown field(s): model"),
    ],
)
async def test_invalid_per_call_option_fails_before_budget_or_provider_call(
    tmp_path: Path,
    option: str,
    value: object,
    error: str,
) -> None:
    registry, service, _manager = _registered(tmp_path)
    ctx = _context()

    result = json.loads(await registry.dispatch(TOOL_NAME, _args(**{option: value}), ctx))

    assert error in result["error"]
    assert ctx.budget_used(BudgetName.IMAGE_GEN_CALLS) == 0
    assert not service.generate_requests
    assert not service.edit_requests


@pytest.mark.asyncio
async def test_result_distinguishes_requested_actual_and_provider_reported_metadata(
    tmp_path: Path,
) -> None:
    registry, service, _manager = _registered(tmp_path)
    service.result_size = "1536x1024"
    service.result_quality = "medium"
    service.result_background = "transparent"
    service.result_output_format = "png"
    service.actual_size = "1536x1024"
    ctx = _context()

    result = json.loads(
        await registry.dispatch(
            TOOL_NAME,
            _args(size="1024x1024", quality="high", background="opaque"),
            ctx,
        )
    )

    assert result["size"] == "1536x1024"
    assert result["background"] == "transparent"
    assert result["requested"] == {
        "size": "1024x1024",
        "quality": "high",
        "background": "opaque",
    }
    assert result["actual"] == {"size": "1536x1024", "output_format": "png"}
    assert result["provider_reported"] == {
        "size": "1536x1024",
        "quality": "medium",
        "background": "transparent",
        "output_format": "png",
    }
    assert result["mismatches"] == {
        "size": {
            "requested": "1024x1024",
            "actual": "1536x1024",
            "provider_reported": "1536x1024",
        },
        "quality": {"requested": "high", "provider_reported": "medium"},
        "background": {"requested": "opaque", "provider_reported": "transparent"},
    }


@pytest.mark.asyncio
async def test_result_includes_backend_model_attestation_and_caveat_metadata(
    tmp_path: Path,
) -> None:
    registry, service, _manager = _registered(tmp_path)
    service.backend_name = "openai_codex"
    service.provider_reported_model = "gpt-image-2.5-flare"
    service.model_verified = False
    service.request_id = "req_123"
    service.model_caveat = "endpoint does not attest the served model"
    ctx = _context(tool_config={"model": "gpt-image-2"})

    result = json.loads(await registry.dispatch(TOOL_NAME, _args(), ctx))

    assert result["backend"] == "openai_codex"
    assert result["provider"] == "openai"
    assert result["requested_model"] == "gpt-image-2"
    assert result["provider_reported_model"] == "gpt-image-2.5-flare"
    assert result["model_verified"] is False
    assert result["request_id"] == "req_123"
    assert result["model_caveat"] == "endpoint does not attest the served model"
    assert result["mismatches"]["model"] == {
        "requested": "gpt-image-2",
        "provider_reported": "gpt-image-2.5-flare",
    }


@pytest.mark.asyncio
async def test_cancellation_waits_for_completed_image_usage_recording(tmp_path: Path) -> None:
    registry, service, _manager = _registered(tmp_path)
    service.usage = {"input_tokens": 17, "output_tokens": 5}
    ctx = _context()
    started = asyncio.Event()
    release = asyncio.Event()
    recorded: list[LLMUsageCall] = []

    async def record_usage(call: LLMUsageCall) -> None:
        recorded.append(call)
        started.set()
        await release.wait()

    ctx.record_usage_call = record_usage
    turn = asyncio.create_task(registry.dispatch(TOOL_NAME, _args(), ctx))
    await asyncio.wait_for(started.wait(), timeout=1)

    turn.cancel()
    await asyncio.sleep(0)
    assert not turn.done()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await turn

    assert len(recorded) == 1
    assert recorded[0].role == "image_generation"
    assert recorded[0].usage.input_tokens == 17
    assert not ctx.outbox.output_files


@pytest.mark.asyncio
async def test_invalid_reference_image_fails_before_billable_call(tmp_path: Path) -> None:
    registry, service, manager = _registered(tmp_path)
    ctx = _context()
    bad = manager.resolve_user_file_path(ctx.workspace_key, "references/not-image.gif")
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"GIF89a")

    result = json.loads(
        await registry.dispatch(
            TOOL_NAME,
            _args(reference_paths=["references/not-image.gif"]),
            ctx,
        )
    )

    assert "PNG, JPEG, or WebP" in result["error"]
    assert ctx.budget_used(BudgetName.IMAGE_GEN_CALLS) == 0
    assert not service.generate_requests
    assert not service.edit_requests


@pytest.mark.asyncio
async def test_reference_per_file_limit_is_bounded_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(image_gen_tool, "MAX_REFERENCE_IMAGE_BYTES", 8)
    registry, service, manager = _registered(tmp_path)
    ctx = _context()
    image = manager.resolve_user_file_path(ctx.workspace_key, "references/large.png")
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(b"\x89PNG\r\n\x1a\nextra")

    result = json.loads(
        await registry.dispatch(
            TOOL_NAME,
            _args(reference_paths=["references/large.png"]),
            ctx,
        )
    )

    assert "exceeds 8 bytes" in result["error"]
    assert not service.edit_requests


@pytest.mark.asyncio
async def test_reference_aggregate_limit_is_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(image_gen_tool, "MAX_REFERENCE_IMAGE_BYTES", 20)
    monkeypatch.setattr(image_gen_tool, "MAX_REFERENCE_TOTAL_BYTES", 17)
    registry, service, manager = _registered(tmp_path)
    ctx = _context()
    for name in ("a.png", "b.png"):
        image = manager.resolve_user_file_path(ctx.workspace_key, f"references/{name}")
        image.parent.mkdir(parents=True, exist_ok=True)
        image.write_bytes(b"\x89PNG\r\n\x1a\nX")

    result = json.loads(
        await registry.dispatch(
            TOOL_NAME,
            _args(reference_paths=["references/a.png", "references/b.png"]),
            ctx,
        )
    )

    assert "aggregate bytes" in result["error"]
    assert not service.edit_requests


@pytest.mark.asyncio
async def test_per_turn_call_limit_blocks_second_call(tmp_path: Path) -> None:
    registry, service, _manager = _registered(tmp_path)
    ctx = _context(tool_config={"max_calls_per_turn": 1})

    first = json.loads(await registry.dispatch(TOOL_NAME, _args(), ctx))
    second = json.loads(await registry.dispatch(TOOL_NAME, _args(), ctx))

    assert first["ok"] is True
    assert "1 calls per turn" in second["error"]
    assert len(service.generate_requests) == 1
    assert ctx.budget_used(BudgetName.IMAGE_GEN_CALLS) == 1


@pytest.mark.asyncio
async def test_quota_error_is_safe_and_includes_reset_time(tmp_path: Path) -> None:
    registry, service, _manager = _registered(tmp_path)
    service.failure = ImageQuotaError("image generation limit reached", 1778836800)
    ctx = _context()

    result = json.loads(await registry.dispatch(TOOL_NAME, _args(), ctx))

    assert result == {
        "error": "image generation limit reached; resets at Unix timestamp 1778836800"
    }
    assert ctx.budget_used(BudgetName.IMAGE_GEN_CALLS) == 1


@pytest.mark.asyncio
async def test_missing_conversation_context_refuses_without_call(tmp_path: Path) -> None:
    registry, service, _manager = _registered(tmp_path)

    result = json.loads(await registry.dispatch(TOOL_NAME, _args(), _context(context_key="")))

    assert "conversation context" in result["error"]
    assert not service.generate_requests


@pytest.mark.asyncio
async def test_workspace_os_error_scrubs_absolute_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, _service, manager = _registered(tmp_path)
    ctx = _context()
    leaked = manager.user_files_dir(ctx.workspace_key) / "generated_images" / "secret.png"

    def fail_write(*_args: object) -> tuple[Path, str, int]:
        raise OSError(5, "disk failure", str(leaked))

    monkeypatch.setattr(image_gen_tool, "_write_output", fail_write)
    result = json.loads(await registry.dispatch(TOOL_NAME, _args(), ctx))

    assert str(manager.user_files_dir(ctx.workspace_key).resolve()) not in result["error"]
    assert "secret.png" in result["error"]


@pytest.mark.asyncio
async def test_unverified_service_result_is_rejected(tmp_path: Path) -> None:
    registry, service, manager = _registered(tmp_path)
    service.image_bytes = None
    ctx = _context()

    result = json.loads(await registry.dispatch(TOOL_NAME, _args(), ctx))

    assert result == {"error": "image generation service returned unverified image data"}
    assert not any(path.is_file() for path in manager.user_files_dir(ctx.workspace_key).rglob("*"))
    assert not ctx.outbox.output_files


@pytest.mark.asyncio
async def test_cancelled_worker_holds_workspace_lease_until_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = WorkspaceManager(tmp_path / "workspaces")
    locks = UserLocks()
    service = StubService()
    registry = ToolRegistry()
    init_image_gen_tool(
        registry,
        service,
        manager,
        locks,
        WorkspaceToolConfig(),
    )
    ctx = _context()
    started = threading.Event()
    release = threading.Event()
    partial = manager.user_files_dir(ctx.workspace_key) / "generated_images" / ".partial"

    def blocking_write(*_args: object) -> tuple[Path, str, int]:
        partial.parent.mkdir(parents=True, exist_ok=True)
        partial.write_bytes(b"partial")
        started.set()
        release.wait(timeout=5)
        partial.unlink(missing_ok=True)
        return partial.with_name("unused.png"), "generated_images/unused.png", 0

    monkeypatch.setattr(image_gen_tool, "_write_output", blocking_write)
    turn = asyncio.create_task(registry.dispatch(TOOL_NAME, _args(), ctx))
    assert await asyncio.to_thread(started.wait, 2)
    turn.cancel()
    await asyncio.sleep(0)
    assert not turn.done()

    acquired = asyncio.Event()

    async def contender() -> None:
        async with locks.activity(ctx.workspace_key):
            acquired.set()

    contender_task = asyncio.create_task(contender())
    await asyncio.sleep(0.05)
    assert not acquired.is_set()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await turn
    await asyncio.wait_for(acquired.wait(), timeout=1)
    await contender_task
    assert not partial.exists()
    assert not ctx.outbox.output_files


@pytest.mark.asyncio
async def test_cancelled_completed_write_removes_only_its_generated_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, _service, manager = _registered(tmp_path)
    ctx = _context()
    output_dir = manager.user_files_dir(ctx.workspace_key) / "generated_images"
    output_dir.mkdir(parents=True, exist_ok=True)
    prior = output_dir / "prior.png"
    prior.write_bytes(b"prior")
    started = threading.Event()
    release = threading.Event()
    generated: list[Path] = []
    write_output = image_gen_tool._write_output

    def blocking_write(
        workspace_manager: WorkspaceManager,
        workspace_config: WorkspaceToolConfig,
        workspace_key: WorkspaceKey,
        image_bytes: bytes,
    ) -> tuple[Path, str, int]:
        result = write_output(
            workspace_manager,
            workspace_config,
            workspace_key,
            image_bytes,
        )
        generated.append(result[0])
        started.set()
        release.wait(timeout=5)
        return result

    monkeypatch.setattr(image_gen_tool, "_write_output", blocking_write)
    turn = asyncio.create_task(registry.dispatch(TOOL_NAME, _args(), ctx))
    assert await asyncio.to_thread(started.wait, 2)
    assert generated[0].exists()

    turn.cancel()
    await asyncio.sleep(0)
    assert not turn.done()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await turn

    assert prior.read_bytes() == b"prior"
    assert not generated[0].exists()
    assert list(output_dir.iterdir()) == [prior]
    assert not ctx.outbox.output_files


def test_output_temp_file_is_removed_when_atomic_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = WorkspaceManager(tmp_path / "workspaces")
    ctx = _context()

    def fail_replace(_source: object, _destination: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(image_gen_tool.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        image_gen_tool._write_output(
            manager,
            WorkspaceToolConfig(),
            ctx.workspace_key,
            PNG_BYTES,
        )

    output_dir = manager.user_files_dir(ctx.workspace_key) / "generated_images"
    assert list(output_dir.iterdir()) == []


def test_registration_requires_flag_and_usable_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = WorkspaceManager(tmp_path / "workspaces")
    locks = UserLocks()
    workspace_config = WorkspaceToolConfig()

    class AuthManager:
        def __init__(self, available: bool) -> None:
            self.available = available

        def is_available(self) -> bool:
            return self.available

        def get_account_id(self) -> str:
            return "account"

        async def get_access_token(self) -> str:
            return "token"

        async def refresh_tokens(self, *, force: bool = False) -> None:
            del force

    auth = AuthManager(available=True)
    monkeypatch.setattr("app.tools.get_codex_auth_manager", lambda _path: auth)

    disabled = ToolRegistry()
    _register_image_gen(
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            image_gen_enabled=False,
        ),
        disabled,
        manager,
        workspace_locks=locks,
        workspace_config=workspace_config,
    )
    assert not disabled.is_registered(TOOL_NAME)

    auth.available = False
    missing = ToolRegistry()
    _register_image_gen(
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            image_gen_enabled=True,
            image_gen_auth_mode="auto",
            image_gen_api_key=SecretStr(""),
        ),
        missing,
        manager,
        workspace_locks=locks,
        workspace_config=workspace_config,
    )
    assert not missing.is_registered(TOOL_NAME)

    api_key = ToolRegistry()
    _register_image_gen(
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            image_gen_enabled=True,
            image_gen_backend="openai_api",
            image_gen_auth_mode="api_key",
            image_gen_api_key=SecretStr("sk-test"),
        ),
        api_key,
        manager,
        workspace_locks=locks,
        workspace_config=workspace_config,
    )
    assert api_key.is_registered(TOOL_NAME)

    auth.available = True
    oauth = ToolRegistry()
    _register_image_gen(
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            image_gen_enabled=True,
            image_gen_backend="openai_codex",
            image_gen_auth_mode="oauth",
        ),
        oauth,
        manager,
        workspace_locks=locks,
        workspace_config=workspace_config,
    )
    assert oauth.is_registered(TOOL_NAME)


def test_image_settings_reject_unknown_backend_and_auth_mode() -> None:
    with pytest.raises(ValidationError, match="IMAGE_GEN_BACKEND"):
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            image_gen_backend="stability",
        )
    with pytest.raises(ValidationError, match="IMAGE_GEN_AUTH_MODE"):
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            image_gen_auth_mode="magic",
        )


def test_image_settings_default_to_explicit_codex_and_persistent_limits() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.image_gen_backend == "openai_codex"
    assert settings.image_gen_user_calls_per_24h == 5
    assert settings.image_gen_guild_calls_per_24h == 100
    assert settings.image_gen_deployment_monthly_usd == 100.0
    assert settings.image_gen_staff_exempt_from_call_limits is True
    assert settings.image_gen_cost_estimates_usd == {
        "gpt-image-2:generate": 0.30,
        "gpt-image-2:edit": 0.45,
        "gpt-image-2.5-flare:generate": 0.30,
        "gpt-image-2.5-flare:edit": 0.45,
        "gpt-image-2.5-sunburst:generate": 0.30,
        "gpt-image-2.5-sunburst:edit": 0.45,
    }
