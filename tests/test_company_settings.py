from datetime import UTC, date, datetime, time
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from nexum.automation import schedule
from nexum.errors import ValidationError
from nexum.models import PayPeriodType, ShiftStatus
from nexum.services import calendar, payroll, scheduling
from nexum.services import company as company_service
from nexum.services.calendar import local_datetime, timezone_name, week_window
from tests.conftest import MONDAY, Company


def test_defaults_come_from_environment(session: Session) -> None:
    row = company_service.get_company(session)
    assert row.timezone == "UTC" and row.currency == "SEK"
    assert row.weekly_overtime_threshold_hours == Decimal("40")
    assert row.overtime_multiplier == Decimal("1.5")
    assert company_service.get_company(session).id == row.id  # singleton


def test_update_validates(session: Session) -> None:
    with pytest.raises(ValidationError):
        company_service.update_company(session, timezone="Mars/Olympus")
    with pytest.raises(ValidationError):
        company_service.update_company(session, currency="kronor")
    with pytest.raises(ValidationError):
        company_service.update_company(session, overtime_multiplier="0.5")
    with pytest.raises(ValidationError):
        company_service.update_company(session, rounding_minutes="-5")
    with pytest.raises(ValidationError):
        company_service.update_company(session, nonsense=1)
    with pytest.raises(ValidationError):
        company_service.update_company(session, pay_period_type="biweekly")  # needs an anchor
    with pytest.raises(ValidationError):
        company_service.update_company(session, premium_rules=[{"weekdays": [9]}])
    with pytest.raises(ValidationError):
        company_service.update_company(session, holidays=[{"date": "not-a-date"}])
    row = company_service.update_company(
        session,
        currency="nok",
        premium_rules=[
            {
                "label": "Evening",
                "weekdays": [4, 0],
                "start": "18:00",
                "end": "22:00",
                "multiplier": "1.25",
            }
        ],
        holidays=[{"date": "2026-12-25", "label": "Christmas"}],
        pay_period_type="biweekly",
        biweekly_anchor="2026-01-05",
    )
    assert row.currency == "NOK"
    assert row.premium_rules == [
        {
            "label": "Evening",
            "weekdays": [0, 4],
            "start": "18:00",
            "end": "22:00",
            "multiplier": "1.25",
        }
    ]
    assert row.holidays[0]["multiplier"] == "1"
    rules = company_service.rules_from(row)
    assert rules.premium_windows[0].weekdays == frozenset({0, 4}) and rules.premium_windows[
        0
    ].start == time(18)
    assert rules.holidays[date(2026, 12, 25)].label == "Christmas"
    assert rules.pay_period_type == PayPeriodType.BIWEEKLY and rules.biweekly_anchor == date(
        2026, 1, 5
    )


def test_timezone_switch_changes_calendar(session: Session) -> None:
    company_service.update_company(session, timezone="Europe/Stockholm")
    assert timezone_name() == "Europe/Stockholm"
    ws, _we = week_window(MONDAY)
    assert ws.astimezone(UTC) == datetime(2026, 9, 6, 22, tzinfo=UTC)  # CEST = UTC+2
    assert local_datetime(date(2026, 1, 12), time(8)).astimezone(UTC) == datetime(
        2026, 1, 12, 7, tzinfo=UTC
    )  # CET = UTC+1
    assert calendar.local_date(datetime(2026, 9, 7, 23, 30, tzinfo=UTC)) == date(2026, 9, 8)
    assert calendar.shift_days(
        datetime(2026, 9, 7, 20, tzinfo=UTC), datetime(2026, 9, 7, 23, tzinfo=UTC)
    ) == [date(2026, 9, 7), date(2026, 9, 8)]
    assert calendar.to_utc(datetime(2026, 9, 7, 8)) == datetime(2026, 9, 7, 6, tzinfo=UTC)


def test_templates_and_schedules_follow_company_zone(
    session: Session, company: Company, stockholm: None
) -> None:
    shifts = scheduling.generate_week_from_templates(session, MONDAY)
    assert shifts[0].starts_at.astimezone(UTC) == datetime(
        2026, 9, 7, 6, tzinfo=UTC
    )  # 08:00 Stockholm
    assert scheduling.generate_week_from_templates(session, MONDAY) == []  # still idempotent
    # daily 06:00 rule fires at 04:00 UTC in summer
    after = datetime(2026, 9, 8, 10, tzinfo=UTC)
    assert schedule.next_run({"kind": "daily", "at": "06:00"}, after).astimezone(UTC) == datetime(
        2026, 9, 9, 4, tzinfo=UTC
    )
    assert "Europe/Stockholm" in schedule.describe({"kind": "daily", "at": "06:00"})
    # a shift crossing local midnight blocks time off on both local days
    late = scheduling.create_shift(
        session,
        department=company.department,
        starts_at=datetime(2026, 9, 9, 20, tzinfo=UTC),  # 22:00 local
        ends_at=datetime(2026, 9, 10, 1, tzinfo=UTC),  # 03:00 local next day
        employee=company.eva,
        status=ShiftStatus.PUBLISHED,
    )
    assert calendar.shift_days(late.starts_at, late.ends_at) == [
        date(2026, 9, 9),
        date(2026, 9, 10),
    ]


def test_biweekly_periods(session: Session, company: Company) -> None:
    company_service.update_company(
        session, pay_period_type="biweekly", biweekly_anchor="2026-08-31"
    )
    period = payroll.current_period(session, date(2026, 9, 16))
    assert (period.start_date, period.end_date) == (date(2026, 9, 14), date(2026, 9, 27))
    previous = payroll.previous_period(session, date(2026, 9, 16))
    assert (previous.start_date, previous.end_date) == (date(2026, 8, 31), date(2026, 9, 13))
    calc = payroll.calculate_payslip(session, company.max, period)
    assert calc.base_amount == payroll.money(Decimal("38000") * 12 / 26)


def test_env_naive_datetimes_are_company_local_in_api(
    client, company: Company, stockholm: None
) -> None:
    from tests.conftest import login

    login(client, "mgr@nexum.test")
    r = client.post(
        "/api/v1/shifts",
        json={
            "department_id": company.department.id,
            "starts_at": "2026-09-12T08:00:00",
            "ends_at": "2026-09-12T12:00:00",
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["starts_at"].startswith("2026-09-12T06:00:00")  # stored/returned as UTC
