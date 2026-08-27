from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal


class ContentPartType(str, Enum):
    TEXT = "text"
    IMAGE = "image"


class ProviderCapability(str, Enum):
    TEXT = "text"
    IMAGE_INPUT = "image_input"
    IMAGE_OUTPUT = "image_output"
    TOOL_CALLING = "tool_calling"
    PREVIOUS_RESPONSE_ID = "previous_response_id"
    # Stateful providers advertise this so client compaction discards upstream
    # continuation from the stale transcript. No shipped provider does today;
    # the member is retained because the guard it drives in agent/core.py is the
    # fail-safe for a backend that keeps transcript state upstream. Declare it on
    # any future stateful provider; replay-style ones such as Codex leave it off.
    SERVER_SIDE_CONTEXT = "server_side_context"
    FLEX_SERVICE_TIER = "flex_service_tier"


REASONING_EFFORT_ORDER = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
)
REASONING_EFFORT_RANK = {effort: rank for rank, effort in enumerate(REASONING_EFFORT_ORDER)}


@dataclass(frozen=True)
class ReasoningEscalation:
    effort: str
    tool_names: frozenset[str]


@dataclass(frozen=True)
class ContentPart:
    type: ContentPartType
    text: str | None = None
    image_url: str | None = None
    media_type: str | None = None
    detail: str | None = None

    @classmethod
    def from_text(cls, value: str) -> ContentPart:
        return cls(type=ContentPartType.TEXT, text=value)

    @classmethod
    def from_image_url(
        cls,
        *,
        url: str,
        media_type: str,
        detail: str | None = "auto",
    ) -> ContentPart:
        return cls(
            type=ContentPartType.IMAGE,
            image_url=url,
            media_type=media_type,
            detail=detail,
        )


@dataclass(frozen=True)
class ConversationMessage:
    role: Literal["user", "assistant", "tool"]
    content: list[ContentPart] = field(default_factory=list)
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw_provider_data: dict[str, Any] = field(default_factory=dict)
    # Local transcript provenance; providers deliberately ignore this field.
    source_discord_message_id: str | None = field(default=None, compare=False)


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class GeneratedAsset:
    kind: Literal["image"]
    media_type: str
    data_base64: str
    suggested_filename: str


@dataclass(frozen=True)
class ProviderRequest:
    conversation_id: int
    system_prompt: str
    messages: list[ConversationMessage]
    current_user_parts: list[ContentPart]
    tools: list[dict[str, Any]]
    max_tokens: int
    temperature: float | None = None
    provider_state: dict[str, Any] = field(default_factory=dict)
    requested_capabilities: set[ProviderCapability] = field(default_factory=set)
    recalled_memories: str = ""
    continuation_context_messages: list[ConversationMessage] = field(default_factory=list)
    reasoning_enabled: bool = True
    # Optional per-call override selected by the provider's model-specific tool
    # escalation policy. Providers without adjustable reasoning ignore it.
    reasoning_effort: str | None = None


@dataclass(frozen=True)
class ProviderResponse:
    content: str | None = None
    reasoning_content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict[str, Any] = field(default_factory=dict)
    provider_state: dict[str, Any] = field(default_factory=dict)
    generated_assets: list[GeneratedAsset] = field(default_factory=list)
    raw_message: dict[str, Any] = field(default_factory=dict)
    # The model that actually served this response. Empty means "the caller's
    # configured model" (readers fall back to provider.model); FailoverProvider
    # stamps it with the serving backend so fallback-served responses stay
    # attributable in the observability stream.
    model: str = ""
    # Configured backend model whose rate card prices this call. This can differ
    # from ``model`` when an API reports a dated/concrete serving model.
    pricing_model: str = ""

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)
