"""Concurrency, verification, and caps in front of an image backend."""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging

from image_gen.backends import ImageBackend
from image_gen.types import (
    ImageEditRequest,
    ImageGenError,
    ImageGenRequest,
    ImageResult,
)

log = logging.getLogger(__name__)

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# Mirrors discord_adapter.io.DISCORD_DEFAULT_FILE_SIZE_LIMIT_BYTES without an
# image_gen -> discord_adapter import (forbidden by the package graph).
DEFAULT_MAX_IMAGE_BYTES = 10 * 1024 * 1024


class ImageGenService:
    """Serializes image calls and verifies responses before they reach the tool."""

    def __init__(
        self,
        backend: ImageBackend,
        *,
        max_concurrency: int = 1,
        max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        self._backend = backend
        self._max_image_bytes = max_image_bytes
        self._semaphore = asyncio.Semaphore(max_concurrency)

    @property
    def backend_name(self) -> str:
        return self._backend.name

    async def generate(self, request: ImageGenRequest) -> ImageResult:
        async with self._semaphore:
            result = await self._backend.generate(request)
        self._verify(result)
        return result

    async def edit(self, request: ImageEditRequest) -> ImageResult:
        async with self._semaphore:
            result = await self._backend.edit(request)
        self._verify(result)
        return result

    def _verify(self, result: ImageResult) -> None:
        """Rejects bodies that are not decodable PNG data within the size cap.

        Provider responses are untrusted bytes: a body that is not a PNG would
        otherwise be written into the workspace and queued as a Discord
        attachment verbatim.
        """
        try:
            raw = base64.b64decode(result.image_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ImageGenError("image API returned data that is not valid base64") from exc
        if len(raw) > self._max_image_bytes:
            raise ImageGenError(f"generated image exceeds the {self._max_image_bytes} byte cap")
        if not raw.startswith(PNG_SIGNATURE):
            raise ImageGenError("image API returned data that is not a PNG image")
