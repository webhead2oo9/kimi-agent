from __future__ import annotations

import base64
import binascii
import logging
import re
from pathlib import Path

from providers.types import GeneratedAsset
from utils.image_types import IMAGE_MEDIA_TYPE_SUFFIXES, decoded_image_media_type

log = logging.getLogger(__name__)

_MAX_GENERATED_ASSETS = 8
_MAX_GENERATED_ASSET_BYTES = 10 * 1024 * 1024
_MAX_TOTAL_GENERATED_ASSET_BYTES = 25 * 1024 * 1024
_MAX_GENERATED_ASSET_ENCODED_BYTES = ((_MAX_GENERATED_ASSET_BYTES + 2) // 3) * 4


def write_generated_assets(
    assets: list[GeneratedAsset],
    *,
    output_dir: Path,
) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    total_bytes = 0
    if len(assets) > _MAX_GENERATED_ASSETS:
        log.warning(
            "Skipping %d generated assets beyond the asset-count cap",
            len(assets) - _MAX_GENERATED_ASSETS,
        )
    for index, asset in enumerate(assets[:_MAX_GENERATED_ASSETS], start=1):
        # data_base64 comes from untrusted upstream provider data and may not be
        # valid base64 (e.g. a provider that returns a raw image URL). Decode
        # defensively so one bad asset cannot crash the whole response turn.
        if len(asset.data_base64) > _MAX_GENERATED_ASSET_ENCODED_BYTES:
            log.warning("Skipping generated asset %d: encoded data exceeds byte cap", index)
            continue
        try:
            raw = base64.b64decode(asset.data_base64, validate=True)
        except binascii.Error, ValueError:
            log.warning("Skipping generated asset %d: data is not valid base64", index)
            continue
        if len(raw) > _MAX_GENERATED_ASSET_BYTES:
            log.warning("Skipping generated asset %d: decoded data exceeds byte cap", index)
            continue
        if total_bytes + len(raw) > _MAX_TOTAL_GENERATED_ASSET_BYTES:
            log.warning("Skipping generated asset %d: aggregate asset bytes exceed cap", index)
            continue
        # The provider's declared type is untrusted upstream data, and these files
        # are handed straight to Discord as attachments. Every provider's assets
        # converge here, so this is the one place that decides whether the bytes
        # are a complete, decodable image and what extension they earn.
        sniffed = decoded_image_media_type(raw)
        if sniffed is None:
            log.warning("Skipping generated asset %d: bytes are not a decodable image", index)
            continue
        if sniffed != asset.media_type:
            log.warning(
                "Generated asset %d declared %s but its bytes are %s",
                index,
                asset.media_type,
                sniffed,
            )
        filename = _safe_filename(asset.suggested_filename or f"asset-{index}", sniffed)
        path = _write_unique_asset(output_dir / filename, raw)
        paths.append(path)
        total_bytes += len(raw)
    return paths


def _safe_filename(value: str, media_type: str) -> str:
    """Sanitize the provider's name and give it the suffix the bytes earned."""

    stem = Path(re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")).stem or "asset"
    return f"{stem}{IMAGE_MEDIA_TYPE_SUFFIXES[media_type]}"


def _write_unique_asset(path: Path, raw: bytes) -> Path:
    """Create an asset without replacing an earlier turn's file."""

    candidate = path
    sequence = 2
    while True:
        try:
            with candidate.open("xb") as handle:
                handle.write(raw)
            return candidate
        except FileExistsError:
            candidate = path.with_name(f"{path.stem}-{sequence}{path.suffix}")
            sequence += 1
