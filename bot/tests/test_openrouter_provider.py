import asyncio
import base64
import zlib
from types import SimpleNamespace
from typing import Any, cast

import pytest

from providers import openrouter as openrouter_module
from providers.assets import validate_generated_assets
from providers.openrouter import OpenRouterProvider
from providers.types import ContentPart, ProviderCapability, ProviderRequest

PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAACXBIWXMAAA7EAAAOxAGVKw4b"
    "AAAADElEQVR4nGP4//8/AAX+Av4N70a4AAAAAElFTkSuQmCC"
)
PNG_BYTES = base64.b64decode(PNG_BASE64)
JPEG_BYTES = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAIBAQEBAQIBAQECAgICAgQDAgICAgUEBAMEBgUG"
    "BgYFBgYGBwkIBgcJBwYGCAsICQoKCgoKBggLDAsKDAkKCgr/2wBDAQICAgICAgUDAwUKBwYH"
    "CgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgr/wgAR"
    "CAABAAEDAREAAhEBAxEB/8QAFAABAAAAAAAAAAAAAAAAAAAACP/EABQBAQAAAAAAAAAAAAAAAA"
    "AAAAD/2gAMAwEAAhADEAAAAX8f/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABBQJ//8QA"
    "FBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAwEBPwF//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAA"
    "gBAgEBPwF//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQAGPwJ//8QAFBABAAAAAAAAAAAA"
    "AAAAAAAAAP/aAAgBAQABPyF//9oADAMBAAIAAwAAABAf/8QAFBEBAAAAAAAAAAAAAAAAAAAAAP"
    "/aAAgBAwEBPxB//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAgEBPxB//8QAFBABAAAAAAAA"
    "AAAAAAAAAAAAAP/aAAgBAQABPxB//9k="
)
GIF_BYTES = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==")
WEBP_BYTES = base64.b64decode("UklGRiQAAABXRUJQVlA4IBgAAAAwAQCdASoBAAEAAgA0JaQAA3AA/vuUAAA=")
GIF_WITHOUT_PALETTE_BYTES = (
    b"GIF89a\x01\x00\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
)
GIF_WITH_INVALID_LZW_BYTES = GIF_BYTES[:29] + b"\x00" + GIF_BYTES[30:]
GIF_WITH_INVALID_FRAME_POSITION_BYTES = GIF_BYTES[:20] + b"\x01" + GIF_BYTES[21:]
_png_with_invalid_compression = bytearray(PNG_BYTES)
_png_with_invalid_compression[26] = 1
_png_with_invalid_compression[29:33] = zlib.crc32(_png_with_invalid_compression[12:29]).to_bytes(
    4, "big"
)
PNG_WITH_INVALID_COMPRESSION_BYTES = bytes(_png_with_invalid_compression)
JPEG_SOF_OFFSET = JPEG_BYTES.index(b"\xff\xc2")
JPEG_WITH_MISMATCHED_ARITHMETIC_SOF_BYTES = (
    JPEG_BYTES[: JPEG_SOF_OFFSET + 1] + b"\xca" + JPEG_BYTES[JPEG_SOF_OFFSET + 2 :]
)
WEBP_REORDERED_BODY = WEBP_BYTES[12:] + b"VP8X\x0a\x00\x00\x00" + b"\x00" * 10
WEBP_WITH_LATE_EXTENDED_HEADER_BYTES = (
    b"RIFF" + (len(WEBP_REORDERED_BODY) + 4).to_bytes(4, "little") + b"WEBP" + WEBP_REORDERED_BODY
)
CORRUPT_WEBP_BYTES = (
    b"RIFF"
    + (18).to_bytes(4, "little")
    + b"WEBPVP8L"
    + (5).to_bytes(4, "little")
    + b"\x2f\x00\x00\x00\x00\x00"
)


class FakeCompletions:
    def __init__(self, response: SimpleNamespace) -> None:
        self.calls: list[dict[str, Any]] = []
        self._response = response

    async def create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        return self._response


def test_openrouter_provider_sends_routing_headers_and_modalities() -> None:
    message = SimpleNamespace(content="done", tool_calls=None, images=None)
    native = SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        usage=SimpleNamespace(
            prompt_tokens=1,
            completion_tokens=2,
            total_tokens=3,
            cost=0.014,
        ),
        model="openai/gpt-4.1",
        service_tier="priority",
        openrouter_metadata={
            "is_byok": False,
            "endpoints": {
                "available": [
                    {"provider": "Other", "selected": False},
                    {"provider": "OpenAI", "selected": True},
                ]
            },
        },
    )
    provider = OpenRouterProvider(
        api_key="test",
        model="openai/gpt-4.1",
        provider_routing={"require_parameters": True, "data_collection": "deny"},
        service_tier="priority",
        app_url="https://example.com",
        app_name="Kímí 🤖\r\nInjected: value",
    )
    completions = FakeCompletions(native)
    provider._client = cast(Any, SimpleNamespace(chat=SimpleNamespace(completions=completions)))

    response = asyncio.run(
        provider.run_turn(
            ProviderRequest(
                conversation_id=1,
                system_prompt="",
                messages=[],
                current_user_parts=[ContentPart.from_text("hi")],
                tools=[],
                max_tokens=128,
                requested_capabilities={ProviderCapability.IMAGE_OUTPUT},
            )
        )
    )

    request = completions.calls[0]
    assert request["extra_headers"]["HTTP-Referer"] == "https://example.com"
    assert request["extra_headers"]["X-OpenRouter-Title"] == "Kimi Injected- value"
    assert "X-Title" not in request["extra_headers"]
    assert request["extra_headers"]["X-OpenRouter-Metadata"] == "enabled"
    assert request["service_tier"] == "priority"
    assert request["extra_body"]["provider"] == {
        "require_parameters": True,
        "data_collection": "deny",
    }
    assert request["modalities"] == ["image", "text"]
    assert response.content == "done"
    assert response.provider_state == {}
    assert response.upstream_provider == "OpenAI"
    assert response.service_tier == "priority"
    assert response.openrouter_charge_usd == 0.014
    assert response.is_byok is False
    assert response.model == "openai/gpt-4.1"


def test_openrouter_provider_does_not_send_openai_reasoning_effort() -> None:
    message = SimpleNamespace(content="done", tool_calls=None, images=None)
    native = SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=2, total_tokens=3),
        model="x/y",
        openrouter_metadata={},
    )
    provider = OpenRouterProvider(api_key="test", model="x/y")
    completions = FakeCompletions(native)
    provider._client = cast(Any, SimpleNamespace(chat=SimpleNamespace(completions=completions)))

    asyncio.run(
        provider.run_turn(
            ProviderRequest(
                conversation_id=1,
                system_prompt="",
                messages=[],
                current_user_parts=[ContentPart.from_text("hi")],
                tools=[],
                max_tokens=128,
                reasoning_effort="high",
            )
        )
    )

    assert "reasoning_effort" not in completions.calls[0]
    assert completions.calls[0]["extra_headers"] == {"X-OpenRouter-Metadata": "enabled"}


def test_openrouter_provider_preserves_zero_charge_and_reads_additive_sdk_fields() -> None:
    message = SimpleNamespace(content="done", tool_calls=None, images=None)
    usage = SimpleNamespace(
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=0,
        model_extra={"cost": 0},
    )
    native = SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        usage=usage,
        model="x/y",
        model_extra={
            "openrouter_metadata": SimpleNamespace(
                model_extra={
                    "is_byok": True,
                    "endpoints": SimpleNamespace(
                        model_extra={
                            "available": [
                                SimpleNamespace(
                                    model_extra={
                                        "provider": "  Provider\nName  ",
                                        "selected": True,
                                    }
                                )
                            ]
                        }
                    ),
                }
            ),
            "service_tier": "flex",
        },
    )
    provider = OpenRouterProvider(api_key="test", model="x/y")
    completions = FakeCompletions(native)
    provider._client = cast(Any, SimpleNamespace(chat=SimpleNamespace(completions=completions)))

    response = asyncio.run(
        provider.run_turn(
            ProviderRequest(
                conversation_id=1,
                system_prompt="",
                messages=[],
                current_user_parts=[ContentPart.from_text("hi")],
                tools=[],
                max_tokens=128,
            )
        )
    )

    assert response.upstream_provider == "ProviderName"
    assert response.service_tier == "flex"
    assert response.openrouter_charge_usd == 0.0
    assert response.is_byok is True


def test_openrouter_provider_captures_reasoning_field() -> None:
    # OpenRouter returns chain-of-thought in `message.reasoning`, not the
    # `reasoning_content` field the base OpenAI-chat provider looks for.
    message = SimpleNamespace(
        content="answer", tool_calls=None, images=None, reasoning="step-by-step thinking"
    )
    native = SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=2, total_tokens=3),
        model="x/y",
        openrouter_metadata={},
    )
    provider = OpenRouterProvider(api_key="test", model="x/y")
    completions = FakeCompletions(native)
    provider._client = cast(Any, SimpleNamespace(chat=SimpleNamespace(completions=completions)))

    response = asyncio.run(
        provider.run_turn(
            ProviderRequest(
                conversation_id=1,
                system_prompt="",
                messages=[],
                current_user_parts=[ContentPart.from_text("hi")],
                tools=[],
                max_tokens=128,
            )
        )
    )

    assert response.reasoning_content == "step-by-step thinking"


def test_openrouter_provider_extracts_generated_images() -> None:
    image = SimpleNamespace(image_url=SimpleNamespace(url=f"data:image/png;base64,{PNG_BASE64}"))
    message = SimpleNamespace(content="made one", tool_calls=None, images=[image])
    native = SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        usage=None,
        openrouter_metadata={},
    )
    provider = OpenRouterProvider(api_key="test", model="google/gemini-image")
    completions = FakeCompletions(native)
    provider._client = cast(Any, SimpleNamespace(chat=SimpleNamespace(completions=completions)))

    response = asyncio.run(
        provider.run_turn(
            ProviderRequest(
                conversation_id=1,
                system_prompt="",
                messages=[],
                current_user_parts=[ContentPart.from_text("make image")],
                tools=[],
                max_tokens=128,
            )
        )
    )

    assert response.generated_assets[0].data_base64 == PNG_BASE64


def test_openrouter_provider_extracts_generated_images_from_sdk_extra_dicts() -> None:
    message = SimpleNamespace(
        content="made one",
        tool_calls=None,
        model_extra={
            "images": [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{PNG_BASE64}"},
                }
            ]
        },
    )
    native = SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        usage=None,
        openrouter_metadata={},
    )
    provider = OpenRouterProvider(api_key="test", model="google/gemini-image")
    completions = FakeCompletions(native)
    provider._client = cast(Any, SimpleNamespace(chat=SimpleNamespace(completions=completions)))

    response = asyncio.run(
        provider.run_turn(
            ProviderRequest(
                conversation_id=1,
                system_prompt="",
                messages=[],
                current_user_parts=[ContentPart.from_text("make image")],
                tools=[],
                max_tokens=128,
            )
        )
    )

    assert response.generated_assets[0].data_base64 == PNG_BASE64


@pytest.mark.parametrize(
    ("declared_media_type", "raw", "expected_media_type", "expected_suffix"),
    [
        ("image/jpeg", JPEG_BYTES, "image/jpeg", ".jpg"),
        ("image/webp", WEBP_BYTES, "image/webp", ".webp"),
        ("image/gif", GIF_BYTES, "image/gif", ".gif"),
        ("image/png;charset=binary", PNG_BYTES, "image/png", ".png"),
        # A valid image with an untrusted or missing label is kept and canonicalized.
        ("image/svg+xml", JPEG_BYTES, "image/jpeg", ".jpg"),
        ("", PNG_BYTES, "image/png", ".png"),
    ],
)
def test_openrouter_provider_canonicalizes_image_type_from_bytes(
    declared_media_type: str,
    raw: bytes,
    expected_media_type: str,
    expected_suffix: str,
) -> None:
    payload = base64.b64encode(raw).decode("ascii")
    [asset] = OpenRouterProvider._parse_images(
        [
            SimpleNamespace(
                image_url=SimpleNamespace(url=f"data:{declared_media_type};base64,{payload}")
            )
        ]
    )

    assert asset.media_type == expected_media_type
    assert asset.data_base64 == payload
    assert asset.suggested_filename == f"openrouter-image-1{expected_suffix}"


@pytest.mark.parametrize(
    "url",
    [
        "https://example.test/image.png",
        "data:text/html;base64,PCFkb2N0eXBlIGh0bWw+",
        "data:image/png;base64,not-valid-base64!",
        f"data:image/png,{PNG_BASE64}",
    ],
)
def test_openrouter_provider_skips_urls_that_are_not_valid_inline_images(url: str) -> None:
    assert (
        OpenRouterProvider._parse_images([SimpleNamespace(image_url=SimpleNamespace(url=url))])
        == []
    )


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(b"\x89PNG\r\n\x1a\n\x00\x00", id="png-signature-only"),
        pytest.param(PNG_WITH_INVALID_COMPRESSION_BYTES, id="png-invalid-compression"),
        pytest.param(base64.b64decode("/9j/2Q=="), id="jpeg-truncated"),
        pytest.param(JPEG_WITH_MISMATCHED_ARITHMETIC_SOF_BYTES, id="jpeg-arithmetic-sof"),
        pytest.param(base64.b64decode("R0lGODlh"), id="gif-signature-only"),
        pytest.param(GIF_WITHOUT_PALETTE_BYTES, id="gif-no-palette"),
        pytest.param(GIF_WITH_INVALID_LZW_BYTES, id="gif-invalid-lzw"),
        pytest.param(GIF_WITH_INVALID_FRAME_POSITION_BYTES, id="gif-frame-position"),
        pytest.param(base64.b64decode("UklGRgAAAABXRUJQ"), id="webp-signature-only"),
        pytest.param(CORRUPT_WEBP_BYTES, id="webp-corrupt"),
        pytest.param(WEBP_WITH_LATE_EXTENDED_HEADER_BYTES, id="webp-late-header"),
    ],
)
def test_sniffable_but_undecodable_payloads_pass_parse_and_die_at_validation(
    payload: bytes,
) -> None:
    """Parse is deliberately cheap (it runs on the event loop): a bare
    signature gets through it and is dropped by the shared decode-level
    validation every provider's assets funnel through before moderation."""
    url = "data:application/octet-stream;base64," + base64.b64encode(payload).decode("ascii")
    parsed = OpenRouterProvider._parse_images([SimpleNamespace(image_url=SimpleNamespace(url=url))])
    assert len(parsed) == 1
    assert validate_generated_assets(parsed) == []


def test_openrouter_provider_caps_images_before_they_reach_moderation() -> None:
    url = f"data:image/png;base64,{PNG_BASE64}"
    images = [SimpleNamespace(image_url=SimpleNamespace(url=url)) for _ in range(10)]

    assets = OpenRouterProvider._parse_images(images)

    assert len(assets) == openrouter_module._MAX_INLINE_IMAGES


def test_openrouter_provider_caps_decoded_and_aggregate_image_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = f"data:image/png;base64,{PNG_BASE64}"
    image = SimpleNamespace(image_url=SimpleNamespace(url=url))
    monkeypatch.setattr(openrouter_module, "_MAX_INLINE_IMAGE_BYTES", len(PNG_BYTES) - 1)
    assert OpenRouterProvider._parse_images([image]) == []

    monkeypatch.setattr(openrouter_module, "_MAX_INLINE_IMAGE_BYTES", len(PNG_BYTES))
    monkeypatch.setattr(openrouter_module, "_MAX_TOTAL_INLINE_IMAGE_BYTES", len(PNG_BYTES))
    assert len(OpenRouterProvider._parse_images([image, image])) == 1


def test_openrouter_provider_counts_rejected_images_toward_processing_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Not sniffable at all, so parse rejects it -- and must still charge its
    # decoded size against the aggregate budget, or a provider could stream
    # rejected payloads past the cap.
    junk = b"\x00" * len(CORRUPT_WEBP_BYTES)
    corrupt = {
        "image_url": {"url": "data:image/webp;base64," + base64.b64encode(junk).decode("ascii")}
    }
    valid = {"image_url": {"url": f"data:image/png;base64,{PNG_BASE64}"}}
    monkeypatch.setattr(
        openrouter_module,
        "_MAX_TOTAL_INLINE_IMAGE_BYTES",
        len(junk) + len(PNG_BYTES) - 1,
    )

    assert OpenRouterProvider._parse_images([corrupt, valid]) == []


@pytest.mark.parametrize("payload", ["AAAA", "AAA!"])
def test_openrouter_provider_bounds_encoded_work_for_rejected_candidates(
    monkeypatch: pytest.MonkeyPatch,
    payload: str,
) -> None:
    real_decode = openrouter_module.base64.b64decode
    decode_calls = 0

    def counted_decode(value: str, *, validate: bool) -> bytes:
        nonlocal decode_calls
        decode_calls += 1
        # Parsing must never decode a payload, only a bounded sniff prefix.
        assert len(value) <= 32
        return real_decode(value, validate=validate)

    monkeypatch.setattr(openrouter_module, "_MAX_INLINE_IMAGE_BYTES", 2)
    monkeypatch.setattr(openrouter_module, "_MAX_TOTAL_INLINE_IMAGE_BYTES", 4)
    monkeypatch.setattr(openrouter_module.base64, "b64decode", counted_decode)
    image = {"image_url": {"url": f"data:image/png;base64,{payload}"}}

    assert OpenRouterProvider._parse_images([image] * 8) == []
    # Work is bounded per candidate (the <=32-char assertion above), not by
    # an early stop: every admitted candidate costs one prefix sniff only.
    assert decode_calls == 8


def test_openrouter_provider_rejects_oversized_data_url_header() -> None:
    url = f"data:image/png;{'x;' * 1000}base64,{PNG_BASE64}"

    assert OpenRouterProvider._parse_images([{"image_url": {"url": url}}]) == []
