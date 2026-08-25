from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TextIO

from utils.format import now_iso

if TYPE_CHECKING:
    from tools.registry import MessageContext

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2

type ContentMode = Literal["metadata", "redacted", "full"]

CONTENT_MODES: frozenset[str] = frozenset({"metadata", "redacted", "full"})
_SENSITIVE_KEY_TERMS = frozenset(
    {
        "api_key",
        "authorization",
        "cookie",
        "credential",
        "password",
        "passwd",
        "private_key",
        "secret",
        "token",
    }
)


def new_turn_id() -> str:
    """Return a collision-resistant id shared by one logical response turn."""
    return uuid.uuid4().hex


def _now_iso() -> str:
    return now_iso("milliseconds")


def _truncate(value: str, max_bytes: int) -> tuple[str, bool]:
    # Clamp a misconfigured negative cap to 0 so we truncate to empty rather than
    # `encoded[:negative]` keeping len-|n| bytes from the head (a content leak).
    max_bytes = max(0, max_bytes)
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value, False
    return encoded[:max_bytes].decode("utf-8", errors="ignore"), True


def _derive_ok_error(result: str) -> tuple[bool, str | None]:
    try:
        parsed = json.loads(result)
    except ValueError, TypeError:
        return True, None
    if isinstance(parsed, dict) and "error" in parsed:
        return False, str(parsed["error"])
    return True, None


def _require_content_mode(value: str) -> ContentMode:
    match value:
        case "metadata" | "redacted" | "full":
            return value
        case _:
            choices = ", ".join(sorted(CONTENT_MODES))
            raise ValueError(
                f"Invalid event-log content mode {value!r}; expected one of: {choices}"
            )


def _secret_values(values: tuple[str, ...]) -> tuple[str, ...]:
    # Longest first: if one secret contains another, replacing the short one first
    # would leave the rest of the long one sitting in the log.
    return tuple(sorted({value for value in values if value}, key=len, reverse=True))


def _redact_text(value: str, secret_values: tuple[str, ...]) -> str:
    for secret in secret_values:
        value = value.replace(secret, "[REDACTED]")
    return value


def _is_sensitive_key(value: object) -> bool:
    # Split camelCase first, then fold every other separator to "_", so
    # Authorization, refresh-token, and clientSecret all end up as the same
    # kind of underscore-separated word list.
    key = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(value))
    normalized = re.sub(r"[^a-z0-9]+", "_", key.casefold()).strip("_")
    # Pad both ends so a term sitting first or last still reads as a complete
    # segment. That lets one substring test catch a bare `token`, a prefixed
    # `secret_access_key`, and a suffixed `password_confirmation` alike, while
    # still skipping words that merely start with one (`tokenizer`, `secretary`).
    padded = f"_{normalized}_"
    return any(f"_{term}_" in padded for term in _SENSITIVE_KEY_TERMS)


def _redact_value(value: Any, secret_values: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        return {
            key: ("[REDACTED]" if _is_sensitive_key(key) else _redact_value(item, secret_values))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(item, secret_values) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(item, secret_values) for item in value)
    if isinstance(value, str):
        return _redact_text(value, secret_values)
    return value


def _redact_result(value: str, secret_values: tuple[str, ...]) -> str:
    try:
        parsed = json.loads(value)
    except ValueError, TypeError:
        return _redact_text(value, secret_values)
    return json.dumps(_redact_value(parsed, secret_values), ensure_ascii=False, default=str)


def build_tool_call_event(
    *,
    ts: str,
    duration_ms: int,
    turn_id: str,
    iteration: int,
    ctx: MessageContext,
    tool: str,
    args: dict[str, Any],
    result: str,
    max_field_bytes: int,
    content_mode: ContentMode,
    secret_values: tuple[str, ...] = (),
    model: str = "",
) -> dict[str, Any]:
    content_mode = _require_content_mode(content_mode)
    secret_values = _secret_values(secret_values)
    ok, error = _derive_ok_error(result)
    args_field: Any

    if content_mode == "metadata":
        args_field = None
        args_truncated = False
        result_field = None
        result_truncated = False
        error_field = None
    else:
        if content_mode == "redacted":
            args = _redact_value(args, secret_values)
            result = _redact_result(result, secret_values)
            # Re-derive so the logged error text matches the redacted result. `ok`
            # came from the original above and does not change under redaction.
            _, error = _derive_ok_error(result)

        args_serialized = json.dumps(args, ensure_ascii=False, default=str)
        if len(args_serialized.encode("utf-8")) <= max_field_bytes:
            args_field = args
            args_truncated = False
        else:
            args_field, _ = _truncate(args_serialized, max_field_bytes)
            args_truncated = True

        result_field, result_truncated = _truncate(result, max_field_bytes)
        error_field = error

    return {
        "v": SCHEMA_VERSION,
        "type": "tool_call",
        "content_mode": content_mode,
        "ts": ts,
        "duration_ms": duration_ms,
        "turn_id": turn_id,
        "iteration": iteration,
        "user_id": ctx.user_id,
        "user_name": ctx.user_name,
        "channel_id": ctx.channel_id,
        "thread_id": ctx.thread_id,
        "trust_tier": ctx.trust_tier.value,
        "tool": tool,
        "model": model,
        "args": args_field,
        "args_truncated": args_truncated,
        "result": result_field,
        "result_truncated": result_truncated,
        "ok": ok,
        "error": error_field,
    }


def _snapshot_message(
    message: dict[str, Any],
    max_field_bytes: int,
    *,
    secret_values: tuple[str, ...] = (),
) -> dict[str, Any]:
    text = _redact_text(str(message.get("text", "")), secret_values)
    text, truncated = _truncate(text, max_field_bytes)
    return {
        "role": message.get("role", ""),
        "section": message.get("section", ""),
        "text": text,
        "truncated": truncated,
    }


def build_turn_event(
    *,
    ts: str,
    turn_id: str,
    ctx: MessageContext,
    trigger: str,
    tool_count: int,
    duration_ms: int,
    request_snapshot: list[dict[str, Any]],
    response_text: str,
    max_field_bytes: int = 8192,
    content_mode: ContentMode,
    secret_values: tuple[str, ...] = (),
    model: str = "",
    models: list[str] | None = None,
    primary_model: str = "",
    llm_calls: int = 0,
    usage: dict[str, int] | None = None,
) -> dict[str, Any]:
    content_mode = _require_content_mode(content_mode)
    secret_values = _secret_values(secret_values)
    if content_mode == "metadata":
        request_field = None
        response_field = None
    else:
        redaction_values = secret_values if content_mode == "redacted" else ()
        request_field = [
            _snapshot_message(message, max_field_bytes, secret_values=redaction_values)
            for message in request_snapshot
        ]
        response_field = _snapshot_message(
            {"role": "assistant", "section": "response", "text": response_text},
            max_field_bytes,
            secret_values=redaction_values,
        )

    return {
        "v": SCHEMA_VERSION,
        "type": "turn",
        "content_mode": content_mode,
        "ts": ts,
        "turn_id": turn_id,
        "user_name": ctx.user_name,
        "channel_id": ctx.channel_id,
        "trigger": trigger,
        "tool_count": tool_count,
        "duration_ms": duration_ms,
        # Model attribution: `model` served the final provider call, `models` is every
        # model that served a call this turn (first-use order; length > 1 means a
        # mid-turn failover), `primary_model` is the configured preferred model so a
        # reader can flag fallback-served turns without knowing config/models.yaml.
        "model": model,
        "models": list(models or []),
        "primary_model": primary_model,
        "llm_calls": llm_calls,
        "usage": dict(usage or {}),
        "request": request_field,
        "response": response_field,
    }


def build_compaction_event(
    *,
    ts: str,
    turn_id: str,
    iteration: int,
    ctx: MessageContext,
    reason: str,
    before_messages: int,
    after_messages: int,
    kept_recent_iterations: int,
    note_chars: int,
    elided_tool_results: int,
    hard_truncated_tool_results: int,
    content_mode: ContentMode,
) -> dict[str, Any]:
    content_mode = _require_content_mode(content_mode)
    return {
        "v": SCHEMA_VERSION,
        "type": "compaction",
        "content_mode": content_mode,
        "ts": ts,
        "turn_id": turn_id,
        "iteration": iteration,
        "user_id": ctx.user_id,
        "user_name": ctx.user_name,
        "channel_id": ctx.channel_id,
        "thread_id": ctx.thread_id,
        "trust_tier": ctx.trust_tier.value,
        "reason": reason,
        "before_messages": before_messages,
        "after_messages": after_messages,
        "kept_recent_iterations": kept_recent_iterations,
        "note_chars": note_chars,
        "elided_tool_results": elided_tool_results,
        "hard_truncated_tool_results": hard_truncated_tool_results,
    }


def build_moderation_event(
    *,
    ts: str,
    direction: str,
    matched_categories: list[str],
    category_scores: dict[str, float],
    user_id: str,
    channel_id: str,
    thread_id: str | None,
    trust_tier: str,
    content_mode: ContentMode,
) -> dict[str, Any]:
    content_mode = _require_content_mode(content_mode)
    return {
        "v": SCHEMA_VERSION,
        "type": "moderation",
        "content_mode": content_mode,
        "ts": ts,
        "direction": direction,
        "matched_categories": matched_categories,
        "category_scores": category_scores,
        "user_id": user_id,
        "channel_id": channel_id,
        "thread_id": thread_id,
        "trust_tier": trust_tier,
    }


_DEFAULT_MAX_FILE_BYTES = 50 * 1024 * 1024
_SENTINEL: dict[str, Any] = {"__stop__": True}


class EventWriter:
    def __init__(
        self,
        path: Path,
        max_field_bytes: int,
        max_file_bytes: int,
        *,
        content_mode: ContentMode,
        secret_values: tuple[str, ...] = (),
    ) -> None:
        self.path = path
        self.max_field_bytes = max_field_bytes
        self.content_mode = _require_content_mode(content_mode)
        self.secret_values = _secret_values(secret_values)
        self._max_file_bytes = max_file_bytes
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=10000)
        self._task: asyncio.Task[None] | None = None
        self._file: TextIO | None = None
        self._bytes_written = 0
        self._warned_full = False

    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._open()
        self._task = asyncio.create_task(self._run())

    def _open(self) -> None:
        # Open by descriptor so the mode is set as the file is created, not in a
        # second step a reader could slip through.
        fd = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            if os.name == "posix":
                # os.open's mode is masked by umask and ignored outright for a file
                # that already exists, so tighten the descriptor we actually hold.
                os.fchmod(fd, 0o600)
            self._file = os.fdopen(fd, "a", encoding="utf-8")
        except BaseException:
            with contextlib.suppress(OSError):
                os.close(fd)
            raise
        self._bytes_written = self.path.stat().st_size

    def enqueue(self, event: dict[str, Any]) -> None:
        if self._task is not None and self._task.done():
            # The writer died (e.g. disk error in _run); don't fill the bounded
            # queue with events nothing will ever consume.
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            if not self._warned_full:
                log.warning("Tool event queue full; dropping events")
                self._warned_full = True

    async def _run(self) -> None:
        while True:
            event = await self._queue.get()
            if event is _SENTINEL:
                return
            try:
                self._write(event)
            except OSError:
                log.warning("Failed to write tool event; stopping writer", exc_info=True)
                return

    def _write(self, event: dict[str, Any]) -> None:
        if self._file is None:
            raise RuntimeError("Event writer is not open")
        line = json.dumps(event, ensure_ascii=False, default=str) + "\n"
        data = line.encode("utf-8")
        if self._bytes_written > 0 and self._bytes_written + len(data) > self._max_file_bytes:
            self._rotate()
        self._file.write(line)
        self._file.flush()
        self._bytes_written += len(data)

    def _rotate(self) -> None:
        if self._file is None:
            raise RuntimeError("Event writer is not open")
        self._file.close()
        backup = self.path.with_name(self.path.name + ".1")
        if backup.exists():
            backup.unlink()
        self.path.rename(backup)
        self._open()

    async def stop(self) -> None:
        if self._task is not None:
            if not self._task.done():
                # Never block shutdown on a full queue: if the consumer died
                # with a backlog, an awaited put(_SENTINEL) would hang forever.
                try:
                    self._queue.put_nowait(_SENTINEL)
                except asyncio.QueueFull:
                    self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._file is not None:
            self._file.close()
            self._file = None


_writer: EventWriter | None = None


def start_event_writer(
    path: str,
    max_field_bytes: int,
    *,
    content_mode: ContentMode,
    secret_values: tuple[str, ...] = (),
    max_file_bytes: int = _DEFAULT_MAX_FILE_BYTES,
) -> None:
    global _writer
    if _writer is not None:
        return
    writer = EventWriter(
        Path(path),
        max_field_bytes,
        max_file_bytes,
        content_mode=content_mode,
        secret_values=secret_values,
    )
    try:
        writer.start()
    except OSError:
        log.warning("Could not open tool event log at %s; logging disabled", path, exc_info=True)
        return
    _writer = writer


async def stop_event_writer() -> None:
    global _writer
    if _writer is not None:
        await _writer.stop()
        _writer = None


def emit_tool_call(
    *,
    turn_id: str,
    iteration: int,
    ctx: MessageContext,
    tool: str,
    args: dict[str, Any],
    result: str,
    duration_ms: int,
    model: str = "",
) -> None:
    if _writer is None:
        return
    _writer.enqueue(
        build_tool_call_event(
            ts=_now_iso(),
            duration_ms=duration_ms,
            turn_id=turn_id,
            iteration=iteration,
            ctx=ctx,
            tool=tool,
            args=args,
            result=result,
            max_field_bytes=_writer.max_field_bytes,
            content_mode=_writer.content_mode,
            secret_values=_writer.secret_values,
            model=model,
        )
    )


def emit_turn(
    *,
    turn_id: str,
    ctx: MessageContext,
    trigger: str,
    tool_count: int,
    duration_ms: int,
    request_snapshot: list[dict[str, Any]],
    response_text: str,
    model: str = "",
    models: list[str] | None = None,
    primary_model: str = "",
    llm_calls: int = 0,
    usage: dict[str, int] | None = None,
) -> None:
    if _writer is None:
        return
    _writer.enqueue(
        build_turn_event(
            ts=_now_iso(),
            turn_id=turn_id,
            ctx=ctx,
            trigger=trigger,
            tool_count=tool_count,
            duration_ms=duration_ms,
            request_snapshot=request_snapshot,
            response_text=response_text,
            max_field_bytes=_writer.max_field_bytes,
            content_mode=_writer.content_mode,
            secret_values=_writer.secret_values,
            model=model,
            models=models,
            primary_model=primary_model,
            llm_calls=llm_calls,
            usage=usage,
        )
    )


def emit_compaction(
    *,
    turn_id: str,
    iteration: int,
    ctx: MessageContext,
    reason: str,
    before_messages: int,
    after_messages: int,
    kept_recent_iterations: int,
    note_chars: int,
    elided_tool_results: int,
    hard_truncated_tool_results: int,
) -> None:
    if _writer is None:
        return
    _writer.enqueue(
        build_compaction_event(
            ts=_now_iso(),
            turn_id=turn_id,
            iteration=iteration,
            ctx=ctx,
            reason=reason,
            before_messages=before_messages,
            after_messages=after_messages,
            kept_recent_iterations=kept_recent_iterations,
            note_chars=note_chars,
            elided_tool_results=elided_tool_results,
            hard_truncated_tool_results=hard_truncated_tool_results,
            content_mode=_writer.content_mode,
        )
    )


def emit_moderation(
    *,
    direction: str,
    matched_categories: list[str],
    category_scores: dict[str, float],
    user_id: str,
    channel_id: str,
    thread_id: str | None,
    trust_tier: str,
) -> None:
    if _writer is None:
        return
    _writer.enqueue(
        build_moderation_event(
            ts=_now_iso(),
            direction=direction,
            matched_categories=matched_categories,
            category_scores=category_scores,
            user_id=user_id,
            channel_id=channel_id,
            thread_id=thread_id,
            trust_tier=trust_tier,
            content_mode=_writer.content_mode,
        )
    )
