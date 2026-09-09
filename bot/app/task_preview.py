"""Compact, deterministic approval text; Discord renders local dates/countdowns."""

from __future__ import annotations

from zoneinfo import ZoneInfo
from typing import Any

from tools.scheduled_tasks import TaskDefinition


def clip(value: str, limit: int) -> str:
    value = " ".join(value.split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def schedule_label(definition: TaskDefinition) -> str:
    s = definition.schedule
    local = s.start.astimezone(ZoneInfo(s.timezone))
    clock = local.strftime("%H:%M")
    if s.kind == "interval":
        seconds = s.interval_seconds or 60
        if seconds % 3600 == 0:
            return f"Every {seconds // 3600} hour(s)"
        if seconds % 60 == 0:
            return f"Every {seconds // 60} minute(s)"
        return f"Every {seconds} seconds"
    if s.kind == "once":
        return f"Once, {local.strftime('%d %b %Y at %H:%M')}"
    if s.kind == "weekly":
        days = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
        return f"{', '.join(days[d] for d in sorted(set(s.weekdays)))} at {clock}"
    if s.kind == "monthly":
        return f"Monthly on day {s.month_day} at {clock}"
    return f"{'Weekdays' if s.kind == 'weekdays' else 'Daily'} at {clock}"


def native_time(timestamp: float) -> str:
    stamp = int(timestamp)
    return f"<t:{stamp}:F> · <t:{stamp}:R>"


def render_preview(
    task: dict[str, Any],
    definition: TaskDefinition,
    *,
    now: float,
    state: str = "pending",
    next_run: float | None = None,
) -> str:
    title = {
        "pending": "Task proposal",
        "activated": "Task activated",
        "denied": "Task denied",
        "superseded": "Superseded",
    }[state]
    if state == "activated":
        title = "Task " + task.get("task_status", "active").replace("attention", "needs attention")
        if task.get("active_revision", task["revision"]) != task["revision"]:
            title = "Approved task revision"
    lines = [
        f"**{title}: {clip(definition.name, 100)}**",
        f"Revision {task['revision']} · Task `{task.get('task_id', task.get('id'))}`",
    ]
    if state in {"denied", "superseded"}:
        lines.append(
            "This revision will not run. Any previously approved version is unchanged."
            if state == "denied"
            else "A newer draft replaced this preview. Use its approval message."
        )
        return "\n".join(lines)
    if state == "activated" and task.get("active_revision", task["revision"]) != task["revision"]:
        lines.append("This approved revision was replaced. Open Manage for the current task.")
        return "\n".join(lines)
    lines.extend(
        [
            f"Objective: {clip(definition.objective, 180)}",
            f"Schedule: {schedule_label(definition)}",
            f"Schedule timezone: {definition.schedule.timezone}",
            "Destination: " + ", ".join(f"<#{x}>" for x in definition.destinations),
            "Post when: " + clip(definition.condition or "Every successful run", 180),
        ]
    )
    times: list[float] = []
    if state == "activated":
        if next_run is not None and task.get("task_status", "active") == "active":
            times.append(next_run)
    else:
        after = now
        for _ in range(3):
            candidate = definition.schedule.next_after(after)
            if candidate is None:
                break
            times.append(candidate)
            after = candidate
    lines.append("Next run" + ("s" if len(times) > 1 else "") + " (your local time):")
    lines.extend(native_time(t) for t in times)
    if not times:
        lines.append(
            "None; update the schedule before approval."
            if state == "pending"
            else "None scheduled."
        )
    if state == "pending":
        lines.append(
            f"First check: {definition.first_check} · Missed runs: {definition.schedule.missed}"
        )
        recipients = [
            *(f"<@{x}>" for x in definition.mention_users),
            *(f"<@&{x}>" for x in definition.mention_roles),
        ]
        shown = ", ".join(recipients[:3]) or "none"
        if len(recipients) > 3:
            shown += f" (+{len(recipients) - 3} in task.json)"
        lines.append(
            f"Notify: {shown} · Log: "
            + (f"<#{definition.log_channel}>" if definition.log_channel else "internal history")
        )
        if definition.reset_state:
            lines.append("**Saved comparison state will be reset.**")
        lines.append(
            "Full instructions and settings are attached. Test preview privately before approving. "
            "Only the requester can test, approve, or reject."
        )
    else:
        lines.append("Open Manage for private controls and run history.")
    return "\n".join(lines)
