from __future__ import annotations

import base64
import binascii
import logging
import re
from pathlib import Path

from providers.types import GeneratedAsset

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
        filename = _safe_filename(asset.suggested_filename or f"asset-{index}.png")
        path = _write_unique_asset(output_dir / filename, raw)
        paths.append(path)
        total_bytes += len(raw)
    return paths


def _safe_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._") or "asset.png"


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
