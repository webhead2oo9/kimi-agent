from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from tools._common import tool_error
from tools.registry import (
    BudgetName,
    MessageContext,
    ToolBudgetSpec,
    ToolRegistry,
    format_untrusted_tool_result,
)
from trust.tiers import TrustTier
from usage.normalization import LLMUsageCall, normalize_usage
from xai.auth import XaiAuthError
from xai.credentials import AUTH_MODE_OAUTH, XaiCredentialResolver
from xai.responses import (
    XaiResponsesClient,
    XaiResponsesError,
    XaiSearchBudgetExceeded,
)

TOOL_NAME = "x_search"
MAX_QUERY_CHARS = 400
MAX_QUERY_WORDS = 50
MAX_HANDLES = 20
MAX_OUTPUT_CHARS = 24_000

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class XSearchConfig:
    client: XaiResponsesClient
    credential_resolver: XaiCredentialResolver
    model: str
    max_calls_per_turn: int = 10
    max_output_chars: int = MAX_OUTPUT_CHARS


@dataclass(frozen=True, slots=True)
class _ParsedSearch:
    answer: str
    citations: tuple[dict[str, Any], ...]
    x_search_calls: int | None

    @property
    def degraded(self) -> bool:
        return not self.citations and not (self.x_search_calls and self.x_search_calls > 0)


def init_x_search_tool(registry: ToolRegistry, config: XSearchConfig) -> None:
    async def handler(args: dict, ctx: MessageContext) -> str:
        try:
            query = _query(args)
            allowed = _handles(args.get("allowed_x_handles"), "allowed_x_handles")
            excluded = _handles(args.get("excluded_x_handles"), "excluded_x_handles")
            if allowed and excluded:
                raise ValueError("allowed_x_handles and excluded_x_handles cannot be used together")
            from_date = _date(args.get("from_date"), "from_date")
            to_date = _date(args.get("to_date"), "to_date")
            _validate_date_range(from_date, to_date)
            image_understanding = _boolean(
                args.get("enable_image_understanding", False),
                "enable_image_understanding",
            )
            video_understanding = _boolean(
                args.get("enable_video_understanding", False),
                "enable_video_understanding",
            )

            payload = _request_payload(
                config.model,
                query,
                allowed=allowed,
                excluded=excluded,
                from_date=from_date,
                to_date=to_date,
                image_understanding=image_understanding,
                video_understanding=video_understanding,
            )

            def consume_call() -> None:
                if not ctx.consume_budget(BudgetName.X_SEARCH_CALLS):
                    raise XaiSearchBudgetExceeded

            result = await config.client.create(payload, consume_call=consume_call)
            await _record_usage(ctx, result.payload, model=config.model)
            parsed = _parse_search(result.payload)

            if (
                parsed.degraded
                and result.credential_source == AUTH_MODE_OAUTH
                and ctx.budget_remaining(BudgetName.X_SEARCH_CALLS) > 0
                and (fallback := config.credential_resolver.api_key_fallback()) is not None
            ):
                log.info("x_search falling back to GROK_API_KEY after degraded OAuth response")
                try:
                    result = await config.client.create(
                        payload,
                        credential=fallback,
                        allow_auth_fallback=False,
                        consume_call=consume_call,
                    )
                except (XaiResponsesError, XaiSearchBudgetExceeded) as exc:
                    # The OAuth answer is already usable. A failed attempt to upgrade
                    # it must not turn a served result into a generic tool error.
                    log.warning("x_search fallback after a degraded response failed: %s", exc)
                else:
                    await _record_usage(ctx, result.payload, model=config.model)
                    parsed = _parse_search(result.payload)

            return _render_result(parsed, config.max_output_chars)
        except XaiSearchBudgetExceeded:
            return tool_error("X search call limit reached for this turn.")
        except (ValueError, XaiAuthError) as exc:
            return tool_error(str(exc))
        except XaiResponsesError as exc:
            log.warning("x_search request failed: %s", exc)
            if exc.status == 429:
                return tool_error("X search is rate limited. Try again shortly.")
            if exc.status in {401, 403}:
                return tool_error(
                    "The configured xAI credential cannot use X search. "
                    "Check its authentication mode and account entitlement."
                )
            return tool_error("X search failed. Try again shortly.")
        except Exception:
            log.exception("x_search failed")
            return tool_error("X search failed.")

    registry.register(
        name=TOOL_NAME,
        description=(
            "Search current posts on X with optional date, account, image, and video filters. "
            "Keep the search query to 50 words or fewer. "
            "Calls may take several minutes; wait for completion instead of retrying only "
            "because the search is slow. "
            "Returns a synthesized answer plus citations and explicitly marks responses where "
            "there is no evidence the live X index was searched."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Question or search query about current posts on X; must be 50 words "
                        "or fewer."
                    ),
                },
                "allowed_x_handles": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": MAX_HANDLES,
                    "description": "Search only these X handles, without or with @.",
                },
                "excluded_x_handles": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": MAX_HANDLES,
                    "description": "Exclude these X handles, without or with @.",
                },
                "from_date": {
                    "type": "string",
                    "description": "Earliest post date in YYYY-MM-DD format.",
                },
                "to_date": {
                    "type": "string",
                    "description": "Latest post date in YYYY-MM-DD format.",
                },
                "enable_image_understanding": {
                    "type": "boolean",
                    "description": "Allow the hosted search model to understand post images.",
                },
                "enable_video_understanding": {
                    "type": "boolean",
                    "description": "Allow the hosted search model to understand post videos.",
                },
            },
            "required": ["query"],
        },
        handler=handler,
        min_tier=TrustTier.MEMBER,
        searchable=True,
        category="Internet",
        untrusted=True,
        budget_specs=(ToolBudgetSpec(BudgetName.X_SEARCH_CALLS, config.max_calls_per_turn),),
    )


def _query(args: dict[str, Any]) -> str:
    raw = args.get("query")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("query is required")
    query = raw.strip()
    if len(query) > MAX_QUERY_CHARS:
        raise ValueError(f"query must be {MAX_QUERY_CHARS} characters or fewer")
    if len(query.split()) > MAX_QUERY_WORDS:
        raise ValueError(f"query must be {MAX_QUERY_WORDS} words or fewer")
    return query


def _handles(value: Any, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array")
    handles: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip().lstrip("@").strip():
            raise ValueError(f"{field} must contain non-empty handle strings")
        handle = item.strip().lstrip("@").strip()
        if handle not in handles:
            handles.append(handle)
    if len(handles) > MAX_HANDLES:
        raise ValueError(f"{field} supports at most {MAX_HANDLES} handles")
    return tuple(handles)


def _date(value: Any, field: str) -> str:
    if value is None or value == "":
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be YYYY-MM-DD")
    raw = value.strip()
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{field} must be YYYY-MM-DD") from exc
    if parsed.isoformat() != raw:
        raise ValueError(f"{field} must be YYYY-MM-DD")
    return raw


def _validate_date_range(from_date: str, to_date: str) -> None:
    parsed_from = date.fromisoformat(from_date) if from_date else None
    parsed_to = date.fromisoformat(to_date) if to_date else None
    if parsed_from and parsed_to and parsed_from > parsed_to:
        raise ValueError("from_date must not be after to_date")
    if parsed_from and parsed_from > datetime.now(UTC).date():
        raise ValueError("from_date must not be in the future")


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean")
    return value


def _request_payload(
    model: str,
    query: str,
    *,
    allowed: tuple[str, ...],
    excluded: tuple[str, ...],
    from_date: str,
    to_date: str,
    image_understanding: bool,
    video_understanding: bool,
) -> dict[str, Any]:
    tool: dict[str, Any] = {"type": "x_search"}
    if allowed:
        tool["allowed_x_handles"] = list(allowed)
    if excluded:
        tool["excluded_x_handles"] = list(excluded)
    if from_date:
        tool["from_date"] = from_date
    if to_date:
        tool["to_date"] = to_date
    if image_understanding:
        tool["enable_image_understanding"] = True
    if video_understanding:
        tool["enable_video_understanding"] = True
    return {
        "model": model,
        "input": [{"role": "user", "content": query}],
        "tools": [tool],
        "store": False,
    }


def _parse_search(payload: dict[str, Any]) -> _ParsedSearch:
    answer = str(payload.get("output_text") or "").strip()
    if not answer:
        parts: list[str] = []
        for item in payload.get("output", []) or []:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            for content in item.get("content", []) or []:
                if not isinstance(content, dict):
                    continue
                if content.get("type") in {"output_text", "text"}:
                    text = content.get("text")
                    if isinstance(text, str) and text.strip():
                        parts.append(text.strip())
        answer = "\n\n".join(parts)

    citations: list[dict[str, Any]] = []
    for raw in payload.get("citations", []) or []:
        citation = _normalize_citation(raw)
        if citation:
            citations.append(citation)
    for item in payload.get("output", []) or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content", []) or []:
            if not isinstance(content, dict):
                continue
            for annotation in content.get("annotations", []) or []:
                if isinstance(annotation, dict) and annotation.get("type") == "url_citation":
                    citation = _normalize_citation(annotation)
                    if citation:
                        citations.append(citation)
    deduped = tuple(
        {
            str(item.get("url") or item.get("title") or index): item
            for index, item in enumerate(citations)
        }.values()
    )
    usage = payload.get("usage")
    details = usage.get("server_side_tool_usage_details") if isinstance(usage, dict) else None
    raw_calls = details.get("x_search_calls") if isinstance(details, dict) else None
    reported_calls = (
        raw_calls if isinstance(raw_calls, int) and not isinstance(raw_calls, bool) else None
    )
    completed_calls = sum(
        1
        for item in payload.get("output", []) or []
        if isinstance(item, dict)
        and item.get("type") == "x_search_call"
        and item.get("status") == "completed"
    )
    calls = (
        max(reported_calls, completed_calls)
        if reported_calls is not None
        else completed_calls or None
    )
    return _ParsedSearch(answer=answer, citations=deduped, x_search_calls=calls)


def _normalize_citation(value: Any) -> dict[str, Any] | None:
    if isinstance(value, str) and value:
        return {"url": value}
    if not isinstance(value, dict):
        return None
    citation = {
        key: value[key]
        for key in ("url", "title", "start_index", "end_index")
        if value.get(key) is not None
    }
    return citation or None


async def _record_usage(
    ctx: MessageContext,
    payload: dict[str, Any],
    *,
    model: str,
) -> None:
    raw_usage = payload.get("usage")
    call = LLMUsageCall(
        model=model,
        role="x_search",
        usage=normalize_usage(raw_usage if isinstance(raw_usage, dict) else None),
        usage_present=isinstance(raw_usage, dict),
        upstream_provider="xai",
    )
    try:
        if ctx.record_usage_call is not None:
            await ctx.record_usage_call(call)
        elif ctx.usage_sink is not None:
            ctx.usage_sink.append(call)
    except Exception:
        log.warning("x_search usage recording failed", exc_info=True)


def _render_result(parsed: _ParsedSearch, max_chars: int) -> str:
    payload: dict[str, Any] = {
        "answer": parsed.answer,
        "citations": list(parsed.citations),
        "x_search_calls": parsed.x_search_calls,
        "degraded": parsed.degraded,
    }
    if parsed.degraded:
        payload["degraded_reason"] = "No citations or positive X-search call count were returned."
    rendered = format_untrusted_tool_result(json.dumps(payload))
    if len(rendered) <= max_chars:
        return rendered

    # Preserve as many citations as fit, then use the remaining budget for the
    # synthesized answer. A single unexpectedly large citation must not defeat
    # the configured tool-output cap.
    marker = "[truncated]" if parsed.answer else ""
    payload["answer"] = marker
    while payload["citations"]:
        rendered = format_untrusted_tool_result(json.dumps(payload))
        if len(rendered) <= max_chars:
            break
        payload["citations"].pop()

    rendered = format_untrusted_tool_result(json.dumps(payload))
    if len(rendered) > max_chars:
        # The fixed trust envelope cannot be represented as valid JSON under an
        # extremely small cap, so return the smallest valid form.
        return rendered

    low = 0
    high = len(parsed.answer)
    best = rendered
    while low <= high:
        midpoint = (low + high) // 2
        prefix = parsed.answer[:midpoint].rstrip()
        payload["answer"] = f"{prefix}\n{marker}" if prefix and marker else prefix or marker
        candidate = format_untrusted_tool_result(json.dumps(payload))
        if len(candidate) <= max_chars:
            best = candidate
            low = midpoint + 1
        else:
            high = midpoint - 1
    return best
