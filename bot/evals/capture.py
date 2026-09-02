from __future__ import annotations

import asyncio
import inspect
import json
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from evals.cassette import Cassette, Fault, cassette_records
from providers.base import LLMProvider
from providers.types import ProviderCapability, ProviderRequest, ProviderResponse
from tools._common import tool_error
from tools.internet_search import BUDGET_EXCEEDED_MESSAGE
from tools.registry import BudgetName, MessageContext, ToolRegistry
from usage.normalization import UsageBreakdown, normalize_usage

# Approximate result tokens for relative eval cost reporting.
CHARS_PER_TOKEN = 4


def sum_tokens(usage: dict[str, Any]) -> int:
    for key in ("total_tokens", "total"):
        value = usage.get(key)
        if isinstance(value, int):
            return value
    prompt = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
    completion = usage.get("completion_tokens") or usage.get("output_tokens") or 0
    try:
        return int(prompt) + int(completion)
    except TypeError, ValueError:
        return 0


@dataclass
class ProviderCall:
    latency_ms: int
    tokens: int
    # Preserve buckets because they can have different rates.
    usage: UsageBreakdown = field(default_factory=UsageBreakdown)
    usage_present: bool = True


class InstrumentedProvider(LLMProvider):
    """Record latency and token usage for successful provider calls."""

    def __init__(
        self,
        inner: LLMProvider,
        *,
        min_request_interval_seconds: float = 0.0,
        request_timeout_seconds: float | None = None,
    ) -> None:
        if not math.isfinite(min_request_interval_seconds) or min_request_interval_seconds < 0:
            raise ValueError("min_request_interval_seconds must be >= 0")
        if request_timeout_seconds is not None and (
            not math.isfinite(request_timeout_seconds) or request_timeout_seconds <= 0
        ):
            raise ValueError("request_timeout_seconds must be > 0 when set")
        self._inner = inner
        self.calls: list[ProviderCall] = []
        self._min_request_interval_seconds = min_request_interval_seconds
        self._request_timeout_seconds = request_timeout_seconds
        self._next_request_at = 0.0
        self._pace_lock = asyncio.Lock()

    @property
    def provider_key(self) -> str:
        return self._inner.provider_key

    @property
    def model(self) -> str:
        return self._inner.model

    @property
    def capabilities(self) -> set[ProviderCapability]:
        return self._inner.capabilities

    @property
    def total_tokens(self) -> int:
        return sum(call.tokens for call in self.calls)

    @property
    def total_usage(self) -> UsageBreakdown:
        total = UsageBreakdown()
        for call in self.calls:
            total = total + call.usage
        return total

    @property
    def has_complete_usage(self) -> bool:
        return all(call.usage_present for call in self.calls)

    @property
    def total_latency_ms(self) -> int:
        return sum(call.latency_ms for call in self.calls)

    def reset(self) -> None:
        self.calls = []

    async def close(self) -> None:
        close = getattr(self._inner, "close", None)
        if close is None:
            return
        result = close()
        if inspect.isawaitable(result):
            await result

    async def run_turn(self, request: ProviderRequest) -> ProviderResponse:
        async with self._pace_lock:
            now = time.monotonic()
            if now < self._next_request_at:
                await asyncio.sleep(self._next_request_at - now)
            self._next_request_at = time.monotonic() + self._min_request_interval_seconds
        start = time.monotonic()
        if self._request_timeout_seconds is None:
            response = await self._inner.run_turn(request)
        else:
            async with asyncio.timeout(self._request_timeout_seconds):
                response = await self._inner.run_turn(request)
        latency_ms = int((time.monotonic() - start) * 1000)
        self.calls.append(
            ProviderCall(
                latency_ms=latency_ms,
                tokens=sum_tokens(response.usage),
                # Keep runtime and eval accounting on the same bucket semantics.
                usage=normalize_usage(response.usage),
                usage_present=response.has_reported_usage,
            )
        )
        return response


@dataclass
class ToolCallRecord:
    tool: str
    args: dict[str, Any]
    result: str
    ok: bool
    duration_ms: int
    # "live", "replay", "fault", "miss", or "denied".
    source: str = "live"
    # Provider calls completed before this result entered the context.
    provider_calls_before: int = 0

    @property
    def result_chars(self) -> int:
        return len(self.result)

    @property
    def estimated_tokens(self) -> int:
        """Approximate result size at four characters per token."""
        return -(-len(self.result) // CHARS_PER_TOKEN)


def _result_ok(result: str) -> bool:
    try:
        parsed = json.loads(result)
    except ValueError, TypeError:
        return True
    return not (isinstance(parsed, dict) and "error" in parsed)


class InstrumentedRegistry(ToolRegistry):
    """Record dispatches with optional cassette replay and fault injection.

    The production gate runs before replay, faults, or live execution.
    """

    def __init__(self) -> None:
        super().__init__()
        self.sink: list[ToolCallRecord] = []
        self._cassette: Cassette | None = None
        self._cassette_mode: str = "off"
        self._fault_budget: dict[str, list[Fault]] = {}
        self._provider_calls: Callable[[], int] = lambda: 0

    def set_provider_call_counter(self, counter: Callable[[], int]) -> None:
        """Set the completed-call counter used for tool cost attribution."""
        self._provider_calls = counter

    def reset_sink(self) -> None:
        self.sink = []

    def configure_cassette(self, cassette: Cassette | None, mode: str = "off") -> None:
        self._cassette = cassette if mode != "off" else None
        self._cassette_mode = mode if cassette is not None else "off"

    def set_faults(self, faults: list[Fault]) -> None:
        """Make the next `times` allowed calls to each tool fail."""
        budget: dict[str, list[Fault]] = {}
        for fault in faults:
            budget.setdefault(fault.tool, []).extend([fault] * max(fault.times, 0))
        self._fault_budget = budget

    def _pop_fault(self, name: str) -> Fault | None:
        queue = self._fault_budget.get(name)
        if not queue:
            return None
        return queue.pop(0)

    async def _resolve(self, name: str, args: dict, ctx: MessageContext) -> tuple[str, str]:
        gate_error = self.dispatch_gate(name, ctx)
        if gate_error is not None:
            return gate_error, "denied"
        fault = self._pop_fault(name)
        if fault is not None:
            return json.dumps({"error": fault.message}), "fault"
        if not cassette_records(name):
            return await super().dispatch(name, args, ctx), "live"
        cassette, mode = self._cassette, self._cassette_mode
        if cassette is not None and mode in ("replay", "strict"):
            recorded = cassette.replay_record(
                name, args if isinstance(args, dict) else {"_raw": args}
            )
            if recorded is not None:
                if name == "internet_search":
                    backend_calls = recorded.internet_search_backend_calls
                    assert backend_calls is not None
                    if not ctx.consume_budget(
                        BudgetName.INTERNET_SEARCH_BACKEND_CALLS,
                        backend_calls,
                    ):
                        return tool_error(BUDGET_EXCEEDED_MESSAGE), "replay"
                return recorded.result, "replay"
            if mode == "strict":
                message = f"cassette miss for {name!r}; re-record with --cassette record"
                return json.dumps({"error": message}), "miss"
        backend_calls_before = ctx.budget_used(BudgetName.INTERNET_SEARCH_BACKEND_CALLS)
        result = await super().dispatch(name, args, ctx)
        if cassette is not None and mode in ("record", "replay"):
            cassette.record(
                name,
                args if isinstance(args, dict) else {"_raw": args},
                result,
                internet_search_backend_calls=(
                    ctx.budget_used(BudgetName.INTERNET_SEARCH_BACKEND_CALLS) - backend_calls_before
                    if name == "internet_search"
                    else None
                ),
            )
        return result, "live"

    async def dispatch(self, name: str, args: dict, ctx: MessageContext) -> str:
        start = time.monotonic()
        provider_calls_before = self._provider_calls()
        result, source = await self._resolve(name, args, ctx)
        duration_ms = int((time.monotonic() - start) * 1000)
        self.sink.append(
            ToolCallRecord(
                tool=name,
                args=args if isinstance(args, dict) else {"_raw": args},
                result=result,
                ok=_result_ok(result),
                duration_ms=duration_ms,
                source=source,
                provider_calls_before=provider_calls_before,
            )
        )
        return result
