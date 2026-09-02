"""Behavioral tests for the model-invoked image generation tool."""

from __future__ import annotations

import asyncio
import base64
import json
import threading
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

import tools.image_gen as image_gen_tool
from app.tools import _register_image_gen
from config.settings import Settings
from image_gen.types import (
    ImageEditRequest,
    ImageGenRequest,
    ImageQuotaError,
    ImageResult,
)
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

    async def generate(self, request: ImageGenRequest) -> ImageResult:
        self.generate_requests.append(request)
        if self.failure is not None:
            raise self.failure
        return ImageResult(
            image_base64=PNG_BASE64,
            size="1024x1024",
            background="opaque",
            usage=self.usage,
        )

    async def edit(self, request: ImageEditRequest) -> ImageResult:
        self.edit_requests.append(request)
        if self.failure is not None:
            raise self.failure
        return ImageResult(
            image_base64=PNG_BASE64,
            size="1024x1536",
            usage=self.usage,
        )


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


def test_tool_is_core_and_regular_tier(tmp_path: Path) -> None:
    registry, _service, _manager = _registered(tmp_path)

    member_names = {entry.name for entry in registry.get_tools_for_tier(TrustTier.MEMBER)}
    regular_tools = registry.get_tools_for_tier(TrustTier.REGULAR)
    regular_entry = next(entry for entry in regular_tools if entry.name == TOOL_NAME)

    assert TOOL_NAME not in member_names
    assert regular_entry.searchable is False
    assert regular_entry.category == "Media"
    assert regular_entry.parameters["required"] == ["prompt", "attachment_description"]
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
            "size": "1024x1024",
            "quality": "high",
            "background": "opaque",
            "max_calls_per_turn": 2,
            "max_reference_images": 5,
            "max_attachments": 5,
        }
    )

    result = json.loads(await registry.dispatch(TOOL_NAME, _args(), ctx))

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
    assert ctx.output_files == [str(saved.resolve())]
    assert ctx.output_file_descriptions[str(saved.resolve())] == (
        "A moonlit cabin surrounded by pine trees."
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
    assert call.est_cost_usd is None


@pytest.mark.asyncio
async def test_generation_records_missing_usage_as_unpriced(tmp_path: Path) -> None:
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
    assert call.est_cost_usd is None


@pytest.mark.asyncio
async def test_edit_loads_workspace_references_as_typed_data_urls(tmp_path: Path) -> None:
    registry, service, manager = _registered(tmp_path)
    ctx = _context()
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
            _args(reference_paths=["references/source.png", "references/source.jpg"]),
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
    assert request.images[0].data_url.startswith("data:image/png;base64,")
    assert request.images[1].data_url.startswith("data:image/jpeg;base64,")
    assert ctx.usage_sink is not None
    assert len(ctx.usage_sink) == 1
    assert ctx.usage_sink[0].usage.input_tokens == 23
    assert ctx.usage_sink[0].usage.output_tokens == 7


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
    assert not ctx.output_files


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
    assert not ctx.output_files


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
        image_base64: str,
        image_bytes: bytes | None,
    ) -> tuple[Path, str, int]:
        result = write_output(
            workspace_manager,
            workspace_config,
            workspace_key,
            image_base64,
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
    assert not ctx.output_files


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
            PNG_BASE64,
            None,
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
