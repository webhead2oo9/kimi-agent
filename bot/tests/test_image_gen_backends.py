"""Unit tests for the image generation backend package."""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest

import image_gen.openai as openai_module
from codex.auth import CodexAuthError
from image_gen.factory import ImageBackendConfig, build_image_backend
from image_gen.openai import (
    API_KEY_BASE_URL,
    OAUTH_BASE_URL,
    OpenAIImageBackend,
)
from image_gen.service import DEFAULT_MAX_IMAGE_BYTES, PNG_SIGNATURE, ImageGenService
from image_gen.types import (
    ImageEditRequest,
    ImageGenError,
    ImageGenRequest,
    ImageQuotaError,
    ImageReference,
    ImageResult,
)

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 16
PNG_BASE64 = base64.b64encode(PNG_BYTES).decode("ascii")


class StubAuthManager:
    def __init__(self, *, available: bool = True, token: str = "tok-1") -> None:
        self.available = available
        self.token = token
        self.forced_refreshes = 0

    def is_available(self) -> bool:
        return self.available

    def get_account_id(self) -> str:
        return "acct-123"

    async def get_access_token(self) -> str:
        return self.token

    async def refresh_tokens(self, *, force: bool = False) -> None:
        self.forced_refreshes += 1
        self.token = "tok-2"


class UnavailableAuthManager(StubAuthManager):
    def __init__(self) -> None:
        super().__init__(available=False)


class BrokenAuthManager(StubAuthManager):
    async def get_access_token(self) -> str:
        raise CodexAuthError("expired")


class BrokenRefreshAuthManager(StubAuthManager):
    async def refresh_tokens(self, *, force: bool = False) -> None:
        del force
        raise CodexAuthError("account changed")


class FakeContent:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def iter_chunked(self, _size: int):
        for chunk in self.chunks:
            yield chunk


class FakeResponse:
    def __init__(
        self,
        status: int,
        body: Any,
        *,
        content_length: int | None = -1,
        chunks: list[bytes] | None = None,
    ) -> None:
        self.status = status
        encoded = body if isinstance(body, bytes) else str(body).encode("utf-8")
        self.content_length = len(encoded) if content_length == -1 else content_length
        self.content = FakeContent(chunks if chunks is not None else [encoded])


class FakePostContext:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response

    async def __aenter__(self) -> FakeResponse:
        return self.response

    async def __aexit__(self, *exc_info: object) -> None:
        return None


class FakeSessionContext:
    def __init__(self, session: FakeSession) -> None:
        self.session = session

    async def __aenter__(self) -> FakeSession:
        return self.session

    async def __aexit__(self, *exc_info: object) -> None:
        return None


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def post(
        self,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        data: object | None = None,
        headers: dict[str, str],
    ):
        self.requests.append({"url": url, "json": json, "data": data, "headers": dict(headers)})
        response = self.responses.pop(0)
        return FakePostContext(response)


class FakeFormData:
    def __init__(self) -> None:
        self.fields: list[tuple[str, object, dict[str, str]]] = []

    def add_field(self, name: str, value: object, **kwargs: str) -> None:
        self.fields.append((name, value, kwargs))


def _backend(
    responses: list[FakeResponse],
    *,
    auth_mode: str = "oauth",
    auth_manager: StubAuthManager | None = None,
    api_key: str = "",
) -> tuple[OpenAIImageBackend, FakeSession]:
    session = FakeSession(responses)
    backend = OpenAIImageBackend(
        auth_mode=auth_mode,
        auth_manager=auth_manager if auth_mode == "oauth" else None,
        api_key=api_key,
        timeout_seconds=5.0,
        session_factory=lambda **_kwargs: FakeSessionContext(session),
        form_factory=FakeFormData,
    )
    return backend, session


def _success_body() -> str:
    return json.dumps(
        {
            "created": 1778832973,
            "data": [{"b64_json": PNG_BASE64}],
            "size": "1024x1024",
            "background": "opaque",
        }
    )


def _request() -> ImageGenRequest:
    return ImageGenRequest(prompt="a red fox", model="gpt-image-2", size="auto")


@pytest.mark.asyncio
async def test_generate_oauth_sends_bearer_account_and_originator() -> None:
    backend, session = _backend(
        [FakeResponse(200, _success_body())], auth_manager=StubAuthManager()
    )

    result = await backend.generate(_request())

    assert result.image_base64 == PNG_BASE64
    assert result.size == "1024x1024"
    assert result.background == "opaque"
    assert result.usage is None
    request = session.requests[0]
    assert request["url"] == f"{OAUTH_BASE_URL}/images/generations"
    headers = request["headers"]
    assert headers["Authorization"] == "Bearer tok-1"
    assert headers["ChatGPT-Account-Id"] == "acct-123"
    assert headers["originator"] == "codex_cli_rs"
    assert request["json"] == {"prompt": "a red fox", "model": "gpt-image-2", "size": "auto"}


@pytest.mark.asyncio
async def test_generate_preserves_provider_reported_usage() -> None:
    body = json.loads(_success_body())
    body["usage"] = {
        "input_tokens": 17,
        "output_tokens": 5,
        "input_tokens_details": {"image_tokens": 12, "text_tokens": 5},
    }
    backend, _session = _backend(
        [FakeResponse(200, json.dumps(body))], auth_manager=StubAuthManager()
    )

    result = await backend.generate(_request())

    assert result.usage == body["usage"]


@pytest.mark.asyncio
async def test_generate_api_key_uses_platform_url_without_account_header() -> None:
    backend, session = _backend(
        [FakeResponse(200, _success_body())], auth_mode="api_key", api_key="sk-test"
    )

    await backend.generate(_request())

    request = session.requests[0]
    assert request["url"] == f"{API_KEY_BASE_URL}/images/generations"
    headers = request["headers"]
    assert headers["Authorization"] == "Bearer sk-test"
    assert "ChatGPT-Account-Id" not in headers
    assert "originator" not in headers


@pytest.mark.asyncio
async def test_edit_sends_image_data_urls() -> None:
    backend, session = _backend(
        [FakeResponse(200, _success_body())], auth_manager=StubAuthManager()
    )

    await backend.edit(
        ImageEditRequest(
            prompt="add a red hat",
            model="gpt-image-2",
            images=(ImageReference(media_type="image/png", data_base64="Zm9v"),),
            quality="low",
        )
    )

    request = session.requests[0]
    assert request["url"] == f"{OAUTH_BASE_URL}/images/edits"
    assert request["json"] == {
        "images": [{"image_url": "data:image/png;base64,Zm9v"}],
        "prompt": "add a red hat",
        "model": "gpt-image-2",
        "quality": "low",
    }
    assert request["data"] is None


@pytest.mark.asyncio
async def test_api_key_edit_uses_multipart_binary_images() -> None:
    backend, session = _backend(
        [FakeResponse(200, _success_body())],
        auth_mode="api_key",
        api_key="sk-test",
    )

    await backend.edit(
        ImageEditRequest(
            prompt="add a red hat",
            model="gpt-image-2",
            images=(ImageReference(media_type="image/png", data_base64="Zm9v"),),
            quality="high",
            size="1024x1024",
        )
    )

    request = session.requests[0]
    assert request["url"] == f"{API_KEY_BASE_URL}/images/edits"
    assert request["json"] is None
    form = request["data"]
    assert isinstance(form, FakeFormData)
    assert form.fields == [
        ("prompt", "add a red hat", {}),
        ("model", "gpt-image-2", {}),
        ("quality", "high", {}),
        ("size", "1024x1024", {}),
        (
            "image[]",
            b"foo",
            {"filename": "reference-1.png", "content_type": "image/png"},
        ),
    ]


@pytest.mark.asyncio
async def test_oauth_401_forces_refresh_and_retries_once() -> None:
    manager = StubAuthManager()
    backend, session = _backend(
        [
            FakeResponse(401, json.dumps({"error": {"message": "bad token"}})),
            FakeResponse(200, _success_body()),
        ],
        auth_manager=manager,
    )

    await backend.generate(_request())

    assert manager.forced_refreshes == 1
    assert session.requests[1]["headers"]["Authorization"] == "Bearer tok-2"


@pytest.mark.asyncio
async def test_oauth_refresh_failure_maps_to_image_error() -> None:
    backend, _session = _backend(
        [FakeResponse(401, json.dumps({"error": {"message": "bad token"}}))],
        auth_manager=BrokenRefreshAuthManager(),
    )

    with pytest.raises(ImageGenError, match="re-authenticate"):
        await backend.generate(_request())


@pytest.mark.asyncio
async def test_oauth_401_twice_is_fatal_without_further_refresh() -> None:
    manager = StubAuthManager()
    backend, _session = _backend(
        [
            FakeResponse(401, json.dumps({"error": {"message": "bad token"}})),
            FakeResponse(401, json.dumps({"error": {"message": "still bad"}})),
        ],
        auth_manager=manager,
    )

    with pytest.raises(ImageGenError, match="401"):
        await backend.generate(_request())
    assert manager.forced_refreshes == 1


@pytest.mark.asyncio
async def test_api_key_401_does_not_refresh() -> None:
    backend, _session = _backend(
        [FakeResponse(401, json.dumps({"error": {"message": "bad key"}}))],
        auth_mode="api_key",
        api_key="sk-test",
    )

    with pytest.raises(ImageGenError, match="401"):
        await backend.generate(_request())


@pytest.mark.asyncio
async def test_usage_limit_reached_maps_to_quota_error_with_reset() -> None:
    backend, _session = _backend(
        [
            FakeResponse(
                429,
                json.dumps({"error": {"type": "usage_limit_reached", "resets_at": 1778836800}}),
            )
        ],
        auth_manager=StubAuthManager(),
    )

    with pytest.raises(ImageQuotaError) as exc_info:
        await backend.generate(_request())
    assert exc_info.value.resets_at == 1778836800
    assert "limit" in str(exc_info.value)


@pytest.mark.asyncio
async def test_usage_not_included_maps_to_plan_error() -> None:
    backend, _session = _backend(
        [FakeResponse(429, json.dumps({"error": {"type": "usage_not_included"}}))],
        auth_manager=StubAuthManager(),
    )

    with pytest.raises(ImageGenError, match="not included"):
        await backend.generate(_request())


@pytest.mark.asyncio
async def test_provider_metadata_is_closed_before_tool_echo() -> None:
    body = json.dumps(
        {
            "created": 1,
            "data": [{"b64_json": PNG_BASE64}],
            "size": "sk-secret-sentinel" * 100,
            "background": "provider-controlled-text",
        }
    )
    backend, _session = _backend(
        [FakeResponse(200, body)],
        auth_manager=StubAuthManager(),
    )

    result = await backend.generate(_request())

    assert result.size is None
    assert result.background is None


@pytest.mark.asyncio
async def test_provider_error_message_never_leaks_upstream_text() -> None:
    sentinel = "sk-secret-sentinel"
    backend, _session = _backend(
        [
            FakeResponse(
                400,
                json.dumps({"error": {"message": f"bad request contained {sentinel}"}}),
            )
        ],
        auth_manager=StubAuthManager(),
    )

    with pytest.raises(ImageGenError) as exc_info:
        await backend.generate(_request())
    assert sentinel not in str(exc_info.value)
    assert str(exc_info.value) == "image request was rejected by the provider (400)"


@pytest.mark.asyncio
async def test_declared_oversized_success_response_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(openai_module, "MAX_SUCCESS_RESPONSE_BYTES", 8)
    backend, _session = _backend(
        [FakeResponse(200, b"{}", content_length=9)],
        auth_manager=StubAuthManager(),
    )

    with pytest.raises(ImageGenError, match="size limit"):
        await backend.generate(_request())


@pytest.mark.asyncio
async def test_chunked_oversized_error_response_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(openai_module, "MAX_ERROR_RESPONSE_BYTES", 8)
    backend, _session = _backend(
        [
            FakeResponse(
                500,
                b"",
                content_length=None,
                chunks=[b"12345", b"6789"],
            )
        ],
        auth_manager=StubAuthManager(),
    )

    with pytest.raises(ImageGenError, match="size limit"):
        await backend.generate(_request())


@pytest.mark.asyncio
async def test_missing_image_data_is_an_error() -> None:
    backend, _session = _backend(
        [FakeResponse(200, json.dumps({"created": 1, "data": []}))],
        auth_manager=StubAuthManager(),
    )

    with pytest.raises(ImageGenError, match="no image data"):
        await backend.generate(_request())


@pytest.mark.asyncio
async def test_broken_oauth_tokens_surface_reauth_message() -> None:
    backend, _session = _backend(
        [FakeResponse(200, _success_body())], auth_manager=BrokenAuthManager()
    )

    with pytest.raises(ImageGenError, match="re-authenticate"):
        await backend.generate(_request())


def test_build_backend_auto_prefers_oauth() -> None:
    backend = build_image_backend(ImageBackendConfig(), StubAuthManager(token="tok"))
    assert backend is not None
    assert backend.auth_mode == "oauth"


def test_build_backend_auto_falls_back_to_api_key() -> None:
    backend = build_image_backend(ImageBackendConfig(api_key="sk-test"), UnavailableAuthManager())
    assert backend is not None
    assert backend.auth_mode == "api_key"


def test_build_backend_auto_without_credentials_returns_none() -> None:
    assert build_image_backend(ImageBackendConfig(), UnavailableAuthManager()) is None
    assert build_image_backend(ImageBackendConfig(), None) is None


def test_build_backend_unknown_name_aborts() -> None:
    with pytest.raises(ValueError, match="unknown image generation backend"):
        build_image_backend(ImageBackendConfig(backend="gemini"), None)


def test_build_backend_explicit_oauth_without_tokens_returns_none() -> None:
    assert (
        build_image_backend(ImageBackendConfig(auth_mode="oauth"), UnavailableAuthManager()) is None
    )


def test_build_backend_explicit_api_key_without_key_returns_none() -> None:
    assert build_image_backend(ImageBackendConfig(auth_mode="api_key"), None) is None


@pytest.mark.asyncio
async def test_service_verifies_png_and_rejects_other_data() -> None:
    class GifBackend:
        name = "stub"

        def available(self) -> bool:
            return True

        async def generate(self, request: ImageGenRequest) -> ImageResult:
            return ImageResult(image_base64=base64.b64encode(b"GIF89a" + b"0" * 8).decode())

        async def edit(self, request: ImageEditRequest) -> ImageResult:
            raise AssertionError("unused")

    service = ImageGenService(GifBackend())
    with pytest.raises(ImageGenError, match="not a PNG"):
        await service.generate(_request())


@pytest.mark.asyncio
async def test_service_returns_verified_bytes_for_workspace_write() -> None:
    png = PNG_SIGNATURE + b"verified"

    class PngBackend:
        name = "stub"

        def available(self) -> bool:
            return True

        async def generate(self, request: ImageGenRequest) -> ImageResult:
            return ImageResult(image_base64=base64.b64encode(png).decode())

        async def edit(self, request: ImageEditRequest) -> ImageResult:
            raise AssertionError("unused")

    result = await ImageGenService(PngBackend()).generate(_request())

    assert result.image_bytes == png


@pytest.mark.asyncio
async def test_service_rejects_oversized_images() -> None:
    huge = PNG_SIGNATURE + b"0" * (DEFAULT_MAX_IMAGE_BYTES + 1)

    class HugeBackend:
        name = "stub"

        def available(self) -> bool:
            return True

        async def generate(self, request: ImageGenRequest) -> ImageResult:
            return ImageResult(image_base64=base64.b64encode(huge).decode())

        async def edit(self, request: ImageEditRequest) -> ImageResult:
            raise AssertionError("unused")

    service = ImageGenService(HugeBackend())
    with pytest.raises(ImageGenError, match="byte cap"):
        await service.generate(_request())
