"""Backend selection for image generation.

Mirrors :mod:`providers.factory`: an explicit supported-name list, and a
builder that resolves auth mode and returns ``None`` when no usable
credentials exist so the caller can skip tool registration.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from image_gen.backends import ImageAuthManager
from image_gen.openai import (
    AUTH_MODE_API_KEY,
    AUTH_MODE_OAUTH,
    OpenAIImageBackend,
    API_KEY_BASE_URL,
    OAUTH_BASE_URL,
)

log = logging.getLogger(__name__)

SUPPORTED_IMAGE_BACKENDS = ("openai",)
_AUTH_MODE_AUTO = "auto"


@dataclass(frozen=True, slots=True)
class ImageBackendConfig:
    """Operational knobs, deliberately free of any Settings import so the
    package mirrors the provider seam (providers own their own config type)."""

    backend: str = "openai"
    auth_mode: str = _AUTH_MODE_AUTO
    api_key: str = ""
    timeout_seconds: float = 300.0


def build_image_backend(
    config: ImageBackendConfig,
    auth_manager: ImageAuthManager | None,
) -> OpenAIImageBackend | None:
    """Builds the configured backend, or ``None`` when it cannot authenticate.

    Raises on an unsupported backend name so a typo aborts startup rather than
    silently disabling the tool.
    """
    if config.backend not in SUPPORTED_IMAGE_BACKENDS:
        raise ValueError(
            f"unknown image generation backend {config.backend!r}; "
            f"supported: {', '.join(SUPPORTED_IMAGE_BACKENDS)}"
        )
    mode = _resolve_auth_mode(config, auth_manager)
    if mode is None:
        return None
    backend = OpenAIImageBackend(
        auth_mode=mode,
        auth_manager=auth_manager if mode == AUTH_MODE_OAUTH else None,
        api_key=config.api_key if mode == AUTH_MODE_API_KEY else "",
        timeout_seconds=config.timeout_seconds,
    )
    base = OAUTH_BASE_URL if mode == AUTH_MODE_OAUTH else API_KEY_BASE_URL
    log.info("Image generation backend %s (%s auth, %s)", backend.name, mode, base)
    return backend


def _resolve_auth_mode(
    config: ImageBackendConfig,
    auth_manager: ImageAuthManager | None,
) -> str | None:
    oauth_ready = auth_manager is not None and auth_manager.is_available()
    key_ready = bool(config.api_key)

    if config.auth_mode == AUTH_MODE_OAUTH:
        if not oauth_ready:
            log.warning("IMAGE_GEN_AUTH_MODE=oauth but no Codex OAuth tokens are available")
            return None
        return AUTH_MODE_OAUTH
    if config.auth_mode == AUTH_MODE_API_KEY:
        if not key_ready:
            log.warning("IMAGE_GEN_AUTH_MODE=api_key but IMAGE_GEN_API_KEY is not set")
            return None
        return AUTH_MODE_API_KEY
    if config.auth_mode != _AUTH_MODE_AUTO:
        raise ValueError(f"unknown image auth mode: {config.auth_mode!r}")
    # OAuth is the primary path; the API key is the fallback.
    if oauth_ready:
        return AUTH_MODE_OAUTH
    if key_ready:
        log.info("Image generation falling back to API-key auth (no Codex OAuth tokens)")
        return AUTH_MODE_API_KEY
    return None
