import asyncio
import json

from agent.compaction import CompactionConfig, Compactor
from evals.capture import InstrumentedProvider, InstrumentedRegistry
from evals.harness import run_scenario_for_model
from evals.scenario import Expect, Scenario
from evals.stub_gateway import StubGateway
from providers.base import LLMProvider
from providers.types import ProviderRequest, ProviderResponse, ToolCall
from trust.tiers import TrustTier


class _Scripted(LLMProvider):
    model = "m"

    def __init__(self, responses):
        self._responses = list(responses)
        self.requests: list[ProviderRequest] = []

    async def run_turn(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        return self._responses.pop(0)


def test_run_scenario_captures_reply_and_tool_trace():
    async def probe(args, ctx):
        return json.dumps({"value": 42})

    registry = InstrumentedRegistry()
    registry.register(
        name="probe",
        description="d",
        parameters={"type": "object", "properties": {}},
        handler=probe,
    )
    provider = InstrumentedProvider(
        _Scripted(
            [
                ProviderResponse(
                    tool_calls=[ToolCall(id="1", name="probe", arguments={})],
                    finish_reason="tool_calls",
                ),
                ProviderResponse(content="the answer is 42"),
            ]
        )
    )
    scenario = Scenario(
        id="probe-it",
        category="tooling",
        trust_tier=TrustTier.MEMBER,
        turns=["compute it"],
        expect=Expect(should_use_tools=["probe"]),
    )

    run = asyncio.run(
        run_scenario_for_model(
            scenario,
            provider=provider,
            registry=registry,
            gateway=StubGateway(),
            memory_client=None,
            preference_store=None,
        )
    )
    assert run.turns[-1].final_text == "the answer is 42"
    assert "probe" in {record.tool for record in run.turns[-1].tool_calls}
    assert run.total_tokens >= 0
    assert provider._inner.requests[0].max_tokens == 65_536


def test_run_scenario_multi_turn_accumulates_records_and_carries_context():
    scripted = _Scripted(
        [ProviderResponse(content="first reply"), ProviderResponse(content="second reply")]
    )
    provider = InstrumentedProvider(scripted)
    scenario = Scenario(
        id="multi",
        category="persona",
        trust_tier=TrustTier.MEMBER,
        turns=["hello", "and again"],
    )

    run = asyncio.run(
        run_scenario_for_model(
            scenario,
            provider=provider,
            registry=InstrumentedRegistry(),
            gateway=StubGateway(),
            memory_client=None,
            preference_store=None,
        )
    )
    assert len(run.turns) == 2
    assert [turn.final_text for turn in run.turns] == ["first reply", "second reply"]
    # One reused context: turn 2's request carries turn 1's exchange, so it has more
    # history messages than turn 1's request.
    assert len(scripted.requests[1].messages) > len(scripted.requests[0].messages)


def test_run_scenario_threads_bot_name_into_system_prompt():
    # Production parity: the persona opens "You are <bot_name>, ..."; an eval run
    # must not render the degraded "You are , ..." prompt.
    scripted = _Scripted([ProviderResponse(content="hi")])
    scenario = Scenario(
        id="named",
        category="persona",
        trust_tier=TrustTier.MEMBER,
        turns=["hello"],
    )

    asyncio.run(
        run_scenario_for_model(
            scenario,
            provider=InstrumentedProvider(scripted),
            registry=InstrumentedRegistry(),
            gateway=StubGateway(),
            memory_client=None,
            preference_store=None,
            bot_name="EvalBotName",
        )
    )
    assert "EvalBotName" in scripted.requests[0].system_prompt


def test_run_scenario_threads_compactor_into_run_conversation():
    # A compactor with a tiny per-iteration tool-output budget must clamp the tool
    # result the model sees on the next request, proving the compactor reaches
    # run_conversation instead of the loop running with active_compactor=None.
    async def probe(args, ctx):
        return "x" * 10_000

    registry = InstrumentedRegistry()
    registry.register(
        name="probe",
        description="d",
        parameters={"type": "object", "properties": {}},
        handler=probe,
    )
    scripted = _Scripted(
        [
            ProviderResponse(
                tool_calls=[ToolCall(id="1", name="probe", arguments={})],
                finish_reason="tool_calls",
            ),
            ProviderResponse(content="done"),
        ]
    )
    compactor = Compactor(
        CompactionConfig(max_iteration_tool_output_tokens=1),
        provider=_Scripted([]),
    )
    scenario = Scenario(
        id="clamped",
        category="tooling",
        trust_tier=TrustTier.MEMBER,
        turns=["go"],
    )

    asyncio.run(
        run_scenario_for_model(
            scenario,
            provider=InstrumentedProvider(scripted),
            registry=registry,
            gateway=StubGateway(),
            memory_client=None,
            preference_store=None,
            compactor=compactor,
        )
    )
    tool_messages = [m for m in scripted.requests[1].messages if m.role == "tool"]
    assert tool_messages, "second request should carry the tool result"
    body = "".join(part.text or "" for part in tool_messages[0].content)
    assert "truncated (per-iteration budget)" in body
    assert "x" * 10_000 not in body
