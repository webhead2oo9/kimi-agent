"""The seam between the tool surface and image providers."""

from __future__ import annotations

from typing import Protocol

from image_gen.types import ImageEditRequest, ImageGenRequest, ImageResult


class ImageBackend(Protocol):
    """A provider-neutral image generation backend."""

    @property
    def name(self) -> str: ...

    def available(self) -> bool:
        """Whether the backend's credentials are present right now."""
        ...

    async def generate(self, request: ImageGenRequest) -> ImageResult: ...

    async def edit(self, request: ImageEditRequest) -> ImageResult: ...
