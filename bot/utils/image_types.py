from __future__ import annotations

import base64
import binascii
import mimetypes

SUPPORTED_IMAGE_MEDIA_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})


def supported_image_media_type(value: str | None) -> str | None:
    if not value:
        return None
    media_type = value.split(";", 1)[0].strip().lower()
    return media_type if media_type in SUPPORTED_IMAGE_MEDIA_TYPES else None


def image_media_type_from_filename(filename: str) -> str | None:
    guessed, _encoding = mimetypes.guess_type(filename)
    return supported_image_media_type(guessed)


IMAGE_FILENAME_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def looks_like_image_attachment(filename: str | None, content_type: str | None) -> bool:
    """Cheap candidate filter for a Discord attachment; byte sniffing decides.

    Shared by the code that *lists* an attachment as addressable and the code that
    *fetches* it: if the two drifted, a listed image could be refused at fetch time
    (or the reverse). Declared metadata is a hint only. Nothing here is a security
    check, and every fetched payload is still sniffed.
    """
    declared = str(content_type or "").strip().lower()
    name = str(filename or "").strip().lower()
    return declared.startswith("image/") or name.endswith(IMAGE_FILENAME_SUFFIXES)


def sniff_image_media_type(payload: bytes) -> str | None:
    """Return the supported image media type from magic bytes, or None."""
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if payload.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if payload.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(payload) >= 12 and payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return "image/webp"
    return None


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
