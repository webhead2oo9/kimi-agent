"""Shared, cached image transcription for model-comparison evals."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

from agent.attachments import image_part_hash
from evals.scenario import Scenario
from providers.image_caption import (
    IMAGE_CAPTION_MAX_TOKENS,
    IMAGE_CAPTION_PROMPT_VERSION,
    IMAGE_CAPTION_SYSTEM_PROMPT,
    format_image_caption,
)
from providers.types import ContentPart, ProviderCapability, ProviderRequest


class ImageCaptionProvider(Protocol):
    @property
    def model(self) -> str: ...

    async def run_turn(self, request: ProviderRequest): ...


def _cache_key(
    provider_model: str,
    images: Sequence[tuple[str, ContentPart]],
) -> tuple[str, list[str]]:
    digest = hashlib.sha256()
    digest.update(f"v{IMAGE_CAPTION_PROMPT_VERSION}\0{provider_model}\0".encode())
    image_hashes: list[str] = []
    for source, part in images:
        image_hash = image_part_hash(part)
        if image_hash is None:
            raise ValueError("eval image input could not be hashed")
        image_hashes.append(image_hash)
        digest.update(source.encode())
        digest.update(b"\0")
        digest.update(image_hash.encode())
        digest.update(b"\0")
    return digest.hexdigest(), image_hashes


def _cache_path(cache_dir: Path, key: str) -> Path:
    return cache_dir / f"v{IMAGE_CAPTION_PROMPT_VERSION}" / f"{key}.json"


def _read_cached_caption(
    path: Path,
    *,
    provider_model: str,
    image_hashes: list[str],
) -> str | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError, TypeError:
        return None
    if not isinstance(raw, dict):
        return None
    if raw.get("prompt_version") != IMAGE_CAPTION_PROMPT_VERSION:
        return None
    if raw.get("configured_model") != provider_model:
        return None
    if raw.get("image_hashes") != image_hashes:
        return None
    description = raw.get("description")
    return description.strip() if isinstance(description, str) and description.strip() else None


def _write_cached_caption(
    path: Path,
    *,
    provider_model: str,
    served_model: str,
    image_hashes: list[str],
    description: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "prompt_version": IMAGE_CAPTION_PROMPT_VERSION,
        "configured_model": provider_model,
        "served_model": served_model,
        "image_hashes": image_hashes,
        "description": description,
    }
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


async def caption_images(
    provider: ImageCaptionProvider,
    images: Sequence[tuple[str, ContentPart]],
    *,
    cache_dir: str | Path,
) -> ContentPart:
    """Return one production-format caption for an ordered image roster."""
    if not images:
        raise ValueError("caption_images requires at least one image")
    key, image_hashes = _cache_key(provider.model, images)
    path = _cache_path(Path(cache_dir), key)
    description = _read_cached_caption(
        path,
        provider_model=provider.model,
        image_hashes=image_hashes,
    )
    if description is None:
        parts = [
            ContentPart.from_text(
                "Produce the visual-context transcription for these numbered images."
            )
        ]
        for index, (source, image_part) in enumerate(images, start=1):
            parts.append(ContentPart.from_text(f"Image {index} ({source}):"))
            parts.append(image_part)
        response = await provider.run_turn(
            ProviderRequest(
                conversation_id=0,
                system_prompt=IMAGE_CAPTION_SYSTEM_PROMPT,
                messages=[],
                current_user_parts=parts,
                tools=[],
                max_tokens=IMAGE_CAPTION_MAX_TOKENS,
                temperature=None,
                requested_capabilities={ProviderCapability.IMAGE_INPUT},
                reasoning_enabled=False,
            )
        )
        description = (response.content or "").strip()
        if not description:
            raise RuntimeError("eval image captioner returned no description")
        _write_cached_caption(
            path,
            provider_model=provider.model,
            served_model=response.model or provider.model,
            image_hashes=image_hashes,
            description=description,
        )
    return ContentPart.from_text(format_image_caption(description))


async def caption_scenario_turns(
    scenario: Scenario,
    provider: ImageCaptionProvider,
    *,
    cache_dir: str | Path,
    image_loader: Callable[[str], ContentPart],
) -> dict[int, ContentPart]:
    """Caption each visual turn, preserving production's current-then-reply order."""
    captions: dict[int, ContentPart] = {}
    for turn_index, turn in enumerate(scenario.turns):
        images = [
            *(("current user message", image_loader(name)) for name in turn.images),
            *(("message being replied to", image_loader(name)) for name in turn.reply_images),
        ]
        if images:
            captions[turn_index] = await caption_images(
                provider,
                images,
                cache_dir=cache_dir,
            )
    return captions
