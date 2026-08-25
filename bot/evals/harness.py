from __future__ import annotations

import base64
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent.compaction import Compactor
from agent.context import ConversationContext
from utils.format import sanitize_author_name
from agent.core import ConversationRunRequest, run_conversation
from agent.reply_context import ReplyContext
from evals.capture import InstrumentedProvider, InstrumentedRegistry, ToolCallRecord
from evals.identity import EVAL_USER_NAME, EvalIdentity, new_eval_run_nonce
from evals.scenario import Scenario, TurnSpec
from utils.image_types import sniff_image_media_type
from memory.recall import recall_current_user_context
from providers.types import ContentPart, ConversationMessage
from usage.normalization import UsageBreakdown


@dataclass
class TurnRecord:
    user_message: str
    final_text: str
    tool_calls: list[ToolCallRecord]
    tokens: int
    latency_ms: int
    # Files queued on the outgoing-attachment rail this turn (workspace paths).
    attached_files: list[str] = field(default_factory=list)
    # Per-bucket usage for the turn, so cost is computable at any rollup level.
    usage: UsageBreakdown = field(default_factory=UsageBreakdown)
    # Provider calls this turn: the ReAct iteration count, and the multiplier on
    # every tool result that entered the context before them.
    provider_calls: int = 0


@dataclass
class ScenarioRun:
    scenario_id: str
    model_label: str
    identity: EvalIdentity | None = None
    turns: list[TurnRecord] = field(default_factory=list)
    # End-to-end scenario time, including model calls, tool execution, memory
    # recall, and every turn. This is informational and never affects scoring.
    wall_time_ms: int = 0

    @property
    def total_tokens(self) -> int:
        return sum(t.tokens for t in self.turns)

    @property
    def total_latency_ms(self) -> int:
        return sum(t.latency_ms for t in self.turns)

    @property
    def all_tool_calls(self) -> list[ToolCallRecord]:
        return [record for turn in self.turns for record in turn.tool_calls]

    @property
    def total_usage(self) -> UsageBreakdown:
        total = UsageBreakdown()
        for turn in self.turns:
            total = total + turn.usage
        return total

    @property
    def provider_calls(self) -> int:
        return sum(turn.provider_calls for turn in self.turns)


def _seed_context(scenario: Scenario, identity: EvalIdentity) -> ConversationContext:
    context = ConversationContext(
        key=f"eval:{scenario.id}",
        user_id=identity.user_id,
        user_name=EVAL_USER_NAME,
        channel_name=scenario.channel_name,
        activated_tools=set(scenario.activated_tools),
    )
    messages: list[ConversationMessage] = []
    for role, name, text in scenario.seeded_history:
        if role == "user":
            label = f"{sanitize_author_name(name or 'User')}: {text}"
            messages.append(
                ConversationMessage(role="user", content=[ContentPart.from_text(label)])
            )
        else:
            messages.append(
                ConversationMessage(role="assistant", content=[ContentPart.from_text(text)])
            )
    context.add_messages(messages)
    return context


FIXTURE_IMAGE_DIR = Path(__file__).resolve().parent / "fixtures" / "images"


def image_part(name: str) -> ContentPart:
    """Load a fixture image as the exact content part production would build.

    Discord attachments reach the model as base64 data URLs with a
    signature-sniffed media type (agent/attachments.py), so eval images take the
    same shape, because a fixture passed as an https URL would exercise a path
    the bot never uses.
    """
    path = (FIXTURE_IMAGE_DIR / name).resolve()
    if FIXTURE_IMAGE_DIR.resolve() not in path.parents:
        raise ValueError(f"Fixture image {name!r} escapes {FIXTURE_IMAGE_DIR}")
    if not path.is_file():
        raise FileNotFoundError(f"Scenario image fixture not found: {path}")
    payload = path.read_bytes()
    media_type = sniff_image_media_type(payload)
    if media_type is None:
        raise ValueError(f"Fixture image {name!r} is not a supported image type")
    encoded = base64.b64encode(payload).decode("ascii")
    return ContentPart.from_image_url(
        url=f"data:{media_type};base64,{encoded}", media_type=media_type
    )


def _reply_context(turn: TurnSpec) -> ReplyContext | None:
    if not turn.reply_images:
        return None
    return ReplyContext(
        referenced_message_id="900000000000000001",
        author_name=turn.reply_author,
        text=turn.reply_text,
        image_parts=tuple(image_part(name) for name in turn.reply_images),
    )


async def run_scenario_for_model(
    scenario: Scenario,
    *,
    provider: InstrumentedProvider,
    registry: InstrumentedRegistry,
    gateway: Any,
    memory_client: Any | None,
    preference_store: Any | None,
    bot_name: str = "",
    compactor: Compactor | None = None,
    max_tokens: int = 65_536,
    identity: EvalIdentity | None = None,
) -> ScenarioRun:
    started_at = time.monotonic()
    identity = identity or EvalIdentity(
        run_nonce=new_eval_run_nonce(),
        arm=provider.model,
        scenario_id=scenario.id,
        repetition=0,
    )
    context = _seed_context(scenario, identity)
    run = ScenarioRun(
        scenario_id=scenario.id,
        model_label=provider.model,
        identity=identity,
    )

    # Places each tool call in the turn's provider-call timeline, which is what
    # makes "how much context did this tool cost" answerable per tool.
    registry.set_provider_call_counter(lambda: len(provider.calls))

    for turn in scenario.turns:
        user_message = turn.text
        gateway.set_fixture(trigger_content=user_message, trigger_author_id=identity.user_id)
        provider.reset()
        registry.reset_sink()

        recalled = await recall_current_user_context(
            memory_client=memory_client,
            preference_store=preference_store,
            user_id=identity.user_id,
            user_message=user_message,
            context=context,
        )

        result = await run_conversation(
            request=ConversationRunRequest(
                user_message=user_message,
                context=context,
                trust_tier=scenario.trust_tier,
                user_name=EVAL_USER_NAME,
                user_id=identity.user_id,
                provider=provider,
                registry=registry,
                max_tokens=max_tokens,
                channel_name=scenario.channel_name,
                guild_id=scenario.guild_id or None,
                guild_name=scenario.guild_name,
                channel_id=scenario.channel_id,
                bot_name=bot_name,
                recalled_memories=recalled,
                compactor=compactor,
                input_parts=[image_part(name) for name in turn.images] or None,
                reply_context=_reply_context(turn),
            )
        )
        run.turns.append(
            TurnRecord(
                user_message=user_message,
                final_text=result.text,
                tool_calls=list(registry.sink),
                tokens=provider.total_tokens,
                latency_ms=provider.total_latency_ms,
                attached_files=[str(p) for p in context.pending_output_files],
                usage=provider.total_usage,
                provider_calls=len(provider.calls),
            )
        )
    run.wall_time_ms = int((time.monotonic() - started_at) * 1000)
    return run
