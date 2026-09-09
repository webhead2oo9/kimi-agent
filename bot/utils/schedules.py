"""Calendar schedules with explicit timezone and DST behavior."""

from __future__ import annotations

import math
from datetime import UTC, datetime, time, timedelta
from typing import Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator


class Schedule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["once", "interval", "daily", "weekdays", "weekly", "monthly"]
    start: datetime
    timezone: str = "UTC"
    interval_seconds: StrictInt | None = Field(default=None, ge=60)
    weekdays: list[StrictInt] = Field(default_factory=list)
    month_day: StrictInt | None = Field(default=None, ge=1, le=31)
    missed: Literal["catch_up", "skip"] = "catch_up"

    @model_validator(mode="after")
    def validate_schedule(self) -> Self:
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
        if self.start.tzinfo is None:
            raise ValueError("start must include a UTC offset")
        if self.kind == "interval" and self.interval_seconds is None:
            raise ValueError("interval_seconds is required")
        if self.kind == "weekly" and not self.weekdays:
            raise ValueError("weekly schedules need weekdays (Monday=0)")
        if any(isinstance(day, bool) or day not in range(7) for day in self.weekdays):
            raise ValueError("weekdays must be integers between 0 and 6")
        if self.kind == "monthly" and self.month_day is None:
            raise ValueError("monthly schedules need month_day")
        return self

    def next_after(self, timestamp: float) -> float | None:
        """Return the first occurrence strictly after timestamp, at/after start."""
        start = self.start.timestamp()
        if self.kind == "once":
            return start if start > timestamp else None
        if self.kind == "interval":
            assert self.interval_seconds is not None
            count = max(0, math.floor((timestamp - start) / self.interval_seconds) + 1)
            return start + count * self.interval_seconds
        zone = ZoneInfo(self.timezone)
        local_start = self.start.astimezone(zone)
        day = datetime.fromtimestamp(max(timestamp, start), zone).date()
        clock = time(local_start.hour, local_start.minute, local_start.second)
        for offset in range(370):
            date = day + timedelta(days=offset)
            if self.kind == "weekdays" and date.weekday() >= 5:
                continue
            if self.kind == "weekly" and date.weekday() not in self.weekdays:
                continue
            if self.kind == "monthly" and date.day != self.month_day:
                continue
            local = datetime.combine(date, clock, zone).replace(fold=0)
            # Round trips distinguish imaginary local times from real ones.
            if local.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != local.replace(
                tzinfo=None
            ):
                continue
            candidate = local.timestamp()
            if candidate > timestamp and candidate >= start:
                return candidate
        raise ValueError("No calendar occurrence found in the next year")

    def preview(self, after: float, count: int = 3) -> list[str]:
        result: list[str] = []
        for _ in range(count):
            next_time = self.next_after(after)
            if next_time is None:
                break
            result.append(datetime.fromtimestamp(next_time, ZoneInfo(self.timezone)).isoformat())
            after = next_time
        return result
