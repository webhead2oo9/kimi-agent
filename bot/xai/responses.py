from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import aiohttp

from branding import provider_identity
from xai.auth import XAI_API_BASE_URL, XaiAuthError
from xai.credentials import AUTH_MODE_API_KEY, AUTH_MODE_OAUTH, XaiCredential, XaiCredentialResolver

log = logging.getLogger(__name__)

_ENTITLEMENT_MARKERS = (
    "entitlement",
    "insufficient_scope",
    "insufficient scope",
    "subscription",
    "not entitled",
    "tier denied",
)


class XaiResponsesError(RuntimeError):
    def __init__(self, message: str, *, status: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.status_code = status
        self.code = code


class XaiSearchBudgetExceeded(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class XaiResponsesResult:
    payload: dict[str, Any]
    credential_source: str


class XaiResponsesClient:
    def __init__(
        self,
        credential_resolver: XaiCredentialResolver,
        *,
        timeout_seconds: float,
        max_retries: int = 2,
        user_agent: str,
    ) -> None:
        self._credentials = credential_resolver
        self._timeout_seconds = timeout_seconds
        self._max_retries = max(0, max_retries)
        self._user_agent = provider_identity(user_agent)

    async def create(
        self,
        payload: dict[str, Any],
        *,
        credential: XaiCredential | None = None,
        allow_auth_fallback: bool = True,
        consume_call: Callable[[], None] | None = None,
    ) -> XaiResponsesResult:
        try:
            active = credential or await self._credentials.primary()
        except XaiAuthError:
            raise

        refreshed = False
        api_fallback_used = active.source == AUTH_MODE_API_KEY
        while True:
            try:
                response = await self._post_with_retries(
                    payload,
                    active,
                    consume_call=consume_call,
                )
            except XaiResponsesError as exc:
                replacement = None
                if allow_auth_fallback and exc.status == 401 and active.source == AUTH_MODE_OAUTH:
                    if not refreshed:
                        refreshed = True
                        replacement = await self._credentials.after_unauthorized(active)
                    elif not api_fallback_used:
                        replacement = self._credentials.api_key_fallback()
                elif (
                    allow_auth_fallback
                    and active.source == AUTH_MODE_OAUTH
                    and not api_fallback_used
                    and _is_entitlement_error(exc)
                ):
                    replacement = self._credentials.api_key_fallback()
                if replacement is None:
                    raise
                api_fallback_used = replacement.source == AUTH_MODE_API_KEY
                if api_fallback_used:
                    log.info("xAI Responses request falling back from OAuth to GROK_API_KEY")
                active = replacement
                continue
            return XaiResponsesResult(response, active.source)

    async def _post_with_retries(
        self,
        payload: dict[str, Any],
        credential: XaiCredential,
        *,
        consume_call: Callable[[], None] | None,
    ) -> dict[str, Any]:
        timeout = aiohttp.ClientTimeout(total=self._timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
            for attempt in range(self._max_retries + 1):
                if consume_call is not None:
                    consume_call()
                try:
                    async with session.post(
                        f"{XAI_API_BASE_URL}/responses",
                        headers={
                            "Authorization": f"Bearer {credential.bearer}",
                            "Content-Type": "application/json",
                            "User-Agent": self._user_agent,
                        },
                        json=payload,
                    ) as response:
                        text = await response.text()
                        if 200 <= response.status < 300:
                            try:
                                parsed = json.loads(text)
                            except json.JSONDecodeError as exc:
                                raise XaiResponsesError(
                                    "xAI Responses returned invalid JSON",
                                    status=response.status,
                                ) from exc
                            if not isinstance(parsed, dict):
                                raise XaiResponsesError(
                                    "xAI Responses returned a non-object payload",
                                    status=response.status,
                                )
                            return parsed

                        error = _response_error(response.status, text)
                        if (
                            not _is_transient_status(response.status)
                            or attempt >= self._max_retries
                        ):
                            raise error
                        delay = _retry_after_seconds(response.headers.get("Retry-After"))
                except (aiohttp.ClientError, TimeoutError) as exc:
                    if attempt >= self._max_retries:
                        raise XaiResponsesError("xAI Responses request failed") from exc
                    delay = min(5.0, 1.5 * (attempt + 1))
                await asyncio.sleep(delay)
        raise XaiResponsesError("xAI Responses request did not complete")


def _response_error(status: int, text: str) -> XaiResponsesError:
    code = ""
    message = ""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            code = str(error.get("code") or "")
            message = str(error.get("message") or error.get("error") or "")
        elif isinstance(error, str):
            message = error
        code = code or str(payload.get("code") or "")
        message = message or str(payload.get("message") or "")
    detail = f": {message[:300]}" if message else ""
    return XaiResponsesError(
        f"xAI Responses request failed ({status}){detail}",
        status=status,
        code=code,
    )


def _is_entitlement_error(exc: XaiResponsesError) -> bool:
    if exc.status != 403:
        return False
    text = f"{exc.code} {exc}".lower()
    return any(marker in text for marker in _ENTITLEMENT_MARKERS)


def _is_transient_status(status: int) -> bool:
    return status in {408, 425, 429} or 500 <= status <= 599


def _retry_after_seconds(value: str | None) -> float:
    if not value:
        return 1.5
    try:
        seconds = float(value)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except TypeError, ValueError, OverflowError:
            return 1.5
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        seconds = max(0.0, (parsed - datetime.now(UTC)).total_seconds())
    return min(30.0, max(0.0, seconds))
