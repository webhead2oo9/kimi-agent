from __future__ import annotations

import logging
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
        app_url: str | None = None,
        app_name: str | None = None,
        user_agent: str = DEFAULT_BOT_NAME,
    ) -> None:
        super().__init__(
            api_key=api_key,
            base_url="https://openrouter.ai/api/v1",
            model=model,
            provider_key="openrouter",
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
            headers["X-Title"] = self._app_name
        if headers:
            kwargs["extra_headers"] = headers

    def _response_from_native(self, response: Any) -> ProviderResponse:
        base = super()._response_from_native(response)
        message = response.choices[0].message
        # OpenRouter exposes chain-of-thought in `message.reasoning`, which the
        # base OpenAI-chat parser (looking for `reasoning_content`) does not pick
        # up. Fall back to it so reasoning is preserved in history.
        reasoning_content = base.reasoning_content or self._message_field(message, "reasoning")
        return replace(
            base,
            reasoning_content=reasoning_content,
            provider_state={
                "openrouter_metadata": getattr(response, "openrouter_metadata", {}) or {}
            },
            generated_assets=self._parse_images(getattr(message, "images", None)),
        )

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
