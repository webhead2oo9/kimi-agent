"""Backend selection for image generation.

Mirrors :mod:`providers.factory`: an explicit supported-name list, and a
builder that resolves auth mode and returns ``None`` when no usable
credentials exist so the caller can skip tool registration.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from image_gen.backends import ImageAuthManager, ImageBackend
from image_gen.openai import (
    AUTH_MODE_API_KEY,
    AUTH_MODE_OAUTH,
    OpenAIAPIImageBackend,
    OpenAICodexImageBackend,
)

log = logging.getLogger(__name__)

SUPPORTED_IMAGE_BACKENDS = ("openai_codex", "openai_api", "openai")
_AUTH_MODE_AUTO = "auto"


@dataclass(frozen=True, slots=True)
class ImageBackendConfig:
    """Operational knobs, deliberately free of any Settings import so the
    package mirrors the provider seam (providers own their own config type)."""

    backend: str = "openai_codex"
    auth_mode: str = _AUTH_MODE_AUTO
    api_key: str = ""
    timeout_seconds: float = 300.0


def build_image_backend(
    config: ImageBackendConfig,
    auth_manager: ImageAuthManager | None,
) -> ImageBackend | None:
    """Builds the configured backend, or ``None`` when it cannot authenticate.

    Raises on an unsupported backend name so a typo aborts startup rather than
    silently disabling the tool.
    """
    if config.auth_mode not in {_AUTH_MODE_AUTO, AUTH_MODE_OAUTH, AUTH_MODE_API_KEY}:
        raise ValueError(f"unknown image auth mode: {config.auth_mode!r}")
    if config.backend not in SUPPORTED_IMAGE_BACKENDS:
        raise ValueError(
            f"unknown image generation backend {config.backend!r}; "
            f"supported: {', '.join(SUPPORTED_IMAGE_BACKENDS)}"
        )
    backend_name = config.backend
    if backend_name == "openai":
        mode = _resolve_auth_mode(config, auth_manager)
        if mode is None:
            return None
        backend_name = "openai_codex" if mode == AUTH_MODE_OAUTH else "openai_api"
        log.warning(
            "IMAGE_GEN_BACKEND=openai is deprecated; resolved unambiguously to %s. "
            "Set the explicit backend name.",
            backend_name,
        )
    elif (backend_name == "openai_codex" and config.auth_mode == AUTH_MODE_API_KEY) or (
        backend_name == "openai_api" and config.auth_mode == AUTH_MODE_OAUTH
    ):
        raise ValueError(
            f"IMAGE_GEN_BACKEND={backend_name} conflicts with "
            f"IMAGE_GEN_AUTH_MODE={config.auth_mode}; remove the legacy auth mode or "
            "make it match the explicit backend"
        )
    backend = IMAGE_BACKEND_BUILDERS[backend_name](config, auth_manager)
    if backend is not None:
        log.info("Image generation backend %s (%s)", backend.name, backend.provider)
    return backend


type ImageBackendBuilder = Callable[
    [ImageBackendConfig, ImageAuthManager | None], ImageBackend | None
]


def _build_openai_codex(
    config: ImageBackendConfig, auth_manager: ImageAuthManager | None
) -> ImageBackend | None:
    if auth_manager is None or not auth_manager.is_available():
        return None
    return OpenAICodexImageBackend(
        auth_manager=auth_manager,
        timeout_seconds=config.timeout_seconds,
    )


def _build_openai_api(
    config: ImageBackendConfig, auth_manager: ImageAuthManager | None
) -> ImageBackend | None:
    del auth_manager
    if not config.api_key:
        return None
    return OpenAIAPIImageBackend(
        api_key=config.api_key,
        timeout_seconds=config.timeout_seconds,
    )


IMAGE_BACKEND_BUILDERS: Mapping[str, ImageBackendBuilder] = MappingProxyType(
    {
        "openai_codex": _build_openai_codex,
        "openai_api": _build_openai_api,
    }
)


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
    if oauth_ready and key_ready:
        raise ValueError(
            "ambiguous legacy IMAGE_GEN_BACKEND=openai configuration: both Codex OAuth "
            "and an API key are available; select openai_codex or openai_api explicitly"
        )
    if oauth_ready:
        return AUTH_MODE_OAUTH
    if key_ready:
        return AUTH_MODE_API_KEY
    return None
