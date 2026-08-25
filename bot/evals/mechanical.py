from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from evals.cassette import call_key
from evals.harness import ScenarioRun
from evals.scenario import Scenario

# Composite-score penalties. Scripted faults (source == "fault") are free, since
# the scenario asked for them, but failing to recover from one is not.
_PENALTY_MISSING_TOOL = 25.0
_PENALTY_UNEXPECTED_TOOL = 10.0
_PENALTY_TOOL_ERROR = 5.0
_PENALTY_UNRECOVERED_ERROR = 15.0
_PENALTY_REPEATED_CALL = 5.0
_PENALTY_RAW_JSON = 15.0
_PENALTY_FAILED_REPLY_CHECK = 15.0
_PENALTY_OVER_BUDGET = 10.0
_PENALTY_MISSING_ATTACHMENT = 20.0


@dataclass
class MechanicalResult:
    missing_tools: list[str]
    unexpected_tools: list[str]
    tool_call_count: int
    tool_errors: int
    tokens: int
    latency_ms: int
    raw_json_reply: bool
    # Identical (tool, args) calls repeated after an identical call already
    # succeeded: the loop smell. Retries after an error are not counted.
    repeated_calls: int = 0
    # Errored calls with no later successful call to the same tool.
    unrecovered_errors: int = 0
    # expect.reply_must_match regexes that matched no reply text.
    failed_reply_checks: list[str] = field(default_factory=list)
    # True when expect.max_tool_calls > 0 and the run exceeded it.
    over_budget: bool = False
    # True when expect.attaches_file and no turn queued an outgoing file.
    missing_attachment: bool = False
    # Index (in flattened call order) of the first should_use tool call; -1 = never.
    # Informational only, not scored.
    first_expected_call_index: int = -1
    # Weighted composite in [0, 100]; the harness-eval regression number.
    score: float = 0.0

    @property
    def passed(self) -> bool:
        """Hard-flag pass: everything the scenario demanded happened.

        Tool errors and repeated calls lower `score` but do not fail a rep on
        their own; recovery is graded via unrecovered_errors.
        """
        return not (
            self.missing_tools
            or self.unexpected_tools
            or self.unrecovered_errors
            or self.failed_reply_checks
            or self.over_budget
            or self.raw_json_reply
            or self.missing_attachment
        )


def _looks_like_raw_json(text: str) -> bool:
    stripped = text.strip()
    if not stripped or stripped[0] not in "{[":
        return False
    try:
        json.loads(stripped)
        return True
    except ValueError, TypeError:
        return False


def compute_mechanical(scenario: Scenario, run: ScenarioRun) -> MechanicalResult:
    calls = run.all_tool_calls
    used = {record.tool for record in calls}
    missing = [t for t in scenario.expect.should_use_tools if t not in used]
    unexpected = [t for t in scenario.expect.should_not_use_tools if t in used]
    final_text = run.turns[-1].final_text if run.turns else ""
    all_replies = "\n".join(turn.final_text for turn in run.turns)

    # A call is "repeated" only when an identical call already SUCCEEDED;
    # retrying identical args after an error is recovery, not a loop.
    repeated = 0
    seen_ok: set[str] = set()
    for record in calls:
        key = call_key(record.tool, record.args)
        if key in seen_ok:
            repeated += 1
        if record.ok:
            seen_ok.add(key)

    unrecovered = 0
    live_errors = 0
    for index, record in enumerate(calls):
        if record.ok:
            continue
        if record.source != "fault":
            live_errors += 1
        if not any(later.tool == record.tool and later.ok for later in calls[index + 1 :]):
            unrecovered += 1

    failed_checks = [
        pattern
        for pattern in scenario.expect.reply_must_match
        if not re.search(pattern, all_replies, re.IGNORECASE)
    ]
    budget = scenario.expect.max_tool_calls
    over_budget = budget > 0 and len(calls) > budget
    missing_attachment = scenario.expect.attaches_file and not any(
        turn.attached_files for turn in run.turns
    )
    expected = set(scenario.expect.should_use_tools)
    first_expected = next((i for i, record in enumerate(calls) if record.tool in expected), -1)

    score = 100.0
    score -= _PENALTY_MISSING_TOOL * len(missing)
    score -= _PENALTY_UNEXPECTED_TOOL * len(unexpected)
    score -= _PENALTY_TOOL_ERROR * live_errors
    score -= _PENALTY_UNRECOVERED_ERROR * unrecovered
    score -= _PENALTY_REPEATED_CALL * repeated
    score -= _PENALTY_FAILED_REPLY_CHECK * len(failed_checks)
    if over_budget:
        score -= _PENALTY_OVER_BUDGET
    if missing_attachment:
        score -= _PENALTY_MISSING_ATTACHMENT
    raw_json = _looks_like_raw_json(final_text)
    if raw_json:
        score -= _PENALTY_RAW_JSON

    return MechanicalResult(
        missing_tools=missing,
        unexpected_tools=unexpected,
        tool_call_count=len(calls),
        tool_errors=sum(1 for record in calls if not record.ok),
        tokens=run.total_tokens,
        latency_ms=run.total_latency_ms,
        raw_json_reply=raw_json,
        repeated_calls=repeated,
        unrecovered_errors=unrecovered,
        failed_reply_checks=failed_checks,
        over_budget=over_budget,
        missing_attachment=missing_attachment,
        first_expected_call_index=first_expected,
        score=round(max(score, 0.0), 1),
    )
