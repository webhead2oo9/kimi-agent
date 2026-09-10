from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from pydantic import ValidationError

import app.scheduled_tasks as scheduled_module
import app.task_python as python_module
import app.tools as app_tools
from app.task_controls import TaskControls
from app.task_preview import render_task_details
from app.task_python import TaskPythonRunner, _read_result, execution_slot, package_config
from sandbox.runner import SandboxConfig, SandboxResult, build_sandbox_command
from sandbox.runner import run_python_in_sandbox as real_run_python
from sandbox.runner import sandbox_available
from tests.sandbox_gate import sandbox_unavailable
from tests.test_scheduled_tasks import active_task, context, definition
from tests.test_task_controls import draft, harness as harness, interaction
from tools.scheduled_tasks import TaskDefinition
from tools.task_python import INPUT_STATE_KEY, TaskPythonResult, TaskPythonSpec
from tools.workspace.config import WorkspaceToolConfig
from tools.workspace.common import UserLocks, workspace_activity
from trust.tiers import TrustTier
from workspace import WorkspaceManager

NOW = datetime(2030, 1, 2, 12, tzinfo=UTC)
DISCORD_INPUT = {
    "name": "discussion",
    "kind": "discord",
    "channel_id": "250",
    "lookback_seconds": 3600,
}


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


@pytest_asyncio.fixture
async def python_harness(harness, tmp_path, monkeypatch):
    service, home, destination = harness
    tools = service.r.tools
    tools.task_python_sandbox_config = SandboxConfig()
    tools.code_exec_guards = SimpleNamespace(semaphore=asyncio.Semaphore(2))
    tools.workspace_manager = WorkspaceManager(base_dir=tmp_path / "workspaces")
    tools.workspace_config = WorkspaceToolConfig(max_attachments=10)
    for name in ("run_code", "get_channel_context", "fetch_url"):
        tools.registry.register(
            name=name,
            description="Test capability",
            parameters={"type": "object", "properties": {}},
            handler=AsyncMock(
                side_effect=AssertionError("Do not dispatch model tools from Python")
            ),
            min_tier=TrustTier.MEMBER,
            searchable=True,
        )
    service.r.gateway.collect_channel_history = AsyncMock(
        return_value={"messages": [], "has_more": False, "next_cursor": None}
    )
    box = SimpleNamespace(
        service=service,
        home=home,
        destination=destination,
        requests=[],
        files={},
        result={"outcome": "no_change", "state": {"observed": True}, "detail": "Checked"},
        execution=SandboxResult(0, "diagnostic only", "", False, 17),
        during=None,
    )

    async def process(config, root, script, **kwargs):
        box.requests.append(
            SimpleNamespace(
                config=config,
                root=root,
                source=script.read_text(),
                input=json.loads((root / "input.json").read_text()),
            )
        )
        for name, data in box.files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        if box.result is not None:
            (root / "result.json").write_text(
                box.result if isinstance(box.result, str) else json.dumps(box.result)
            )
        if box.during:
            await box.during(root)
        return box.execution

    box.process = AsyncMock(side_effect=process)
    monkeypatch.setattr(python_module, "run_python_in_sandbox", box.process)
    monkeypatch.setattr(python_module, "datetime", FixedDateTime)
    monkeypatch.setattr(
        scheduled_module,
        "run_conversation",
        AsyncMock(side_effect=AssertionError("Unexpected generation-model call")),
    )
    return box


async def run_task(box, *, mode="python_only", inputs=(), **changes):
    service = box.service
    task = await active_task(
        service.r.store,
        execution=mode,
        python={"code": "pass\n", "inputs": list(inputs)},
        **changes,
    )
    await service.r.store.lease(service._token, time.time())
    await service._run(task["id"])
    return await service.r.store.get(task["id"])


def test_existing_definitions_default_to_llm():
    old = definition()
    old.pop("execution")
    old.pop("python")
    parsed = TaskDefinition.model_validate(old)
    assert parsed.execution == "llm" and parsed.python is None
    with pytest.raises(ValidationError, match="require a script"):
        definition(execution="python_only")
    with pytest.raises(ValidationError, match="must not include"):
        definition(python={"code": "pass"})


@pytest.mark.parametrize(
    "changes",
    [
        {"code": "def broken("},
        {"code": "#" + "😀" * 30_000},
        {"inputs": [DISCORD_INPUT, DISCORD_INPUT]},
        {"inputs": [{**DISCORD_INPUT, "name": "../escape"}]},
        {"inputs": [{"name": "x", "kind": "https", "url": "http://example.org"}]},
        {"inputs": [{"name": "x", "kind": "https", "url": "https://127.0.0.1"}]},
        {"inputs": [{"name": "x", "kind": "https", "url": "https://u:p@example.org"}]},
    ],
)
def test_reject_invalid_scripts_and_inputs(changes):
    with pytest.raises(ValidationError):
        TaskPythonSpec.model_validate({"code": "pass", **changes})


@pytest.mark.parametrize(
    "changes",
    [
        {"outcome": "skip"},
        {"state": {"bad": float("nan")}},
        {"state": {"_task_initialized": True}},
        {"state": {"large": "x" * 64_000}},
        {"content": "A post during no_change"},
        {"llm_context": "A handoff during no_change"},
        {"outcome": "needs_input", "detail": " "},
        {"outcome": "completed"},
        {"outcome": "completed", "files": [{"path": "../outside.csv"}]},
        {"outcome": "completed", "files": [{"path": "/work/outputs/report.csv"}]},
        {"outcome": "completed", "files": [{"path": "outputs/../result.json"}]},
        {"outcome": "completed", "files": [{"path": "outputs/report.csv"}] * 11},
    ],
)
def test_result_contract_rejects_ambiguous_or_unbounded_outcomes(changes):
    with pytest.raises(ValidationError):
        TaskPythonResult.model_validate(
            {"outcome": "no_change", "state": {}, "detail": "Checked", **changes}
        )


@pytest.mark.parametrize("kind", ["fifo", "symlink", "oversize", "duplicate", "nan"])
def test_result_file_must_be_bounded_regular_unambiguous_json(tmp_path, kind):
    path = tmp_path / "result.json"
    if kind == "fifo":
        os.mkfifo(path)
    elif kind == "symlink":
        path.symlink_to(tmp_path / "other")
    elif kind == "oversize":
        path.write_bytes(b" " * (python_module.MAX_RESULT_BYTES + 1))
    elif kind == "duplicate":
        path.write_text('{"outcome":"no_change","outcome":"completed"}')
    else:
        path.write_text('{"outcome":"no_change","state":{"n":NaN},"detail":"x"}')
    with pytest.raises((OSError, ValueError)):
        _read_result(tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["python_only", "python_gate"])
async def test_no_change_never_constructs_model_conversation(python_harness, mode):
    box = python_harness
    task = await run_task(box, mode=mode)
    assert task["state"] == {
        "observed": True,
        "_task_initialized": True,
        INPUT_STATE_KEY: {},
    }
    history = await box.service.r.store.history(task["id"])
    assert history[0]["status"] == "no_change"
    assert "Python only (17 ms)" in history[0]["detail"]
    assert await box.service.r.store.deliveries() == []
    box.service.r.providers.resolve.assert_not_called()
    box.service.r.usage.record_turn.assert_not_awaited()
    assert not box.requests[0].root.exists()
    async with box.service.r.store.db.conn.execute("SELECT COUNT(*) FROM conversations") as cursor:
        assert (await cursor.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_python_files_are_frozen_and_retry_without_execution(python_harness):
    box = python_harness
    box.files = {"outputs/report.csv": b"author_id,count\n10,2\n"}
    box.result = {
        "outcome": "completed",
        "state": {"count": 2},
        "detail": "Counted messages",
        "files": [{"path": "outputs/report.csv", "description": "Counts"}],
    }
    task = await run_task(box)
    assert task["state"] == {}
    delivery = (await box.service.r.store.deliveries())[0]
    payload = json.loads(delivery["payload_json"])
    async with box.service.r.store.db.conn.execute(
        "SELECT data,filename FROM scheduled_task_files WHERE id=?", (payload["file_ids"][0],)
    ) as cursor:
        row = await cursor.fetchone()
        assert bytes(row[0]) == box.files["outputs/report.csv"]
        assert row[1] == "report.csv"
    assert not box.requests[0].root.exists()
    await box.service.r.store.delivery_status(delivery["id"], "failed", error="Unavailable")
    await box.service.r.store.attention(task["id"], delivery["run_id"])
    # Retrying frozen publication does not require a still-working Python environment.
    box.service.r.tools.task_python_sandbox_config = None
    response = json.loads(
        await box.service.manage({"action": "retry_delivery", "task_id": task["id"]}, context())
    )
    assert response["status"] == "retrying_saved_output"
    box.process.assert_awaited_once()
    box.service.r.providers.resolve.assert_not_called()
    await box.service.r.store.delivery_status(delivery["id"], "sent", message_id="999")
    assert (await box.service.r.store.get(task["id"]))["state"]["count"] == 2


@pytest.mark.asyncio
async def test_gate_handoff_uses_candidate_state_but_commits_only_final_outcome(
    python_harness, monkeypatch
):
    box = python_harness
    box.result = {
        "outcome": "invoke_llm",
        "state": {"candidate": "v2"},
        "detail": "New release",
        "llm_context": "Release v2 observations",
    }

    async def model(request):
        assert "v2" in request.user_message
        assert "observations" in request.user_message
        task_id = box.requests[0].input["task_id"]
        assert (await box.service.r.store.get(task_id))["state"] == {}
        request.scheduled_result.update(
            outcome="no_change",
            state={"verified": "v2", INPUT_STATE_KEY: {"forged": "bad"}},
            detail="Verified",
            content="",
        )
        return scheduled_module.ConversationRunResult(text="")

    call = AsyncMock(side_effect=model)
    monkeypatch.setattr(scheduled_module, "run_conversation", call)
    task = await asyncio.wait_for(run_task(box, mode="python_gate"), timeout=5)
    call.assert_awaited_once()
    assert task["state"]["verified"] == "v2"
    assert task["state"][INPUT_STATE_KEY] == {}
    assert "candidate" not in task["state"]
    assert "Python → LLM" in (await box.service.r.store.history(task["id"]))[0]["detail"]


@pytest.mark.asyncio
async def test_silent_first_gate_saves_baseline_without_model(python_harness):
    box = python_harness
    box.result = {
        "outcome": "invoke_llm",
        "state": {"release": "v1"},
        "detail": "Release found",
        "llm_context": "Release details",
    }
    task = await run_task(box, mode="python_gate", condition="New release only")
    assert task["state"]["release"] == "v1"
    assert "baseline" in (await box.service.r.store.history(task["id"]))[0]["detail"]
    box.service.r.providers.resolve.assert_not_called()


@pytest.mark.asyncio
async def test_python_only_rejects_handoff_even_for_silent_baseline(python_harness):
    box = python_harness
    box.result = {"outcome": "invoke_llm", "state": {"new": 1}, "detail": "Needs model"}
    task = await run_task(box, condition="Changed")
    assert task["status"] == "attention" and task["state"] == {}
    assert "cannot invoke" in (await box.service.r.store.history(task["id"]))[0]["detail"]
    box.service.r.providers.resolve.assert_not_called()


@pytest.mark.asyncio
async def test_needs_input_pauses_without_advancing_state(python_harness):
    box = python_harness
    box.result = {"outcome": "needs_input", "state": {"new": 1}, "detail": "Which threshold?"}
    task = await run_task(box)
    assert task["status"] == "attention" and task["state"] == {}
    assert (await box.service.r.store.history(task["id"]))[0]["status"] == "needs_input"
    assert all(row["is_log"] for row in await box.service.r.store.deliveries())


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exit", "timeout", "quota", "missing", "malformed"])
async def test_python_failure_preserves_state_and_never_falls_back(python_harness, failure):
    box = python_harness
    if failure in {"exit", "timeout", "quota"}:
        box.execution = SandboxResult(
            1 if failure == "exit" else 0,
            "",
            "Module missing or resource limit",
            failure == "timeout",
            12,
            quota_exceeded=failure == "quota",
        )
    else:
        box.result = None if failure == "missing" else "not-json"
    task = await run_task(box, mode="python_gate")
    assert task["status"] == "attention" and task["state"] == {}
    assert (await box.service.r.store.history(task["id"]))[0]["status"] == "failed"
    assert not box.requests[0].root.exists()
    box.service.r.providers.resolve.assert_not_called()


@pytest.mark.asyncio
async def test_discord_paging_uses_complete_half_open_window_and_commits_cursor(python_harness):
    box = python_harness
    start = NOW - timedelta(hours=1)
    message = {"id": "1", "timestamp": start.isoformat(), "content": "At boundary"}
    box.service.r.gateway.collect_channel_history.side_effect = [
        {"messages": [message], "has_more": True, "next_cursor": "1"},
        {
            "messages": [message, {"id": "2", "timestamp": NOW.isoformat(), "content": "Later"}],
            "has_more": False,
            "next_cursor": None,
        },
    ]
    captured = []

    async def read_input(root):
        captured.extend(json.loads((root / "inputs/discussion").read_text()))

    box.during = read_input
    task = await run_task(box, inputs=[DISCORD_INPUT])
    assert captured == [message]
    calls = box.service.r.gateway.collect_channel_history.await_args_list
    assert calls[0].args[1]["after"] == (start - timedelta(microseconds=1)).isoformat()
    assert calls[0].args[1]["before"] == calls[1].args[1]["before"] == NOW.isoformat()
    assert calls[1].args[1]["cursor"] == "1"
    assert task["state"][INPUT_STATE_KEY] == {"discussion": NOW.isoformat()}


@pytest.mark.asyncio
async def test_since_success_cursor_is_not_advanced_before_publication(python_harness):
    box = python_harness
    box.result = {"outcome": "completed", "content": "Observed", "detail": "Checked", "state": {}}
    task = await run_task(box, inputs=[DISCORD_INPUT])
    assert task["state"] == {}
    delivery = (await box.service.r.store.deliveries())[0]
    await box.service.r.store.delivery_status(delivery["id"], "sent", message_id="999")
    stored = await box.service.r.store.get(task["id"])
    assert stored["state"][INPUT_STATE_KEY]["discussion"] == NOW.isoformat()
    runner = TaskPythonRunner(box.service.r.tools, box.service.r.gateway)
    await runner.run(
        TaskDefinition.model_validate(stored["definition"]),
        context(),
        task_id=task["id"],
        revision=1,
        state=stored["state"],
        output_channel=box.destination,
        guard=AsyncMock(),
    )
    assert box.requests[-1].input["inputs"]["discussion"]["count"] == 0
    assert box.requests[-1].input["inputs"]["discussion"]["window_start"] == NOW.isoformat()
    assert INPUT_STATE_KEY not in box.requests[-1].input["state"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["pages", "bytes", "cursor", "permission"])
async def test_incomplete_inputs_fail_before_python(python_harness, monkeypatch, failure):
    box = python_harness
    if failure == "pages":
        monkeypatch.setattr(python_module, "MAX_HISTORY_PAGES", 0)
    elif failure == "bytes":
        monkeypatch.setattr(python_module, "MAX_INPUT_BYTES", 1)
    elif failure == "cursor":
        box.service.r.gateway.collect_channel_history.return_value = {
            "messages": [],
            "has_more": True,
            "next_cursor": None,
        }
    else:
        box.service.r.gateway.collect_channel_history.side_effect = ValueError(
            "Channel unavailable"
        )
    task = await run_task(box, inputs=[DISCORD_INPUT])
    assert task["state"] == {} and task["status"] == "attention"
    box.process.assert_not_awaited()
    box.service.r.providers.resolve.assert_not_called()


@pytest.mark.asyncio
async def test_public_input_uses_bounded_download_and_offline_script(python_harness, monkeypatch):
    box = python_harness

    async def fetch(url, path, **kwargs):
        assert url == "https://example.org/release.json"
        assert kwargs["max_bytes"] == python_module.MAX_INPUT_BYTES
        assert kwargs["timeout_seconds"] == 30
        path.write_bytes(b'{"id":"v2"}')
        return SimpleNamespace(size_bytes=11, content_type="application/json")

    fetcher = AsyncMock(side_effect=fetch)
    monkeypatch.setattr(python_module, "fetch_url_to_file", fetcher)
    await run_task(
        box,
        inputs=[{"kind": "https", "name": "release", "url": "https://example.org/release.json"}],
    )
    fetcher.assert_awaited_once()
    assert box.requests[0].config.network_mode == "none"
    assert box.requests[0].input["inputs"]["release"]["content_type"] == "application/json"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["redirect", "timeout"])
async def test_failed_public_input_is_not_no_change(python_harness, monkeypatch, failure):
    box = python_harness
    monkeypatch.setattr(
        python_module,
        "fetch_url_to_file",
        AsyncMock(
            side_effect=ValueError("Private redirect") if failure == "redirect" else TimeoutError()
        ),
    )
    task = await run_task(
        box, inputs=[{"kind": "https", "name": "x", "url": "https://example.org"}]
    )
    assert task["status"] == "attention"
    box.process.assert_not_awaited()


@pytest.mark.asyncio
async def test_revoked_source_after_python_prevents_publication(python_harness):
    box = python_harness
    box.result = {"outcome": "completed", "content": "Private data", "state": {}, "detail": "Read"}

    async def revoke(root):
        previous = box.service.r.access.channel.side_effect

        def channel(ctx, channel_id, **kwargs):
            if channel_id == "250":
                raise ValueError("Source permission revoked")
            return previous(ctx, channel_id, **kwargs)

        box.service.r.access.channel.side_effect = channel

    box.during = revoke
    task = await run_task(box, inputs=[DISCORD_INPUT])
    assert task["status"] == "attention" and task["state"] == {}
    assert all(row["is_log"] for row in await box.service.r.store.deliveries())


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["run_code", "get_channel_context", "fetch_url"])
async def test_python_respects_dispatch_denylist_including_preview(
    python_harness, monkeypatch, tool
):
    box = python_harness
    monkeypatch.setattr(scheduled_module, "load_blocked_tools", lambda *args: frozenset({tool}))
    source = (
        {"kind": "https", "name": "x", "url": "https://example.org"}
        if tool == "fetch_url"
        else DISCORD_INPUT
    )
    task_id = await draft(
        box.service, execution="python_only", python={"code": "pass", "inputs": [source]}
    )
    with pytest.raises(ValueError, match="current access"):
        await box.service.test_preview(context(), task_id, 1)
    box.process.assert_not_awaited()


@pytest.mark.asyncio
async def test_preview_returns_private_files_without_state_or_history(python_harness):
    box = python_harness
    box.result = {
        "outcome": "completed",
        "state": {"new": 1},
        "detail": "Built",
        "files": [{"path": "outputs/report.csv"}],
    }
    box.files = {"outputs/report.csv": b"count\n1\n"}
    task_id = await draft(box.service, execution="python_only", python={"code": "pass"})
    before = await box.service.r.store.get(task_id)
    result = await box.service.test_preview(context(), task_id, 1)
    assert result["files"] == [("report.csv", None, b"count\n1\n")]
    assert await box.service.r.store.get(task_id) == before
    assert await box.service.r.store.history(task_id) == []
    assert await box.service.r.store.deliveries() == []
    assert not box.requests[0].root.exists()
    box.destination.send.assert_not_awaited()
    shown = interaction()
    captured = []

    async def send(**kwargs):
        assert kwargs["ephemeral"] is True
        captured.extend((file.filename, file.fp.read()) for file in kwargs.get("files", []))

    shown.followup.send.side_effect = send
    await TaskControls(box.service)._test_result(shown, result)
    assert captured == [("report.csv", b"count\n1\n")]
    box.service.r.providers.resolve.assert_not_called()


@pytest.mark.asyncio
async def test_preview_rejects_revision_decided_during_python(python_harness):
    box = python_harness
    task_id = await draft(box.service, execution="python_only", python={"code": "pass"})

    async def reject(root):
        await box.service.r.store.reject(task_id, 1, "10")

    box.during = reject
    with pytest.raises(ValueError, match="decided or replaced"):
        await box.service.test_preview(context(), task_id, 1)
    assert not box.requests[0].root.exists()
    assert await box.service.r.store.history(task_id) == []


@pytest.mark.asyncio
async def test_cancelled_preview_cleans_scratch_and_does_not_commit(python_harness):
    box = python_harness
    task_id = await draft(box.service, execution="python_only", python={"code": "pass"})
    started = asyncio.Event()

    async def wait(root):
        started.set()
        await asyncio.Event().wait()

    box.during = wait
    preview = asyncio.create_task(box.service.test_preview(context(), task_id, 1))
    await asyncio.wait_for(started.wait(), 5)
    await box.service._cancel(task_id)
    assert preview.cancelled()
    assert not box.requests[0].root.exists()
    assert (await box.service.r.store.get(task_id))["state"] == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["code", "inputs", "mode", "schedule"])
async def test_script_edits_reset_state_but_schedule_edits_preserve_it(python_harness, change):
    box = python_harness
    task = await run_task(box)
    changed = dict(task["definition"])
    if change == "code":
        changed["python"] = {"code": "print('diagnostic')"}
    elif change == "inputs":
        changed["python"] = {"code": "pass\n", "inputs": [DISCORD_INPUT]}
    elif change == "mode":
        changed["execution"] = "python_gate"
    else:
        changed["schedule"] = {**changed["schedule"], "start": "2031-01-01T09:00:00+01:00"}
    response = json.loads(
        await box.service.manage(
            {
                "action": "draft",
                "task_id": task["id"],
                "expected_revision": 1,
                "definition": changed,
            },
            context(),
        )
    )
    assert "error" not in response
    updated = await box.service.r.store.get(task["id"])
    assert updated["definition"]["reset_state"] is (change != "schedule")
    details = render_task_details(updated, TaskDefinition.model_validate(updated["definition"]))
    assert "Python" in details and "task.py" in details


def test_owner_packages_are_mounted_read_only_without_normal_files(tmp_path):
    owner_files = tmp_path / "owner" / "files"
    venv = owner_files / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin/python3").write_text("interpreter")
    (venv / "pyvenv.cfg").write_text("home = /usr/bin")
    scratch = tmp_path / "job"
    scratch.mkdir()
    script = scratch / "task.py"
    script.write_text("pass")
    config = package_config(SandboxConfig(), owner_files)
    command = build_sandbox_command(config, scratch, script, seccomp_fd=7)
    assert command[command.index("--bind") + 1 : command.index("--bind") + 3] == [
        str(scratch),
        "/work",
    ]
    assert str(owner_files) not in command
    assert str(venv) in command
    index = command.index(str(venv))
    assert command[index - 1] == "--ro-bind-try"
    assert config.python_bin_override == str(venv / "bin/python3")
    assert "--share-net" not in command


def test_broken_or_symlinked_environment_does_not_fall_back(tmp_path):
    (tmp_path / ".venv").symlink_to(tmp_path / "missing")
    with pytest.raises(ValueError, match="repair it"):
        package_config(SandboxConfig(), tmp_path)


def test_networked_code_requires_separate_offline_probe(monkeypatch):
    probe = Mock(return_value=True)
    monkeypatch.setattr(app_tools, "sandbox_available", probe)
    config = SandboxConfig(network_mode="netns")
    offline = app_tools._task_python_sandbox_config(config)
    assert offline == replace(config, network_mode="none")
    probe.assert_called_once_with(offline)
    probe.return_value = False
    assert app_tools._task_python_sandbox_config(config) is None
    assert app_tools._task_python_sandbox_config(None) is None


@pytest.mark.asyncio
async def test_python_does_not_reserve_code_slot_while_waiting_for_llm_workspace():
    locks, semaphore, ctx = UserLocks(), asyncio.Semaphore(1), context()
    started = asyncio.Event()

    async def python():
        started.set()
        async with execution_slot(semaphore, locks, ctx):
            pass

    async with locks.activity(ctx.workspace_key):
        competitor = asyncio.create_task(python())
        await started.wait()
        # Simulate run_code inside a scheduled LLM's outer workspace ownership.
        async with asyncio.timeout(1), semaphore:
            async with workspace_activity(locks, replace(ctx, workspace_lock_held=True)):
                pass
    await asyncio.wait_for(competitor, 1)
    assert not semaphore.locked()


@pytest.mark.asyncio
async def test_python_releases_workspace_for_existing_semaphore_first_code_call():
    locks, semaphore, ctx = UserLocks(), asyncio.Semaphore(1), context()
    started = asyncio.Event()

    async def python():
        started.set()
        async with execution_slot(semaphore, locks, ctx):
            pass

    async with semaphore:
        competitor = asyncio.create_task(python())
        await started.wait()
        # Simulate an ordinary code call that reserved the code slot first.
        async with asyncio.timeout(1), workspace_activity(locks, ctx):
            pass
    await asyncio.wait_for(competitor, 1)
    assert not semaphore.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize("capacity", [1, 2])
@pytest.mark.parametrize("shared_workspace", [True, False])
async def test_multiple_python_waiters_make_progress(capacity, shared_workspace):
    locks, semaphore, ctx = UserLocks(), asyncio.Semaphore(capacity), context()
    completed = []
    active = 0

    async def python(index):
        nonlocal active
        task_ctx = ctx if shared_workspace else replace(ctx, user_id=str(1000 + index))
        async with execution_slot(semaphore, locks, task_ctx):
            active += 1
            assert active <= capacity
            await asyncio.sleep(0)
            completed.append(index)
            active -= 1

    for _ in range(capacity):
        await semaphore.acquire()
    competitors = [asyncio.create_task(python(index)) for index in range(4)]
    await asyncio.sleep(0)
    for _ in range(capacity):
        semaphore.release()
    await asyncio.wait_for(asyncio.gather(*competitors), 2)
    assert sorted(completed) == list(range(4))
    assert not semaphore.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["workspace", "semaphore", "executing"])
async def test_execution_slot_cancellation_releases_both_resources(phase):
    locks, semaphore, ctx = UserLocks(), asyncio.Semaphore(1), context()
    started = asyncio.Event()
    executing = asyncio.Event()

    async def competitor():
        started.set()
        async with execution_slot(semaphore, locks, ctx):
            executing.set()
            await asyncio.Event().wait()

    if phase == "workspace":
        async with locks.activity(ctx.workspace_key):
            task = asyncio.create_task(competitor())
            await started.wait()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    elif phase == "semaphore":
        async with semaphore:
            task = asyncio.create_task(competitor())
            await started.wait()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    else:
        task = asyncio.create_task(competitor())
        await executing.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    async with asyncio.timeout(1), execution_slot(semaphore, locks, ctx):
        pass
    assert not semaphore.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["file_symlink", "parent_symlink", "fifo", "size", "count"])
async def test_unsafe_or_oversized_files_fail_before_persistence(python_harness, tmp_path, kind):
    box = python_harness
    outside = tmp_path / "outside.csv"
    outside.write_text("outside")
    box.result = {
        "outcome": "completed",
        "state": {"new": 1},
        "detail": "Built",
        "files": [{"path": "outputs/report.csv"}],
    }

    async def prepare(root):
        path = root / "outputs/report.csv"
        if kind == "file_symlink":
            path.symlink_to(outside)
        elif kind == "parent_symlink":
            (root / "outputs").rmdir()
            (root / "outputs").symlink_to(tmp_path, target_is_directory=True)
        elif kind == "fifo":
            os.mkfifo(path)
        else:
            path.write_bytes(b"x" * 100)

    box.during = prepare
    if kind == "size":
        box.destination.guild.filesize_limit = 10
    elif kind == "count":
        box.service.r.tools.workspace_config = WorkspaceToolConfig(max_attachments=1)
        box.result["files"].append({"path": "outputs/second.csv"})
    task = await run_task(box)
    assert task["status"] == "attention" and task["state"] == {}
    assert outside.read_text() == "outside"
    assert not box.requests[0].root.exists()
    async with box.service.r.store.db.conn.execute(
        "SELECT COUNT(*) FROM scheduled_task_files"
    ) as cursor:
        assert (await cursor.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_approval_attaches_exact_python_revision(python_harness):
    box = python_harness
    code = "print('approved source')\n"
    task_id = await draft(box.service, execution="python_only", python={"code": code})
    files = {}

    async def send(content, **kwargs):
        for file in kwargs["files"]:
            files[file.filename] = file.fp.read().decode()
        assert "Python only" in content
        return SimpleNamespace(id=500, jump_url="https://discord.com/channels/100/200/500")

    box.home.send.side_effect = send
    message = SimpleNamespace(
        guild=SimpleNamespace(id=100), author=SimpleNamespace(id=10), channel=box.home
    )
    from tools.registry import TaskPreviewRequest

    notice = await box.service.deliver_preview(
        message, Mock(), 1, TaskPreviewRequest(task_id, 1, True), "unused"
    )
    assert "[Review task]" in notice
    assert files["task.py"] == code
    assert "task-details.md" in files and "SKILL.md" in files
    assert "task.json" not in files


@pytest.mark.asyncio
async def test_live_python_reuses_packages_offline_and_keeps_workspace_private(
    python_harness, monkeypatch
):
    box = python_harness
    config = box.service.r.tools.task_python_sandbox_config
    if not await asyncio.to_thread(sandbox_available, config):
        sandbox_unavailable("the scheduled Python offline sandbox cannot start on this host")
    monkeypatch.setattr(python_module, "run_python_in_sandbox", real_run_python)
    owner_files = box.service.r.tools.workspace_manager.user_files_dir(context().workspace_key)
    venv = owner_files / ".venv"

    def environment() -> Path:
        subprocess.run(
            [config.python_bin, "-m", "venv", "--copies", "--without-pip", str(venv)],
            capture_output=True,
            check=True,
            timeout=30,
        )
        packages = next((venv / "lib").glob("python*/site-packages"))
        module = packages / "scheduled_fixture_package.py"
        module.write_text('VALUE = "installed package"\n')
        return module

    module = await asyncio.to_thread(environment)
    secret = owner_files / "private.txt"
    secret.write_text("not mounted")
    code = "\n".join(
        [
            "import json, socket",
            "from pathlib import Path",
            "import scheduled_fixture_package as package",
            "assert package.VALUE == 'installed package'",
            f"assert not Path({str(secret)!r}).exists()",
            "try:",
            "    Path(package.__file__).write_text('changed')",
            "except OSError:",
            "    pass",
            "else:",
            "    raise AssertionError('package mount is writable')",
            "with socket.socket() as sock:",
            "    sock.settimeout(0.2)",
            "    try:",
            "        sock.connect(('1.1.1.1', 80))",
            "    except OSError:",
            "        pass",
            "    else:",
            "        raise AssertionError('network is available')",
            "Path('outputs/result.csv').write_text(package.VALUE)",
            "Path('result.json').write_text(json.dumps({'outcome': 'completed', 'state': {},",
            "    'detail': 'Validated', 'files': [{'path': 'outputs/result.csv'}]}))",
        ]
    )
    result = await TaskPythonRunner(box.service.r.tools, box.service.r.gateway).run(
        TaskDefinition.model_validate(definition(execution="python_only", python={"code": code})),
        context(),
        task_id="live",
        revision=1,
        state={},
        output_channel=box.destination,
        guard=AsyncMock(),
    )
    assert result.files == [("result.csv", None, b"installed package")]
    assert module.read_text() == 'VALUE = "installed package"\n'
    assert secret.read_text() == "not mounted"
    assert not list(owner_files.parent.glob("jobs/*"))
