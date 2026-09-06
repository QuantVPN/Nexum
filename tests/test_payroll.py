from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from nexum.config import get_settings
from nexum.errors import ConflictError
from nexum.models import PayPeriodStatus, ShiftStatus, TimeOffKind
from nexum.services import payroll, people, scheduling, time_tracking
from nexum.services.calendar import week_window
from tests.conftest import MONDAY, Company

SEPT = (date(2026, 9, 1), date(2026, 9, 30))


def publish_week(
    session: Session, company: Company, monday: date, employee, hours_per_day: int, days: int
) -> None:
    for i in range(days):
        day = monday + timedelta(days=i)
        start = datetime(day.year, day.month, day.day, 8, tzinfo=UTC)
        scheduling.create_shift(
            session,
            department=company.department,
            starts_at=start,
            ends_at=start + timedelta(hours=hours_per_day),
            employee=employee,
            status=ShiftStatus.PUBLISHED,
        )


def test_hourly_regular_and_overtime(session: Session, company: Company) -> None:
    publish_week(session, company, MONDAY, company.eva, 9, 5)  # 45 h in one week
    period = payroll.get_or_create_period(session, *SEPT)
    calc = payroll.calculate_payslip(session, company.eva, period)
    assert calc.regular_hours == Decimal("40.00")
    assert calc.overtime_hours == Decimal("5.00")
    assert calc.base_amount == Decimal("8400.00")  # 40 * 210
    assert calc.overtime_amount == Decimal("1575.00")  # 5 * 210 * 1.5
    assert calc.gross_amount == Decimal("9975.00")
    week = next(w for w in calc.details["weeks"] if w["week_start"] == MONDAY.isoformat())
    assert week["from_shifts"] == "45.00" and week["from_time_entries"] == "0.00"


def test_time_entries_replace_linked_shifts_only(session: Session, company: Company) -> None:
    publish_week(session, company, MONDAY, company.eva, 8, 2)  # two 8h shifts
    ws, we = week_window(MONDAY)
    first, second = scheduling.list_shifts(session, ws, we, employee_id=company.eva.id)
    # worked 10h on the first shift (approved), nothing recorded for the second
    entry = time_tracking.record_entry(
        session, company.eva, first.starts_at, first.starts_at + timedelta(hours=10), shift=first
    )
    time_tracking.decide_entry(session, entry, approve=True)
    # a submitted-but-unapproved entry must not count
    time_tracking.record_entry(
        session, company.eva, second.starts_at, second.starts_at + timedelta(hours=12)
    )
    period = payroll.get_or_create_period(session, *SEPT)
    calc = payroll.calculate_payslip(session, company.eva, period)
    assert calc.regular_hours == Decimal(
        "18.00"
    )  # 10 (entry) + 8 (second shift, no approved entry)


def test_monthly_salary_unpaid_leave_and_overtime(session: Session, company: Company) -> None:
    req = people.request_time_off(
        session,
        company.max,
        kind=TimeOffKind.UNPAID,
        start_date=date(2026, 9, 19),
        end_date=date(2026, 9, 22),
    )  # Sat-Tue: 2 workdays
    people.decide_time_off(session, req, approve=True)
    publish_week(session, company, MONDAY, company.max, 10, 5)  # 50h => 10h OT
    period = payroll.get_or_create_period(session, *SEPT)
    calc = payroll.calculate_payslip(session, company.max, period)
    daily = Decimal("38000") * 12 / Decimal("260")
    assert calc.base_amount == Decimal("38000.00")
    assert calc.adjustments_amount == payroll.money(-daily * 2)
    hourly_equiv = Decimal("38000") / (Decimal("40") * Decimal("52") / Decimal("12"))
    assert calc.overtime_amount == payroll.money(Decimal("10") * hourly_equiv * Decimal("1.5"))
    assert calc.gross_amount == payroll.money(
        calc.base_amount + calc.overtime_amount + calc.adjustments_amount
    )
    assert calc.details["unpaid_leave_days"] == 2


def test_monthly_proration_for_partial_period(session: Session, company: Company) -> None:
    period = payroll.get_or_create_period(session, date(2026, 9, 1), date(2026, 9, 15))
    calc = payroll.calculate_payslip(session, company.max, period)
    assert calc.base_amount == payroll.money(Decimal("38000") * Decimal(15) / Decimal(30))


def test_compute_close_pay_lifecycle(session: Session, company: Company) -> None:
    publish_week(session, company, MONDAY, company.eva, 8, 3)
    period = payroll.get_or_create_period(session, *SEPT)
    slips = payroll.compute_payslips(session, period)
    assert {s.employee_id for s in slips} == {company.eva.id, company.max.id, company.ida.id}
    assert len(period.payslips) == 3
    # recompute updates in place (no duplicates)
    payroll.compute_payslips(session, period)
    assert len(period.payslips) == 3
    totals = payroll.period_totals(period)
    assert totals["gross"] == Decimal("24.00") * 210 + Decimal("38000")
    payroll.close_period(session, period)
    assert period.status == PayPeriodStatus.CLOSED
    with pytest.raises(ConflictError):
        payroll.close_period(session, period)
    payroll.mark_paid(session, period)
    with pytest.raises(ConflictError):
        payroll.compute_payslips(session, period)
    assert payroll.payslips_for_employee(session, company.eva.id)[0].pay_period_id == period.id


def test_employees_outside_period_are_skipped(session: Session, company: Company) -> None:
    people.deactivate_employee(session, company.ida, end_date=date(2026, 8, 31))
    period = payroll.get_or_create_period(session, *SEPT)
    assert company.ida.id not in {e.id for e in payroll.employees_in_period(session, period)}


def test_settings_drive_threshold_and_multiplier(session: Session, company: Company) -> None:
    settings = get_settings().model_copy(
        update={"weekly_overtime_threshold_hours": 30.0, "overtime_multiplier": 2.0}
    )
    publish_week(session, company, MONDAY, company.eva, 8, 5)  # 40h
    period = payroll.get_or_create_period(session, *SEPT)
    calc = payroll.calculate_payslip(session, company.eva, period, settings)
    assert calc.overtime_hours == Decimal("10.00")
    assert calc.overtime_amount == Decimal("4200.00")  # 10 * 210 * 2


def test_current_period_is_calendar_month(session: Session) -> None:
    period = payroll.current_period(session, date(2026, 2, 14))
    assert (period.start_date, period.end_date) == (date(2026, 2, 1), date(2026, 2, 28))
    assert payroll.current_period(session, date(2026, 2, 1)).id == period.id
