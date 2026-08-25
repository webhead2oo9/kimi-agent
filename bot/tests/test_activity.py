from __future__ import annotations

import pytest

from agent.activity import emit_narration_step, emit_plan_update, tool_display_label


def test_unknown_tool_falls_back_to_title_case() -> None:
    assert tool_display_label("my_custom_skill") == "My Custom Skill"


@pytest.mark.asyncio
async def test_emit_narration_step_noops_for_plain_callable() -> None:
    calls: list[object] = []

    async def reporter(update: object) -> None:
        calls.append(update)

    await emit_narration_step(reporter, "narr", ["browse_tools"])

    assert calls == []


@pytest.mark.asyncio
async def test_emit_narration_step_calls_commit_step() -> None:
    recorded: list[tuple[str, list[str]]] = []

    class Reporter:
        async def __call__(self, update: object) -> None: ...

        async def commit_step(self, narration: str, tool_names: list[str]) -> None:
            recorded.append((narration, tool_names))

    await emit_narration_step(Reporter(), "narr", ["t1", "t2"])

    assert recorded == [("narr", ["t1", "t2"])]


@pytest.mark.asyncio
async def test_emit_narration_step_swallows_commit_errors() -> None:
    class Reporter:
        async def __call__(self, update: object) -> None: ...

        async def commit_step(self, narration: str, tool_names: list[str]) -> None:
            raise RuntimeError("boom")

    await emit_narration_step(Reporter(), "narr", ["t1"])


@pytest.mark.asyncio
async def test_emit_narration_step_handles_none_reporter() -> None:
    await emit_narration_step(None, "narr", ["t1"])


@pytest.mark.asyncio
async def test_emit_plan_update_noops_for_plain_callable() -> None:
    calls: list[object] = []

    async def reporter(update: object) -> None:
        calls.append(update)

    await emit_plan_update(reporter, [{"content": "a", "status": "pending"}])

    assert calls == []


@pytest.mark.asyncio
async def test_emit_plan_update_calls_update_plan_with_copies() -> None:
    recorded: list[list[dict[str, str]]] = []

    class Reporter:
        async def __call__(self, update: object) -> None: ...

        async def update_plan(self, steps: list[dict[str, str]]) -> None:
            recorded.append(steps)

    steps = [{"content": "a", "status": "pending"}]
    await emit_plan_update(Reporter(), steps)

    assert recorded == [steps]
    assert recorded[0] is not steps
    assert recorded[0][0] is not steps[0]


@pytest.mark.asyncio
async def test_emit_plan_update_swallows_errors() -> None:
    class Reporter:
        async def __call__(self, update: object) -> None: ...

        async def update_plan(self, steps: list[dict[str, str]]) -> None:
            raise RuntimeError("boom")

    await emit_plan_update(Reporter(), [{"content": "a", "status": "pending"}])


@pytest.mark.asyncio
async def test_emit_plan_update_handles_none_reporter() -> None:
    await emit_plan_update(None, [{"content": "a", "status": "pending"}])
