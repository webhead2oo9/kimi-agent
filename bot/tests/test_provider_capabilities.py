import asyncio

from agent.core import ConversationRunRequest, run_conversation
from agent.context import ConversationContext
from providers.base import LLMProvider
from providers.types import ContentPart, ProviderCapability, ProviderRequest, ProviderResponse
from tools.registry import ToolRegistry
from trust.tiers import TrustTier


class TextOnlyProvider(LLMProvider):
    provider_key = "text_only"
    model = "text-model"
    capabilities = {ProviderCapability.TEXT}

    async def run_turn(self, request: ProviderRequest) -> ProviderResponse:
        return ProviderResponse(content="done")


class ImageCapableProvider(LLMProvider):
    provider_key = "img"
    model = "img-model"
    capabilities = {ProviderCapability.TEXT, ProviderCapability.IMAGE_OUTPUT}

    def __init__(self) -> None:
        self.requests: list[ProviderRequest] = []

    async def run_turn(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        return ProviderResponse(content="done")


def test_core_downgrades_image_output_intent_for_text_only_provider() -> None:
    # A bare visual verb ("draw") in ordinary chat must not abort the turn on a
    # text-only provider; image output is a soft hint, not a hard precondition.
    provider = TextOnlyProvider()
    ctx = ConversationContext(key="k", db_conversation_id=1)

    result = asyncio.run(
        run_conversation(
            request=ConversationRunRequest(
                user_message="let's call it a draw",
                context=ctx,
                trust_tier=TrustTier.MEMBER,
                user_name="webhead",
                user_id="1",
                provider=provider,
                registry=ToolRegistry(),
            )
        )
    )

    assert "does not support image output" not in result.text
    assert result.text == "done"


def test_core_requests_image_output_when_provider_supports_it() -> None:
    provider = ImageCapableProvider()
    ctx = ConversationContext(key="k", db_conversation_id=1)

    asyncio.run(
        run_conversation(
            request=ConversationRunRequest(
                user_message="draw me a sunset",
                context=ctx,
                trust_tier=TrustTier.MEMBER,
                user_name="webhead",
                user_id="1",
                provider=provider,
                registry=ToolRegistry(),
            )
        )
    )

    assert ProviderCapability.IMAGE_OUTPUT in provider.requests[0].requested_capabilities


def test_core_rejects_image_when_provider_lacks_image_input() -> None:
    provider = TextOnlyProvider()
    ctx = ConversationContext(key="k", db_conversation_id=1)

    result = asyncio.run(
        run_conversation(
            request=ConversationRunRequest(
                user_message="look",
                input_parts=[
                    ContentPart.from_image_url(
                        url="data:image/png;base64,abc",
                        media_type="image/png",
                    )
                ],
                provider_state={},
                context=ctx,
                trust_tier=TrustTier.MEMBER,
                user_name="webhead",
                user_id="1",
                provider=provider,
                registry=ToolRegistry(),
            )
        )
    )

    assert "does not support image input" in result.text
