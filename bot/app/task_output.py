"""Freeze validated task attachments before the workspace can change."""

from __future__ import annotations

from dataclasses import asdict
import base64
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from discord_adapter.io import prepare_attachment_delivery
from providers.assets import write_generated_assets
from providers.types import ContentPart, GeneratedAsset
from tools.registry import TurnOutbox
from utils.image_types import sniff_image_media_type


def snapshot_images(files: list[tuple[str, str | None, bytes]]) -> list[ContentPart]:
    """Present deterministic image files to the configured output moderation boundary."""
    images = []
    for _name, _description, data in files:
        if media_type := sniff_image_media_type(data):
            images.append(
                ContentPart.from_image_url(
                    url=f"data:{media_type};base64,{base64.b64encode(data).decode('ascii')}",
                    media_type=media_type,
                )
            )
    return images


def snapshot_output(
    channel: Any,
    outbox: TurnOutbox,
    assets: list[GeneratedAsset],
) -> tuple[list[tuple[str, str | None, bytes]], dict[str, Any] | None]:
    """Called under workspace ownership, off the event loop. Bound durable bytes."""
    with TemporaryDirectory(prefix="bram-task-output-") as temporary:
        roots = list(outbox.allowed_file_roots)
        paths = list(outbox.output_files)
        if outbox.embed_attachment is not None:
            paths.append(outbox.embed_attachment.path)
            roots.append(outbox.embed_attachment.root)
        generated = write_generated_assets(assets, output_dir=Path(temporary))
        paths.extend(str(path) for path in generated)
        roots.append(temporary)
        plan = prepare_attachment_delivery(
            channel,
            output_files=list(dict.fromkeys(paths)),
            allowed_file_roots=roots,
            output_file_descriptions=dict(outbox.output_file_descriptions),
            embed=outbox.embed,
        )
        requested = {str(Path(path).resolve()) for path in paths}
        if plan.omitted or len(plan.files) != len(requested):
            raise ValueError("Task output contains unavailable files or exceeds attachment limits")
        descriptions = dict(plan.file_descriptions)
        files: list[tuple[str, str | None, bytes]] = []
        total = 0
        for path in plan.files:
            with path.open("rb") as handle:
                data = handle.read(25 * 1024 * 1024 + 1)
            total += len(data)
            if total > 25 * 1024 * 1024:
                raise ValueError("Task output attachments exceed 25 MiB in total")
            files.append((path.name, descriptions.get(str(path)), data))
        return files, asdict(plan.embed) if plan.embed else None
