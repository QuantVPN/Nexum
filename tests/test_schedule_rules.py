from datetime import UTC, datetime

import pytest

from nexum.automation import schedule
from nexum.errors import ValidationError

T = datetime(2026, 9, 8, 10, 30, tzinfo=UTC)  # Tuesday


def test_interval_and_hourly() -> None:
    assert schedule.next_run({"kind": "interval", "minutes": 15}, T) == datetime(
        2026, 9, 8, 10, 45, tzinfo=UTC
    )
    assert schedule.next_run({"kind": "hourly", "minute": 0}, T) == datetime(
        2026, 9, 8, 11, 0, tzinfo=UTC
    )
    assert schedule.next_run({"kind": "hourly", "minute": 45}, T) == datetime(
        2026, 9, 8, 10, 45, tzinfo=UTC
    )


def test_daily_before_and_after_time() -> None:
    assert schedule.next_run({"kind": "daily", "at": "12:00"}, T) == datetime(
        2026, 9, 8, 12, 0, tzinfo=UTC
    )
    assert schedule.next_run({"kind": "daily", "at": "07:00"}, T) == datetime(
        2026, 9, 9, 7, 0, tzinfo=UTC
    )


def test_weekly_same_day_and_next_week() -> None:
    assert schedule.next_run({"kind": "weekly", "weekday": 1, "at": "18:00"}, T) == datetime(
        2026, 9, 8, 18, 0, tzinfo=UTC
    )
    assert schedule.next_run({"kind": "weekly", "weekday": 1, "at": "06:00"}, T) == datetime(
        2026, 9, 15, 6, 0, tzinfo=UTC
    )
    assert schedule.next_run({"kind": "weekly", "weekday": 0, "at": "06:00"}, T) == datetime(
        2026, 9, 14, 6, 0, tzinfo=UTC
    )


def test_monthly_clamps_to_month_length() -> None:
    assert schedule.next_run({"kind": "monthly", "day": 1, "at": "06:00"}, T) == datetime(
        2026, 10, 1, 6, 0, tzinfo=UTC
    )
    assert schedule.next_run({"kind": "monthly", "day": 31, "at": "06:00"}, T) == datetime(
        2026, 9, 30, 6, 0, tzinfo=UTC
    )
    feb = datetime(2027, 2, 1, tzinfo=UTC)
    assert schedule.next_run({"kind": "monthly", "day": 30, "at": "00:00"}, feb) == datetime(
        2027, 2, 28, tzinfo=UTC
    )
    dec = datetime(2026, 12, 15, tzinfo=UTC)
    assert schedule.next_run({"kind": "monthly", "day": 1, "at": "00:00"}, dec) == datetime(
        2027, 1, 1, tzinfo=UTC
    )


def test_validate() -> None:
    for bad in (
        {"kind": "cron"},
        {"kind": "interval", "minutes": 0},
        {"kind": "daily", "at": "25:99x"},
        {"kind": "weekly", "weekday": 7},
        {"kind": "monthly", "day": 0},
        {"kind": "hourly", "minute": 60},
    ):
        with pytest.raises(ValidationError):
            schedule.validate(bad)
    assert schedule.validate({"kind": "daily", "at": "07:00"})


def test_describe() -> None:
    assert "every 15 min" in schedule.describe({"kind": "interval", "minutes": 15})
    assert "Mon" in schedule.describe({"kind": "weekly", "weekday": 0, "at": "06:00"})
    assert "day 1" in schedule.describe({"kind": "monthly", "day": 1, "at": "06:00"})
