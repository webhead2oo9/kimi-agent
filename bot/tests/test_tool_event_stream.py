from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent.context import ConversationContext
from agent.core import ConversationRunRequest, run_conversation
from config.settings import Settings
from observability import events as ev
from providers.base import LLMProvider
from providers.types import (
    ContentPart,
    ConversationMessage,
    ProviderRequest,
    ProviderResponse,
    ToolCall,
)
from tools.registry import MessageContext, ToolRegistry
from trust.tiers import TrustTier
import contextlib


@pytest.fixture(autouse=True)
def _reset_event_writer():
    asyncio.run(ev.stop_event_writer())
    yield
    asyncio.run(ev.stop_event_writer())


def _read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_new_turn_id_is_full_hex() -> None:
    tid = ev.new_turn_id()
    assert isinstance(tid, str)
    assert len(tid) == 32
    int(tid, 16)  # raises if not hex


def test_schema_is_content_mode_version() -> None:
    assert ev.SCHEMA_VERSION == 2


def test_now_iso_is_utc_zulu_millis() -> None:
    ts = ev._now_iso()
    assert ts.endswith("Z")
    assert "T" in ts and "+" not in ts


def test_truncate_under_cap_is_unchanged() -> None:
    value, truncated = ev._truncate("hello", 8192)
    assert value == "hello"
    assert truncated is False


def test_truncate_over_cap_flags_and_shortens() -> None:
    value, truncated = ev._truncate("x" * 100, 10)
    assert truncated is True
    assert len(value.encode("utf-8")) <= 10


def test_truncate_negative_cap_keeps_nothing() -> None:
    # A misconfigured negative cap must truncate to empty, not head-keep
    # len-|n| bytes (which would leak field content).
    assert ev._truncate("xxxxxyyyyy", -3) == ("", True)
    assert ev._truncate("hello", -1) == ("", True)


def test_truncate_zero_cap_keeps_nothing() -> None:
    assert ev._truncate("hello", 0) == ("", True)


def test_settings_reject_nonpositive_script_caps() -> None:
    for field in (
        "script_default_timeout",
        "script_max_timeout",
        "script_max_concurrency",
        "script_output_max_chars",
        "script_output_max_files",
        "script_output_max_file_bytes",
        "script_output_max_scan_entries",
        "script_sandbox_memory_max_mb",
        "script_sandbox_cpu_seconds",
        "script_sandbox_max_file_bytes",
        "script_sandbox_max_open_files",
        "script_sandbox_max_processes",
        "script_sandbox_tmpfs_max_mb",
    ):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **{field: 0})  # type: ignore[call-arg, arg-type]
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **{field: -1})  # type: ignore[call-arg, arg-type]


def test_settings_field_bytes_allows_zero_rejects_negative() -> None:
    ok = Settings(_env_file=None, tool_event_log_max_field_bytes=0)  # type: ignore[call-arg]
    assert ok.tool_event_log_max_field_bytes == 0
    with pytest.raises(ValidationError):
        Settings(_env_file=None, tool_event_log_max_field_bytes=-1)  # type: ignore[call-arg]


def test_settings_content_mode_defaults_safe_and_validates_choices() -> None:
    assert Settings(_env_file=None).tool_event_log_content_mode == "metadata"  # type: ignore[call-arg]
    assert (
        Settings.model_validate(
            {"_env_file": None, "tool_event_log_content_mode": " REDACTED "}
        ).tool_event_log_content_mode
        == "redacted"
    )
    with pytest.raises(ValidationError):
        Settings.model_validate({"_env_file": None, "tool_event_log_content_mode": "unsafe"})


def test_derive_ok_error_on_error_object() -> None:
    ok, error = ev._derive_ok_error('{"error": "Tool execution failed."}')
    assert ok is False
    assert error == "Tool execution failed."


def test_derive_ok_error_on_success_object() -> None:
    ok, error = ev._derive_ok_error('{"ok": true, "value": 1}')
    assert ok is True
    assert error is None


def test_derive_ok_error_on_plain_string() -> None:
    ok, error = ev._derive_ok_error("just some text")
    assert ok is True
    assert error is None


def _ctx() -> MessageContext:
    return MessageContext(
        user_id="123",
        user_name="webhead",
        guild_id="g1",
        channel_id="chan1",
        thread_id=None,
        trust_tier=TrustTier.STAFF,
        context_key="g1:chan1:main",
    )


def test_build_tool_call_event_shape() -> None:
    e = ev.build_tool_call_event(
        ts="2026-05-31T19:04:22.118Z",
        duration_ms=412,
        turn_id="abc123",
        iteration=2,
        ctx=_ctx(),
        tool="edit_file",
        args={"path": "notes.md"},
        result='{"ok": true}',
        max_field_bytes=8192,
        content_mode="full",
        model="glm-4.7",
    )
    assert e["v"] == ev.SCHEMA_VERSION
    assert e["type"] == "tool_call"
    assert e["content_mode"] == "full"
    assert e["ts"] == "2026-05-31T19:04:22.118Z"
    assert e["duration_ms"] == 412
    assert e["turn_id"] == "abc123"
    assert e["iteration"] == 2
    assert e["user_name"] == "webhead"
    assert e["channel_id"] == "chan1"
    assert e["thread_id"] is None
    assert e["trust_tier"] == "staff"
    assert e["tool"] == "edit_file"
    assert e["model"] == "glm-4.7"
    assert e["args"] == {"path": "notes.md"}
    assert e["args_truncated"] is False
    assert e["result"] == '{"ok": true}'
    assert e["result_truncated"] is False
    assert e["ok"] is True
    assert e["error"] is None


def test_build_tool_call_event_marks_failure() -> None:
    e = ev.build_tool_call_event(
        ts="t",
        duration_ms=0,
        turn_id="t",
        iteration=0,
        ctx=_ctx(),
        tool="generate_image",
        args={},
        result='{"error": "boom"}',
        max_field_bytes=8192,
        content_mode="full",
    )
    assert e["ok"] is False
    assert e["error"] == "boom"


def test_build_tool_call_event_truncates_result_and_oversize_args() -> None:
    big_args = {"blob": "y" * 5000}
    e = ev.build_tool_call_event(
        ts="t",
        duration_ms=0,
        turn_id="t",
        iteration=0,
        ctx=_ctx(),
        tool="write_file",
        args=big_args,
        result="z" * 5000,
        max_field_bytes=100,
        content_mode="full",
    )
    assert e["result_truncated"] is True
    assert len(e["result"].encode("utf-8")) <= 100
    # Oversize args degrade to a truncated string form rather than a corrupt object.
    assert e["args_truncated"] is True
    assert isinstance(e["args"], str)


def test_metadata_mode_omits_tool_content_but_keeps_failure_status() -> None:
    e = ev.build_tool_call_event(
        ts="t",
        duration_ms=1,
        turn_id="turn",
        iteration=0,
        ctx=_ctx(),
        tool="lookup",
        args={"api_key": "secret", "query": "private question"},
        result='{"error": "private failure"}',
        max_field_bytes=8192,
        content_mode="metadata",
    )

    assert e["content_mode"] == "metadata"
    assert e["args"] is None
    assert e["result"] is None
    assert e["error"] is None
    assert e["ok"] is False
    assert e["args_truncated"] is False
    assert e["result_truncated"] is False


def test_redacted_mode_scrubs_nested_fields_and_exact_secret_values() -> None:
    secret = "known-provider-secret"
    e = ev.build_tool_call_event(
        ts="t",
        duration_ms=1,
        turn_id="turn",
        iteration=0,
        ctx=_ctx(),
        tool="lookup",
        args={
            "query": f"use {secret}",
            "headers": {"Authorization": "Bearer visible", "max_tokens": 42},
            "refresh-token": "visible-token",
        },
        result=f'{{"error": "request with {secret} failed"}}',
        max_field_bytes=8192,
        content_mode="redacted",
        secret_values=(secret,),
    )

    assert e["args"] == {
        "query": "use [REDACTED]",
        "headers": {"Authorization": "[REDACTED]", "max_tokens": 42},
        "refresh-token": "[REDACTED]",
    }
    assert e["result"] == '{"error": "request with [REDACTED] failed"}'
    assert e["error"] == "request with [REDACTED] failed"
    assert secret not in json.dumps(e)


def test_redacted_mode_scrubs_sensitive_keys_in_json_result_and_error() -> None:
    e = ev.build_tool_call_event(
        ts="t",
        duration_ms=1,
        turn_id="turn",
        iteration=0,
        ctx=_ctx(),
        tool="issue_credential",
        args={},
        result=json.dumps(
            {
                "token": "runtime-secret",
                "nested": {
                    "clientSecret": "another-runtime-secret",
                    "password_confirmation": "runtime-password",
                    "authorization_header": "runtime-authorization",
                    "secret_access_key": "runtime-access-key",
                },
                "error": {"private_key": "runtime-private-key"},
                "private_key_pem": "runtime-private-key-pem",
            }
        ),
        max_field_bytes=8192,
        content_mode="redacted",
    )

    assert json.loads(e["result"]) == {
        "token": "[REDACTED]",
        "nested": {
            "clientSecret": "[REDACTED]",
            "password_confirmation": "[REDACTED]",
            "authorization_header": "[REDACTED]",
            "secret_access_key": "[REDACTED]",
        },
        "error": {"private_key": "[REDACTED]"},
        "private_key_pem": "[REDACTED]",
    }
    # A non-string "error" value reaches the field as str(...), so the repr shape
    # here is expected. What matters is that it went through redaction first.
    assert e["error"] == "{'private_key': '[REDACTED]'}"
    assert "runtime-secret" not in json.dumps(e)
    assert "runtime-private-key" not in json.dumps(e)


def test_redacted_mode_scrubs_compact_secret_keys_without_partial_word_matches() -> None:
    e = ev.build_tool_call_event(
        ts="t",
        duration_ms=1,
        turn_id="turn",
        iteration=0,
        ctx=_ctx(),
        tool="issue_credential",
        args={
            "nested": {
                "apikey": "argument-api-key",
                "provider_apikey_value": "prefixed-api-key",
                "tokenizer": "sentencepiece",
            }
        },
        result=json.dumps(
            {
                "nested": {
                    "accesskey": "result-access-key",
                    "aws-accesskey-id": "prefixed-access-key",
                    "monkey": "capuchin",
                }
            }
        ),
        max_field_bytes=8192,
        content_mode="redacted",
    )

    assert e["args"] == {
        "nested": {
            "apikey": "[REDACTED]",
            "provider_apikey_value": "[REDACTED]",
            "tokenizer": "sentencepiece",
        }
    }
    assert json.loads(e["result"]) == {
        "nested": {
            "accesskey": "[REDACTED]",
            "aws-accesskey-id": "[REDACTED]",
            "monkey": "capuchin",
        }
    }


def test_build_turn_event_shape() -> None:
    e = ev.build_turn_event(
        ts="t",
        turn_id="abc123",
        ctx=_ctx(),
        trigger="unknown",
        tool_count=3,
        duration_ms=1840,
        request_snapshot=[],
        response_text="Final answer.",
        content_mode="full",
        model="kimi-k2",
        models=["glm-4.7", "kimi-k2"],
        primary_model="glm-4.7",
        llm_calls=4,
        usage={
            "input_tokens": 100,
            "cached_read_tokens": 50,
            "cache_write_tokens": 0,
            "output_tokens": 25,
        },
    )
    assert e["v"] == ev.SCHEMA_VERSION
    assert e["type"] == "turn"
    assert e["content_mode"] == "full"
    assert e["turn_id"] == "abc123"
    assert e["user_name"] == "webhead"
    assert e["channel_id"] == "chan1"
    assert e["trigger"] == "unknown"
    assert e["tool_count"] == 3
    assert e["duration_ms"] == 1840
    assert e["model"] == "kimi-k2"
    assert e["models"] == ["glm-4.7", "kimi-k2"]
    assert e["primary_model"] == "glm-4.7"
    assert e["llm_calls"] == 4
    assert e["usage"] == {
        "input_tokens": 100,
        "cached_read_tokens": 50,
        "cache_write_tokens": 0,
        "output_tokens": 25,
    }
    assert e["request"] == []
    assert e["response"] == {
        "role": "assistant",
        "section": "response",
        "text": "Final answer.",
        "truncated": False,
    }


def test_build_turn_event_embeds_request_snapshot_with_truncation() -> None:
    snapshot = [
        {"role": "system", "section": "system", "text": "s" * 5000},
        {"role": "user", "section": "history", "text": "webhead: hi"},
        {"role": "user", "section": "message", "text": "webhead: look this up"},
        {"role": "tool", "section": "tools", "text": "lookup\nbrowse_tools"},
    ]
    e = ev.build_turn_event(
        ts="t",
        turn_id="abc123",
        ctx=_ctx(),
        trigger="unknown",
        tool_count=0,
        duration_ms=1,
        request_snapshot=snapshot,
        response_text="r" * 5000,
        max_field_bytes=100,
        content_mode="full",
    )
    req = e["request"]
    assert [m["section"] for m in req] == ["system", "history", "message", "tools"]
    assert [m["role"] for m in req] == ["system", "user", "user", "tool"]
    # Oversize system prompt is truncated and flagged; small messages are not.
    assert req[0]["truncated"] is True
    assert len(req[0]["text"].encode("utf-8")) <= 100
    assert req[1]["truncated"] is False
    assert req[2]["text"] == "webhead: look this up"
    assert req[3]["text"] == "lookup\nbrowse_tools"
    assert e["response"]["truncated"] is True
    assert len(e["response"]["text"].encode("utf-8")) <= 100


def test_turn_content_modes_omit_or_redact_snapshots() -> None:
    snapshot = [{"role": "user", "section": "message", "text": "token is exact-secret"}]
    metadata = ev.build_turn_event(
        ts="t",
        turn_id="turn",
        ctx=_ctx(),
        trigger="unknown",
        tool_count=0,
        duration_ms=1,
        request_snapshot=snapshot,
        response_text="exact-secret",
        content_mode="metadata",
    )
    redacted = ev.build_turn_event(
        ts="t",
        turn_id="turn",
        ctx=_ctx(),
        trigger="unknown",
        tool_count=0,
        duration_ms=1,
        request_snapshot=snapshot,
        response_text="exact-secret",
        content_mode="redacted",
        secret_values=("exact-secret",),
    )

    assert metadata["request"] is None
    assert metadata["response"] is None
    assert redacted["request"][0]["text"] == "token is [REDACTED]"
    assert redacted["response"]["text"] == "[REDACTED]"


def test_build_compaction_event_shape() -> None:
    e = ev.build_compaction_event(
        ts="2026-06-02T01:02:03.004Z",
        turn_id="abc123",
        iteration=3,
        ctx=_ctx(),
        reason="threshold",
        before_messages=9,
        after_messages=5,
        kept_recent_iterations=2,
        note_chars=1234,
        elided_tool_results=0,
        hard_truncated_tool_results=1,
        content_mode="full",
    )
    assert e["v"] == ev.SCHEMA_VERSION
    assert e["type"] == "compaction"
    assert e["content_mode"] == "full"
    assert e["ts"] == "2026-06-02T01:02:03.004Z"
    assert e["turn_id"] == "abc123"
    assert e["iteration"] == 3
    assert e["user_name"] == "webhead"
    assert e["channel_id"] == "chan1"
    assert e["trust_tier"] == "staff"
    assert e["reason"] == "threshold"
    assert e["before_messages"] == 9
    assert e["after_messages"] == 5
    assert e["kept_recent_iterations"] == 2
    assert e["note_chars"] == 1234
    assert e["elided_tool_results"] == 0
    assert e["hard_truncated_tool_results"] == 1


def test_writer_appends_one_line_per_emit(tmp_path: Path) -> None:
    log_path = tmp_path / "events.jsonl"

    async def run() -> None:
        ev.start_event_writer(str(log_path), max_field_bytes=8192, content_mode="full")
        ev.emit_tool_call(
            turn_id="t1",
            iteration=0,
            ctx=_ctx(),
            tool="lookup",
            args={"q": "vr"},
            result='{"ok": true}',
            duration_ms=5,
        )
        ev.emit_compaction(
            turn_id="t1",
            iteration=1,
            ctx=_ctx(),
            reason="threshold",
            before_messages=7,
            after_messages=4,
            kept_recent_iterations=2,
            note_chars=900,
            elided_tool_results=0,
            hard_truncated_tool_results=0,
        )
        ev.emit_turn(
            turn_id="t1",
            ctx=_ctx(),
            trigger="unknown",
            tool_count=1,
            duration_ms=10,
            request_snapshot=[],
            response_text="Done.",
        )
        await ev.stop_event_writer()

    asyncio.run(run())
    lines = _read_lines(log_path)
    assert [line["type"] for line in lines] == ["tool_call", "compaction", "turn"]
    assert lines[0]["tool"] == "lookup"
    assert lines[0]["turn_id"] == "t1"
    assert lines[1]["reason"] == "threshold"
    assert lines[2]["response"]["text"] == "Done."


def test_blocked_writer_does_not_stall_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_path = tmp_path / "events.jsonl"

    async def run() -> None:
        writer = ev.EventWriter(
            log_path,
            max_field_bytes=8192,
            max_file_bytes=10_000_000,
            content_mode="metadata",
        )
        writer.start()
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        release = threading.Event()
        original_write = writer._write

        def blocked_write(event: dict[str, object]) -> None:
            loop.call_soon_threadsafe(started.set)
            release.wait()
            original_write(event)

        monkeypatch.setattr(writer, "_write", blocked_write)
        # Prevent a broken implementation from hanging the suite while still
        # making it observable that the loop could not run during the write.
        safety_release = threading.Timer(1.0, release.set)
        safety_release.start()
        try:
            writer.enqueue({"sequence": 1})
            await started.wait()
            assert not release.is_set()
        finally:
            release.set()
            safety_release.cancel()
            await writer.stop()

    asyncio.run(run())


def test_emit_is_noop_when_writer_disabled(tmp_path: Path) -> None:
    # No start_event_writer() call -> emits do nothing and no file is created.
    ev.emit_tool_call(
        turn_id="t",
        iteration=0,
        ctx=_ctx(),
        tool="lookup",
        args={},
        result="{}",
        duration_ms=1,
    )
    assert not (tmp_path / "events.jsonl").exists()


def test_writer_degrades_when_path_unwritable(tmp_path: Path) -> None:
    # A path whose parent is a FILE (not a dir) cannot be opened; writer stays disabled.
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    bad_path = blocker / "events.jsonl"

    async def run() -> None:
        ev.start_event_writer(str(bad_path), max_field_bytes=8192, content_mode="metadata")
        ev.emit_tool_call(
            turn_id="t",
            iteration=0,
            ctx=_ctx(),
            tool="lookup",
            args={},
            result="{}",
            duration_ms=1,
        )
        await ev.stop_event_writer()

    asyncio.run(run())  # must not raise
    assert not bad_path.exists()


def test_writer_rotates_when_file_exceeds_cap(tmp_path: Path) -> None:
    log_path = tmp_path / "events.jsonl"

    async def run() -> None:
        # Tiny cap forces a rotation after the first line.
        ev.start_event_writer(
            str(log_path),
            max_field_bytes=8192,
            content_mode="metadata",
            max_file_bytes=50,
        )
        for i in range(3):
            ev.emit_turn(
                turn_id=f"t{i}",
                ctx=_ctx(),
                trigger="unknown",
                tool_count=0,
                duration_ms=1,
                request_snapshot=[],
                response_text="Done.",
            )
        await ev.stop_event_writer()

    asyncio.run(run())
    backup = tmp_path / "events.jsonl.1"
    assert backup.exists()
    assert log_path.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits are not portable to Windows")
def test_writer_creates_and_rotates_owner_only_files(tmp_path: Path) -> None:
    log_path = tmp_path / "events.jsonl"

    async def run() -> None:
        # Tiny cap so the rotated backup exists and its mode can be checked too.
        ev.start_event_writer(
            str(log_path),
            max_field_bytes=8192,
            content_mode="metadata",
            max_file_bytes=50,
        )
        for i in range(3):
            ev.emit_turn(
                turn_id=f"t{i}",
                ctx=_ctx(),
                trigger="unknown",
                tool_count=0,
                duration_ms=1,
                request_snapshot=[],
                response_text="Done.",
            )
        await ev.stop_event_writer()

    asyncio.run(run())
    assert log_path.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "events.jsonl.1").stat().st_mode & 0o777 == 0o600


class _ScriptedProvider(LLMProvider):
    provider_key = "scripted"
    model = "scripted-model"

    def __init__(self, responses: list[ProviderResponse]) -> None:
        self.responses = responses

    async def run_turn(self, request: ProviderRequest) -> ProviderResponse:
        return self.responses.pop(0)


def test_run_conversation_emits_tool_call_and_turn_events(tmp_path: Path) -> None:
    log_path = tmp_path / "events.jsonl"

    async def lookup(args: dict, ctx: MessageContext) -> str:
        return json.dumps({"value": args["query"]})

    registry = ToolRegistry()
    registry.register(
        name="lookup",
        description="Lookup a value",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        handler=lookup,
    )
    provider = _ScriptedProvider(
        responses=[
            ProviderResponse(
                content="",
                tool_calls=[ToolCall(id="c1", name="lookup", arguments={"query": "vr"})],
                finish_reason="tool_calls",
                usage={"input_tokens": 10, "output_tokens": 5},
            ),
            ProviderResponse(
                content="Final answer.",
                usage={"input_tokens": 20, "output_tokens": 7},
            ),
        ]
    )

    context = ConversationContext(key="g1:chan1:main", user_name="webhead")
    # Seed a prior channel message so the snapshot carries a `history` section.
    context.add_messages(
        [ConversationMessage(role="user", content=[ContentPart.from_text("webhead: earlier")])]
    )

    async def run() -> None:
        ev.start_event_writer(str(log_path), max_field_bytes=8192, content_mode="full")
        await run_conversation(
            request=ConversationRunRequest(
                user_message="look this up",
                context=context,
                trust_tier=TrustTier.STAFF,
                user_name="webhead",
                user_id="123",
                provider=provider,
                registry=registry,
                channel_id="chan1",
                recalled_memories="webhead likes VR.",
            )
        )
        await ev.stop_event_writer()

    asyncio.run(run())
    lines = _read_lines(log_path)
    tool_calls = [line for line in lines if line["type"] == "tool_call"]
    turns = [line for line in lines if line["type"] == "turn"]
    assert len(tool_calls) == 1
    assert tool_calls[0]["tool"] == "lookup"
    assert tool_calls[0]["args"] == {"query": "vr"}
    assert tool_calls[0]["ok"] is True
    # Model attribution: the tool call carries the model whose response issued it;
    # the turn event carries the final/served model set and the summed usage.
    assert tool_calls[0]["model"] == "scripted-model"
    assert len(turns) == 1
    assert turns[0]["tool_count"] == 1
    assert turns[0]["response"]["text"] == "Final answer."
    assert turns[0]["model"] == "scripted-model"
    assert turns[0]["models"] == ["scripted-model"]
    assert turns[0]["primary_model"] == "scripted-model"
    assert turns[0]["llm_calls"] == 2
    assert turns[0]["usage"] == {
        "input_tokens": 30,
        "cached_read_tokens": 0,
        "cache_write_tokens": 0,
        "output_tokens": 12,
    }
    # All events in the turn share one turn_id.
    assert tool_calls[0]["turn_id"] == turns[0]["turn_id"]

    # The end-of-turn event carries the iteration-0 model-input snapshot, in order,
    # with each piece the live run actually assembled.
    req = turns[0]["request"]
    assert [m["section"] for m in req] == ["system", "history", "context", "message", "tools"]
    system = next(m for m in req if m["section"] == "system")
    assert system["role"] == "system"
    history = next(m for m in req if m["section"] == "history")
    assert "webhead: earlier" in history["text"]
    context_msg = next(m for m in req if m["section"] == "context")
    assert "webhead likes VR." in context_msg["text"]
    message = next(m for m in req if m["section"] == "message")
    assert message["text"] == "webhead: look this up"
    tools = next(m for m in req if m["section"] == "tools")
    assert "lookup" in tools["text"].split("\n")


def test_writer_stop_returns_after_consumer_death_with_full_queue(tmp_path: Path) -> None:
    # If the consumer task died (e.g. disk error) and the bounded queue then
    # filled, stop() must not block forever on an awaited put of the sentinel.
    log_path = tmp_path / "events.jsonl"

    async def run() -> None:
        writer = ev.EventWriter(
            log_path,
            max_field_bytes=8192,
            max_file_bytes=10_000_000,
            content_mode="metadata",
        )
        writer.start()
        assert writer._task is not None
        writer._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await writer._task
        while not writer._queue.full():
            writer._queue.put_nowait({"k": "v"})

        await asyncio.wait_for(writer.stop(), timeout=2.0)

    asyncio.run(run())


def test_writer_stop_waits_for_blocked_write_before_closing_full_queue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_path = tmp_path / "events.jsonl"

    async def run() -> None:
        writer = ev.EventWriter(
            log_path,
            max_field_bytes=8192,
            max_file_bytes=10_000_000,
            content_mode="metadata",
        )
        writer.start()
        assert writer._file is not None
        opened_file = writer._file
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        release = threading.Event()
        closed_during_write: list[bool] = []

        def blocked_write(_: dict[str, object]) -> None:
            loop.call_soon_threadsafe(started.set)
            release.wait()
            closed_during_write.append(opened_file.closed)

        monkeypatch.setattr(writer, "_write", blocked_write)
        safety_release = threading.Timer(1.0, release.set)
        safety_release.start()
        stop_task: asyncio.Task[None] | None = None
        try:
            writer.enqueue({"in_flight": True})
            await started.wait()
            while not writer._queue.full():
                writer._queue.put_nowait({"backlog": True})

            stop_task = asyncio.create_task(writer.stop())
            await asyncio.sleep(0)

            assert not stop_task.done()
            assert not opened_file.closed
            assert writer._queue.qsize() == 1
        finally:
            release.set()
            safety_release.cancel()
            if stop_task is not None:
                await asyncio.wait_for(stop_task, timeout=2.0)
            else:
                await writer.stop()

        assert closed_during_write == [False]
        assert opened_file.closed
        assert writer._file is None

    asyncio.run(run())


def test_cancelling_writer_stop_drains_in_flight_write_before_closing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_path = tmp_path / "events.jsonl"

    async def run() -> None:
        writer = ev.EventWriter(
            log_path,
            max_field_bytes=8192,
            max_file_bytes=10_000_000,
            content_mode="metadata",
        )
        writer.start()
        assert writer._file is not None
        opened_file = writer._file
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        release = threading.Event()
        closed_during_write: list[bool] = []
        original_write = writer._write

        def blocked_write(event: dict[str, object]) -> None:
            loop.call_soon_threadsafe(started.set)
            release.wait()
            closed_during_write.append(opened_file.closed)
            original_write(event)

        monkeypatch.setattr(writer, "_write", blocked_write)
        safety_release = threading.Timer(1.0, release.set)
        safety_release.start()
        stop_task: asyncio.Task[None] | None = None
        try:
            writer.enqueue({"in_flight": True})
            await started.wait()
            stop_task = asyncio.create_task(writer.stop())
            await asyncio.sleep(0)

            stop_task.cancel()
            await asyncio.sleep(0)

            assert not stop_task.done()
            assert not opened_file.closed
        finally:
            release.set()
            safety_release.cancel()

        assert stop_task is not None
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(stop_task, timeout=2.0)

        assert closed_during_write == [False]
        assert opened_file.closed
        assert writer._file is None
        assert writer._task is None
        await writer.stop()  # repeated shutdown is idempotent

    asyncio.run(run())


def test_enqueue_drops_events_after_consumer_death(tmp_path: Path) -> None:
    log_path = tmp_path / "events.jsonl"

    async def run() -> None:
        writer = ev.EventWriter(
            log_path,
            max_field_bytes=8192,
            max_file_bytes=10_000_000,
            content_mode="metadata",
        )
        writer.start()
        assert writer._task is not None
        writer._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await writer._task

        writer.enqueue({"k": "v"})

        assert writer._queue.empty()
        await asyncio.wait_for(writer.stop(), timeout=2.0)

    asyncio.run(run())
