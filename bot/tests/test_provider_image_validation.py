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
from tests.helpers import (
    PNG_SIGNATURE_ONLY,
    VALID_PNG_BYTES,
    corrupt_png_crc,
    corrupt_png_idat_stream,
)
from kimi_agent_module_api.images import SUPPORTED_IMAGE_MEDIA_TYPES
from utils import image_types

BAD_PNGS = [
    pytest.param(PNG_SIGNATURE_ONLY + b"not really a png", id="signature-only"),
    pytest.param(corrupt_png_crc(VALID_PNG_BYTES), id="crc-corrupt"),
    pytest.param(VALID_PNG_BYTES[:-8], id="truncated"),
    pytest.param(corrupt_png_idat_stream(VALID_PNG_BYTES), id="undecodable-idat"),
]


def test_the_idat_fixture_is_caught_only_by_the_decode_layer() -> None:
    payload = corrupt_png_idat_stream(VALID_PNG_BYTES)
    assert image_types.structurally_valid_image_media_type(payload) == "image/png"
    assert image_types.decoded_image_media_type(payload) is None


def test_frame_discovery_is_bounded_without_reading_untrusted_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The validator probes one frame past its cap instead of trusting
    n_frames, so a hostile animation cannot force an unbounded walk."""

    class TooManyFrames:
        format = "GIF"
        size = (1, 1)

        @property
        def n_frames(self) -> int:
            raise AssertionError("validation must not scan an untrusted frame count")

        def __enter__(self) -> TooManyFrames:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def seek(self, frame: int) -> None:
            seek_calls.append(frame)

        def load(self) -> None:
            load_calls.append(None)

    seek_calls: list[int] = []
    load_calls: list[None] = []
    monkeypatch.setattr(image_types.Image, "open", lambda _stream: TooManyFrames())
    gif = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==")

    assert image_types.decoded_image_media_type(gif) is None
    assert seek_calls == list(range(image_types._MAX_VALIDATED_IMAGE_FRAMES + 1))
    assert len(load_calls) == image_types._MAX_VALIDATED_IMAGE_FRAMES


def test_every_sniffable_type_has_a_container_validator() -> None:
    # An unknown-but-sniffable type fails closed at runtime; this is the CI
    # tripwire that turns SDK drift into a red build instead.
    assert set(image_types._CONTAINER_VALIDATORS) == set(SUPPORTED_IMAGE_MEDIA_TYPES)


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


@pytest.mark.parametrize(
    ("pillow_format", "media_type"),
    [("GIF", "image/gif"), ("WEBP", "image/webp")],
)
def test_ordinary_animated_images_survive_frame_budgeting(
    pillow_format: str, media_type: str
) -> None:
    """The frame bound is a per-frame pixel charge, not a flat cap. A 40-frame
    animation is ordinary provider output and must not be dropped as
    undecodable."""

    from io import BytesIO

    from PIL import Image

    frames = [Image.new("RGB", (64, 48), (index, 0, 0)) for index in range(40)]
    buffer = BytesIO()
    frames[0].save(
        buffer,
        format=pillow_format,
        save_all=True,
        append_images=frames[1:],
        duration=50,
        loop=0,
    )

    assert image_types.decoded_image_media_type(buffer.getvalue()) == media_type


def test_trailing_bytes_after_the_container_terminator_are_tolerated() -> None:
    """Decoders ignore padding after IEND/EOI/trailer/RIFF end, and providers
    occasionally emit it; the container walk must not be stricter than Pillow."""

    from io import BytesIO

    from PIL import Image

    payloads: list[tuple[bytes, str]] = [(VALID_PNG_BYTES, "image/png")]
    for pillow_format, media_type in (
        ("JPEG", "image/jpeg"),
        ("GIF", "image/gif"),
        ("WEBP", "image/webp"),
    ):
        buffer = BytesIO()
        Image.new("RGB", (8, 8), (200, 10, 10)).save(buffer, format=pillow_format)
        payloads.append((buffer.getvalue(), media_type))

    for payload, media_type in payloads:
        padded = payload + b"\x00"
        assert image_types.structurally_valid_image_media_type(padded) == media_type, media_type
        assert image_types.decoded_image_media_type(padded) == media_type, media_type
