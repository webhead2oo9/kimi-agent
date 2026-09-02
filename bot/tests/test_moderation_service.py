from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Any

import pytest

from moderation.backends.base import ModerationBackend
from moderation.service import ModerationService
from moderation.types import Direction, ModerationError, ModerationVerdict
from providers.openrouter import OpenRouterProvider
from providers.types import ContentPart, GeneratedAsset
from tools.embeds import EmbedAttachment, EmbedSpec


class RecordingBackend(ModerationBackend):
    def __init__(
        self,
        verdict: ModerationVerdict | None = None,
        *,
        error: Exception | None = None,
        delay: float = 0.0,
    ) -> None:
        self.verdict = verdict or ModerationVerdict(categories={}, category_scores={})
        self.error = error
        self.delay = delay
        self.calls: list[list[Any]] = []

    async def moderate(self, items):
        self.calls.append(list(items))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.verdict


class SelectiveImageBackend(ModerationBackend):
    def __init__(self, failed_urls: set[str]) -> None:
        self.failed_urls = failed_urls
        self.calls: list[list[Any]] = []

    async def moderate(self, items):
        self.calls.append(list(items))
        for item in items:
            if item.image_url in self.failed_urls:
                raise ModerationError("image failed")
        return ModerationVerdict(categories={}, category_scores={})


@pytest.mark.asyncio
async def test_disabled_service_allows_without_calling_backend() -> None:
    backend = RecordingBackend(
        ModerationVerdict(categories={"sexual": True}, category_scores={"sexual": 1.0})
    )
    service = ModerationService(backend=backend, enabled=False, timeout_seconds=1.0)

    decision = await service.check(text="unsafe", direction=Direction.INPUT)

    assert not decision.blocked
    assert backend.calls == []


@pytest.mark.asyncio
async def test_service_fails_open_for_input_errors_and_closed_for_output_errors() -> None:
    service = ModerationService(
        backend=RecordingBackend(error=ModerationError("transport failed")),
        enabled=True,
        timeout_seconds=1.0,
    )

    input_decision = await service.check(text="hello", direction=Direction.INPUT)
    output_decision = await service.check(text="hello", direction=Direction.OUTPUT)

    assert not input_decision.blocked
    assert output_decision.blocked
    assert output_decision.matched_categories == ["moderation_error"]


@pytest.mark.asyncio
async def test_invalid_openrouter_inline_image_never_enters_output_moderation() -> None:
    backend = RecordingBackend()
    service = ModerationService(backend=backend, enabled=True, timeout_seconds=1.0)
    assets = OpenRouterProvider._parse_images(
        [
            {
                "image_url": {
                    "url": "data:text/html;base64,PCFkb2N0eXBlIGh0bWw+",
                }
            }
        ]
    )

    decision = await service.check(
        text="valid answer",
        direction=Direction.OUTPUT,
        generated_assets=assets,
    )

    assert assets == []
    assert not decision.blocked
    assert [[item.type for item in call] for call in backend.calls] == [["text"]]


@pytest.mark.asyncio
async def test_service_timeout_uses_directional_failure_policy() -> None:
    service = ModerationService(
        backend=RecordingBackend(delay=0.05),
        enabled=True,
        timeout_seconds=0.001,
    )

    input_decision = await service.check(text="hello", direction=Direction.INPUT)
    output_decision = await service.check(text="hello", direction=Direction.OUTPUT)

    assert not input_decision.blocked
    assert output_decision.blocked


@pytest.mark.asyncio
async def test_service_assembles_text_images_generated_assets_and_embed_attachment(
    tmp_path: Path,
) -> None:
    backend = RecordingBackend()
    service = ModerationService(backend=backend, enabled=True, timeout_seconds=1.0)
    input_image = ContentPart.from_image_url(
        url="data:image/png;base64,aW5wdXQ=",
        media_type="image/png",
    )
    asset = GeneratedAsset(
        kind="image",
        media_type="image/png",
        data_base64=base64.b64encode(b"generated").decode("ascii"),
        suggested_filename="generated.png",
    )
    embed_file = tmp_path / "embed.png"
    embed_file.write_bytes(b"\x89PNG\r\n\x1a\nembed")
    embed = EmbedSpec(
        title="Title",
        description="Description",
        image="attachment://embed.png",
        thumbnail_url="https://example.com/thumb.png",
        fields=(("Name", "Value", False),),
    )
    attachment = EmbedAttachment(
        path=str(embed_file),
        root=str(tmp_path),
        filename="embed.png",
    )

    await service.check(
        text="plain text",
        images=[input_image],
        generated_assets=[asset],
        embed=embed,
        embed_attachment=attachment,
        direction=Direction.OUTPUT,
    )

    assert len(backend.calls) == 5
    text_items = backend.calls[0]
    assert [item.type for item in text_items] == ["text", "text"]
    assert text_items[0].text == "plain text"
    assert "Title" in text_items[1].text
    assert "Description" in text_items[1].text
    assert "Name" in text_items[1].text
    image_urls = [call[0].image_url for call in backend.calls[1:]]
    assert image_urls == [
        "data:image/png;base64,aW5wdXQ=",
        "data:image/png;base64,Z2VuZXJhdGVk",
        "https://example.com/thumb.png",
        "data:image/png;base64,iVBORw0KGgplbWJlZA==",
    ]


@pytest.mark.asyncio
async def test_service_moderates_images_individually_and_reports_partial_failures() -> None:
    ok_image = ContentPart.from_image_url(
        url="data:image/png;base64,aW1hZ2Ux",
        media_type="image/png",
    )
    failed_image = ContentPart.from_image_url(
        url="data:image/png;base64,aW1hZ2Uy",
        media_type="image/png",
    )
    backend = SelectiveImageBackend({failed_image.image_url or ""})
    service = ModerationService(backend=backend, enabled=True, timeout_seconds=1.0)

    decision = await service.check(
        text="plain text",
        images=[ok_image, failed_image],
        direction=Direction.INPUT,
    )

    assert not decision.blocked
    assert decision.checked_image_urls == (ok_image.image_url,)
    assert decision.failed_image_urls == (failed_image.image_url,)
    assert [len(call) for call in backend.calls] == [1, 1, 1]


@pytest.mark.asyncio
async def test_service_blocks_input_when_all_image_checks_fail() -> None:
    failed_image = ContentPart.from_image_url(
        url="data:image/png;base64,aW1hZ2Uy",
        media_type="image/png",
    )
    backend = SelectiveImageBackend({failed_image.image_url or ""})
    service = ModerationService(backend=backend, enabled=True, timeout_seconds=1.0)

    decision = await service.check(
        text="plain text",
        images=[failed_image],
        direction=Direction.INPUT,
    )

    assert decision.blocked
    assert decision.matched_categories == ["moderation_error"]
    assert decision.checked_image_urls == ()
    assert decision.failed_image_urls == (failed_image.image_url,)


@pytest.mark.asyncio
async def test_backend_outage_marks_the_block_as_an_error_not_a_category_match() -> None:
    """An outage blocks every output; the reply must not read as a refusal to answer."""
    service = ModerationService(
        backend=RecordingBackend(error=ModerationError("transport failed")),
        enabled=True,
        timeout_seconds=1.0,
        output_refusal="policy text",
        error_refusal="outage text",
    )

    decision = await service.check(text="hello", direction=Direction.OUTPUT)

    assert decision.blocked
    assert decision.error
    assert service.refusal_for(Direction.OUTPUT, error=decision.error) == "outage text"


@pytest.mark.asyncio
async def test_category_match_keeps_the_directional_refusal() -> None:
    service = ModerationService(
        backend=RecordingBackend(
            ModerationVerdict(categories={"sexual": True}, category_scores={"sexual": 1.0})
        ),
        enabled=True,
        timeout_seconds=1.0,
        input_refusal="input text",
        output_refusal="output text",
        error_refusal="outage text",
    )

    decision = await service.check(text="unsafe", direction=Direction.INPUT)

    assert decision.blocked
    assert not decision.error
    assert service.refusal_for(Direction.INPUT, error=decision.error) == "input text"
    assert service.refusal_for(Direction.OUTPUT, error=decision.error) == "output text"
