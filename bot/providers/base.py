from __future__ import annotations

from abc import ABC, abstractmethod

from providers.types import (
    ProviderCapability,
    ProviderRequest,
    ProviderResponse,
    ReasoningEscalation,
)

__all__ = ["LLMProvider"]


class LLMProvider(ABC):
    """Abstract interface for LLM providers.

    Each provider normalizes its native response format into ProviderResponse/ToolCall
    so the agent core never touches provider-specific types.
    """

    @property
    def provider_key(self) -> str:
        return self.__class__.__name__

    @property
    def model(self) -> str:
        return ""

    @property
    def capabilities(self) -> set[ProviderCapability]:
        return {ProviderCapability.TEXT, ProviderCapability.TOOL_CALLING}

    @property
    def reasoning_escalations(self) -> tuple[ReasoningEscalation, ...]:
        """Model-specific reasoning increases triggered by tool calls."""
        return ()

    @abstractmethod
    async def run_turn(self, request: ProviderRequest) -> ProviderResponse:
        raise NotImplementedError
