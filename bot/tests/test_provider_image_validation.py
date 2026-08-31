"""Every path that turns provider-returned image bytes into a user-visible file
must apply the same decode-level validation.

Three sinks exist today: OpenRouter parses inline images itself, the Codex
provider hands ``image_generation_call`` results to ``write_generated_assets``
(as does every other provider that emits ``GeneratedAsset``), and the
``generate_image`` tool's ``ImageGenService`` verifies its backend's output.
A validator hardened on one of them and not the others is silent drift, so
this file drives all three with the same bad payloads.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from image_gen.service import ImageGenService
from image_gen.types import ImageEditRequest, ImageGenError, ImageGenRequest, ImageResult
from providers.assets import write_generated_assets
from providers.codex import CodexProvider
from providers.openrouter import OpenRouterProvider
from tests.helpers import PNG_SIGNATURE_ONLY, VALID_PNG_BYTES, corrupt_png_crc

BAD_PNGS = [
    pytest.param(PNG_SIGNATURE_ONLY + b"not really a png", id="signature-only"),
    pytest.param(corrupt_png_crc(VALID_PNG_BYTES), id="crc-corrupt"),
    pytest.param(VALID_PNG_BYTES[:-8], id="truncated"),
]


def _b64(payload: bytes) -> str:
    return base64.b64encode(payload).decode("ascii")


def _openrouter_files(payload: bytes, tmp_path: Path) -> list[Path]:
    assets = OpenRouterProvider._parse_images(
        [{"image_url": {"url": f"data:image/png;base64,{_b64(payload)}"}}]
    )
    return write_generated_assets(assets, output_dir=tmp_path)


def _codex_files(payload: bytes, tmp_path: Path) -> list[Path]:
    provider = CodexProvider(transport=object(), model="gpt-5.5", image_format="png")
    assets = provider._parse_generated_assets(
        [{"type": "image_generation_call", "result": _b64(payload)}]
    )
    return write_generated_assets(assets, output_dir=tmp_path)


SINKS = [
    pytest.param(_openrouter_files, id="openrouter"),
    pytest.param(_codex_files, id="codex-generated-assets"),
]


@pytest.mark.parametrize("sink", SINKS)
@pytest.mark.parametrize("payload", BAD_PNGS)
def test_provider_asset_paths_reject_pngs_that_do_not_fully_decode(
    sink, payload: bytes, tmp_path: Path
) -> None:
    assert sink(payload, tmp_path) == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("sink", SINKS)
def test_provider_asset_paths_accept_a_decodable_png(sink, tmp_path: Path) -> None:
    paths = sink(VALID_PNG_BYTES, tmp_path)

    assert len(paths) == 1
    assert paths[0].suffix == ".png"
    assert paths[0].read_bytes() == VALID_PNG_BYTES


class _Backend:
    name = "stub"

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def available(self) -> bool:
        return True

    async def generate(self, request: ImageGenRequest) -> ImageResult:
        return ImageResult(image_base64=_b64(self._payload))

    async def edit(self, request: ImageEditRequest) -> ImageResult:
        return ImageResult(image_base64=_b64(self._payload))


def _request() -> ImageGenRequest:
    return ImageGenRequest(prompt="a red fox", model="gpt-image-2", size="auto")


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", BAD_PNGS)
async def test_image_gen_service_rejects_pngs_that_do_not_fully_decode(payload: bytes) -> None:
    service = ImageGenService(_Backend(payload))

    with pytest.raises(ImageGenError, match="not a decodable PNG"):
        await service.generate(_request())
    with pytest.raises(ImageGenError, match="not a decodable PNG"):
        await service.edit(
            ImageEditRequest(prompt="bluer", model="gpt-image-2", images=(), size="auto")
        )


@pytest.mark.asyncio
async def test_image_gen_service_accepts_a_decodable_png() -> None:
    result = await ImageGenService(_Backend(VALID_PNG_BYTES)).generate(_request())

    assert result.image_bytes == VALID_PNG_BYTES
