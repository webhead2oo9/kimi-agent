"""Image generation backends.

The tool surface lives in ``tools/image_gen.py``; this package owns the
provider-neutral request/response vocabulary and the HTTP backends behind it.
Mirrors the provider seam: a new image provider is a module plus a factory
entry, and the tool handler only ever sees :class:`ImageResult`.
"""

from __future__ import annotations

from image_gen.types import (
    ImageEditRequest,
    ImageGenError,
    ImageGenRequest,
    ImageQuotaError,
    ImageResult,
)

__all__ = [
    "ImageEditRequest",
    "ImageGenError",
    "ImageGenRequest",
    "ImageQuotaError",
    "ImageResult",
]
