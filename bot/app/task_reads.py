"""Bounded recovery for application-owned reads before task actions begin."""

from __future__ import annotations

import asyncio
import errno
import math
import socket
from collections.abc import Awaitable, Callable
from email.utils import parsedate_to_datetime
import time

import aiohttp
import discord

from tools.downloads import FetchUrlError

RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
RETRY_DELAYS = (1.0, 4.0)
READ_TIMEOUT_SECONDS = 60.0


class TaskReadError(ValueError):
    def __init__(
        self,
        cause: Exception,
        *,
        retryable: bool,
        status: int | None = None,
        retry_after: float = 0,
    ) -> None:
        super().__init__(str(cause) or type(cause).__name__)
        self.cause = cause
        self.retryable = retryable
        self.status = status
        self.retry_after = retry_after


def _retry_after(value: str | None) -> float:
    if value is None:
        return 0
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = parsedate_to_datetime(value).timestamp() - time.time()
        except ValueError, TypeError, OverflowError:
            return 0
    return max(0, seconds) if math.isfinite(seconds) else 0


def read_error(exc: Exception) -> TaskReadError:
    """Use types and preserved causes, never human-readable error strings."""
    if isinstance(exc, TaskReadError):
        return exc
    if isinstance(exc, (discord.HTTPException, aiohttp.ClientResponseError, FetchUrlError)):
        status = exc.status
        if status is not None:
            headers = (
                getattr(exc.response, "headers", {})
                if isinstance(exc, discord.HTTPException)
                else exc.headers
                if isinstance(exc, aiohttp.ClientResponseError)
                else None
            )
            value = (
                exc.retry_after
                if isinstance(exc, FetchUrlError)
                else (headers or {}).get("Retry-After")
            )
            return TaskReadError(
                exc,
                retryable=status in RETRY_STATUSES,
                status=status,
                retry_after=_retry_after(value),
            )
    if isinstance(exc, (aiohttp.ClientSSLError, aiohttp.InvalidURL)):
        return TaskReadError(exc, retryable=False)
    # A wrapped permanent DNS or policy error must not become a generic connection retry.
    if isinstance(exc.__cause__, Exception):
        return read_error(exc.__cause__)
    retryable = isinstance(
        exc,
        (TimeoutError, ConnectionError, aiohttp.ClientConnectionError, aiohttp.ClientPayloadError),
    )
    if isinstance(exc, socket.gaierror):
        retryable = exc.errno == socket.EAI_AGAIN
    elif isinstance(exc, OSError) and exc.errno in {
        errno.ECONNRESET,
        errno.ECONNREFUSED,
        errno.ECONNABORTED,
        errno.ETIMEDOUT,
        errno.ENETUNREACH,
        errno.EHOSTUNREACH,
    }:
        retryable = True
    return TaskReadError(exc, retryable=retryable)


async def retry_read[T](
    operation: Callable[[], Awaitable[T]], *, timeout_seconds: float = READ_TIMEOUT_SECONDS
) -> T:
    """Three total attempts within one deadline, including backoff and Retry-After."""
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    try:
        async with asyncio.timeout_at(deadline):
            for attempt in range(len(RETRY_DELAYS) + 1):
                try:
                    return await operation()
                except Exception as exc:
                    failure = read_error(exc)
                    if not failure.retryable or attempt == len(RETRY_DELAYS):
                        raise failure from exc
                    delay = max(RETRY_DELAYS[attempt], failure.retry_after)
                    if delay >= deadline - asyncio.get_running_loop().time():
                        raise failure from exc
                    await asyncio.sleep(delay)
    except TimeoutError as exc:
        raise TaskReadError(exc, retryable=True) from exc
    raise AssertionError("Read attempts exhausted without an outcome")
