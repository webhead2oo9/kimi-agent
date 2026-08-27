from pathlib import Path

import pytest

from providers import assets as asset_writer
from providers.assets import write_generated_assets
from providers.types import GeneratedAsset


def test_write_generated_assets_decodes_base64_images(tmp_path: Path) -> None:
    paths = write_generated_assets(
        [
            GeneratedAsset(
                kind="image",
                media_type="image/png",
                data_base64="iVBORw0K",
                suggested_filename="answer.png",
            )
        ],
        output_dir=tmp_path,
    )

    assert len(paths) == 1
    assert paths[0].name == "answer.png"
    assert paths[0].read_bytes() == b"\x89PNG\r\n"


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
                data_base64="iVBORw0K",
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
                data_base64="iVBORw0K",
                suggested_filename="answer.png",
            )
        ],
        output_dir=tmp_path,
    )

    assert existing.read_bytes() == b"original"
    assert [path.name for path in paths] == ["answer-2.png"]
    assert paths[0].read_bytes() == b"\x89PNG\r\n"


def test_write_generated_assets_rejects_oversized_decoded_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(asset_writer, "_MAX_GENERATED_ASSET_BYTES", 5)

    paths = write_generated_assets(
        [GeneratedAsset("image", "image/png", "iVBORw0K", "too-large.png")],
        output_dir=tmp_path,
    )

    assert paths == []
    assert not (tmp_path / "too-large.png").exists()


def test_write_generated_assets_caps_asset_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(asset_writer, "_MAX_GENERATED_ASSETS", 1)
    assets = [
        GeneratedAsset("image", "image/png", "iVBORw0K", f"image-{index}.png") for index in range(2)
    ]

    paths = write_generated_assets(assets, output_dir=tmp_path)

    assert [path.name for path in paths] == ["image-0.png"]


def test_write_generated_assets_caps_aggregate_decoded_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(asset_writer, "_MAX_TOTAL_GENERATED_ASSET_BYTES", 6)
    assets = [
        GeneratedAsset("image", "image/png", "iVBORw0K", f"image-{index}.png") for index in range(2)
    ]

    paths = write_generated_assets(assets, output_dir=tmp_path)

    assert [path.name for path in paths] == ["image-0.png"]
