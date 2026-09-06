from datetime import UTC, date, datetime

from nexum.services import calendar
from nexum.services.security import hash_password, verify_password


def test_password_roundtrip() -> None:
    encoded = hash_password("correct horse battery staple")
    assert encoded.startswith("scrypt$")
    assert verify_password("correct horse battery staple", encoded)
    assert not verify_password("wrong", encoded)
    assert not verify_password("x", "garbage")
    assert hash_password("a") != hash_password("a")  # salted


def test_week_helpers() -> None:
    assert calendar.week_start(date(2026, 9, 10)) == date(2026, 9, 7)
    ws, we = calendar.week_window(date(2026, 9, 7))
    assert ws == datetime(2026, 9, 7, tzinfo=UTC) and we == datetime(2026, 9, 14, tzinfo=UTC)
    assert calendar.iter_weeks(date(2026, 9, 1), date(2026, 9, 30)) == [
        date(2026, 8, 31),
        date(2026, 9, 7),
        date(2026, 9, 14),
        date(2026, 9, 21),
        date(2026, 9, 28),
    ]


def test_month_bounds_and_days() -> None:
    assert calendar.month_bounds(date(2026, 2, 10)) == (date(2026, 2, 1), date(2026, 2, 28))
    assert calendar.month_bounds(date(2026, 12, 31)) == (date(2026, 12, 1), date(2026, 12, 31))
    assert calendar.days_between(date(2026, 9, 1), date(2026, 9, 30)) == 30
    _start, end = calendar.period_window(date(2026, 9, 1), date(2026, 9, 30))
    assert end == datetime(2026, 10, 1, tzinfo=UTC)
