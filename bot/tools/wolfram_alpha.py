from __future__ import annotations

import asyncio
import json
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import aiohttp

from storage.usage import PaidUsageCall
from tools._common import get_string, tool_error
from tools.registry import BudgetName, MessageContext, ToolBudgetSpec, ToolRegistry
from trust.tiers import TrustTier

log = logging.getLogger(__name__)

TOOL_NAME = "wolfram_alpha"
API_URL = "https://www.wolframalpha.com/api/v1/llm-api"
MAX_INPUT_CHARS = 1_000
MAX_INPUT_WORDS = 100
_TRANSIENT_STATUSES = {408, 425, 429, 500, 502, 503, 504}
_FORMATTING_NOTE = (
    "Discord does not render LaTeX. Present math using readable Unicode or plain text, "
    "for example `∫₀^π x² sin(x) dx = π² − 4`. Do not use `\\(...\\)`, "
    "`\\[...\\]`, or `$...$` delimiters."
)
_UNIT_SYSTEMS = {"metric", "nonmetric"}


class WolframAlphaProviderError(RuntimeError):
    """A safe, model-facing failure from the Wolfram|Alpha provider."""


@dataclass(frozen=True)
class WolframAlphaResponse:
    status: int
    text: str


WolframAlphaRequest = Callable[..., Awaitable[WolframAlphaResponse]]


@dataclass(frozen=True)
class WolframAlphaConfig:
    client: WolframAlphaClient
    max_calls_per_turn: int = 3
    max_output_chars: int = 6_800
    timeout_seconds: float = 30.0
    call_cost_usd: float | None = None


class WolframAlphaClient:
    def __init__(
        self,
        app_id: str,
        *,
        request: WolframAlphaRequest | None = None,
    ) -> None:
        self._app_id = app_id
        self._request = request or request_llm_result

    async def query(
        self,
        input_text: str,
        *,
        units: str | None,
        max_chars: int,
        timeout_seconds: float,
    ) -> str:
        response = await self._request(
            app_id=self._app_id,
            input_text=input_text,
            units=units,
            max_chars=max_chars,
            timeout_seconds=timeout_seconds,
        )
        if response.status == 200:
            result = response.text.strip()
            if not result:
                raise WolframAlphaProviderError("Wolfram|Alpha returned an empty response.")
            return _truncate(result, max_chars)
        if response.status in {401, 403}:
            raise WolframAlphaProviderError("Wolfram|Alpha credentials were rejected.")
        if response.status == 429:
            raise WolframAlphaProviderError("Wolfram|Alpha rate limit or quota reached.")
        if response.status == 400:
            raise WolframAlphaProviderError("Wolfram|Alpha rejected the query.")
        if response.status == 501:
            suggestion = response.text.strip()
            if suggestion:
                return _truncate(
                    "Wolfram|Alpha could not interpret the query. Its suggested inputs are:\n"
                    f"{suggestion}",
                    max_chars,
                )
            raise WolframAlphaProviderError("Wolfram|Alpha could not interpret the query.")
        if response.status in _TRANSIENT_STATUSES:
            raise WolframAlphaProviderError("Wolfram|Alpha is temporarily unavailable.")
        raise WolframAlphaProviderError(f"Wolfram|Alpha returned HTTP {response.status}.")


def init_wolfram_alpha_tool(registry: ToolRegistry, config: WolframAlphaConfig) -> None:
    async def handler(args: dict, ctx: MessageContext) -> str:
        try:
            input_text = _input_text(args)
            units = _units(args.get("units"))
            if not ctx.consume_budget(BudgetName.WOLFRAM_ALPHA_CALLS):
                return tool_error("Wolfram|Alpha call limit reached for this turn.")
            try:
                async with asyncio.timeout(config.timeout_seconds):
                    result = await config.client.query(
                        input_text,
                        units=units,
                        max_chars=config.max_output_chars,
                        timeout_seconds=config.timeout_seconds,
                    )
            finally:
                # The configured estimate is per logical request, including a
                # possible internal retry, and applies even when the provider
                # returns an error after accepting the request.
                await _record_cost(ctx, config.call_cost_usd)
            return json.dumps(
                {"query": input_text, "result": result, "formatting": _FORMATTING_NOTE}
            )
        except TimeoutError:
            return tool_error("Wolfram|Alpha timed out.")
        except (ValueError, WolframAlphaProviderError) as exc:
            return tool_error(str(exc))
        except Exception:
            # Do not attach the exception: HTTP client errors can include the
            # user query and full request URL.
            log.error("Wolfram|Alpha tool failed with an unexpected error")
            return tool_error("Wolfram|Alpha failed.")

    registry.register(
        name=TOOL_NAME,
        description=(
            "Compute exact answers with Wolfram|Alpha for mathematics, science, units, "
            "dates, statistics, and factual data. Use a concise single-line English query."
        ),
        parameters={
            "type": "object",
            "properties": {
                "input": {
                    "type": "string",
                    "maxLength": MAX_INPUT_CHARS,
                    "description": (
                        "A concise, single-line English Wolfram|Alpha query, such as "
                        "'integrate x^2 sin(x) from 0 to pi'."
                    ),
                },
                "units": {
                    "type": "string",
                    "enum": sorted(_UNIT_SYSTEMS),
                    "description": "Optional measurement system for the result.",
                },
            },
            "required": ["input"],
        },
        handler=handler,
        min_tier=TrustTier.MEMBER,
        searchable=True,
        category="Computation",
        untrusted=True,
        budget_specs=(ToolBudgetSpec(BudgetName.WOLFRAM_ALPHA_CALLS, config.max_calls_per_turn),),
    )


async def request_llm_result(
    *,
    app_id: str,
    input_text: str,
    units: str | None,
    max_chars: int,
    timeout_seconds: float,
) -> WolframAlphaResponse:
    """Call the LLM API with one bounded retry and a bounded response body."""

    params: dict[str, str | int] = {"input": input_text, "maxchars": max_chars}
    if units is not None:
        params["units"] = units
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
                async with session.get(
                    API_URL,
                    params=params,
                    headers={
                        "Accept": "text/plain",
                        "Authorization": f"Bearer {app_id}",
                    },
                ) as response:
                    body = await _bounded_text(response, max_chars)
                    result = WolframAlphaResponse(status=response.status, text=body)
                    if response.status not in _TRANSIENT_STATUSES or attempt == 1:
                        return result
        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt == 1:
                break
        await asyncio.sleep(1.0)
    raise WolframAlphaProviderError("Wolfram|Alpha request failed.") from last_error


async def _bounded_text(response: aiohttp.ClientResponse, max_chars: int) -> str:
    max_bytes = max_chars * 4 + 1_024
    body = bytearray()
    truncated = False
    async for chunk in response.content.iter_chunked(8_192):
        remaining = max_bytes - len(body)
        if remaining <= 0:
            truncated = True
            break
        body.extend(chunk[:remaining])
        if len(chunk) > remaining:
            truncated = True
            break
    text = body.decode(response.charset or "utf-8", errors="replace")
    return _truncate(text, max_chars, force=truncated)


def _input_text(args: dict) -> str:
    value = get_string(args, "input", required=True, max_chars=MAX_INPUT_CHARS)
    if "\n" in value or "\r" in value:
        raise ValueError("input must be a single-line string")
    if len(value.split()) > MAX_INPUT_WORDS:
        raise ValueError(f"input must be {MAX_INPUT_WORDS} words or fewer")
    return value


def _units(value: object) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or value not in _UNIT_SYSTEMS:
        raise ValueError("units must be one of: metric, nonmetric")
    return value


async def _record_cost(ctx: MessageContext, cost: float | None) -> None:
    if cost is None:
        return
    if not math.isfinite(cost) or cost < 0:
        log.warning("Ignoring invalid Wolfram|Alpha call cost: %r", cost)
        return
    await ctx.record_paid_usage(
        PaidUsageCall(tool_name=TOOL_NAME, provider="wolfram_alpha", cost_usd=cost)
    )


def _truncate(value: str, limit: int, *, force: bool = False) -> str:
    if not force and len(value) <= limit:
        return value
    suffix = "\n[truncated]"
    return f"{value[: max(0, limit - len(suffix))].rstrip()}{suffix}"
