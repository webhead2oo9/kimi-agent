from __future__ import annotations

import base64
import binascii
import mimetypes

from kimi_agent_module_api.images import (
    SUPPORTED_IMAGE_MEDIA_TYPES,
    looks_like_image_attachment as looks_like_image_attachment,
    sniff_image_media_type as sniff_image_media_type,
)


def supported_image_media_type(value: str | None) -> str | None:
    if not value:
        return None
    media_type = value.split(";", 1)[0].strip().lower()
    return media_type if media_type in SUPPORTED_IMAGE_MEDIA_TYPES else None


def image_media_type_from_filename(filename: str) -> str | None:
    guessed, _encoding = mimetypes.guess_type(filename)
    return supported_image_media_type(guessed)


def normalize_image_data_url(value: str, media_type: str | None = None) -> tuple[str, str | None]:
    """Correct a base64 image data URL's media type from its bytes when possible."""
    if not value.startswith("data:") or "," not in value:
        return value, supported_image_media_type(media_type) or media_type
    header, payload = value.split(",", 1)
    header_parts = header.split(";")
    if len(header_parts) < 2 or not any(part.lower() == "base64" for part in header_parts[1:]):
        return value, supported_image_media_type(media_type) or media_type
    try:
        raw = base64.b64decode(payload, validate=True)
    except ValueError, binascii.Error:
        return value, supported_image_media_type(media_type) or media_type
    sniffed = sniff_image_media_type(raw)
    if sniffed is None:
        return value, supported_image_media_type(media_type) or media_type
    return f"data:{sniffed};base64,{payload}", sniffed
