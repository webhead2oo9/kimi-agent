import base64
from pathlib import Path

import pytest

from kimi_agent_module_api.images import SUPPORTED_IMAGE_MEDIA_TYPES
from providers import assets as asset_writer
from providers.assets import write_generated_assets
from providers.types import GeneratedAsset
from tests.helpers import (
    PNG_SIGNATURE_ONLY,
    VALID_JPEG_BYTES,
    VALID_PNG_BYTES,
    corrupt_png_crc,
)
from utils.image_types import IMAGE_MEDIA_TYPE_SUFFIXES

from tests.helpers import VALID_PNG_BASE64 as PNG_BASE64


def test_write_generated_assets_decodes_base64_images(tmp_path: Path) -> None:
    paths = write_generated_assets(
        [
            GeneratedAsset(
                kind="image",
                media_type="image/png",
                data_base64=PNG_BASE64,
                suggested_filename="answer.png",
            )
        ],
        output_dir=tmp_path,
    )

    assert len(paths) == 1
    assert paths[0].name == "answer.png"
    assert paths[0].read_bytes() == VALID_PNG_BYTES


def test_write_generated_assets_skips_undecodable_data(tmp_path: Path) -> None:
    # Upstream providers can hand back a non-data URL (or otherwise invalid
    # base64); decoding it must not crash the response turn. The bad asset is
    # skipped and valid ones are still written.
    paths = write_generated_assets(
        [
            GeneratedAsset(
                kind="image",
                media_type="image/png",
                data_base64="https://example.com/not-base64.png",
                suggested_filename="bad.png",
            ),
            GeneratedAsset(
                kind="image",
                media_type="image/png",
                data_base64=PNG_BASE64,
                suggested_filename="good.png",
            ),
        ],
        output_dir=tmp_path,
    )

    assert [p.name for p in paths] == ["good.png"]
    assert not (tmp_path / "bad.png").exists()


def test_write_generated_assets_does_not_overwrite_existing_name(tmp_path: Path) -> None:
    existing = tmp_path / "answer.png"
    existing.write_bytes(b"original")

    paths = write_generated_assets(
        [
            GeneratedAsset(
                kind="image",
                media_type="image/png",
                data_base64=PNG_BASE64,
                suggested_filename="answer.png",
            )
        ],
        output_dir=tmp_path,
    )

    assert existing.read_bytes() == b"original"
    assert [path.name for path in paths] == ["answer-2.png"]
    assert paths[0].read_bytes() == VALID_PNG_BYTES


def test_write_generated_assets_rejects_oversized_decoded_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(asset_writer, "_MAX_GENERATED_ASSET_BYTES", 5)

    paths = write_generated_assets(
        [GeneratedAsset("image", "image/png", PNG_BASE64, "too-large.png")],
        output_dir=tmp_path,
    )

    assert paths == []
    assert not (tmp_path / "too-large.png").exists()


def test_write_generated_assets_caps_asset_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(asset_writer, "_MAX_GENERATED_ASSETS", 1)
    assets = [
        GeneratedAsset("image", "image/png", PNG_BASE64, f"image-{index}.png") for index in range(2)
    ]

    paths = write_generated_assets(assets, output_dir=tmp_path)

    assert [path.name for path in paths] == ["image-0.png"]


def test_write_generated_assets_caps_aggregate_decoded_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(asset_writer, "_MAX_TOTAL_GENERATED_ASSET_BYTES", len(VALID_PNG_BYTES))
    assets = [
        GeneratedAsset("image", "image/png", PNG_BASE64, f"image-{index}.png") for index in range(2)
    ]

    paths = write_generated_assets(assets, output_dir=tmp_path)

    assert [path.name for path in paths] == ["image-0.png"]


def test_write_generated_assets_names_the_file_from_the_sniffed_bytes(tmp_path: Path) -> None:
    # A provider that mislabels JPEG bytes as PNG must not hand Discord a file
    # whose extension contradicts its contents.
    jpeg = base64.b64encode(VALID_JPEG_BYTES).decode()

    paths = write_generated_assets(
        [GeneratedAsset("image", "image/png", jpeg, "answer.png")],
        output_dir=tmp_path,
    )

    assert [path.name for path in paths] == ["answer.jpg"]
    assert paths[0].read_bytes().startswith(b"\xff\xd8\xff")


def test_write_generated_assets_skips_payloads_that_are_not_images(tmp_path: Path) -> None:
    html = base64.b64encode(b"<!doctype html><script>alert(1)</script>").decode()

    paths = write_generated_assets(
        [
            GeneratedAsset("image", "image/png", html, "not-an-image.png"),
            GeneratedAsset("image", "image/png", PNG_BASE64, "real.png"),
        ],
        output_dir=tmp_path,
    )

    assert [path.name for path in paths] == ["real.png"]
    assert not (tmp_path / "not-an-image.png").exists()


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(PNG_SIGNATURE_ONLY, id="signature-only"),
        pytest.param(corrupt_png_crc(VALID_PNG_BYTES), id="crc-corrupt"),
        pytest.param(VALID_PNG_BYTES[:-8], id="truncated"),
    ],
)
def test_write_generated_assets_skips_images_that_do_not_fully_decode(
    tmp_path: Path, payload: bytes
) -> None:
    # A PNG signature is not a PNG. Every provider's assets converge on this
    # writer, so it must apply the same decode-level check as the strictest
    # provider path rather than trusting magic bytes.
    paths = write_generated_assets(
        [
            GeneratedAsset("image", "image/png", base64.b64encode(payload).decode(), "bad.png"),
            GeneratedAsset("image", "image/png", PNG_BASE64, "good.png"),
        ],
        output_dir=tmp_path,
    )

    assert [path.name for path in paths] == ["good.png"]
    assert not (tmp_path / "bad.png").exists()


def test_every_supported_image_media_type_has_a_filename_suffix() -> None:
    # write_generated_assets indexes the suffix map with whatever the sniffer
    # returns, so the two sets must not drift apart.
    assert set(IMAGE_MEDIA_TYPE_SUFFIXES) == set(SUPPORTED_IMAGE_MEDIA_TYPES)
