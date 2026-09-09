"""Concurrency, verification, and caps in front of an image backend."""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import math
from collections.abc import Mapping
from dataclasses import replace
from types import MappingProxyType

from image_gen.backends import ImageBackend
from image_gen.types import (
    ImageEditRequest,
    ImageGenError,
    ImageGenRequest,
    ImageResult,
)
from utils.image_types import decoded_image_media_type

log = logging.getLogger(__name__)

# Mirrors discord_adapter.io.DISCORD_DEFAULT_FILE_SIZE_LIMIT_BYTES without an
# image_gen -> discord_adapter import (forbidden by the package graph).
DEFAULT_MAX_IMAGE_BYTES = 10 * 1024 * 1024
DEFAULT_IMAGE_COST_ESTIMATES: Mapping[tuple[str, str], float] = MappingProxyType(
    {
        ("gpt-image-2", "generate"): 0.30,
        ("gpt-image-2", "edit"): 0.45,
        ("gpt-image-2.5-flare", "generate"): 0.30,
        ("gpt-image-2.5-flare", "edit"): 0.45,
        ("gpt-image-2.5-sunburst", "generate"): 0.30,
        ("gpt-image-2.5-sunburst", "edit"): 0.45,
    }
)


class ImageGenService:
    """Serializes image calls and verifies responses before they reach the tool."""

    def __init__(
        self,
        backend: ImageBackend,
        *,
        max_concurrency: int = 1,
        max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
        cost_estimates: Mapping[tuple[str, str], float] | None = None,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        self._backend = backend
        self._max_image_bytes = max_image_bytes
        self._semaphore = asyncio.Semaphore(max_concurrency)
        estimates = dict(cost_estimates or DEFAULT_IMAGE_COST_ESTIMATES)
        if backend.requires_persistent_usage_reservation:
            for model in backend.capabilities.allowed_model_ids:
                for operation in ("generate", "edit"):
                    estimate = estimates.get((model, operation))
                    if estimate is None or not math.isfinite(estimate) or estimate <= 0:
                        raise ValueError(
                            f"missing positive image cost estimate for {model}:{operation}"
                        )
        self._cost_estimates = MappingProxyType(estimates)

    @property
    def backend_name(self) -> str:
        return self._backend.name

    @property
    def provider(self) -> str:
        return self._backend.provider

    @property
    def requires_persistent_usage_reservation(self) -> bool:
        return self._backend.requires_persistent_usage_reservation

    def estimate_cost(self, request: ImageGenRequest | ImageEditRequest) -> float:
        if not self.requires_persistent_usage_reservation:
            return 0.0
        operation = "edit" if isinstance(request, ImageEditRequest) else "generate"
        try:
            return self._cost_estimates[(request.model, operation)]
        except KeyError as exc:
            raise ImageGenError(
                f"no paid-image cost estimate is configured for {request.model}:{operation}"
            ) from exc

    async def generate(self, request: ImageGenRequest) -> ImageResult:
        self.validate_generate(request)
        async with self._semaphore:
            result = await self._backend.generate(request)
            # Verify inside the permit, off the event loop: max_concurrency
            # bounds decoded images in flight, not just backend calls.
            verified, actual_size = await asyncio.to_thread(self._verify, result)
        return replace(result, image_bytes=verified, actual_size=actual_size)

    async def edit(self, request: ImageEditRequest) -> ImageResult:
        self.validate_edit(request)
        async with self._semaphore:
            result = await self._backend.edit(request)
            verified, actual_size = await asyncio.to_thread(self._verify, result)
        return replace(result, image_bytes=verified, actual_size=actual_size)

    def validate_generate(self, request: ImageGenRequest) -> None:
        self._validate_options(request, operation="generation")
        if not self._backend.capabilities.supports_generation:
            raise ImageGenError(f"{self._backend.name} does not support image generation")

    def validate_edit(self, request: ImageEditRequest) -> None:
        self._validate_options(request, operation="edit")
        capabilities = self._backend.capabilities
        if not capabilities.supports_edit:
            raise ImageGenError(f"{self._backend.name} does not support image edits")
        if not request.images:
            raise ImageGenError("image edits require at least one reference image")
        if len(request.images) > capabilities.max_reference_images:
            raise ImageGenError(
                f"{self._backend.name} accepts at most "
                f"{capabilities.max_reference_images} reference images"
            )

    def _validate_options(
        self,
        request: ImageGenRequest | ImageEditRequest,
        *,
        operation: str,
    ) -> None:
        capabilities = self._backend.capabilities
        if request.model not in capabilities.allowed_model_ids:
            allowed = ", ".join(capabilities.allowed_model_ids)
            raise ImageGenError(
                f"model {request.model!r} is not allowed for {self._backend.name} "
                f"{operation}; allowed: {allowed}"
            )
        for field, allowed_values in (
            ("size", capabilities.sizes),
            ("quality", capabilities.qualities),
            ("background", capabilities.backgrounds),
        ):
            value = getattr(request, field)
            if value is not None and value not in allowed_values:
                raise ImageGenError(
                    f"{field} {value!r} is not allowed for {self._backend.name} {operation}"
                )

    def _verify(self, result: ImageResult) -> tuple[bytes, str]:
        """Rejects bodies that are not decodable PNG data within the size cap.

        Provider responses are untrusted bytes: a body that is not a PNG would
        otherwise be written into the workspace and queued as a Discord
        attachment verbatim.
        """
        # Reject before allocating the decoded body: a backend contract is not
        # a size cap, and the string length already bounds the decoded size.
        if len(result.image_base64) > ((self._max_image_bytes + 2) // 3) * 4 + 4:
            raise ImageGenError(f"generated image exceeds the {self._max_image_bytes} byte cap")
        try:
            raw = base64.b64decode(result.image_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ImageGenError("image API returned data that is not valid base64") from exc
        if len(raw) > self._max_image_bytes:
            raise ImageGenError(f"generated image exceeds the {self._max_image_bytes} byte cap")
        # Same validator as provider-native assets: a signature is not an image,
        # and the size cap above bounds what the decoder is asked to touch.
        if decoded_image_media_type(raw) != "image/png":
            raise ImageGenError("image API returned data that is not a decodable PNG image")
        # Full PNG validation above guarantees the first chunk is a valid IHDR,
        # so these fixed offsets are trustworthy and avoid a second pixel decode.
        width = int.from_bytes(raw[16:20], "big")
        height = int.from_bytes(raw[20:24], "big")
        return raw, f"{width}x{height}"
