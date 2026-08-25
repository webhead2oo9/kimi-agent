from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import aiohttp


@dataclass
class CodexSessionState:
    last_request_input: list[dict[str, Any]] = field(default_factory=list)
    last_request_signature: str = ""
    last_response_id: str | None = None
    last_response_output_items: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class PreparedCodexWebSocketRequest:
    request: dict[str, Any]
    full_input: list[dict[str, Any]]
    request_signature: str
    used_previous_response_id: bool


@dataclass
class CodexSession:
    key: str
    ws: aiohttp.ClientWebSocketResponse | None = None
    client_session: aiohttp.ClientSession | None = None
    state: CodexSessionState = field(default_factory=CodexSessionState)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    idle_task: asyncio.Task[None] | None = None
    last_used_at: float = 0.0
    connection_session_id: str = ""
    reconnect_attempts: int = 0
