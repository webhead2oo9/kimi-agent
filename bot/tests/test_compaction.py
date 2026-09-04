"""Exercises agent/compaction.py in isolation from any real provider: token
estimation, head/tail truncation, and transcript serialization used when a
conversation is summarized.
"""

from __future__ import annotations

import asyncio
import json

from agent.compaction import (
    CompactionConfig,
    Compactor,
    NOTE_PREFIX,
    _FALLBACK_NOTE_BODY,
    _REQUEST_REMINDER_PREFIX,
    _head_tail,
    _is_refresh_note,
    _is_request_reminder,
    _render_prefix,
    elide_prefix,
    est_request_tokens,
    est_tokens,
    input_tokens,
    note_message,
    request_reminder_message,
    serialize_message,
    split_iterations,
)
from providers.base import LLMProvider
from providers.types import (
    ContentPart,
    ConversationMessage,
    ProviderResponse,
)


def _is_fallback_note(msg: ConversationMessage) -> bool:
    """Did this pass emit the elision fallback note (no summary was produced)?"""
    if msg.role != "user" or not msg.content:
        return False
    return (msg.content[0].text or "").startswith(NOTE_PREFIX + _FALLBACK_NOTE_BODY)


def _tool(text: str, name: str = "read_file") -> ConversationMessage:
    return ConversationMessage(
        role="tool",
        content=[ContentPart.from_text(text)],
        tool_call_id="c1",
        tool_name=name,
    )


def _assistant(text: str = "", tool_id: str = "c1") -> ConversationMessage:
    return ConversationMessage(
        role="assistant",
        content=[ContentPart.from_text(text)],
        raw_provider_data={"role": "assistant", "tool_calls": [{"id": tool_id}]},
    )


def test_est_tokens():
    assert est_tokens("abcdefgh") == 2  # 8 / 3.5 -> 2
    assert est_tokens("") == 0


def test_head_tail_keeps_start_and_end_for_large_bodies():
    text = "HEAD" + ("m" * 5000) + "TAIL"
    out = _head_tail(text, 2048, "read_file", "elided to save context")
    assert out.startswith("HEAD")
    assert out.endswith("TAIL")
    assert "elided to save context" in out
    assert len(out) < len(text)


def test_head_tail_stubs_when_no_room():
    # keep budget below the floor collapses to a content-free stub.
    out = _head_tail("x" * 5000, 10, "read_file", "hard-truncated")
    assert "hard-truncated" in out
    assert "x" * 50 not in out


def test_head_tail_returns_short_text_unchanged():
    assert _head_tail("small", 2048, "read_file", "elided to save context") == "small"


def test_input_tokens_prefers_prompt_then_input():
    assert input_tokens({"prompt_tokens": 100}) == 100
    assert input_tokens({"input_tokens": 250}) == 250


def test_input_tokens_returns_none_when_unusable():
    assert input_tokens({}) is None
    assert input_tokens({"total_tokens": 100}) is None
    assert input_tokens({"prompt_tokens": None}) is None
    assert input_tokens({"prompt_tokens": 0}) is None


def test_serialize_prefers_raw_provider_data():
    msg = ConversationMessage(
        role="assistant",
        content=[ContentPart.from_text("hi")],
        raw_provider_data={"role": "assistant", "reasoning_content": "x" * 40},
    )
    assert "reasoning_content" in serialize_message(msg)


def test_serialize_falls_back_to_content():
    assert "hello world" in serialize_message(_tool("hello world"))


def test_est_request_tokens_counts_system_and_messages():
    msgs = [_tool("a" * 40)]
    est = est_request_tokens("b" * 40, msgs)
    assert est >= 20  # ~10 (system) + ~10 (body) tokens


class _NullProvider(LLMProvider):
    async def run_turn(self, request):  # pragma: no cover - not called in this task
        raise AssertionError("summarizer should not run here")


def _compactor(**overrides) -> Compactor:
    cfg = CompactionConfig(
        trigger_tokens=overrides.get("trigger_tokens", 1000),
        keep_recent_iterations=overrides.get("keep_recent_iterations", 2),
        max_tokens=overrides.get("max_tokens", 256),
        max_iteration_tool_output_tokens=overrides.get("max_iteration_tool_output_tokens", 10),
    )
    return Compactor(cfg, _NullProvider())


def test_projected_tokens_uses_usage_plus_delta():
    c = _compactor()
    appended = [_tool("x" * 400)]  # ~100 tokens
    projected = c.projected_tokens(
        last_usage={"prompt_tokens": 500},
        appended=appended,
        fallback_messages=[],
        system_prompt="",
    )
    assert projected >= 600


def test_projected_tokens_falls_back_to_estimate_without_usage():
    c = _compactor()
    fallback = [_tool("y" * 4000)]  # ~1000 tokens
    projected = c.projected_tokens(
        last_usage={},
        appended=[],
        fallback_messages=fallback,
        system_prompt="",
    )
    assert projected >= 900


def test_clamp_tool_output_stubs_past_budget():
    c = _compactor(max_iteration_tool_output_tokens=10)  # budget = 40 chars
    first, running = c.clamp_tool_output(0, "a" * 30, "read_file")
    assert first == "a" * 30
    assert running == 30
    second, running = c.clamp_tool_output(running, "b" * 100, "read_file")
    assert "truncated" in second
    assert running == 30


def test_clamp_tool_output_head_tails_when_room_remains():
    c = _compactor(max_iteration_tool_output_tokens=1000)  # budget = 4000 chars
    out, running = c.clamp_tool_output(0, "S" + "m" * 6000 + "E", "read_file")
    assert out.startswith("S")
    assert out.endswith("E")
    assert "truncated (per-iteration budget)" in out
    assert running <= 4000 + 256  # bounded near the per-iteration budget


def test_split_iterations_groups_assistant_plus_tools():
    msgs = [_assistant(tool_id="c1"), _tool("r1"), _assistant(tool_id="c2"), _tool("r2")]
    iters = split_iterations(msgs)
    assert len(iters) == 2
    assert iters[0][0].role == "assistant"
    assert iters[0][1].role == "tool"


def test_split_iterations_leading_user_note_is_own_unit():
    note = note_message("prior summary")
    msgs = [note, _assistant(), _tool("r1")]
    iters = split_iterations(msgs)
    assert iters[0] == [note]
    assert len(iters) == 2


def test_note_message_is_user_role_and_labeled():
    note = note_message("did stuff")
    assert note.role == "user"
    assert note.content[0].text.startswith(NOTE_PREFIX)
    assert "did stuff" in note.content[0].text


def test_note_message_appends_checklist_verbatim():
    plan = [
        {"content": "step a", "status": "completed"},
        {"content": "step b", "status": "in_progress"},
    ]
    text = note_message("did stuff", plan).content[0].text
    assert text.startswith(NOTE_PREFIX)
    assert "Current checklist" in text
    assert json.dumps({"plan": plan, "count": 2}, ensure_ascii=False) in text


def test_note_message_without_plan_is_unchanged():
    baseline = note_message("did stuff").content[0].text
    assert note_message("did stuff", None).content[0].text == baseline
    assert note_message("did stuff", []).content[0].text == baseline
    assert "Current checklist" not in baseline


def test_elide_prefix_stubs_tool_bodies_keeps_assistant():
    asst = _assistant(text="thinking")
    prefix = [asst, _tool("x" * 5000)]
    out = elide_prefix(prefix)
    assert out[0] is asst
    assert "elided" in out[1].content[0].text


class _ScriptedSummarizer(LLMProvider):
    def __init__(self, text: str = "PROGRESS", fail: bool = False) -> None:
        self._text = text
        self._fail = fail
        self.calls = 0

    async def run_turn(self, request):
        self.calls += 1
        if self._fail:
            raise RuntimeError("summarizer down")
        return ProviderResponse(content=self._text)


def _user(text: str = "do the thing") -> ConversationMessage:
    return ConversationMessage(role="user", content=[ContentPart.from_text(text)])


def _turn_with_n_iterations(n: int, body_chars: int = 4000) -> list[ConversationMessage]:
    msgs: list[ConversationMessage] = [_user()]
    for i in range(n):
        msgs.append(_assistant(tool_id=f"c{i}"))
        msgs.append(_tool("z" * body_chars, name="read_file"))
    return msgs


def _run(coro):
    return asyncio.run(coro)


def test_maybe_compact_noop_below_threshold():
    c = Compactor(CompactionConfig(trigger_tokens=10_000_000), _ScriptedSummarizer())
    turn = _turn_with_n_iterations(3)
    out = _run(
        c.maybe_compact(
            turn_messages=turn,
            head_messages=[],
            system_prompt="",
            last_response=ProviderResponse(usage={"prompt_tokens": 10}, usage_present=True),
            appended=turn[-2:],
        )
    )
    assert out is turn


def _compact_small_window(summarizer, turn):
    """Run maybe_compact with the standard tiny window the summarize tests share."""
    c = Compactor(
        CompactionConfig(
            trigger_tokens=100,
            keep_recent_iterations=2,
            keep_recent_tokens=0,
            max_tokens=64,
        ),
        summarizer,
    )
    return _run(
        c.maybe_compact(
            turn_messages=turn,
            head_messages=[],
            system_prompt="",
            last_response=ProviderResponse(usage={"prompt_tokens": 200000}, usage_present=True),
            appended=turn[-2:],
        )
    )


def test_maybe_compact_summarizes_and_pins_user_message():
    summarizer = _ScriptedSummarizer(text="SUMMARY")
    turn = _turn_with_n_iterations(5, body_chars=4000)
    out = _compact_small_window(summarizer, turn)
    assert summarizer.calls == 1
    assert out[0] is turn[0]
    assert out[1].role == "user"
    assert out[1].content[0].text.startswith(NOTE_PREFIX)
    assert len(out) < len(turn)


def test_keep_split_budget_keeps_more_than_floor():
    # 10 iterations of ~1000 tokens each (3500-char tool bodies); a ~3100-token
    # budget keeps 3 whole recent iterations verbatim, above the floor of 1.
    summarizer = _ScriptedSummarizer(text="SUMMARY")
    c = Compactor(
        CompactionConfig(trigger_tokens=100, keep_recent_iterations=1, keep_recent_tokens=3100),
        summarizer,
    )
    turn = _turn_with_n_iterations(10, body_chars=3500)
    out = _run(
        c.maybe_compact(
            turn_messages=turn,
            head_messages=[],
            system_prompt="",
            last_response=ProviderResponse(usage={"prompt_tokens": 200000}, usage_present=True),
            appended=turn[-2:],
        )
    )
    kept_assistants = sum(1 for m in out if m.role == "assistant")
    assert kept_assistants == 3
    assert summarizer.calls == 1
    assert out[1].content[0].text.startswith(NOTE_PREFIX)


def test_compaction_reappends_request_reminder_at_tail():
    turn = _turn_with_n_iterations(5, body_chars=4000)
    out = _compact_small_window(_ScriptedSummarizer(text="SUMMARY"), turn)
    # Original user message stays pinned at the front...
    assert out[0] is turn[0]
    # ...and its request is restated as the final (most salient) message.
    assert out[-1].role == "user"
    assert _is_request_reminder(out[-1])
    assert "do the thing" in out[-1].content[0].text
    assert sum(1 for m in out if _is_request_reminder(m)) == 1


def test_compaction_reminder_does_not_accumulate():
    summarizer = _ScriptedSummarizer(text="SUMMARY")
    c = Compactor(
        CompactionConfig(
            trigger_tokens=100,
            keep_recent_iterations=2,
            keep_recent_tokens=0,
            max_tokens=64,
        ),
        summarizer,
    )
    turn = _turn_with_n_iterations(5, body_chars=4000)
    first = _run(c.emergency_compact(turn_messages=turn, head_messages=[], system_prompt=""))
    # More tool churn lands after the reminder; the next compaction must strip the
    # stale reminder and re-append exactly one fresh copy at the tail.
    grown = [*first, _assistant(tool_id="c9"), _tool("y" * 4000, name="read_file")]
    second = _run(c.emergency_compact(turn_messages=grown, head_messages=[], system_prompt=""))
    assert sum(1 for m in second if _is_request_reminder(m)) == 1
    assert _is_request_reminder(second[-1])


def test_request_reminder_message_restates_text():
    msg = request_reminder_message(_user("please fix the crash"))
    assert msg is not None
    assert msg.role == "user"
    assert msg.content[0].text.startswith(_REQUEST_REMINDER_PREFIX)
    assert "please fix the crash" in msg.content[0].text
    # Not framed as untrusted tool output, unlike the progress note.
    assert NOTE_PREFIX not in msg.content[0].text


def test_request_reminder_message_none_without_text():
    assert request_reminder_message(_user("")) is None
    assert request_reminder_message(ConversationMessage(role="user", content=[])) is None


def test_request_reminder_message_is_size_capped():
    # Command-path triggering prompts can embed a whole transcript window; the
    # user-role reminder is exempt from every truncation layer, so it must never
    # duplicate the prompt wholesale.
    huge = "COMMAND HEAD " + ("x" * 50_000) + " COMMAND TAIL"
    msg = request_reminder_message(_user(huge))
    assert msg is not None
    text = msg.content[0].text
    assert len(text) < 5000
    assert "COMMAND HEAD" in text
    assert "COMMAND TAIL" in text
    assert "cut from this restatement" in text


def _already_compacted_turn() -> list[ConversationMessage]:
    """A turn shaped like the output of a prior summarize pass plus new churn."""
    reminder = request_reminder_message(_user())
    assert reminder is not None
    return [
        _user(),
        note_message("REAL SUMMARY"),
        _assistant(tool_id="c1"),
        _tool("small result"),
        reminder,
        _assistant(tool_id="c2"),
        _tool("more churn"),
    ]


def _split_zero_compactor() -> Compactor:
    # Floor and budget large enough that every iteration is kept: split == 0.
    return Compactor(
        CompactionConfig(
            trigger_tokens=10_000_000,
            keep_recent_iterations=50,
            keep_recent_tokens=1_000_000,
        ),
        _ScriptedSummarizer(text="SUMMARY"),
    )


def test_split_zero_recompaction_preserves_reminder():
    # A pass that finds nothing to summarize must not drop an existing
    # re-anchor, and must not fabricate an elision fallback note when only the
    # reminder was stripped.
    out = _run(
        _split_zero_compactor().emergency_compact(
            turn_messages=_already_compacted_turn(), head_messages=[], system_prompt=""
        )
    )
    assert sum(1 for m in out if _is_request_reminder(m)) == 1
    assert _is_request_reminder(out[-1])
    # The genuine summary note survives and no content-free note is fabricated.
    assert any("REAL SUMMARY" in (m.content[0].text or "") for m in out if m.content)
    assert not any(_is_refresh_note(m) for m in out)


def test_split_zero_recompaction_with_plan_appends_accurate_note():
    plan = [{"content": "keep going", "status": "in_progress"}]
    out = _run(
        _split_zero_compactor().emergency_compact(
            turn_messages=_already_compacted_turn(),
            head_messages=[],
            system_prompt="",
            plan=plan,
        )
    )
    [note] = [m for m in out if _is_refresh_note(m)]
    text = note.content[0].text
    assert "Current checklist" in text
    assert "keep going" in text
    # Nothing was summarized and nothing elided; the note must not claim otherwise.
    assert _FALLBACK_NOTE_BODY not in text
    assert _is_request_reminder(out[-1])


def test_keep_split_zero_budget_keeps_exactly_floor():
    summarizer = _ScriptedSummarizer(text="SUMMARY")
    c = Compactor(
        CompactionConfig(trigger_tokens=100, keep_recent_iterations=2, keep_recent_tokens=0),
        summarizer,
    )
    turn = _turn_with_n_iterations(6, body_chars=3500)
    out = _run(
        c.maybe_compact(
            turn_messages=turn,
            head_messages=[],
            system_prompt="",
            last_response=ProviderResponse(usage={"prompt_tokens": 200000}, usage_present=True),
            appended=turn[-2:],
        )
    )
    assert sum(1 for m in out if m.role == "assistant") == 2


def test_keep_split_no_compaction_when_everything_fits_budget():
    # All iterations fit the budget -> nothing summarized, summarizer never runs,
    # and the request falls through to hard-truncation only.
    summarizer = _ScriptedSummarizer(fail=True)
    c = Compactor(
        CompactionConfig(
            trigger_tokens=1_000_000, keep_recent_iterations=1, keep_recent_tokens=50000
        ),
        summarizer,
    )
    turn = _turn_with_n_iterations(4, body_chars=1000)
    out = _run(c.emergency_compact(turn_messages=turn, head_messages=[], system_prompt=""))
    assert summarizer.calls == 0
    assert [m.role for m in out] == [m.role for m in turn]


def test_summarize_scales_word_target_with_prefix():
    captured: dict[str, str] = {}

    class _CapturingSummarizer(LLMProvider):
        async def run_turn(self, request):
            captured["body"] = request.messages[0].content[0].text
            return ProviderResponse(content="note")

    c = Compactor(CompactionConfig(), _CapturingSummarizer())
    _run(c._summarize([_tool("z" * 175_000)]))  # ~50K tokens -> ~2000 words
    header = captured["body"].split("\n")[0]
    assert "roughly 2000 words" in header


def test_maybe_compact_elides_when_summarizer_fails():
    summarizer = _ScriptedSummarizer(fail=True)
    # Trigger high enough that the head/tail-elided prefix fits under it, so the elide
    # markers survive (hard-truncate would otherwise overwrite them with its own).
    c = Compactor(
        CompactionConfig(trigger_tokens=50000, keep_recent_iterations=1, keep_recent_tokens=0),
        summarizer,
    )
    turn = _turn_with_n_iterations(4, body_chars=4000)
    out = _run(
        c.maybe_compact(
            turn_messages=turn,
            head_messages=[],
            system_prompt="",
            last_response=ProviderResponse(usage={"prompt_tokens": 200000}, usage_present=True),
            appended=turn[-2:],
        )
    )
    assert out[0] is turn[0]
    assert any("elided" in (m.content[0].text if m.content else "") for m in out)


def test_maybe_compact_carries_plan_into_note():
    summarizer = _ScriptedSummarizer(text="SUMMARY")
    c = Compactor(
        CompactionConfig(
            trigger_tokens=100,
            keep_recent_iterations=2,
            keep_recent_tokens=0,
            max_tokens=64,
        ),
        summarizer,
    )
    turn = _turn_with_n_iterations(5, body_chars=4000)
    out = _run(
        c.maybe_compact(
            turn_messages=turn,
            head_messages=[],
            system_prompt="",
            last_response=ProviderResponse(usage={"prompt_tokens": 200000}, usage_present=True),
            appended=turn[-2:],
            plan=[{"content": "keep going", "status": "in_progress"}],
        )
    )
    note_text = out[1].content[0].text
    assert note_text.startswith(NOTE_PREFIX)
    assert "Current checklist" in note_text
    assert "keep going" in note_text


def test_summarizer_failure_appends_checklist_note_after_elision():
    # Elision can middle-cut the plan tool's own echo, so the fallback path still
    # carries the checklist on a note of its own.
    summarizer = _ScriptedSummarizer(fail=True)
    c = Compactor(
        CompactionConfig(trigger_tokens=50000, keep_recent_iterations=1, keep_recent_tokens=0),
        summarizer,
    )
    turn = _turn_with_n_iterations(4, body_chars=4000)
    out = _run(
        c.maybe_compact(
            turn_messages=turn,
            head_messages=[],
            system_prompt="",
            last_response=ProviderResponse(usage={"prompt_tokens": 200000}, usage_present=True),
            appended=turn[-2:],
            plan=[{"content": "keep going", "status": "in_progress"}],
        )
    )
    assert any("elided" in (m.content[0].text if m.content else "") for m in out)
    [note] = [
        m
        for m in out
        if m.role == "user" and m.content and m.content[0].text.startswith(NOTE_PREFIX)
    ]
    assert "Current checklist" in note.content[0].text
    assert "keep going" in note.content[0].text


def test_repeated_summarizer_failures_do_not_stack_checklist_notes():
    # Prior fallback notes are stripped from the whole turn before splitting, so
    # repeated failures never stack notes, including with a nonzero keep budget,
    # where a stale note would otherwise ride the kept-verbatim tail and land
    # after the fresh one.
    summarizer = _ScriptedSummarizer(fail=True)
    plan = [{"content": "keep going", "status": "in_progress"}]
    for keep_tokens in (0, 5000):
        c = Compactor(
            CompactionConfig(
                trigger_tokens=50000,
                keep_recent_iterations=1,
                keep_recent_tokens=keep_tokens,
            ),
            summarizer,
        )
        out = _turn_with_n_iterations(4, body_chars=4000)
        for _ in range(3):
            out = out + _turn_with_n_iterations(3, body_chars=4000)[1:]
            out = _run(
                c.maybe_compact(
                    turn_messages=out,
                    head_messages=[],
                    system_prompt="",
                    last_response=ProviderResponse(
                        usage={"prompt_tokens": 200000}, usage_present=True
                    ),
                    appended=out[-2:],
                    plan=plan,
                )
            )
        notes = [
            m
            for m in out
            if m.role == "user" and m.content and "Current checklist" in (m.content[0].text or "")
        ]
        assert len(notes) == 1, f"keep_recent_tokens={keep_tokens}"


def test_no_split_compaction_refreshes_fallback_checklist_note():
    # A checklist note created by an earlier failed compaction must survive a later
    # compaction whose keep budget fits everything (split == 0): the whole-body
    # filter strips it, and that branch appends a fresh no-summary note.
    summarizer = _ScriptedSummarizer(fail=True)
    plan = [{"content": "keep going", "status": "in_progress"}]
    first = Compactor(
        CompactionConfig(trigger_tokens=50000, keep_recent_iterations=1, keep_recent_tokens=0),
        summarizer,
    )
    turn = _turn_with_n_iterations(4, body_chars=4000)
    out = _run(
        first.maybe_compact(
            turn_messages=turn,
            head_messages=[],
            system_prompt="",
            last_response=ProviderResponse(usage={"prompt_tokens": 200000}, usage_present=True),
            appended=turn[-2:],
            plan=plan,
        )
    )
    assert any(_is_fallback_note(m) for m in out)

    second = Compactor(
        CompactionConfig(
            trigger_tokens=50000,
            keep_recent_iterations=50,
            keep_recent_tokens=1_000_000,
        ),
        summarizer,
    )
    out2 = _run(
        second.emergency_compact(turn_messages=out, head_messages=[], system_prompt="", plan=plan)
    )
    [note] = [m for m in out2 if _is_refresh_note(m)]
    assert "Current checklist" in note.content[0].text
    assert "keep going" in note.content[0].text


def test_hard_truncate_preserves_note_checklist():
    # _hard_truncate only stubs role=="tool" bodies; the user-role note (and the
    # checklist riding it) must come through intact.
    c = Compactor(CompactionConfig(trigger_tokens=50), _ScriptedSummarizer())
    note = note_message("SUMMARY", [{"content": "step a", "status": "pending"}])
    working = [_user(), note, _assistant(tool_id="c1"), _tool("z" * 9000)]
    out = c._hard_truncate("", [], working)
    assert note in out
    assert "Current checklist" in note.content[0].text


def test_hard_truncate_reaches_budget_and_terminates():
    summarizer = _ScriptedSummarizer(text="x" * 200000)
    c = Compactor(
        CompactionConfig(
            trigger_tokens=100,
            keep_recent_iterations=2,
            keep_recent_tokens=0,
            max_tokens=999999,
        ),
        summarizer,
    )
    turn = _turn_with_n_iterations(4, body_chars=8000)
    out = _run(
        c.maybe_compact(
            turn_messages=turn,
            head_messages=[],
            system_prompt="",
            last_response=ProviderResponse(usage={"prompt_tokens": 200000}, usage_present=True),
            appended=turn[-2:],
        )
    )
    assert any("hard-truncated" in (m.content[0].text if m.content else "") for m in out)


def test_hard_truncate_stubs_medium_tool_outputs_until_under_budget():
    summarizer = _ScriptedSummarizer(fail=True)
    c = Compactor(
        CompactionConfig(trigger_tokens=1000, keep_recent_iterations=0, keep_recent_tokens=0),
        summarizer,
    )
    turn = _turn_with_n_iterations(8, body_chars=500)
    out = _run(
        c.maybe_compact(
            turn_messages=turn,
            head_messages=[],
            system_prompt="",
            last_response=ProviderResponse(usage={"prompt_tokens": 200000}, usage_present=True),
            appended=turn[-2:],
        )
    )
    assert est_request_tokens("", out) < c.config.trigger_tokens
    assert any("hard-truncated" in (m.content[0].text if m.content else "") for m in out)


def test_render_prefix_preserves_responses_and_codex_raw_tool_calls():
    asst = ConversationMessage(
        role="assistant",
        raw_provider_data={
            "type": "response_output",
            "output": [
                {
                    "type": "reasoning",
                    "summary": [{"type": "summary_text", "text": "Need data."}],
                },
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "lookup",
                    "arguments": '{"query":"vr"}',
                },
            ],
        },
    )
    rendered = _render_prefix([asst])
    assert "lookup" in rendered
    assert "query" in rendered
    assert "Need data" in rendered


def test_render_prefix_preserves_anthropic_tool_use_blocks():
    asst = ConversationMessage(
        role="assistant",
        raw_provider_data={
            "role": "assistant",
            "content": [
                {"type": "text", "text": "I should look it up."},
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "lookup",
                    "input": {"query": "vr"},
                },
            ],
        },
    )
    rendered = _render_prefix([asst])
    assert "I should look it up" in rendered
    assert "lookup" in rendered
    assert "vr" in rendered


def test_summarize_runs_without_semaphore_when_none():
    summarizer = _ScriptedSummarizer(text="note")
    c = Compactor(CompactionConfig(), summarizer)
    out = _run(c._summarize([_user("hello")]))
    assert out == "note"
    assert summarizer.calls == 1


def test_summarize_acquires_semaphore_when_provided():
    sem = asyncio.Semaphore(1)
    captured: dict[str, bool] = {}

    class _PermitProbe(LLMProvider):
        async def run_turn(self, request):
            # Inside the summarizer call the global permit must be held.
            captured["locked"] = sem.locked()
            return ProviderResponse(content="note")

    c = Compactor(CompactionConfig(), _PermitProbe(), llm_semaphore=sem)
    out = _run(c._summarize([_user("hello")]))
    assert out == "note"
    assert captured["locked"] is True
    assert sem.locked() is False  # released after the call
