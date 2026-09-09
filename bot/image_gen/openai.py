"""OpenAI images backend with OAuth and API-key auth modes.

Generation uses JSON in both modes. OAuth edits use the Codex backend's JSON
data-URL contract; API-key edits use the public API's multipart binary
contract. OAuth reuses the shared :class:`~codex.auth.CodexAuthManager` so
token refresh stays single-owner with the Codex chat transport, and mirrors
its forced-refresh-and-retry-once recovery from 401s.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
from collections.abc import Callable
from typing import Any

import aiohttp

from codex.auth import CodexAuthError
from codex.transport import CODEX_ORIGINATOR
from image_gen.backends import ImageAuthManager, ImageBackendCapabilities
from image_gen.types import (
    ImageEditRequest,
    ImageGenError,
    ImageGenRequest,
    ImageQuotaError,
    ImageResult,
    ImageProviderRejectedError,
)

log = logging.getLogger(__name__)

OAUTH_BASE_URL = "https://chatgpt.com/backend-api/codex"
API_KEY_BASE_URL = "https://api.openai.com/v1"
AUTH_MODE_OAUTH = "oauth"
AUTH_MODE_API_KEY = "api_key"
# A 10 MiB PNG expands to at most this much base64, plus bounded JSON metadata.
MAX_SUCCESS_RESPONSE_BYTES = ((10 * 1024 * 1024 + 2) // 3) * 4 + 128 * 1024
MAX_ERROR_RESPONSE_BYTES = 64 * 1024
_RESPONSE_CHUNK_BYTES = 64 * 1024
_SIZE_RE = re.compile(r"^(?:auto|[0-9]{1,4}x[0-9]{1,4})$")
_ALLOWED_BACKGROUNDS = frozenset({"auto", "opaque", "transparent"})
_ALLOWED_QUALITIES = frozenset({"auto", "low", "medium", "high", "xhigh", "max"})
_ALLOWED_OUTPUT_FORMATS = frozenset({"png", "jpeg", "webp"})
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,200}$")
CODEX_MODEL_CAVEAT = (
    "The Codex OAuth endpoint accepts model IDs, but empirical testing indicates it does "
    "not reliably respect them: it accepted an invalid model ID and does not attest the "
    "served model."
)
_IMAGE_MODEL_IDS = ("gpt-image-2", "gpt-image-2.5-flare", "gpt-image-2.5-sunburst")
_IMAGE_SIZES = ("auto", "1024x1024", "1024x1536", "1536x1024")
_IMAGE_QUALITIES = ("auto", "low", "medium", "high")
_IMAGE_BACKGROUNDS = ("auto", "opaque", "transparent")
CODEX_CAPABILITIES = ImageBackendCapabilities(
    allowed_model_ids=_IMAGE_MODEL_IDS,
    sizes=_IMAGE_SIZES,
    qualities=_IMAGE_QUALITIES,
    backgrounds=_IMAGE_BACKGROUNDS,
    supports_generation=True,
    supports_edit=True,
    max_reference_images=5,
    model_identity_verifiable=False,
)
API_CAPABILITIES = ImageBackendCapabilities(
    allowed_model_ids=_IMAGE_MODEL_IDS,
    sizes=_IMAGE_SIZES,
    qualities=_IMAGE_QUALITIES,
    backgrounds=_IMAGE_BACKGROUNDS,
    supports_generation=True,
    supports_edit=True,
    max_reference_images=5,
    model_identity_verifiable=True,
)


class _OpenAIImageBackend:
    """Shared transport mechanics; callers construct one explicit subclass."""

    def __init__(
        self,
        *,
        auth_mode: str,
        auth_manager: ImageAuthManager | None = None,
        api_key: str = "",
        timeout_seconds: float = 300.0,
        session_factory: Callable[..., Any] = aiohttp.ClientSession,
        form_factory: Callable[[], Any] = aiohttp.FormData,
    ) -> None:
        if auth_mode not in (AUTH_MODE_OAUTH, AUTH_MODE_API_KEY):
            raise ValueError(f"unknown image auth mode: {auth_mode!r}")
        self._auth_mode = auth_mode
        self._auth_manager = auth_manager
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._session_factory = session_factory
        self._form_factory = form_factory

    @property
    def name(self) -> str:
        return "openai"

    @property
    def auth_mode(self) -> str:
        return self._auth_mode

    @property
    def provider(self) -> str:
        return "openai"

    @property
    def capabilities(self) -> ImageBackendCapabilities:
        return CODEX_CAPABILITIES

    @property
    def requires_persistent_usage_reservation(self) -> bool:
        return self._auth_mode == AUTH_MODE_API_KEY

    def available(self) -> bool:
        if self._auth_mode == AUTH_MODE_OAUTH:
            return self._auth_manager is not None and self._auth_manager.is_available()
        return bool(self._api_key)

    async def generate(self, request: ImageGenRequest) -> ImageResult:
        body, request_id = await self._post_json(
            "/images/generations", _generation_payload(request)
        )
        return self._result(body, request.model, request_id)

    async def edit(self, request: ImageEditRequest) -> ImageResult:
        if self._auth_mode == AUTH_MODE_OAUTH:
            body, request_id = await self._post_json("/images/edits", _edit_payload(request))
        else:
            body, request_id = await self._post_form("/images/edits", self._edit_form(request))
        return self._result(body, request.model, request_id)

    def _result(
        self, body: dict[str, Any], requested_model: str, request_id: str | None
    ) -> ImageResult:
        raw_reported_model = body.get("model")
        reported_model = _safe_model_id(raw_reported_model)
        verified = bool(
            self.capabilities.model_identity_verifiable
            and (raw_reported_model is None or reported_model == requested_model)
        )
        caveat = CODEX_MODEL_CAVEAT if self._auth_mode == AUTH_MODE_OAUTH else None
        return _result_from_response(
            body,
            backend=self.name,
            provider=self.provider,
            requested_model=requested_model,
            provider_reported_model=reported_model,
            model_verified=verified,
            request_id=request_id,
            model_caveat=caveat,
        )

    async def _headers(self) -> dict[str, str]:
        if self._auth_mode == AUTH_MODE_OAUTH:
            manager = self._auth_manager
            if manager is None:
                raise ImageGenError("image OAuth auth manager is not configured")
            try:
                token = await manager.get_access_token()
            except CodexAuthError as exc:
                raise ImageGenError(
                    "Codex OAuth token is unavailable; re-authenticate the bot"
                ) from exc
            except (aiohttp.ClientError, TimeoutError) as exc:
                raise ImageGenError(
                    "Codex OAuth refresh failed; try image generation again"
                ) from exc
            headers = {
                "Authorization": f"Bearer {token}",
                "originator": CODEX_ORIGINATOR,
            }
            account_id = manager.get_account_id()
            if account_id:
                headers["ChatGPT-Account-Id"] = account_id
            return headers
        return {"Authorization": f"Bearer {self._api_key}"}

    @property
    def _base_url(self) -> str:
        return OAUTH_BASE_URL if self._auth_mode == AUTH_MODE_OAUTH else API_KEY_BASE_URL

    async def _post_json(
        self, path: str, payload: dict[str, Any]
    ) -> tuple[dict[str, Any], str | None]:
        url = f"{self._base_url}{path}"
        for attempt in range(2):
            headers = await self._headers()
            status, text, request_id = await self._request_json(url, payload, headers)
            if status == 200:
                return _parse_success_body(text), request_id
            if status == 401 and attempt == 0 and self._auth_mode == AUTH_MODE_OAUTH:
                manager = self._auth_manager
                if manager is not None:
                    # The access-token timestamp can still look valid after
                    # the authority has invalidated it; force one OAuth
                    # refresh before giving up, like the Codex chat transport.
                    try:
                        await manager.refresh_tokens(force=True)
                    except CodexAuthError as exc:
                        raise ImageGenError(
                            "Codex OAuth token is unavailable; re-authenticate the bot"
                        ) from exc
                    except (aiohttp.ClientError, TimeoutError) as exc:
                        raise ImageGenError(
                            "Codex OAuth refresh failed; try image generation again"
                        ) from exc
                    continue
            raise _error_from_response(status, text, request_id)
        raise ImageGenError("image request failed after token refresh")

    async def _post_form(self, path: str, form: Any) -> tuple[dict[str, Any], str | None]:
        url = f"{self._base_url}{path}"
        headers = await self._headers()
        status, text, request_id = await self._request_form(url, form, headers)
        if status != 200:
            raise _error_from_response(status, text, request_id)
        return _parse_success_body(text), request_id

    def _edit_form(self, request: ImageEditRequest) -> Any:
        form = self._form_factory()
        form.add_field("prompt", request.prompt)
        form.add_field("model", request.model)
        for field in ("background", "quality", "size"):
            value = getattr(request, field)
            if value is not None:
                form.add_field(field, value)
        for index, reference in enumerate(request.images, start=1):
            try:
                raw = base64.b64decode(reference.data_base64, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ImageGenError("reference image is not valid base64") from exc
            extension = {
                "image/png": "png",
                "image/jpeg": "jpg",
                "image/webp": "webp",
            }.get(reference.media_type)
            if extension is None:
                raise ImageGenError("reference image has an unsupported media type")
            form.add_field(
                "image[]",
                raw,
                filename=f"reference-{index}.{extension}",
                content_type=reference.media_type,
            )
        return form

    async def _request_json(
        self, url: str, payload: dict[str, Any], headers: dict[str, str]
    ) -> tuple[int, str, str | None]:
        try:
            async with self._session_factory(
                trust_env=False,
                timeout=aiohttp.ClientTimeout(total=self._timeout_seconds),
            ) as session:
                async with session.post(url, json=payload, headers=headers) as response:
                    return (
                        response.status,
                        await _read_response(response),
                        _safe_request_id(response),
                    )
        except aiohttp.ClientError as exc:
            raise ImageGenError(f"image request failed: {type(exc).__name__}") from exc
        except TimeoutError as exc:
            raise ImageGenError("image request timed out") from exc

    async def _request_form(
        self, url: str, form: Any, headers: dict[str, str]
    ) -> tuple[int, str, str | None]:
        try:
            async with self._session_factory(
                trust_env=False,
                timeout=aiohttp.ClientTimeout(total=self._timeout_seconds),
            ) as session:
                async with session.post(url, data=form, headers=headers) as response:
                    return (
                        response.status,
                        await _read_response(response),
                        _safe_request_id(response),
                    )
        except aiohttp.ClientError as exc:
            raise ImageGenError(f"image request failed: {type(exc).__name__}") from exc
        except TimeoutError as exc:
            raise ImageGenError("image request timed out") from exc


class OpenAICodexImageBackend(_OpenAIImageBackend):
    """ChatGPT Codex OAuth image endpoint."""

    def __init__(self, *, auth_manager: ImageAuthManager, **kwargs: Any) -> None:
        super().__init__(auth_mode=AUTH_MODE_OAUTH, auth_manager=auth_manager, **kwargs)

    @property
    def name(self) -> str:
        return "openai_codex"


class OpenAIAPIImageBackend(_OpenAIImageBackend):
    """Official OpenAI Platform Images API backend."""

    def __init__(self, *, api_key: str, **kwargs: Any) -> None:
        super().__init__(auth_mode=AUTH_MODE_API_KEY, api_key=api_key, **kwargs)

    @property
    def name(self) -> str:
        return "openai_api"

    @property
    def capabilities(self) -> ImageBackendCapabilities:
        return API_CAPABILITIES


def _generation_payload(request: ImageGenRequest) -> dict[str, Any]:
    payload: dict[str, Any] = {"prompt": request.prompt, "model": request.model}
    _merge_optional(payload, request)
    return payload


def _edit_payload(request: ImageEditRequest) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "images": [{"image_url": reference.data_url} for reference in request.images],
        "prompt": request.prompt,
        "model": request.model,
    }
    _merge_optional(payload, request)
    return payload


def _merge_optional(payload: dict[str, Any], request: Any) -> None:
    for field in ("background", "quality", "size"):
        value = getattr(request, field)
        if value is not None:
            payload[field] = value


def _parse_success_body(text: str) -> dict[str, Any]:
    try:
        body = json.loads(text)
    except ValueError as exc:
        raise ImageGenError("image API returned an unreadable response") from exc
    if not isinstance(body, dict):
        raise ImageGenError("image API returned an unreadable response")
    return body


def _result_from_response(
    body: dict[str, Any],
    *,
    backend: str,
    provider: str,
    requested_model: str,
    provider_reported_model: str | None,
    model_verified: bool,
    request_id: str | None,
    model_caveat: str | None,
) -> ImageResult:
    data = body.get("data")
    if not isinstance(data, list) or not data:
        raise ImageGenError("image API returned no image data")
    first = data[0]
    b64 = first.get("b64_json") if isinstance(first, dict) else None
    if not isinstance(b64, str) or not b64:
        raise ImageGenError("image API returned no image data")
    size = body.get("size")
    quality = body.get("quality")
    background = body.get("background")
    output_format = body.get("output_format")
    raw_usage = body.get("usage")
    return ImageResult(
        image_base64=b64,
        size=size if isinstance(size, str) and _SIZE_RE.fullmatch(size) else None,
        background=(
            background
            if isinstance(background, str) and background in _ALLOWED_BACKGROUNDS
            else None
        ),
        usage=dict(raw_usage) if isinstance(raw_usage, dict) else None,
        quality=(quality if isinstance(quality, str) and quality in _ALLOWED_QUALITIES else None),
        output_format=(
            output_format
            if isinstance(output_format, str) and output_format in _ALLOWED_OUTPUT_FORMATS
            else None
        ),
        backend=backend,
        provider=provider,
        requested_model=requested_model,
        provider_reported_model=provider_reported_model,
        model_verified=model_verified,
        request_id=request_id,
        model_caveat=model_caveat,
    )


def _safe_request_id(response: Any) -> str | None:
    headers = getattr(response, "headers", {})
    value = headers.get("x-request-id") or headers.get("request-id")
    return value if isinstance(value, str) and _REQUEST_ID_RE.fullmatch(value) else None


def _safe_model_id(value: object) -> str | None:
    return value if isinstance(value, str) and _MODEL_ID_RE.fullmatch(value) else None


async def _read_response(response: Any) -> str:
    limit = MAX_SUCCESS_RESPONSE_BYTES if response.status == 200 else MAX_ERROR_RESPONSE_BYTES
    declared = response.content_length
    if isinstance(declared, int) and declared > limit:
        raise ImageGenError("image API response exceeded the size limit")
    body = bytearray()
    async for chunk in response.content.iter_chunked(_RESPONSE_CHUNK_BYTES):
        body.extend(chunk)
        if len(body) > limit:
            raise ImageGenError("image API response exceeded the size limit")
    return bytes(body).decode("utf-8", errors="replace")


def _error_from_response(
    status: int, text: str, request_id: str | None = None
) -> ImageGenError | ImageQuotaError:
    try:
        body = json.loads(text)
    except ValueError:
        body = None
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        error_type = error.get("type")
        if status == 429 and error_type == "usage_limit_reached":
            resets_at = error.get("resets_at")
            return ImageQuotaError(
                "image generation limit reached for this account",
                resets_at if isinstance(resets_at, int) else None,
            )
        if status == 429 and error_type == "usage_not_included":
            return ImageGenError("image generation is not included on this account's plan")
    if status == 400:
        return ImageProviderRejectedError(
            "image request was rejected by the provider (400)", request_id
        )
    if status == 401:
        return ImageGenError("image API authentication failed (401)")
    if status == 403:
        return ImageGenError("image API request is not authorized (403)")
    if status == 429:
        return ImageGenError("image API rate limit reached (429)")
    if status >= 500:
        return ImageGenError(f"image API is temporarily unavailable ({status})")
    return ImageGenError(f"image API request failed ({status})")
