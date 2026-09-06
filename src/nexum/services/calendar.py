"""Date/time helpers in the *company* time zone.

The database stores UTC. Everything that means "a day", "a week" or "08:00" is interpreted
in the company time zone set by :func:`set_timezone` (loaded from company settings on
startup and whenever settings change). Weeks start on Monday.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_TZ: tzinfo = UTC
_TZ_NAME = "UTC"


def set_timezone(name: str) -> tzinfo:
    """Switch the company time zone; raises ValueError for unknown names."""
    global _TZ, _TZ_NAME
    zone = load_timezone(name)
    _TZ, _TZ_NAME = zone, name
    return zone


def load_timezone(name: str) -> tzinfo:
    if name in ("UTC", "Etc/UTC", "Z"):
        return UTC
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise ValueError(f"Unknown time zone '{name}'") from exc


def get_timezone() -> tzinfo:
    return _TZ


def timezone_name() -> str:
    return _TZ_NAME


def now_utc() -> datetime:
    return datetime.now(UTC)


def now_local() -> datetime:
    return datetime.now(_TZ)


def today() -> date:
    return now_local().date()


def to_local(value: datetime) -> datetime:
    """Aware datetime in the company zone (naive input is assumed UTC)."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(_TZ)


def to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=_TZ)
    return value.astimezone(UTC)


def localize(naive: datetime) -> datetime:
    """Interpret a wall-clock time as company local time (e.g. from an HTML form)."""
    if naive.tzinfo is not None:
        return naive.astimezone(_TZ)
    return naive.replace(tzinfo=_TZ)


def local_date(value: datetime) -> date:
    return to_local(value).date()


def local_datetime(day: date, at: time) -> datetime:
    """Company-local wall clock ``at`` on ``day`` as an aware datetime."""
    return datetime.combine(day, at, tzinfo=_TZ)


def shift_days(starts_at: datetime, ends_at: datetime) -> list[date]:
    """Local calendar days a [starts_at, ends_at) window touches."""
    first = local_date(starts_at)
    last = local_date(ends_at - timedelta(microseconds=1))
    days: list[date] = []
    cursor = first
    while cursor <= last:
        days.append(cursor)
        cursor += timedelta(days=1)
    return days


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def week_window(start: date) -> tuple[datetime, datetime]:
    begin = local_datetime(start, time.min)
    return begin, local_datetime(start + timedelta(days=7), time.min)


def day_window(day: date) -> tuple[datetime, datetime]:
    return local_datetime(day, time.min), local_datetime(day + timedelta(days=1), time.min)


def period_window(start: date, end: date) -> tuple[datetime, datetime]:
    """Half-open datetime window covering calendar days ``start``..``end`` inclusive."""
    return local_datetime(start, time.min), local_datetime(end + timedelta(days=1), time.min)


def month_bounds(day: date) -> tuple[date, date]:
    first = day.replace(day=1)
    if first.month == 12:
        nxt = first.replace(year=first.year + 1, month=1)
    else:
        nxt = first.replace(month=first.month + 1)
    return first, nxt - timedelta(days=1)


def days_between(start: date, end: date) -> int:
    return (end - start).days + 1


def iter_weeks(start: date, end: date) -> list[date]:
    """Monday dates of every ISO week touching [start, end]."""
    weeks: list[date] = []
    cursor = week_start(start)
    while cursor <= end:
        weeks.append(cursor)
        cursor += timedelta(days=7)
    return weeks


def iter_days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range(days_between(start, end))]


COMMON_TIMEZONES: tuple[str, ...] = (
    "UTC",
    "Europe/Stockholm",
    "Europe/Oslo",
    "Europe/Copenhagen",
    "Europe/Helsinki",
    "Europe/London",
    "Europe/Berlin",
    "Europe/Paris",
    "Europe/Madrid",
    "Europe/Warsaw",
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "America/Sao_Paulo",
    "Asia/Dubai",
    "Asia/Kolkata",
    "Asia/Singapore",
    "Asia/Tokyo",
    "Australia/Sydney",
)
