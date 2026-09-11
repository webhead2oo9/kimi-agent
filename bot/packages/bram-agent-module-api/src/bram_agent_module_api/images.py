"""Pure image helpers that do not depend on a host runtime."""

from __future__ import annotations

SUPPORTED_IMAGE_MEDIA_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
IMAGE_FILENAME_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def looks_like_image_attachment(filename: str | None, content_type: str | None) -> bool:
    """Use declared metadata as a cheap candidate filter before byte sniffing."""
    declared = str(content_type or "").strip().lower()
    name = str(filename or "").strip().lower()
    return declared.startswith("image/") or name.endswith(IMAGE_FILENAME_SUFFIXES)


def sniff_image_media_type(payload: bytes) -> str | None:
    """Return a supported image media type from magic bytes, or None."""
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if payload.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if payload.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(payload) >= 12 and payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return "image/webp"
    return None


__all__ = [
    "IMAGE_FILENAME_SUFFIXES",
    "SUPPORTED_IMAGE_MEDIA_TYPES",
    "looks_like_image_attachment",
    "sniff_image_media_type",
]
