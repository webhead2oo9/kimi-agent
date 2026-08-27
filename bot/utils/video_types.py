from __future__ import annotations

from pathlib import PurePath

# Gemini Files API video MIME vocabulary. Common real-world aliases are
# normalized at the bot boundary; the provider receives only these values.
_VIDEO_MIME_BY_SUFFIX = {
    ".3gp": "video/3gpp",
    ".3gpp": "video/3gpp",
    ".avi": "video/avi",
    ".flv": "video/x-flv",
    ".mov": "video/quicktime",
    ".mp4": "video/mp4",
    ".mpeg": "video/mpeg",
    ".mpg": "video/mpeg",
    ".webm": "video/webm",
    ".wmv": "video/wmv",
}

_MIME_ALIASES = {
    "video/3gpp": "video/3gpp",
    "video/avi": "video/avi",
    "video/mp4": "video/mp4",
    "video/mov": "video/quicktime",
    "video/mpeg": "video/mpeg",
    "video/mpg": "video/mpeg",
    "video/quicktime": "video/quicktime",
    "video/webm": "video/webm",
    "video/wmv": "video/wmv",
    "video/x-flv": "video/x-flv",
    "video/x-ms-wmv": "video/wmv",
    "video/x-msvideo": "video/avi",
}


def video_media_type(filename: str, declared: str | None) -> str | None:
    """Return one canonical Gemini video MIME when name and type are compatible."""

    suffix = PurePath(filename).suffix.casefold()
    from_suffix = _VIDEO_MIME_BY_SUFFIX.get(suffix)
    normalized_declared = _MIME_ALIASES.get((declared or "").split(";", 1)[0].strip().casefold())
    if from_suffix is None:
        return None
    if normalized_declared is not None and normalized_declared != from_suffix:
        return None
    # Discord sometimes omits or generalizes Content-Type. A recognized suffix
    # is still useful, while the Files API performs the authoritative decode.
    return from_suffix


def supported_video_suffixes() -> tuple[str, ...]:
    return tuple(sorted(_VIDEO_MIME_BY_SUFFIX))
