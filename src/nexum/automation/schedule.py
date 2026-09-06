"""When should a scheduled rule fire next?

Supported ``trigger_config`` shapes::

    {"kind": "interval", "minutes": 15}
    {"kind": "hourly", "minute": 0}
    {"kind": "daily", "at": "07:00"}
    {"kind": "weekly", "weekday": 0, "at": "06:00"}      # 0 = Monday
    {"kind": "monthly", "day": 1, "at": "06:00"}         # day clamped to month length

Wall-clock rules are evaluated in the company time zone (``nexum.services.calendar``).
"""

from __future__ import annotations

import calendar
from datetime import datetime, time, timedelta
from typing import Any

from nexum.errors import ValidationError
from nexum.services.calendar import timezone_name, to_local

KINDS: tuple[str, ...] = ("interval", "hourly", "daily", "weekly", "monthly")


def _parse_time(value: Any, default: str = "00:00") -> time:
    raw = str(value or default)
    try:
        hour, minute = raw.split(":")
        return time(int(hour), int(minute))
    except ValueError as exc:
        raise ValidationError(f"Invalid time '{raw}', expected HH:MM") from exc


def validate(config: dict[str, Any]) -> dict[str, Any]:
    kind = config.get("kind")
    if kind not in KINDS:
        raise ValidationError(f"Unknown schedule kind {kind!r}; expected one of {KINDS}")
    if kind == "interval":
        minutes = config.get("minutes")
        if not isinstance(minutes, int | float) or minutes < 1:
            raise ValidationError("interval schedules need 'minutes' >= 1")
    if kind == "hourly":
        minute = config.get("minute", 0)
        if not isinstance(minute, int) or not 0 <= minute <= 59:
            raise ValidationError("hourly schedules need 'minute' in 0..59")
    if kind in ("daily", "weekly", "monthly"):
        _parse_time(config.get("at"))
    if kind == "weekly":
        weekday = config.get("weekday", 0)
        if not isinstance(weekday, int) or not 0 <= weekday <= 6:
            raise ValidationError("weekly schedules need 'weekday' in 0..6")
    if kind == "monthly":
        day = config.get("day", 1)
        if not isinstance(day, int) or not 1 <= day <= 31:
            raise ValidationError("monthly schedules need 'day' in 1..31")
    return config


def next_run(config: dict[str, Any], after: datetime) -> datetime:
    """First fire time strictly after ``after``."""
    kind = config.get("kind")
    if kind == "interval":
        return after + timedelta(minutes=float(config.get("minutes", 60)))
    after = to_local(after)
    if kind == "hourly":
        minute = int(config.get("minute", 0))
        candidate = after.replace(minute=minute, second=0, microsecond=0)
        if candidate <= after:
            candidate += timedelta(hours=1)
        return candidate
    at = _parse_time(config.get("at"))
    if kind == "daily":
        candidate = datetime.combine(after.date(), at, tzinfo=after.tzinfo)
        if candidate <= after:
            candidate += timedelta(days=1)
        return candidate
    if kind == "weekly":
        weekday = int(config.get("weekday", 0))
        days_ahead = (weekday - after.weekday()) % 7
        candidate = datetime.combine(
            after.date() + timedelta(days=days_ahead), at, tzinfo=after.tzinfo
        )
        if candidate <= after:
            candidate += timedelta(days=7)
        return candidate
    if kind == "monthly":
        day = int(config.get("day", 1))
        year, month = after.year, after.month
        for _ in range(3):
            last = calendar.monthrange(year, month)[1]
            candidate = datetime.combine(
                after.date().replace(year=year, month=month, day=min(day, last)),
                at,
                tzinfo=after.tzinfo,
            )
            if candidate > after:
                return candidate
            month += 1
            if month > 12:
                month, year = 1, year + 1
    raise ValidationError(f"Unknown schedule kind {kind!r}")


def describe(config: dict[str, Any]) -> str:
    kind = config.get("kind")
    if kind == "interval":
        return f"every {config.get('minutes', 60)} min"
    if kind == "hourly":
        return f"hourly at :{int(config.get('minute', 0)):02d}"
    if kind == "daily":
        return f"daily at {config.get('at', '00:00')} {timezone_name()}"
    if kind == "weekly":
        names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        return (
            f"weekly on {names[int(config.get('weekday', 0))]} "
            f"at {config.get('at', '00:00')} {timezone_name()}"
        )
    if kind == "monthly":
        return (
            f"monthly on day {config.get('day', 1)} at {config.get('at', '00:00')} "
            f"{timezone_name()}"
        )
    return str(config)
