"""Application-calculated schedule descriptions and Discord timestamps."""

from __future__ import annotations

from datetime import datetime
from typing import TypedDict
from zoneinfo import ZoneInfo

from utils.schedules import Schedule


class RunTime(TypedDict):
    unix: float
    iso: str
    native_time: str


class ScheduleInterpretation(TypedDict):
    recurrence: str
    timezone: str
    start: RunTime
    next_runs: list[RunTime]


def native_time(timestamp: float) -> str:
    stamp = int(timestamp)
    return f"<t:{stamp}:F> · <t:{stamp}:R>"


def schedule_label(schedule: Schedule, *, exported: bool = False) -> str:
    local = schedule.start.astimezone(ZoneInfo(schedule.timezone))
    clock = local.strftime("%H:%M:%S" if local.second else "%H:%M")
    if schedule.kind == "interval":
        seconds = schedule.interval_seconds or 60
        if seconds % 3600 == 0:
            return f"Every {seconds // 3600} hour(s)"
        if seconds % 60 == 0:
            return f"Every {seconds // 60} minute(s)"
        return f"Every {seconds} seconds"
    if schedule.kind == "once":
        start = local.isoformat() if exported else native_time(local.timestamp())
        return f"Once, {start}"
    if schedule.kind == "weekly":
        days = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
        return f"{', '.join(days[d] for d in sorted(set(schedule.weekdays)))} at {clock}"
    if schedule.kind == "monthly":
        return f"Monthly on day {schedule.month_day} at {clock}"
    return f"{'Weekdays' if schedule.kind == 'weekdays' else 'Daily'} at {clock}"


def interpret_schedule(schedule: Schedule, now: float) -> ScheduleInterpretation:
    zone = ZoneInfo(schedule.timezone)

    def run_time(timestamp: float) -> RunTime:
        return {
            "unix": timestamp,
            "iso": datetime.fromtimestamp(timestamp, zone).isoformat(),
            "native_time": native_time(timestamp),
        }

    next_runs: list[RunTime] = []
    for _ in range(3):
        candidate = schedule.next_after(now)
        if candidate is None:
            break
        next_runs.append(run_time(candidate))
        now = candidate
    return {
        "recurrence": schedule_label(schedule),
        "timezone": schedule.timezone,
        "start": run_time(schedule.start.timestamp()),
        "next_runs": next_runs,
    }
