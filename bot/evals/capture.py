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
from tools.registry import MessageContext, ToolRegistry
from usage.normalization import UsageBreakdown, normalize_usage

# Result text is sized with this heuristic rather than a real tokenizer; see
# ToolCallRecord.estimated_tokens. Reporting wants an accurate estimate, so this
# is the ~4 chars/token English-prose figure; agent/compaction.py deliberately
# uses a denser 3.5 to over-count and trip compaction early. Not drift.
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
    # Per-bucket split, needed because pricing is per bucket: a cached read is
    # commonly a quarter of the input rate, so a flat total cannot be costed.
    usage: UsageBreakdown = field(default_factory=UsageBreakdown)


class InstrumentedProvider(LLMProvider):
    """Wraps a provider to record per-call latency + token usage."""

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
                # Reuse the production normalizer rather than re-deriving bucket
                # names per provider shape; eval costs then match /usage costs.
                usage=normalize_usage(response.usage),
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
    # Where the result came from: "live" (real handler), "replay" (cassette),
    # "fault" (scenario-injected failure), "miss" (strict-mode cassette miss),
    # or "denied" (the registry's dispatch gate rejected the call).
    source: str = "live"
    # How many provider calls this turn had already completed when the tool was
    # dispatched. A tool result is not billed once: it joins the context and is
    # re-sent as input on every later call of the same turn, so this is what
    # turns a result size into a share of the bill.
    provider_calls_before: int = 0

    @property
    def result_chars(self) -> int:
        return len(self.result)

    @property
    def estimated_tokens(self) -> int:
        """Rough token size of the result text.

        A heuristic (~4 chars/token), not a tokenizer: the eval harness must not
        take a tokenizer dependency per model family, and the number is used for
        *relative* comparison between tools, where a consistent bias cancels.
        """
        return -(-len(self.result) // CHARS_PER_TOKEN)


def _result_ok(result: str) -> bool:
    try:
        parsed = json.loads(result)
    except ValueError, TypeError:
        return True
    return not (isinstance(parsed, dict) and "error" in parsed)


class InstrumentedRegistry(ToolRegistry):
    """A ToolRegistry that records every dispatch into a resettable sink.

    Optionally fronts dispatch with a per-scenario cassette (record/replay) and
    a fault queue; see evals/cassette.py. Replay hits and faults return without
    invoking the real handler, but every source first passes the registry's
    authoritative side-effect-free dispatch gates.
    """

    def __init__(self, *, internet_search_max_backend_calls_per_turn: int = 10) -> None:
        super().__init__()
        self.sink: list[ToolCallRecord] = []
        self._cassette: Cassette | None = None
        self._cassette_mode: str = "off"
        self._fault_budget: dict[str, list[Fault]] = {}
        self._provider_calls: Callable[[], int] = lambda: 0
        self._internet_search_max_backend_calls_per_turn = (
            internet_search_max_backend_calls_per_turn
        )

    def set_provider_call_counter(self, counter: Callable[[], int]) -> None:
        """Supply "how many provider calls have completed this turn".

        Injected rather than held as a provider reference: the registry needs one
        integer to place a tool call in the turn's timeline, and taking the
        provider itself would couple dispatch to the capture wrapper.
        """
        self._provider_calls = counter

    def reset_sink(self) -> None:
        self.sink = []

    def configure_cassette(self, cassette: Cassette | None, mode: str = "off") -> None:
        self._cassette = cassette if mode != "off" else None
        self._cassette_mode = mode if cassette is not None else "off"

    def set_faults(self, faults: list[Fault]) -> None:
        """Arm scenario faults: the next `times` calls to each tool fail."""
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
                    if (
                        ctx.internet_search_backend_calls_this_turn + backend_calls
                        > self._internet_search_max_backend_calls_per_turn
                    ):
                        return tool_error(BUDGET_EXCEEDED_MESSAGE), "replay"
                    ctx.internet_search_backend_calls_this_turn += backend_calls
                return recorded.result, "replay"
            if mode == "strict":
                message = f"cassette miss for {name!r}; re-record with --cassette record"
                return json.dumps({"error": message}), "miss"
        backend_calls_before = ctx.internet_search_backend_calls_this_turn
        result = await super().dispatch(name, args, ctx)
        if cassette is not None and mode in ("record", "replay"):
            cassette.record(
                name,
                args if isinstance(args, dict) else {"_raw": args},
                result,
                internet_search_backend_calls=(
                    ctx.internet_search_backend_calls_this_turn - backend_calls_before
                    if name == "internet_search"
                    else None
                ),
            )
        return result, "live"

    async def dispatch(self, name: str, args: dict, ctx: MessageContext) -> str:
        start = time.monotonic()
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
                provider_calls_before=self._provider_calls(),
            )
        )
        return result
