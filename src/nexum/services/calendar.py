"""Week/date helpers. All times are UTC; weeks start on Monday."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def week_window(start: date) -> tuple[datetime, datetime]:
    begin = datetime.combine(start, time.min, tzinfo=UTC)
    return begin, begin + timedelta(days=7)


def day_window(day: date) -> tuple[datetime, datetime]:
    begin = datetime.combine(day, time.min, tzinfo=UTC)
    return begin, begin + timedelta(days=1)


def period_window(start: date, end: date) -> tuple[datetime, datetime]:
    """Half-open datetime window covering calendar days ``start``..``end`` inclusive."""
    begin = datetime.combine(start, time.min, tzinfo=UTC)
    finish = datetime.combine(end + timedelta(days=1), time.min, tzinfo=UTC)
    return begin, finish


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
