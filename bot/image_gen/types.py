"""Provider-neutral image generation vocabulary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ImageGenRequest:
    """One text-to-image request. ``None`` fields are omitted from the payload."""

    prompt: str
    model: str
    size: str | None = None
    quality: str | None = None
    background: str | None = None


@dataclass(frozen=True, slots=True)
class ImageReference:
    """One bounded source image, encoded once for either HTTP contract."""

    media_type: str
    data_base64: str

    @property
    def data_url(self) -> str:
        return f"data:{self.media_type};base64,{self.data_base64}"


@dataclass(frozen=True, slots=True)
class ImageEditRequest:
    """One image edit request, with bounded provider-neutral references."""

    prompt: str
    model: str
    images: tuple[ImageReference, ...]
    size: str | None = None
    quality: str | None = None
    background: str | None = None


@dataclass(frozen=True, slots=True)
class ImageResult:
    """A completed generation. ``image_base64`` is the raw PNG body."""

    image_base64: str
    size: str | None = None
    background: str | None = None
    usage: dict[str, Any] | None = None
    image_bytes: bytes | None = None


class ImageGenError(RuntimeError):
    """User-facing generation failure. The message is safe for Discord."""


class ImageQuotaError(ImageGenError):
    """The account's image generation allowance is exhausted."""

    def __init__(self, message: str, resets_at: int | None) -> None:
        super().__init__(message)
        self.resets_at = resets_at
