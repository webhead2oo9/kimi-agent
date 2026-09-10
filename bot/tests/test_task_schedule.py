from __future__ import annotations

import json
import time
from datetime import UTC, datetime

import pytest

from app.task_preview import render_preview, render_task_details
from app.task_schedule import interpret_schedule, native_time
from tests.test_scheduled_tasks import active_task, context, definition
from tests.test_task_controls import harness as harness, interaction
from tools.scheduled_tasks import TaskDefinition
from utils.schedules import Schedule


def timestamp(value):
    return datetime.fromisoformat(value).timestamp()


@pytest.mark.parametrize(
    "schedule,after,expected",
    [
        (
            {"kind": "once", "start": "2030-01-01T09:00:00+05:30", "timezone": "Asia/Kolkata"},
            "2029-12-31T00:00:00Z",
            ["2030-01-01T09:00:00+05:30"],
        ),
        (
            {
                "kind": "interval",
                "start": "2030-01-01T00:00:00Z",
                "timezone": "UTC",
                "interval_seconds": 3600,
            },
            "2030-01-01T01:00:01Z",
            ["2030-01-01T02:00:00+00:00", "2030-01-01T03:00:00+00:00", "2030-01-01T04:00:00+00:00"],
        ),
        (
            {"kind": "daily", "start": "2026-03-28T02:30:00+01:00", "timezone": "Europe/Berlin"},
            "2026-03-28T03:00:00+01:00",
            ["2026-03-30T02:30:00+02:00", "2026-03-31T02:30:00+02:00", "2026-04-01T02:30:00+02:00"],
        ),
        (
            {"kind": "weekdays", "start": "2030-01-04T09:00:00Z", "timezone": "UTC"},
            "2030-01-04T10:00:00Z",
            ["2030-01-07T09:00:00+00:00", "2030-01-08T09:00:00+00:00", "2030-01-09T09:00:00+00:00"],
        ),
        (
            {
                "kind": "weekly",
                "start": "2030-01-01T09:00:00Z",
                "timezone": "UTC",
                "weekdays": [0, 2],
            },
            "2030-01-01T00:00:00Z",
            ["2030-01-02T09:00:00+00:00", "2030-01-07T09:00:00+00:00", "2030-01-09T09:00:00+00:00"],
        ),
        (
            {
                "kind": "monthly",
                "start": "2030-01-31T09:00:00Z",
                "timezone": "UTC",
                "month_day": 31,
            },
            "2030-02-01T00:00:00Z",
            ["2030-03-31T09:00:00+00:00", "2030-05-31T09:00:00+00:00", "2030-07-31T09:00:00+00:00"],
        ),
    ],
)
def test_interpreted_schedule_matches_calendar_and_native_instants(schedule, after, expected):
    result = interpret_schedule(Schedule.model_validate(schedule), timestamp(after))
    assert [run["iso"] for run in result["next_runs"]] == expected
    assert result["timezone"] == schedule["timezone"]
    for run in result["next_runs"]:
        stamp = int(timestamp(run["iso"]))
        assert run["unix"] == stamp
        assert run["native_time"] == f"<t:{stamp}:F> · <t:{stamp}:R>"


def test_native_instants_remain_equal_across_viewer_timezones_and_fold_is_once():
    utc = Schedule(kind="once", start=datetime(2030, 1, 1, 12, tzinfo=UTC), timezone="UTC")
    berlin = utc.model_copy(update={"timezone": "Europe/Berlin"})
    assert (
        interpret_schedule(utc, 0)["next_runs"][0]["native_time"]
        == interpret_schedule(berlin, 0)["next_runs"][0]["native_time"]
    )
    schedule = Schedule(
        kind="daily",
        start=datetime.fromisoformat("2026-10-24T02:30:00+02:00"),
        timezone="Europe/Berlin",
    )
    result = interpret_schedule(schedule, timestamp("2026-10-24T03:00:00+02:00"))
    assert [row["iso"] for row in result["next_runs"]][:2] == [
        "2026-10-25T02:30:00+02:00",
        "2026-10-26T02:30:00+01:00",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("timezone", [None, "CST", "Invalid/Zone"])
async def test_new_drafts_and_validation_require_explicit_valid_timezone(harness, timezone):
    service, _, _ = harness
    candidate = definition()
    if timezone is None:
        del candidate["schedule"]["timezone"]
    else:
        candidate["schedule"]["timezone"] = timezone
    for arguments in (
        {"action": "draft", "definition": candidate},
        {"action": "validate_schedule", "schedule": candidate["schedule"]},
    ):
        response = json.loads(await service.manage(arguments, context()))
        assert "error" in response and "timezone" in response["error"]
    assert await service.r.store.list_tasks("100", "10") == []


@pytest.mark.asyncio
async def test_validation_draft_inspect_and_edit_setup_share_interpretation(harness):
    service, _, _ = harness
    candidate = definition()
    validation = await service.manager.dispatch(
        {"action": "validate_schedule", "schedule": candidate["schedule"]}, context()
    )
    drafted = await service.manager.dispatch(
        {"action": "draft", "definition": candidate}, context()
    )
    inspected = await service.manager.dispatch(
        {"action": "inspect", "task_id": drafted["task_id"]}, context()
    )
    setup = await service.manager.dispatch(
        {"action": "setup", "task_id": drafted["task_id"]}, context()
    )
    assert validation["next_runs"] == drafted["schedule_preview"]["next_runs"]
    assert inspected["schedule_preview"] == drafted["schedule_preview"]
    assert setup["current_task"]["schedule_preview"] == inspected["schedule_preview"]
    assert setup["server_timezone"] == "UTC"
    assert setup["current_task"]["definition"]["schedule"]["timezone"] == "Europe/Berlin"
    edited = await service.manager.dispatch(
        {
            "action": "draft",
            "task_id": drafted["task_id"],
            "expected_revision": 1,
            "definition": {**inspected["definition"], "name": "Renamed"},
        },
        context(),
    )
    assert edited["revision"] == 2 and edited["schedule_preview"] == drafted["schedule_preview"]


@pytest.mark.asyncio
async def test_legacy_stored_timezone_still_runs_and_renders_without_rewriting(
    harness, monkeypatch
):
    service, home, _ = harness
    legacy = definition()
    del legacy["schedule"]["timezone"]
    task_id = await service.r.store.draft(
        task_id=None,
        guild_id="100",
        owner_id="10",
        channel_id="200",
        proposer_id="10",
        definition=legacy,
    )
    await service.r.store.activate(task_id, 1, "10", 100, reset_state=False)
    observed = []

    async def execute(task, run_id, ctx, parsed, **kwargs):
        observed.append(parsed.schedule.timezone)
        await service.publisher.finish(task, run_id, "no_change", "Read", {}, [])

    monkeypatch.setattr(service.executor, "execute", execute)
    await service.r.store.lease(service.authority.token, time.time())
    await service.scheduler.run(task_id)
    assert observed == ["UTC"]
    current = await service.r.store.get(task_id)
    assert "timezone" not in current["definition"]["schedule"]
    inspected = await service.manager.dispatch({"action": "inspect", "task_id": task_id}, context())
    assert inspected["definition"]["schedule"]["timezone"] == "UTC"
    await service.approvals.previews.remember(task_id, 1, "200", "500")
    await service.approvals.reconcile()
    assert (
        "Schedule timezone: UTC"
        in home.get_partial_message.return_value.edit.call_args.kwargs["content"]
    )


def test_once_card_uses_native_time_and_download_uses_offset_iso():
    task = {"id": "task", "revision": 1, "guild_id": "100"}
    parsed = TaskDefinition.model_validate(
        definition(
            schedule={
                "kind": "once",
                "start": "2030-01-01T09:00:00+05:30",
                "timezone": "Asia/Kolkata",
            }
        )
    )
    rendered = render_preview(task, parsed, now=0)
    assert native_time(parsed.schedule.start.timestamp()) in rendered
    assert "2030-01-01" not in rendered
    exported = render_task_details(task, parsed)
    assert "2030-01-01T09:00:00+05:30" in exported and "<t:" not in exported


@pytest.mark.asyncio
async def test_management_instants_and_history_download_use_correct_formats(harness):
    service, _, _ = harness
    task = await active_task(service.r.store)
    run_id = await service.r.store.claim(task, 200)
    await service.r.store.finish(run_id, "no_change", "Checked", {}, [])
    task = await service.r.store.get(task["id"])
    history = await service.r.store.history(task["id"])
    for action in ("list", "inspect", "history"):
        event = interaction()
        await service.controls.handle(event, action, task_id=task["id"] if action != "list" else "")
        payload = event.sent[-1]
        rendered = json.dumps(payload["embed"].to_dict(), ensure_ascii=False)
        assert ":F>" in rendered and rendered.count(":F>") == rendered.count(":R>")
        if action == "history":
            assert "<t:" not in payload["file_text"]
            record = json.loads(payload["file_text"])["runs"][0]
            assert datetime.fromisoformat(record["created_at"]).timestamp() == pytest.approx(
                history[0]["created_at"], abs=0.000001, rel=0
            )
            assert record["finished_at"].endswith("+00:00")
        if action == "inspect":
            assert native_time(history[0]["created_at"]) in rendered
        if action == "list":
            assert native_time(task["next_run"]) in rendered
    response = await service.manager.dispatch(
        {"action": "history", "task_id": task["id"]}, context()
    )
    assert response["runs"][0]["native_times"]["created_at"] == native_time(
        history[0]["created_at"]
    )
