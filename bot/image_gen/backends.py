"""The seam between the tool surface and image providers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from image_gen.types import ImageEditRequest, ImageGenRequest, ImageResult


class ImageAuthManager(Protocol):
    """The Codex OAuth subset the images backend needs."""

    def is_available(self) -> bool: ...

    def get_account_id(self) -> str: ...

    async def get_access_token(self) -> str: ...

    async def refresh_tokens(self, *, force: bool = False) -> None: ...


@dataclass(frozen=True, slots=True)
class ImageBackendCapabilities:
    """Immutable local contract advertised by one concrete backend."""

    allowed_model_ids: tuple[str, ...]
    sizes: tuple[str, ...]
    qualities: tuple[str, ...]
    backgrounds: tuple[str, ...]
    supports_generation: bool
    supports_edit: bool
    max_reference_images: int
    model_identity_verifiable: bool


class ImageBackend(Protocol):
    """A provider-neutral image generation backend."""

    @property
    def name(self) -> str: ...

    @property
    def provider(self) -> str: ...

    @property
    def auth_mode(self) -> str: ...

    @property
    def capabilities(self) -> ImageBackendCapabilities: ...

    @property
    def requires_persistent_usage_reservation(self) -> bool: ...

    def available(self) -> bool:
        """Whether the backend's credentials are present right now."""
        ...

    async def generate(self, request: ImageGenRequest) -> ImageResult: ...

    async def edit(self, request: ImageEditRequest) -> ImageResult: ...
