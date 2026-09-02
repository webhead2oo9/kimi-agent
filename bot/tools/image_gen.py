"""OpenAI-backed image generation and editing tool."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from image_gen.types import (
    ImageEditRequest,
    ImageGenError,
    ImageGenRequest,
    ImageQuotaError,
    ImageReference,
    ImageResult,
)
from tools._common import tool_error
from tools.config_spec import KIND_CHOICE, KIND_INT, ToolConfigField
from tools.output_queue import AttachmentLimitError, enqueue_workspace_file
from tools.registry import BudgetName, MessageContext, ToolBudgetSpec, ToolRegistry
from tools.workspace.common import (
    UserLocks,
    ensure_quota,
    scrub_user_paths,
    workspace_activity,
)
from tools.workspace.config import WorkspaceToolConfig
from trust.tiers import TrustTier
from usage.normalization import LLMUsageCall, normalize_usage
from utils.asyncio import await_uncancellable
from workspace import WorkspaceKey, WorkspaceManager

TOOL_NAME = "generate_image"
MAX_PROMPT_CHARS = 10_000
MAX_ATTACHMENT_DESCRIPTION_CHARS = 1_000
MAX_REFERENCE_IMAGES = 5
MAX_REFERENCE_IMAGE_BYTES = 10 * 1024 * 1024
MAX_REFERENCE_TOTAL_BYTES = 25 * 1024 * 1024

log = logging.getLogger(__name__)


class ImageGenServiceLike(Protocol):
    async def generate(self, request: ImageGenRequest) -> ImageResult: ...

    async def edit(self, request: ImageEditRequest) -> ImageResult: ...


_CONFIG_SPEC = (
    ToolConfigField(
        field="model",
        label="Image model",
        kind=KIND_CHOICE,
        default="gpt-image-2",
        choices=("gpt-image-2",),
        help="Image model used for generation and editing.",
    ),
    ToolConfigField(
        field="size",
        label="Image size",
        kind=KIND_CHOICE,
        default="auto",
        choices=("auto", "1024x1024", "1024x1536", "1536x1024"),
        help="Requested output dimensions; auto lets the image model choose.",
    ),
    ToolConfigField(
        field="quality",
        label="Image quality",
        kind=KIND_CHOICE,
        default="auto",
        choices=("auto", "low", "medium", "high"),
        help="Requested render quality.",
    ),
    ToolConfigField(
        field="background",
        label="Background",
        kind=KIND_CHOICE,
        default="auto",
        choices=("auto", "opaque", "transparent"),
        help="Requested background treatment.",
    ),
    ToolConfigField(
        field="max_calls_per_turn",
        label="Calls per turn",
        kind=KIND_INT,
        default=2,
        minimum=1,
        maximum=8,
        help="Maximum billable image calls in one outer Kimi turn.",
    ),
    ToolConfigField(
        field="max_reference_images",
        label="Reference images",
        kind=KIND_INT,
        default=5,
        minimum=1,
        maximum=5,
        help="Maximum workspace images accepted by one edit request.",
    ),
    ToolConfigField(
        field="max_attachments",
        label="Reply attachments",
        kind=KIND_INT,
        default=5,
        minimum=1,
        maximum=10,
        help="Maximum files this tool may queue on one reply.",
    ),
)


def init_image_gen_tool(
    registry: ToolRegistry,
    service: ImageGenServiceLike,
    workspace_manager: WorkspaceManager,
    workspace_locks: UserLocks,
    workspace_config: WorkspaceToolConfig,
) -> None:
    async def generate_image(args: dict, ctx: MessageContext) -> str:
        try:
            prompt, description, reference_paths = _parse_args(args, ctx)
        except ValueError as exc:
            return tool_error(str(exc))
        if not ctx.context_key:
            return tool_error("images can only be generated in a conversation context")
        max_calls = _configured_int(ctx, "max_calls_per_turn", default=2)
        max_attachments = _configured_int(ctx, "max_attachments", default=5)
        if ctx.budget_remaining(BudgetName.IMAGE_GEN_CALLS) <= 0:
            return tool_error(f"image generation limit reached ({max_calls} calls per turn)")
        if len(ctx.output_files) >= max_attachments:
            return tool_error(f"attachment limit reached ({max_attachments})")

        async with workspace_activity(workspace_locks, ctx):
            # Re-check mutable per-turn rails after acquiring the workspace lock.
            if ctx.budget_remaining(BudgetName.IMAGE_GEN_CALLS) <= 0:
                return tool_error(f"image generation limit reached ({max_calls} calls per turn)")
            if len(ctx.output_files) >= max_attachments:
                return tool_error(f"attachment limit reached ({max_attachments})")
            try:
                reference_urls = await _run_worker(
                    _reference_images,
                    workspace_manager,
                    ctx.workspace_key,
                    reference_paths,
                )
                request_fields = _request_fields(ctx)
                if not ctx.consume_budget(BudgetName.IMAGE_GEN_CALLS):
                    return tool_error(
                        f"image generation limit reached ({max_calls} calls per turn)"
                    )
                if reference_urls:
                    result = await service.edit(
                        ImageEditRequest(
                            prompt=prompt,
                            images=reference_urls,
                            **request_fields,
                        )
                    )
                else:
                    result = await service.generate(
                        ImageGenRequest(prompt=prompt, **request_fields)
                    )
                await _record_image_usage(
                    ctx,
                    result,
                    model=request_fields["model"],
                )
                output_path, relative_path, output_bytes = await _run_worker(
                    _write_output,
                    workspace_manager,
                    workspace_config,
                    ctx.workspace_key,
                    result.image_base64,
                    result.image_bytes,
                    on_cancelled_result=_remove_cancelled_output,
                )
                enqueue_workspace_file(
                    ctx,
                    workspace_manager,
                    output_path,
                    max_attachments=max_attachments,
                    description=description,
                )
            except ImageQuotaError as exc:
                reset = f"; resets at Unix timestamp {exc.resets_at}" if exc.resets_at else ""
                return tool_error(f"{exc}{reset}")
            except OSError as exc:
                return tool_error(scrub_user_paths(str(exc), workspace_manager, ctx.workspace_key))
            except (AttachmentLimitError, ImageGenError, ValueError) as exc:
                return tool_error(str(exc))

        return json.dumps(
            {
                "ok": True,
                "operation": "edit" if reference_paths else "generate",
                "path": relative_path,
                "filename": output_path.name,
                "bytes": output_bytes,
                "size": result.size,
                "background": result.background,
                "attachment_description": description,
                "attached_to_reply": True,
            },
            ensure_ascii=False,
        )

    registry.register(
        name=TOOL_NAME,
        description=(
            "Generate and attach an image from a prompt, or edit up to five workspace images. "
            "Use this tool whenever the user asks to create, draw, paint, render, or edit an "
            "image. For edits, provide workspace-relative reference_paths; import current "
            "Discord attachments into the workspace first. A successful call saves a reusable "
            "PNG under generated_images/ and queues it for the final Discord reply. The "
            "attachment_description must concisely describe the visual for accessibility."
        ),
        parameters={
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": (
                        "Complete image generation or editing prompt. Include composition, "
                        "subjects, style, lighting, text, and constraints needed by the model."
                    ),
                },
                "attachment_description": {
                    "type": "string",
                    "description": (
                        "Concise accessible description of the expected image for Discord."
                    ),
                },
                "reference_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": MAX_REFERENCE_IMAGES,
                    "description": (
                        "Workspace-relative PNG, JPEG, or WebP paths to edit. Omit for a new "
                        "image. Do not pass attachment filenames until import_attachment has "
                        "saved them to the workspace."
                    ),
                },
            },
            "required": ["prompt", "attachment_description"],
            "additionalProperties": False,
        },
        handler=generate_image,
        min_tier=TrustTier.REGULAR,
        searchable=False,
        category="Media",
        config_spec=_CONFIG_SPEC,
        budget_specs=(
            ToolBudgetSpec(
                BudgetName.IMAGE_GEN_CALLS,
                2,
                config_field="max_calls_per_turn",
            ),
        ),
    )


def _parse_args(args: dict[str, Any], ctx: MessageContext) -> tuple[str, str, tuple[str, ...]]:
    unknown = sorted(set(args) - {"prompt", "attachment_description", "reference_paths"})
    if unknown:
        raise ValueError(f"unknown field(s): {', '.join(unknown)}")
    prompt = _required_text(args.get("prompt"), "prompt", MAX_PROMPT_CHARS)
    description = _required_text(
        args.get("attachment_description"),
        "attachment_description",
        MAX_ATTACHMENT_DESCRIPTION_CHARS,
    )
    raw_paths = args.get("reference_paths")
    if raw_paths is None:
        paths: tuple[str, ...] = ()
    elif isinstance(raw_paths, list) and all(isinstance(item, str) for item in raw_paths):
        paths = tuple(item.strip() for item in raw_paths)
        if not all(paths):
            raise ValueError("reference_paths entries must not be empty")
    else:
        raise ValueError("reference_paths must be an array of workspace paths")
    configured_max = min(
        _configured_int(ctx, "max_reference_images", default=5), MAX_REFERENCE_IMAGES
    )
    if len(paths) > configured_max:
        raise ValueError(f"reference_paths is limited to {configured_max} images")
    if len(set(paths)) != len(paths):
        raise ValueError("reference_paths must not contain duplicates")
    return prompt, description, paths


def _required_text(value: object, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    text = value.strip()
    if len(text) > maximum:
        raise ValueError(f"{name} exceeds {maximum} characters")
    return text


def _configured_int(ctx: MessageContext, field: str, *, default: int) -> int:
    value = (ctx.tool_configs.get(TOOL_NAME) or {}).get(field, default)
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _request_fields(ctx: MessageContext) -> dict[str, str]:
    config = ctx.tool_configs.get(TOOL_NAME) or {}
    return {
        "model": str(config.get("model") or "gpt-image-2"),
        "size": str(config.get("size") or "auto"),
        "quality": str(config.get("quality") or "auto"),
        "background": str(config.get("background") or "auto"),
    }


async def _record_image_usage(
    ctx: MessageContext,
    result: ImageResult,
    *,
    model: str,
) -> None:
    call = LLMUsageCall(
        model=model,
        role="image_generation",
        usage=normalize_usage(result.usage),
        usage_present=result.usage is not None,
    )
    if ctx.record_usage_call is None:
        if ctx.usage_sink is not None:
            ctx.usage_sink.append(call)
        return

    task: asyncio.Future[None] = asyncio.ensure_future(ctx.record_usage_call(call))
    try:
        await await_uncancellable(task)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.warning("image generation usage recording failed", exc_info=True)


def _reference_images(
    workspace_manager: WorkspaceManager,
    workspace_key: WorkspaceKey,
    reference_paths: tuple[str, ...],
) -> tuple[ImageReference, ...]:
    references: list[ImageReference] = []
    total = 0
    resolved_seen: set[Path] = set()
    for user_path in reference_paths:
        path = workspace_manager.resolve_user_file_path(workspace_key, user_path, must_exist=True)
        if path in resolved_seen:
            raise ValueError("reference_paths resolve to duplicate files")
        resolved_seen.add(path)
        if not path.is_file():
            raise ValueError(f"reference image is not a file: {user_path}")
        with path.open("rb") as handle:
            raw = handle.read(MAX_REFERENCE_IMAGE_BYTES + 1)
        if len(raw) > MAX_REFERENCE_IMAGE_BYTES:
            raise ValueError(
                f"reference image exceeds {MAX_REFERENCE_IMAGE_BYTES} bytes: {user_path}"
            )
        total += len(raw)
        if total > MAX_REFERENCE_TOTAL_BYTES:
            raise ValueError(f"reference images exceed {MAX_REFERENCE_TOTAL_BYTES} aggregate bytes")
        media_type = _image_media_type(raw)
        if media_type is None:
            raise ValueError(f"reference image must be PNG, JPEG, or WebP: {user_path}")
        references.append(
            ImageReference(
                media_type=media_type,
                data_base64=base64.b64encode(raw).decode("ascii"),
            )
        )
    return tuple(references)


def _image_media_type(raw: bytes) -> str | None:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


async def _run_worker[T](
    func: Callable[..., T],
    *args: Any,
    on_cancelled_result: Callable[[T], None] | None = None,
) -> T:
    """Delay caller cancellation until an in-flight file worker has finished.

    ``asyncio.to_thread`` cannot stop a thread. Keeping the outer coroutine
    alive preserves the workspace activity lease until the worker is done, so
    privacy deletion and other workspace mutations cannot race a late read or
    write. A completed result can be disposed before cancellation propagates;
    worker and disposal errors remain visible to the caller.
    """
    worker = asyncio.create_task(asyncio.to_thread(func, *args))
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(worker)
        except asyncio.CancelledError:
            if worker.cancelled():
                raise
            cancelled = True
            continue
        if cancelled:
            if on_cancelled_result is not None:
                on_cancelled_result(result)
            raise asyncio.CancelledError
        return result


def _remove_cancelled_output(result: tuple[Path, str, int]) -> None:
    result[0].unlink(missing_ok=True)


def _write_output(
    workspace_manager: WorkspaceManager,
    workspace_config: WorkspaceToolConfig,
    workspace_key: WorkspaceKey,
    image_base64: str,
    image_bytes: bytes | None,
) -> tuple[Path, str, int]:
    raw = image_bytes
    if raw is None:
        # Test doubles and alternate services may not return verified bytes.
        try:
            raw = base64.b64decode(image_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("generated image is not valid base64") from exc
    relative_path = f"generated_images/image-{uuid4().hex}.png"
    destination = workspace_manager.resolve_user_file_path(workspace_key, relative_path)
    ensure_quota(
        workspace_manager,
        workspace_key,
        new_size=len(raw),
        destination=destination,
        temp_path=None,
        max_user_bytes=workspace_config.max_user_bytes,
        max_entries=workspace_config.max_workspace_entries,
    )
    if destination.exists():
        raise ValueError("generated image destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(raw)
        if destination.exists():
            raise ValueError("generated image destination already exists")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination, relative_path, len(raw)
