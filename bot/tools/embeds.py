"""The ``build_discord_embed`` tool: attach one rich Discord embed to the reply.

A browse-tools-only (searchable), MEMBER-tier tool with no config gate. It does **not**
return data to the model for reasoning; it queues a single validated embed that rides to
Discord on the final reply alongside any caption the model still chose to write.

``build_embed_payload`` is a pure, ``ctx``-read-only validator: it normalizes the model's
arguments into an ``EmbedSpec`` (plain data, no ``discord`` import) plus an optional
``EmbedAttachment`` for a workspace image, raising ``ValueError`` with a self-correcting
message on any rule violation. The handler only mutates ``ctx`` (storing the spec/attachment)
*after* validation fully succeeds, so a rejected call never leaves a partial embed behind.

The ``discord.Embed`` object is built later, at the Discord boundary in ``discord_adapter/io.py``;
nothing here imports ``discord``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from workspace import WorkspaceManager
from tools._common import tool_error
from tools.registry import MessageContext, ToolRegistry
from tools.workspace.common import UserLocks, workspace_activity
from trust.tiers import TrustTier

log = logging.getLogger(__name__)

# Discord's documented hard limits.
TITLE_MAX = 256
DESCRIPTION_MAX = 4096
FOOTER_MAX = 2048
AUTHOR_NAME_MAX = 256
FIELDS_MAX = 25
FIELD_NAME_MAX = 256
FIELD_VALUE_MAX = 1024
TOTAL_MAX = 6000
URL_MAX = 2048
COLOR_MAX = 0xFFFFFF


@dataclass(frozen=True)
class EmbedSpec:
    """Validated, plain-data description of one embed. No ``discord`` types."""

    title: str | None = None
    description: str | None = None
    url: str | None = None
    color: int | None = None
    author_name: str | None = None
    author_url: str | None = None
    author_icon_url: str | None = None
    footer_text: str | None = None
    footer_icon_url: str | None = None
    image: str | None = None  # an https URL or an "attachment://<name>" reference
    thumbnail_url: str | None = None
    fields: tuple[tuple[str, str, bool], ...] = ()
    timestamp: bool = False


@dataclass(frozen=True)
class EmbedAttachment:
    """The one workspace image owned by the pending embed, to upload with the reply."""

    path: str
    root: str
    filename: str


def build_embed_payload(
    args: Mapping[str, Any],
    ctx: MessageContext,
    workspace_manager: WorkspaceManager,
) -> tuple[EmbedSpec, EmbedAttachment | None]:
    """Validate model args into an ``EmbedSpec`` + optional ``EmbedAttachment``.

    Raises ``ValueError`` (message becomes the ``tool_error`` string) on any rule
    violation. Reads ``ctx`` but never mutates it.
    """
    title = _text(args.get("title"), "title", TITLE_MAX)
    description = _text(args.get("description"), "description", DESCRIPTION_MAX)
    footer_text = _text(args.get("footer_text"), "footer_text", FOOTER_MAX)
    author_name = _text(args.get("author_name"), "author_name", AUTHOR_NAME_MAX)

    url = _url(args.get("url"), "url")
    author_url = _url(args.get("author_url"), "author_url")
    author_icon_url = _url(args.get("author_icon_url"), "author_icon_url")
    footer_icon_url = _url(args.get("footer_icon_url"), "footer_icon_url")
    thumbnail_url = _url(args.get("thumbnail_url"), "thumbnail_url")
    image_url = _url(args.get("image_url"), "image_url")

    color = _color(args.get("color"))
    fields = _fields(args.get("fields"))

    image_ws = _stripped(args.get("image_workspace_path"))
    if image_url and image_ws:
        raise ValueError("Provide either image_url or image_workspace_path, not both.")

    has_image = bool(image_url or image_ws)
    if not (title or description or fields or has_image):
        raise ValueError("An embed needs at least a title, description, fields, or an image.")
    _check_total(title, description, footer_text, author_name, fields)

    # Workspace-image resolution is the only filesystem step; do it last, after every
    # pure rule passes, so a cheap rejection never touches the disk.
    image = image_url
    attachment: EmbedAttachment | None = None
    if image_ws:
        image, attachment = _resolve_workspace_image(workspace_manager, ctx, image_ws)

    return (
        EmbedSpec(
            title=title,
            description=description,
            url=url,
            color=color,
            author_name=author_name,
            author_url=author_url,
            author_icon_url=author_icon_url,
            footer_text=footer_text,
            footer_icon_url=footer_icon_url,
            image=image,
            thumbnail_url=thumbnail_url,
            fields=fields,
            timestamp=_bool(args.get("timestamp")),
        ),
        attachment,
    )


_SUMMARY_MAX = 200


def embed_transcript_summary(spec: EmbedSpec) -> str:
    """A compact one-line stand-in persisted when an embed-only reply has no caption.

    Keeps embed replies visible to later turns built from the SQLite transcript, which
    stores only message text.
    """
    title = spec.title
    desc_line = spec.description.splitlines()[0].strip() if spec.description else ""
    if title and desc_line:
        body = f"{title}: {desc_line}"
    elif title:
        body = title
    elif desc_line:
        body = desc_line
    elif spec.author_name:
        body = spec.author_name
    elif spec.fields:
        body = spec.fields[0][0]
    elif spec.image:
        body = "(image)"
    else:
        body = "(no text)"
    return f"[embed] {body}"[:_SUMMARY_MAX]


def init_embed_tool(
    registry: ToolRegistry,
    workspace_manager: WorkspaceManager,
    workspace_locks: UserLocks | None = None,
) -> None:
    locks = workspace_locks or UserLocks()

    async def handler(args: dict, ctx: MessageContext) -> str:
        try:
            async with workspace_activity(locks, ctx):
                spec, attachment = build_embed_payload(args, ctx, workspace_manager)
        except (ValueError, FileNotFoundError) as exc:
            return tool_error(str(exc))
        # Mutate ctx only after full success: replace both slots together so a re-call
        # never leaves a stale embed image behind.
        ctx.embed = spec
        ctx.embed_attachment = attachment
        return json.dumps({"queued": True, "image": spec.image})

    registry.register(
        name="build_discord_embed",
        description=(
            "Build a rich Discord embed for this reply. See the 'embed' skill for "
            "fields, limits, and examples."
        ),
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Embed title (<=256 chars)."},
                "description": {
                    "type": "string",
                    "description": "Main body text; supports Markdown (<=4096 chars).",
                },
                "url": {
                    "type": "string",
                    "description": "https URL the title links to.",
                },
                "color": {
                    "type": "string",
                    "description": 'Accent color, e.g. "#5865F2", "0x5865F2", or an integer.',
                },
                "author_name": {"type": "string", "description": "Author line text."},
                "author_url": {"type": "string", "description": "https URL for the author."},
                "author_icon_url": {
                    "type": "string",
                    "description": "https icon shown beside the author name.",
                },
                "footer_text": {"type": "string", "description": "Footer text (<=2048 chars)."},
                "footer_icon_url": {
                    "type": "string",
                    "description": "https icon shown beside the footer.",
                },
                "image_url": {
                    "type": "string",
                    "description": "https URL for the large embed image.",
                },
                "image_workspace_path": {
                    "type": "string",
                    "description": (
                        "Workspace/generated image path to show as the large image "
                        "(uploaded and referenced via attachment://). Mutually exclusive "
                        "with image_url."
                    ),
                },
                "thumbnail_url": {
                    "type": "string",
                    "description": "https URL for the small corner thumbnail.",
                },
                "fields": {
                    "type": "array",
                    "description": "Up to 25 name/value field rows.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "value": {"type": "string"},
                            "inline": {"type": "boolean"},
                        },
                        "required": ["name", "value"],
                    },
                },
                "timestamp": {
                    "type": "boolean",
                    "description": "Show the current time in the footer.",
                },
            },
        },
        handler=handler,
        min_tier=TrustTier.MEMBER,
        searchable=True,
        category="Discord",
    )


def _stripped(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _text(value: Any, name: str, max_chars: int) -> str | None:
    text = _stripped(value)
    if text is None:
        return None
    if len(text) > max_chars:
        raise ValueError(f"{name} must be {max_chars} characters or fewer.")
    return text


def _url(value: Any, name: str) -> str | None:
    text = _stripped(value)
    if text is None:
        return None
    if len(text) > URL_MAX:
        raise ValueError(f"{name} must be {URL_MAX} characters or fewer.")
    if not text.lower().startswith("https://"):
        raise ValueError(f"{name} must be an https:// URL.")
    return text


def _color(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("color must be a hex string or an integer 0–16777215.")
    if isinstance(value, int):
        parsed = value
    else:
        text = str(value).strip()
        try:
            if text.startswith("#"):
                parsed = int(text[1:], 16)
            elif text.lower().startswith("0x"):
                parsed = int(text[2:], 16)
            else:
                parsed = int(text, 10)
        except ValueError as exc:
            raise ValueError(
                'color must be a hex string like "#5865F2" or an integer 0–16777215.'
            ) from exc
    if not (0 <= parsed <= COLOR_MAX):
        raise ValueError("color must be between 0x000000 and 0xFFFFFF.")
    return parsed


def _fields(value: Any) -> tuple[tuple[str, str, bool], ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError("fields must be a list of {name, value} objects.")
    if len(value) > FIELDS_MAX:
        raise ValueError(f"An embed may have at most {FIELDS_MAX} fields.")
    out: list[tuple[str, str, bool]] = []
    for entry in value:
        if not isinstance(entry, Mapping):
            raise ValueError("each field must be an object with name and value.")
        name = str(entry.get("name", "")).strip()
        field_value = str(entry.get("value", "")).strip()
        if not name:
            raise ValueError("field name is required and cannot be empty.")
        if not field_value:
            raise ValueError("field value is required and cannot be empty.")
        if len(name) > FIELD_NAME_MAX:
            raise ValueError(f"field name must be {FIELD_NAME_MAX} characters or fewer.")
        if len(field_value) > FIELD_VALUE_MAX:
            raise ValueError(f"field value must be {FIELD_VALUE_MAX} characters or fewer.")
        out.append((name, field_value, bool(entry.get("inline", False))))
    return tuple(out)


def _check_total(
    title: str | None,
    description: str | None,
    footer_text: str | None,
    author_name: str | None,
    fields: tuple[tuple[str, str, bool], ...],
) -> None:
    total = len(title or "") + len(description or "")
    total += len(footer_text or "") + len(author_name or "")
    total += sum(len(name) + len(value) for name, value, _ in fields)
    if total > TOTAL_MAX:
        raise ValueError(
            f"Embed text totals {total} characters; the combined limit is {TOTAL_MAX}."
        )


def _bool(value: Any) -> bool:
    return value is True


def _resolve_workspace_image(
    workspace_manager: WorkspaceManager,
    ctx: MessageContext,
    raw_path: str,
) -> tuple[str, EmbedAttachment]:
    """Resolve a workspace/generated image and return (attachment_ref, EmbedAttachment).

    Mirrors ``tools/media.py:_workspace_ref``: try the per-user files dir first, then fall
    back to the conversation's generated artifacts for ``generated/`` paths.
    """
    path, root = _resolve_image_path(workspace_manager, ctx, raw_path)
    filename = path.name
    existing = {Path(queued).name for queued in ctx.output_files}
    if filename in existing:
        raise ValueError(
            f"A file named '{filename}' is already attached to this reply; rename the "
            "image so its filename is unique."
        )
    return f"attachment://{filename}", EmbedAttachment(
        path=str(path), root=str(root), filename=filename
    )


def _resolve_image_path(
    workspace_manager: WorkspaceManager,
    ctx: MessageContext,
    raw_path: str,
) -> tuple[Path, Path]:
    try:
        path = workspace_manager.resolve_user_file_path(
            ctx.workspace_key, raw_path, must_exist=True
        )
    except FileNotFoundError:
        # mypy pins path to Path from the try block; None is the not-found
        # sentinel that the generated/ fallback below checks for.
        path = None  # type: ignore[assignment]
    if path is not None:
        if path.is_symlink() or not path.is_file():
            raise ValueError("image path is not a file.")
        return path, workspace_manager.user_files_dir(ctx.workspace_key).resolve()

    if not raw_path.startswith("generated/"):
        raise ValueError(f"workspace image not found: {raw_path}")
    try:
        resolved = workspace_manager.resolve_context_generated_file(
            raw_path, context_key=ctx.context_key, must_exist=True
        )
    except FileNotFoundError as exc:
        raise ValueError(f"generated image not found: {raw_path}") from exc
    return resolved.path, resolved.root
