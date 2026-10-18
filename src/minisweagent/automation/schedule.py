"""Minimal cron/interval scheduling for automation tasks."""

from __future__ import annotations

from datetime import datetime

_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_interval(value: str | int | float) -> float:
    if isinstance(value, (int, float)):
        return float(value) * 60  # bare numbers are minutes
    text = str(value).strip().lower()
    if not text:
        raise ValueError("empty interval")
    suffix = text[-1]
    if suffix in _UNIT_SECONDS:
        amount = float(text[:-1])
        return amount * _UNIT_SECONDS[suffix]
    return float(text) * 60


def _field_matches(field: str, value: int, *, sunday_seven: bool = False) -> bool:
    for part in field.split(","):
        part = part.strip()
        if not part:
            continue
        if part == "*":
            return True
        if part.startswith("*/"):
            step = int(part[2:])
            if step > 0 and value % step == 0:
                return True
            continue
        if "-" in part:
            start, end = part.split("-", 1)
            start_i, end_i = int(start), int(end)
            if sunday_seven:
                value_cmp = 0 if value == 7 else value
                if start_i <= value_cmp <= end_i:
                    return True
            elif start_i <= value <= end_i:
                return True
            continue
        target = int(part)
        if sunday_seven and target == 7:
            target = 0
        if target == value:
            return True
    return False


def cron_matches(expression: str, moment: datetime) -> bool:
    """Match a 5-field cron expression (minute hour day-of-month month day-of-week)."""
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError(f"cron expression must have 5 fields, got {len(fields)}: {expression!r}")
    minute, hour, day, month, weekday = fields
    cron_weekday = (moment.weekday() + 1) % 7  # cron: Sunday=0, Monday=1, ...
    return (
        _field_matches(minute, moment.minute)
        and _field_matches(hour, moment.hour)
        and _field_matches(day, moment.day)
        and _field_matches(month, moment.month)
        and _field_matches(weekday, cron_weekday, sunday_seven=True)
    )


def is_due(
    *,
    schedule: str | None,
    interval_seconds: float | None,
    last_run: datetime | None,
    now: datetime,
) -> bool:
    if interval_seconds is not None:
        return last_run is None or (now - last_run).total_seconds() >= interval_seconds
    if schedule:
        minute_start = now.replace(second=0, microsecond=0)
        return cron_matches(schedule, now) and (last_run is None or last_run < minute_start)
    return False
