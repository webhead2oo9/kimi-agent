"""Provider-neutral image generation vocabulary."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ImageGenRequest:
    """One text-to-image request. ``None`` fields are omitted from the payload."""

    prompt: str
    model: str
    size: str | None = None
    quality: str | None = None
    background: str | None = None


@dataclass(frozen=True, slots=True)
class ImageEditRequest:
    """One image edit request. ``images`` are data URLs, newest target last."""

    prompt: str
    model: str
    images: tuple[str, ...]
    size: str | None = None
    quality: str | None = None
    background: str | None = None


@dataclass(frozen=True, slots=True)
class ImageResult:
    """A completed generation. ``image_base64`` is the raw PNG body."""

    image_base64: str
    size: str | None = None
    background: str | None = None


class ImageGenError(RuntimeError):
    """User-facing generation failure. The message is safe for Discord."""


class ImageQuotaError(ImageGenError):
    """The account's image generation allowance is exhausted."""

    def __init__(self, message: str, resets_at: int | None) -> None:
        super().__init__(message)
        self.resets_at = resets_at
