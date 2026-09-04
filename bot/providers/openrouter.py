from __future__ import annotations

import base64
import binascii
import logging
import math
from dataclasses import replace
from typing import Any

from branding import DEFAULT_BOT_NAME, provider_identity
from providers.openai_chat import OpenAIChatProvider
from providers.types import (
    GeneratedAsset,
    ProviderCapability,
    ProviderRequest,
    ProviderResponse,
)
from utils.image_types import IMAGE_MEDIA_TYPE_SUFFIXES, sniff_image_media_type

log = logging.getLogger(__name__)

_MAX_INLINE_IMAGES = 8
_MAX_INLINE_IMAGE_BYTES = 10 * 1024 * 1024
_MAX_TOTAL_INLINE_IMAGE_BYTES = 25 * 1024 * 1024
_MAX_INLINE_IMAGE_HEADER_CHARS = 1024


class OpenRouterProvider(OpenAIChatProvider):
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        provider_routing: dict[str, Any] | None = None,
        service_tier: str | None = None,
        timeout_seconds: float = 900.0,
        app_url: str | None = None,
        app_name: str | None = None,
        user_agent: str = DEFAULT_BOT_NAME,
    ) -> None:
        super().__init__(
            api_key=api_key,
            base_url="https://openrouter.ai/api/v1",
            model=model,
            provider_key="openrouter",
            service_tier=service_tier,
            timeout_seconds=timeout_seconds,
            user_agent=user_agent,
        )
        self._provider_routing = provider_routing or {}
        self._app_url = app_url
        self._app_name = provider_identity(app_name) if app_name else None

    @property
    def capabilities(self) -> set[ProviderCapability]:
        return {
            ProviderCapability.TEXT,
            ProviderCapability.IMAGE_INPUT,
            ProviderCapability.IMAGE_OUTPUT,
            ProviderCapability.TOOL_CALLING,
        }

    def _apply_provider_options(
        self,
        kwargs: dict[str, Any],
        request: ProviderRequest,
    ) -> None:
        if self._provider_routing:
            # OpenRouter's routing object is an API-body extension, not a named
            # parameter in the OpenAI SDK's Chat Completions signature.
            kwargs.setdefault("extra_body", {})["provider"] = self._provider_routing
        if ProviderCapability.IMAGE_OUTPUT in request.requested_capabilities:
            kwargs["modalities"] = ["image", "text"]
        headers: dict[str, str] = {}
        if self._app_url:
            headers["HTTP-Referer"] = self._app_url
        if self._app_name:
            headers["X-OpenRouter-Title"] = self._app_name
        # Router metadata is opt-in. The response parser retains only normalized
        # attribution fields, never the full provider payload.
        headers["X-OpenRouter-Metadata"] = "enabled"
        kwargs.setdefault("extra_headers", {}).update(headers)

    def _response_from_native(self, response: Any) -> ProviderResponse:
        base = super()._response_from_native(response)
        message = response.choices[0].message
        # OpenRouter exposes chain-of-thought in `message.reasoning`, which the
        # base OpenAI-chat parser (looking for `reasoning_content`) does not pick
        # up. Fall back to it so reasoning is preserved in history.
        reasoning_content = base.reasoning_content or self._message_field(message, "reasoning")
        metadata = self._native_field(response, "openrouter_metadata")
        usage = self._native_field(response, "usage")
        return replace(
            base,
            reasoning_content=reasoning_content,
            provider_state={},
            generated_assets=self._parse_images(self._native_field(message, "images")),
            upstream_provider=self._selected_provider(metadata),
            service_tier=self._bounded_text(self._native_field(response, "service_tier")),
            openrouter_charge_usd=self._non_negative_number(self._native_field(usage, "cost")),
            is_byok=self._optional_bool(self._native_field(metadata, "is_byok")),
        )

    @staticmethod
    def _native_field(value: Any, field: str) -> Any:
        if isinstance(value, dict):
            return value.get(field)
        direct = getattr(value, field, None)
        if direct is not None:
            return direct
        model_extra = getattr(value, "model_extra", None)
        if isinstance(model_extra, dict):
            return model_extra.get(field)
        return None

    @staticmethod
    def _bounded_text(value: Any, *, max_chars: int = 160) -> str:
        if not isinstance(value, str):
            return ""
        # Bound work before stripping/filtering: provider metadata is untrusted,
        # and scanning a multi-megabyte value just to retain 160 characters is
        # avoidable event-loop work.
        return "".join(char for char in value[:max_chars].strip() if char.isprintable())

    @classmethod
    def _selected_provider(cls, metadata: Any) -> str:
        endpoints = cls._native_field(metadata, "endpoints")
        available = cls._native_field(endpoints, "available")
        if not isinstance(available, (list, tuple)):
            return ""
        for endpoint in available:
            if cls._native_field(endpoint, "selected") is True:
                return cls._bounded_text(cls._native_field(endpoint, "provider"))
        return ""

    @staticmethod
    def _non_negative_number(value: Any) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None

    @staticmethod
    def _optional_bool(value: Any) -> bool | None:
        return value if isinstance(value, bool) else None

    @classmethod
    def _parse_images(cls, images: Any) -> list[GeneratedAsset]:
        assets: list[GeneratedAsset] = []
        processed_bytes = 0
        processed_encoded_chars = 0
        for index, image in enumerate(images or [], start=1):
            if index > _MAX_INLINE_IMAGES:
                log.warning("Skipping OpenRouter images beyond the image-count cap")
                break
            image_url = cls._native_field(image, "image_url")
            url = cls._native_field(image_url, "url")
            if not isinstance(url, str):
                continue
            # Only data URLs carry inline base64. A plain http(s) URL is not
            # base64 and must not be smuggled into data_base64, where it would
            # later fail to decode.
            if url[:5].lower() != "data:":
                log.warning("Skipping non-data image URL from OpenRouter: %s", url[:80])
                continue
            comma_index = url.find(",", 5, 6 + _MAX_INLINE_IMAGE_HEADER_CHARS)
            if comma_index < 0:
                log.warning("Skipping OpenRouter image data URL with a missing/oversized header")
                continue
            header_parts = url[5:comma_index].split(";")
            if not any(part.strip().lower() == "base64" for part in header_parts[1:]):
                log.warning("Skipping non-base64 OpenRouter image data URL")
                continue
            encoded_length = len(url) - comma_index - 1
            # Derived from the live byte caps so a test (or reload) that
            # adjusts the base value cannot leave a stale encoded bound.
            if encoded_length > ((_MAX_INLINE_IMAGE_BYTES + 2) // 3) * 4:
                log.warning("Skipping OpenRouter image %d: encoded data exceeds byte cap", index)
                continue
            total_encoded_cap = (
                (_MAX_TOTAL_INLINE_IMAGE_BYTES + 2) // 3
            ) * 4 + 4 * _MAX_INLINE_IMAGES
            if processed_encoded_chars + encoded_length > total_encoded_cap:
                log.warning("Stopping OpenRouter image parsing at the aggregate encoded-data cap")
                break
            # Charge every bounded candidate before validating base64. Rejected
            # data must not repeat full scans/decodes outside the total response
            # processing budget.
            processed_encoded_chars += encoded_length
            data_base64 = url[comma_index + 1 :]
            # Parsing runs on the event loop, so the payload is never decoded
            # here: a 32-character prefix is enough for the signature sniff,
            # the exact decoded size falls out of the encoded form, and full
            # base64 validation plus the decode-level image check run in a
            # worker thread before moderation (agent/turn.py ->
            # providers/assets.py:validate_generated_assets).
            try:
                prefix = base64.b64decode(data_base64[:32], validate=False)
            except binascii.Error, ValueError:
                prefix = b""
            # max(0, ...) keeps the running budget monotonic: a degenerate
            # padding-only payload would otherwise subtract from it.
            decoded_length = max(
                0,
                (encoded_length * 3) // 4
                - (2 if data_base64.endswith("==") else 1 if data_base64.endswith("=") else 0),
            )
            if decoded_length > _MAX_INLINE_IMAGE_BYTES:
                log.warning("Skipping OpenRouter image %d: decoded data exceeds byte cap", index)
                continue
            if processed_bytes + decoded_length > _MAX_TOTAL_INLINE_IMAGE_BYTES:
                # Capacity, not a work bound: a later smaller image may fit.
                log.warning("Skipping OpenRouter image %d: aggregate byte cap", index)
                continue
            processed_bytes += decoded_length

            media_type = sniff_image_media_type(prefix)
            if media_type is None:
                log.warning("Skipping OpenRouter image %d: bytes are not a supported image", index)
                continue
            declared = cls._bounded_text(header_parts[0]).lower()
            if declared != media_type:
                log.warning(
                    "OpenRouter image %d declared %s but its bytes are %s",
                    index,
                    declared or "an empty media type",
                    media_type,
                )
            assets.append(
                GeneratedAsset(
                    kind="image",
                    media_type=media_type,
                    data_base64=data_base64,
                    suggested_filename=(
                        f"openrouter-image-{index}{IMAGE_MEDIA_TYPE_SUFFIXES[media_type]}"
                    ),
                )
            )
        return assets
