from __future__ import annotations

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

log = logging.getLogger(__name__)


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
            # Keep the legacy title header during migration for deployments that
            # still inspect it downstream.
            headers["X-Title"] = self._app_name
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
            generated_assets=self._parse_images(getattr(message, "images", None)),
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
        return "".join(char for char in value.strip() if char.isprintable())[:max_chars]

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

    @staticmethod
    def _parse_images(images: Any) -> list[GeneratedAsset]:
        assets: list[GeneratedAsset] = []
        for index, image in enumerate(images or [], start=1):
            image_url = getattr(image, "image_url", None)
            url = getattr(image_url, "url", None)
            if not isinstance(url, str):
                continue
            # Only data URLs carry inline base64. A plain http(s) URL is not
            # base64 and must not be smuggled into data_base64, where it would
            # later fail to decode.
            if not (url.startswith("data:") and "," in url):
                log.warning("Skipping non-data image URL from OpenRouter: %s", url[:80])
                continue
            data_base64 = url.split(",", 1)[1]
            assets.append(
                GeneratedAsset(
                    kind="image",
                    media_type="image/png",
                    data_base64=data_base64,
                    suggested_filename=f"openrouter-image-{index}.png",
                )
            )
        return assets
