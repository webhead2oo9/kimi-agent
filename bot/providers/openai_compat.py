from __future__ import annotations

from branding import DEFAULT_BOT_NAME
from providers.openai_chat import OpenAIChatProvider


class OpenAICompatProvider(OpenAIChatProvider):
    """Provider for generic OpenAI-compatible Chat Completions APIs.

    Requests stream so a stalled backend is observable (chunk cadence in the
    log) instead of an opaque multi-minute silence; a backend that rejects
    streaming is retried and downgraded to non-streaming automatically.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        service_tier: str | None = None,
        reasoning_effort: str = "",
        request_id_header: str = "",
        stall_timeout_seconds: float = 90.0,
        user_agent: str = DEFAULT_BOT_NAME,
    ) -> None:
        super().__init__(
            api_key=api_key,
            base_url=base_url,
            model=model,
            provider_key="openai_compat",
            service_tier=service_tier,
            reasoning_effort=reasoning_effort,
            request_id_header=request_id_header,
            stream=True,
            stall_timeout_seconds=stall_timeout_seconds,
            user_agent=user_agent,
        )
