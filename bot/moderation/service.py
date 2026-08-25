from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from utils.image_types import normalize_image_data_url, sniff_image_media_type
from moderation.backends.base import ModerationBackend
from moderation.policy import ModerationPolicy
from moderation.types import (
    Direction,
    ModerationDecision,
    ModerationError,
    ModerationItem,
    ModerationVerdict,
)
from observability.events import emit_moderation
from providers.types import ContentPart, ContentPartType, GeneratedAsset
from trust.tiers import TrustTier

if TYPE_CHECKING:
    from tools.embeds import EmbedAttachment, EmbedSpec

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ModerationRun:
    verdict: ModerationVerdict
    checked_image_urls: tuple[str, ...] | None = None
    failed_image_urls: tuple[str, ...] = ()


class _ImageModerationUnavailable(Exception):
    def __init__(self, failed_image_urls: tuple[str, ...]) -> None:
        super().__init__("image moderation failed")
        self.failed_image_urls = failed_image_urls


class ModerationService:
    def __init__(
        self,
        *,
        backend: ModerationBackend,
        enabled: bool,
        timeout_seconds: float,
        input_images: bool = True,
        output_images: bool = True,
        input_refusal: str = (
            "That message didn't pass my content filter, so I didn't read it. Try rewording it."
        ),
        output_refusal: str = (
            "I wrote a reply, but it didn't pass my content filter, so I'm not posting it. Nothing's wrong on your end; try asking a different way."
        ),
        error_refusal: str = (
            "I can't run my content check right now, so I'm holding this one back. Try again in a minute."
        ),
        output_exempt_tier: TrustTier | None = None,
        policy: ModerationPolicy | None = None,
    ) -> None:
        self._backend = backend
        self.enabled = enabled
        self._timeout_seconds = timeout_seconds
        self._input_images = input_images
        self._output_images = output_images
        self._input_refusal = input_refusal
        self._output_refusal = output_refusal
        self._error_refusal = error_refusal
        self.output_exempt_tier = output_exempt_tier
        self._policy = policy or ModerationPolicy()

    async def close(self) -> None:
        await self._backend.close()

    def refusal_for(self, direction: Direction, *, error: bool = False) -> str:
        # An outage blocks every output (see _failure_decision), so reusing the
        # policy-hit text would tell the whole server it refused to answer them.
        if error:
            return self._error_refusal
        if direction is Direction.INPUT:
            return self._input_refusal
        return self._output_refusal

    async def check(
        self,
        *,
        text: str,
        direction: Direction,
        images: Sequence[ContentPart | None] = (),
        generated_assets: Sequence[GeneratedAsset] = (),
        embed: EmbedSpec | None = None,
        embed_attachment: EmbedAttachment | None = None,
        user_id: str = "",
        channel_id: str = "",
        thread_id: str | None = None,
        trust_tier: str = "",
    ) -> ModerationDecision:
        if not self.enabled:
            return _allowed(direction)
        try:
            items = self._assemble_items(
                text=text,
                direction=direction,
                images=images,
                generated_assets=generated_assets,
                embed=embed,
                embed_attachment=embed_attachment,
            )
            if not items:
                return _allowed(direction)
            moderation_run = await self._moderate_items(items, direction)
        except _ImageModerationUnavailable as exc:
            log.warning(
                "Image moderation unavailable for %s content; blocking because no safe "
                "filtered image set is available",
                direction.value,
            )
            return ModerationDecision(
                blocked=True,
                matched_categories=["moderation_error"],
                checked_image_urls=(),
                failed_image_urls=exc.failed_image_urls,
                error=True,
            )
        except Exception:
            log.warning(
                "Moderation check failed; applying %s failure policy",
                direction.value,
                exc_info=True,
            )
            return self._failure_decision(direction)

        verdict = moderation_run.verdict
        decision = self._policy.decide(verdict, direction)
        if moderation_run.checked_image_urls is not None or moderation_run.failed_image_urls:
            decision = ModerationDecision(
                blocked=decision.blocked,
                matched_categories=decision.matched_categories,
                category_scores=decision.category_scores,
                checked_image_urls=moderation_run.checked_image_urls,
                failed_image_urls=moderation_run.failed_image_urls,
            )
        if decision.blocked:
            log.warning(
                "Moderation blocked %s content categories=%s user_id=%s channel_id=%s "
                "trust_tier=%s",
                direction.value,
                ",".join(decision.matched_categories),
                user_id,
                channel_id,
                trust_tier,
            )
            emit_moderation(
                direction=direction.value,
                matched_categories=decision.matched_categories,
                category_scores=decision.category_scores,
                user_id=user_id,
                channel_id=channel_id,
                thread_id=thread_id,
                trust_tier=trust_tier,
            )
        return decision

    def _failure_decision(self, direction: Direction) -> ModerationDecision:
        # Deliberate asymmetry (pinned by tests/test_moderation_service.py): on
        # general backend errors/timeouts, INPUT fails open (availability wins,
        # so the unscreened text still reaches the provider and can drive tool
        # side effects), while OUTPUT fails closed so the bot never emits an
        # unchecked reply (during an outage every reply becomes the output
        # refusal string). Image-specific input failures are handled earlier:
        # partially failed image sets are filtered, while fully failed image
        # sets block because no checked image can be forwarded.
        # Failure-path blocks are logged but skip the emit_moderation event,
        # which fires only on real category matches in check() above.
        if direction is Direction.INPUT:
            return _allowed(direction)
        return ModerationDecision(
            blocked=True,
            matched_categories=["moderation_error"],
            error=True,
        )

    def _assemble_items(
        self,
        *,
        text: str,
        direction: Direction,
        images: Sequence[ContentPart | None],
        generated_assets: Sequence[GeneratedAsset],
        embed: EmbedSpec | None,
        embed_attachment: EmbedAttachment | None,
    ) -> list[ModerationItem]:
        items: list[ModerationItem] = []
        seen_images: set[str] = set()
        _append_text(items, text)
        if self._images_enabled(direction):
            for image in images:
                if image is not None:
                    _append_content_part_image(items, image, seen_images)
            for asset in generated_assets:
                _append_image_url(items, _generated_asset_data_url(asset), seen_images)
            if embed is not None:
                _append_text(items, _embed_text(embed))
                for url in (embed.image, embed.thumbnail_url):
                    if _is_direct_image_url(url):
                        _append_image_url(items, url or "", seen_images)
            if embed_attachment is not None:
                _append_image_url(items, _embed_attachment_data_url(embed_attachment), seen_images)
        elif embed is not None:
            _append_text(items, _embed_text(embed))
        return items

    def _images_enabled(self, direction: Direction) -> bool:
        if direction is Direction.INPUT:
            return self._input_images
        return self._output_images

    async def _moderate_items(
        self,
        items: list[ModerationItem],
        direction: Direction,
    ) -> _ModerationRun:
        image_items = [item for item in items if item.type == "image_url"]
        if not image_items:
            return _ModerationRun(await self._moderate_backend(items))

        verdicts: list[ModerationVerdict] = []
        text_items = [item for item in items if item.type == "text"]
        if text_items:
            try:
                verdicts.append(await self._moderate_backend(text_items))
            except Exception:
                if direction is Direction.OUTPUT:
                    raise
                log.warning(
                    "Text moderation check failed for input content; applying input "
                    "failure policy to text and continuing with image checks",
                    exc_info=True,
                )

        checked_image_urls: list[str] = []
        failed_image_urls: list[str] = []
        for index, item in enumerate(image_items, start=1):
            try:
                verdicts.append(await self._moderate_backend([item]))
                if item.image_url:
                    checked_image_urls.append(item.image_url)
            except Exception:
                if item.image_url:
                    failed_image_urls.append(item.image_url)
                log.warning(
                    "Image moderation check %d/%d failed for %s content",
                    index,
                    len(image_items),
                    direction.value,
                    exc_info=True,
                )

        if failed_image_urls and (direction is Direction.OUTPUT or not checked_image_urls):
            raise _ImageModerationUnavailable(tuple(failed_image_urls))

        return _ModerationRun(
            verdict=_merge_verdicts(verdicts),
            checked_image_urls=tuple(checked_image_urls),
            failed_image_urls=tuple(failed_image_urls),
        )

    async def _moderate_backend(self, items: list[ModerationItem]) -> ModerationVerdict:
        return await asyncio.wait_for(
            self._backend.moderate(items),
            timeout=self._timeout_seconds,
        )


def _allowed(direction: Direction) -> ModerationDecision:
    return ModerationDecision(blocked=False, matched_categories=[])


def _append_text(items: list[ModerationItem], text: str) -> None:
    stripped = text.strip()
    if stripped:
        items.append(ModerationItem.from_text(stripped))


def _append_content_part_image(
    items: list[ModerationItem],
    part: ContentPart,
    seen: set[str],
) -> None:
    if part.type is not ContentPartType.IMAGE or not part.image_url:
        return
    image_url, _media_type = normalize_image_data_url(part.image_url, part.media_type)
    _append_image_url(items, image_url, seen)


def _append_image_url(
    items: list[ModerationItem],
    url: str,
    seen: set[str],
) -> None:
    if not url or url in seen:
        return
    seen.add(url)
    items.append(ModerationItem.from_image_url(url))


def _generated_asset_data_url(asset: GeneratedAsset) -> str:
    data_url = f"data:{asset.media_type};base64,{asset.data_base64}"
    normalized, _media_type = normalize_image_data_url(data_url, asset.media_type)
    return normalized


def _embed_text(embed: EmbedSpec) -> str:
    chunks: list[str] = []
    for value in (
        embed.title,
        embed.description,
        embed.author_name,
        embed.footer_text,
    ):
        if value:
            chunks.append(value)
    for name, value, _inline in embed.fields:
        chunks.extend([name, value])
    return "\n".join(chunks)


def _is_direct_image_url(url: str | None) -> bool:
    if not url:
        return False
    return url.startswith(("https://", "data:"))


def _embed_attachment_data_url(attachment: EmbedAttachment) -> str:
    path = Path(attachment.path)
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ModerationError("Could not read embed attachment for moderation") from exc
    media_type = sniff_image_media_type(payload)
    if media_type is None:
        raise ModerationError("Embed attachment is not a supported image")
    encoded = base64.b64encode(payload).decode("ascii")
    return f"data:{media_type};base64,{encoded}"


def _merge_verdicts(verdicts: Sequence[ModerationVerdict]) -> ModerationVerdict:
    categories: dict[str, bool] = {}
    category_scores: dict[str, float] = {}
    for verdict in verdicts:
        for name, matched in verdict.categories.items():
            categories[name] = categories.get(name, False) or matched
        for name, score in verdict.category_scores.items():
            category_scores[name] = max(category_scores.get(name, 0.0), score)
    return ModerationVerdict(categories=categories, category_scores=category_scores)
