"""The seam between the tool surface and image providers."""

from __future__ import annotations

from typing import Protocol

from image_gen.types import ImageEditRequest, ImageGenRequest, ImageResult


class ImageAuthManager(Protocol):
    """The Codex OAuth subset the images backend needs."""

    def is_available(self) -> bool: ...

    def get_account_id(self) -> str: ...

    async def get_access_token(self) -> str: ...

    async def refresh_tokens(self, *, force: bool = False) -> None: ...


class ImageBackend(Protocol):
    """A provider-neutral image generation backend."""

    @property
    def name(self) -> str: ...

    def available(self) -> bool:
        """Whether the backend's credentials are present right now."""
        ...

    async def generate(self, request: ImageGenRequest) -> ImageResult: ...

    async def edit(self, request: ImageEditRequest) -> ImageResult: ...
