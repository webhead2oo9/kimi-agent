import asyncio
import json

from agent.compaction import CompactionConfig, Compactor
from evals.capture import InstrumentedProvider, InstrumentedRegistry
from evals.harness import ScenarioRun, run_scenario_for_model
from evals.identity import EvalIdentity
from evals.scenario import Expect, Scenario, TurnSpec
from evals.stub_gateway import StubGateway
from providers.base import LLMProvider
from providers.image_caption import format_image_caption, is_image_caption
from providers.types import (
    ContentPart,
    ContentPartType,
    ProviderRequest,
    ProviderResponse,
    ToolCall,
)
from trust.tiers import TrustTier


class _Scripted(LLMProvider):
    model = "m"

    def __init__(self, responses):
        self._responses = list(responses)
        self.requests: list[ProviderRequest] = []

    async def run_turn(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        return self._responses.pop(0)


def test_scenario_run_preserves_original_positional_field_order():
    turns = []
    run = ScenarioRun("scenario", "model", turns, 123)

    assert run.turns is turns
    assert run.wall_time_ms == 123
    assert run.identity is None


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


def test_run_scenario_seeds_workspace_files_outside_model_tool_trace():
    writes: list[tuple[dict, str]] = []

    async def write_file(args, ctx):
        writes.append((args, ctx.context_key))
        return json.dumps({"path": args["path"], "written": True})

    registry = InstrumentedRegistry()
    registry.register(
        name="write_file",
        description="d",
        parameters={"type": "object", "properties": {}},
        handler=write_file,
    )
    scenario = Scenario(
        id="seeded-workspace",
        category="coding",
        trust_tier=TrustTier.MEMBER,
        turns=["fix it"],
        workspace_files=(("notes.md", "teh first line\n"),),
    )
    identity = EvalIdentity("run", "candidate", scenario.id, 0)

    run = asyncio.run(
        run_scenario_for_model(
            scenario,
            provider=InstrumentedProvider(_Scripted([ProviderResponse(content="done")])),
            registry=registry,
            gateway=StubGateway(),
            memory_client=None,
            preference_store=None,
            identity=identity,
        )
    )

    assert writes == [
        (
            {"path": "notes.md", "content": "teh first line\n", "attach": False},
            identity.context_key,
        )
    ]
    assert run.all_tool_calls == []


def test_run_scenario_captures_max_iteration_termination():
    async def probe(args, ctx):
        return json.dumps({"ok": True})

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
                    tool_calls=[ToolCall(id=str(index), name="probe", arguments={})],
                    finish_reason="tool_calls",
                )
                for index in range(10)
            ]
        )
    )
    scenario = Scenario(
        id="exhausted",
        category="tooling",
        trust_tier=TrustTier.MEMBER,
        turns=["keep probing"],
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

    assert run.turns[0].termination_reason == "max_iterations"
    assert run.turns[0].provider_calls == 10


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


def test_run_scenario_uses_one_identity_context_key_per_multi_turn_run():
    observed_context_keys: list[str] = []

    async def probe(args, ctx):
        observed_context_keys.append(ctx.context_key)
        return json.dumps({"context_key": ctx.context_key})

    registry = InstrumentedRegistry()
    registry.register(
        name="probe",
        description="d",
        parameters={"type": "object", "properties": {}},
        handler=probe,
    )
    scenario = Scenario(
        id="context-isolation",
        category="tooling",
        trust_tier=TrustTier.MEMBER,
        turns=["first", "second"],
    )
    first = EvalIdentity("run", "candidate", scenario.id, 0)
    second = EvalIdentity("run", "candidate", scenario.id, 1)

    async def exercise() -> None:
        first_provider = InstrumentedProvider(
            _Scripted(
                [
                    ProviderResponse(
                        tool_calls=[ToolCall(id="1", name="probe", arguments={})],
                        finish_reason="tool_calls",
                    ),
                    ProviderResponse(content="first done"),
                    ProviderResponse(
                        tool_calls=[ToolCall(id="2", name="probe", arguments={})],
                        finish_reason="tool_calls",
                    ),
                    ProviderResponse(content="second done"),
                ]
            )
        )
        await run_scenario_for_model(
            scenario,
            provider=first_provider,
            registry=registry,
            gateway=StubGateway(),
            memory_client=None,
            preference_store=None,
            identity=first,
        )
        await run_scenario_for_model(
            Scenario(
                id=scenario.id,
                category=scenario.category,
                trust_tier=scenario.trust_tier,
                turns=[TurnSpec(text="other repetition")],
            ),
            provider=InstrumentedProvider(
                _Scripted(
                    [
                        ProviderResponse(
                            tool_calls=[ToolCall(id="3", name="probe", arguments={})],
                            finish_reason="tool_calls",
                        ),
                        ProviderResponse(content="other done"),
                    ]
                )
            ),
            registry=registry,
            gateway=StubGateway(),
            memory_client=None,
            preference_store=None,
            identity=second,
        )

    asyncio.run(exercise())
    assert observed_context_keys == [first.context_key, first.context_key, second.context_key]
    assert first.context_key != second.context_key


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


def test_run_scenario_uses_caption_instead_of_either_image_rail():
    scripted = _Scripted([ProviderResponse(content="described")])
    scenario = Scenario(
        id="captioned",
        category="vision",
        trust_tier=TrustTier.MEMBER,
        turns=[
            TurnSpec(
                text="compare them",
                images=("checker-yellow.png",),
                reply_images=("bands-rgb.png",),
                reply_author="Ana",
                reply_text="my pattern",
            )
        ],
    )
    caption = ContentPart.from_text(
        format_image_caption("Image 1: checkerboard. Image 2: colored bands.")
    )

    asyncio.run(
        run_scenario_for_model(
            scenario,
            provider=InstrumentedProvider(scripted),
            registry=InstrumentedRegistry(),
            gateway=StubGateway(),
            memory_client=None,
            preference_store=None,
            image_captions={0: caption},
        )
    )

    [request] = scripted.requests
    all_parts = [
        *request.current_user_parts,
        *(part for message in request.messages for part in message.content),
        *(part for message in request.continuation_context_messages for part in message.content),
    ]
    assert all(part.type is not ContentPartType.IMAGE for part in all_parts)
    assert any(is_image_caption(part.text or "") for part in request.current_user_parts)


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
