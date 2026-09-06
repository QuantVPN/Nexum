"""Payroll: pay periods and payslip computation.

Rules come from company settings (:mod:`nexum.services.company`):

* Paid hours per ISO week = approved time entries (minus breaks, rounded per the rules)
  + published shifts that have no time entry.
* Hours above the weekly overtime threshold count as overtime.
* Premium windows (evenings, nights, weekends) and public holidays pay an *extra*
  ``(multiplier - 1)`` on the hours inside them; where windows overlap, the highest
  multiplier applies.
* Monthly employees: base salary (pro-rated when the period is not a full month or a
  bi-weekly period), minus approved unpaid leave, plus overtime and premiums at the
  hourly equivalent of their salary.
* Hourly employees: regular hours * rate + overtime hours * rate * multiplier + premiums.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from itertools import pairwise
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from nexum.errors import ConflictError, NotFoundError, ValidationError
from nexum.models import (
    Employee,
    PayPeriod,
    PayPeriodStatus,
    PayPeriodType,
    Payslip,
    PayType,
    ShiftStatus,
    TimeEntryStatus,
    TimeOffKind,
    User,
)
from nexum.models.types import utcnow
from nexum.services import audit
from nexum.services.calendar import (
    days_between,
    iter_weeks,
    local_date,
    local_datetime,
    month_bounds,
    period_window,
    shift_days,
    today,
    week_window,
)
from nexum.services.company import PayrollRules, PremiumWindow, get_company, payroll_rules
from nexum.services.events import emit
from nexum.services.people import approved_time_off_days, list_employees
from nexum.services.scheduling import list_shifts
from nexum.services.time_tracking import entries_in_window, entry_paid_hours, shift_paid_hours

CENT = Decimal("0.01")
WEEKS_PER_MONTH = Decimal("52") / Decimal("12")
WORKDAYS_PER_YEAR = Decimal("260")
BIWEEKLY_PERIODS_PER_YEAR = Decimal("26")

__all__ = [
    "PayslipCalculation",
    "WeekHours",
    "calculate_payslip",
    "close_period",
    "compute_payslips",
    "current_period",
    "effective_hours_by_week",
    "employees_in_period",
    "export_csv",
    "get_or_create_period",
    "get_period",
    "hours",
    "list_periods",
    "mark_paid",
    "money",
    "payroll_rules",
    "payslips_for_employee",
    "period_totals",
    "premium_segments",
    "previous_period",
    "reference_datetime",
]


def money(value: Decimal | float | int) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def hours(value: Decimal | float | int) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass
class PaidInterval:
    start: datetime
    end: datetime
    paid_hours: Decimal
    source: str  # "entry" | "shift"


@dataclass
class WeekHours:
    week_start: date
    regular: Decimal
    overtime: Decimal
    from_entries: Decimal
    from_shifts: Decimal
    premium_hours: dict[str, Decimal] = field(default_factory=dict)  # label -> hours
    premium_extra_factor_hours: Decimal = Decimal("0")  # sum(hours * (multiplier - 1))


@dataclass
class PayslipCalculation:
    regular_hours: Decimal
    overtime_hours: Decimal
    premium_hours: Decimal
    base_amount: Decimal
    overtime_amount: Decimal
    premium_amount: Decimal
    adjustments_amount: Decimal
    gross_amount: Decimal
    details: dict[str, Any]


def get_period(session: Session, period_id: int) -> PayPeriod:
    period = session.get(PayPeriod, period_id)
    if period is None:
        raise NotFoundError(f"Pay period {period_id} not found")
    return period


def get_or_create_period(
    session: Session, start: date, end: date, *, currency: str | None = None
) -> PayPeriod:
    if end < start:
        raise ValidationError("Pay period end must be after start")
    existing = session.scalar(
        select(PayPeriod).where(PayPeriod.start_date == start, PayPeriod.end_date == end)
    )
    if existing is not None:
        return existing
    period = PayPeriod(
        start_date=start, end_date=end, currency=currency or get_company(session).currency
    )
    session.add(period)
    session.flush()
    return period


def period_bounds_for(rules: PayrollRules, day: date) -> tuple[date, date]:
    """Start/end of the pay period containing ``day`` under the company's period type."""
    if rules.pay_period_type == PayPeriodType.BIWEEKLY and rules.biweekly_anchor is not None:
        offset = (day - rules.biweekly_anchor).days % 14
        start = day - timedelta(days=offset)
        return start, start + timedelta(days=13)
    return month_bounds(day)


def current_period(session: Session, day: date | None = None) -> PayPeriod:
    start, end = period_bounds_for(payroll_rules(session), day or today())
    return get_or_create_period(session, start, end)


def previous_period(session: Session, day: date | None = None) -> PayPeriod:
    start, _ = period_bounds_for(payroll_rules(session), day or today())
    prev_start, prev_end = period_bounds_for(payroll_rules(session), start - timedelta(days=1))
    return get_or_create_period(session, prev_start, prev_end)


def list_periods(session: Session, limit: int = 24) -> list[PayPeriod]:
    stmt = select(PayPeriod).order_by(PayPeriod.start_date.desc()).limit(limit)
    return list(session.scalars(stmt))


def _window_bounds(window: PremiumWindow, day: date) -> tuple[datetime, datetime]:
    start = local_datetime(day, window.start)
    end = local_datetime(day, window.end)
    if end <= start:
        end += timedelta(days=1)
    return start, end


def _multiplier_at(moment: datetime, rules: PayrollRules) -> tuple[Decimal, str]:
    """Highest premium multiplier (and its label) that applies at ``moment``."""
    day = local_date(moment)
    best = Decimal("1")
    label = ""
    holiday = rules.holidays.get(day)
    if holiday is not None and holiday.multiplier > best:
        best, label = holiday.multiplier, holiday.label
    for window in rules.premium_windows:
        for candidate in (day, day - timedelta(days=1)):
            if candidate.weekday() not in window.weekdays:
                continue
            w0, w1 = _window_bounds(window, candidate)
            if w0 <= moment < w1 and window.multiplier > best:
                best, label = window.multiplier, window.label
    return best, label


def premium_segments(
    start: datetime, end: datetime, rules: PayrollRules
) -> list[tuple[datetime, datetime, Decimal, str]]:
    """Split [start, end) into segments with a constant premium multiplier."""
    if not rules.premium_windows and not rules.holidays:
        return []
    cuts: set[datetime] = {start, end}
    for day in shift_days(start, end):
        for candidate in (day - timedelta(days=1), day, day + timedelta(days=1)):
            midnight = local_datetime(candidate, datetime.min.time())
            if start < midnight < end:
                cuts.add(midnight)
            for window in rules.premium_windows:
                if candidate.weekday() not in window.weekdays:
                    continue
                for edge in _window_bounds(window, candidate):
                    if start < edge < end:
                        cuts.add(edge)
    points = sorted(cuts)
    segments: list[tuple[datetime, datetime, Decimal, str]] = []
    for a, b in pairwise(points):
        multiplier, label = _multiplier_at(a, rules)
        if multiplier > 1:
            segments.append((a, b, multiplier, label))
    return segments


def premium_for_interval(
    interval: PaidInterval, rules: PayrollRules
) -> tuple[dict[str, Decimal], Decimal]:
    """Premium hours by label and the summed ``hours * (multiplier - 1)`` for one interval.

    Paid hours can be fewer than the span (breaks), so premium time is scaled by the paid
    share of the span."""
    span_hours = Decimal(str((interval.end - interval.start).total_seconds() / 3600))
    if span_hours <= 0 or interval.paid_hours <= 0:
        return {}, Decimal("0")
    share = interval.paid_hours / span_hours
    by_label: dict[str, Decimal] = {}
    extra = Decimal("0")
    for a, b, multiplier, label in premium_segments(interval.start, interval.end, rules):
        seg_hours = Decimal(str((b - a).total_seconds() / 3600)) * share
        by_label[label] = by_label.get(label, Decimal("0")) + seg_hours
        extra += seg_hours * (multiplier - 1)
    return {k: hours(v) for k, v in by_label.items()}, extra


def paid_intervals(
    session: Session, employee: Employee, lo: datetime, hi: datetime, rules: PayrollRules
) -> list[PaidInterval]:
    entries = entries_in_window(
        session, lo, hi, employee_id=employee.id, statuses=(TimeEntryStatus.APPROVED,)
    )
    intervals: list[PaidInterval] = []
    covered_shift_ids: set[int] = set()
    for entry in entries:
        if entry.clock_out is None:
            continue
        intervals.append(
            PaidInterval(entry.clock_in, entry.clock_out, entry_paid_hours(entry, rules), "entry")
        )
        if entry.shift_id is not None:
            covered_shift_ids.add(entry.shift_id)
    for shift in list_shifts(
        session, lo, hi, employee_id=employee.id, statuses=(ShiftStatus.PUBLISHED,)
    ):
        if shift.id in covered_shift_ids:
            continue
        intervals.append(
            PaidInterval(shift.starts_at, shift.ends_at, shift_paid_hours(shift, rules), "shift")
        )
    return intervals


def effective_hours_by_week(
    session: Session, employee: Employee, start: date, end: date, rules: PayrollRules
) -> list[WeekHours]:
    p_start, p_end = period_window(start, end)
    threshold = rules.weekly_overtime_threshold_hours
    weeks: list[WeekHours] = []
    for monday in iter_weeks(start, end):
        w_start, w_end = week_window(monday)
        lo, hi = max(w_start, p_start), min(w_end, p_end)
        intervals = paid_intervals(session, employee, lo, hi, rules)
        from_entries = sum((i.paid_hours for i in intervals if i.source == "entry"), Decimal("0"))
        from_shifts = sum((i.paid_hours for i in intervals if i.source == "shift"), Decimal("0"))
        total = from_entries + from_shifts
        overtime = max(total - threshold, Decimal("0"))
        premium: dict[str, Decimal] = {}
        extra = Decimal("0")
        for interval in intervals:
            by_label, interval_extra = premium_for_interval(interval, rules)
            for label, value in by_label.items():
                premium[label] = premium.get(label, Decimal("0")) + value
            extra += interval_extra
        weeks.append(
            WeekHours(
                week_start=monday,
                regular=hours(total - overtime),
                overtime=hours(overtime),
                from_entries=hours(from_entries),
                from_shifts=hours(from_shifts),
                premium_hours={k: hours(v) for k, v in premium.items()},
                premium_extra_factor_hours=extra,
            )
        )
    return weeks


def _monthly_proration(rules: PayrollRules, period: PayPeriod) -> Decimal:
    month_first, month_last = month_bounds(period.start_date)
    if period.start_date == month_first and period.end_date == month_last:
        return Decimal("1")
    period_days = days_between(period.start_date, period.end_date)
    if rules.pay_period_type == PayPeriodType.BIWEEKLY and period_days == 14:
        return Decimal("12") / BIWEEKLY_PERIODS_PER_YEAR
    return Decimal(period_days) / Decimal(days_between(month_first, month_last))


def calculate_payslip(
    session: Session, employee: Employee, period: PayPeriod, rules: PayrollRules | None = None
) -> PayslipCalculation:
    rules = rules or payroll_rules(session)
    weeks = effective_hours_by_week(session, employee, period.start_date, period.end_date, rules)
    regular = sum((w.regular for w in weeks), Decimal("0"))
    overtime = sum((w.overtime for w in weeks), Decimal("0"))
    premium_extra = sum((w.premium_extra_factor_hours for w in weeks), Decimal("0"))
    premium_by_label: dict[str, Decimal] = {}
    for w in weeks:
        for label, value in w.premium_hours.items():
            premium_by_label[label] = premium_by_label.get(label, Decimal("0")) + value
    premium_hours = hours(sum(premium_by_label.values(), Decimal("0")))
    multiplier = rules.overtime_multiplier
    details: dict[str, Any] = {
        "pay_type": employee.pay_type.value,
        "weeks": [
            {
                "week_start": w.week_start.isoformat(),
                "regular_hours": str(w.regular),
                "overtime_hours": str(w.overtime),
                "from_time_entries": str(w.from_entries),
                "from_shifts": str(w.from_shifts),
                "premium_hours": {k: str(v) for k, v in w.premium_hours.items()},
            }
            for w in weeks
        ],
        "overtime_multiplier": str(multiplier),
        "weekly_overtime_threshold_hours": str(rules.weekly_overtime_threshold_hours),
        "premiums": {k: str(hours(v)) for k, v in premium_by_label.items()},
        "rounding_minutes": rules.rounding_minutes,
        "auto_break_minutes": rules.auto_break_minutes,
    }
    adjustments = Decimal("0")

    if employee.pay_type == PayType.HOURLY:
        rate = employee.hourly_rate
        base = money(regular * rate)
        overtime_amount = money(overtime * rate * multiplier)
        premium_amount = money(premium_extra * rate)
        details["hourly_rate"] = str(rate)
    else:
        proration = _monthly_proration(rules, period)
        base = money(employee.monthly_salary * proration)
        weekly_hours = employee.weekly_hours or Decimal("40")
        hourly_equivalent = employee.monthly_salary / (weekly_hours * WEEKS_PER_MONTH)
        overtime_amount = money(overtime * hourly_equivalent * multiplier)
        premium_amount = money(premium_extra * hourly_equivalent)
        unpaid_days = approved_time_off_days(
            session, employee.id, period.start_date, period.end_date, kinds=(TimeOffKind.UNPAID,)
        )
        workdays_off = [d for d in unpaid_days if d.weekday() < 5]
        daily_rate = employee.monthly_salary * Decimal("12") / WORKDAYS_PER_YEAR
        if workdays_off:
            adjustments = money(-daily_rate * len(workdays_off))
        details.update(
            {
                "monthly_salary": str(employee.monthly_salary),
                "proration": str(proration.quantize(Decimal("0.0001"))),
                "hourly_equivalent": str(money(hourly_equivalent)),
                "unpaid_leave_days": len(workdays_off),
            }
        )

    gross = money(base + overtime_amount + premium_amount + adjustments)
    return PayslipCalculation(
        regular_hours=hours(regular),
        overtime_hours=hours(overtime),
        premium_hours=premium_hours,
        base_amount=base,
        overtime_amount=overtime_amount,
        premium_amount=premium_amount,
        adjustments_amount=money(adjustments),
        gross_amount=gross,
        details=details,
    )


def employees_in_period(session: Session, period: PayPeriod) -> list[Employee]:
    return [
        e
        for e in list_employees(session, active_only=False)
        if e.start_date <= period.end_date
        and (e.end_date is None or e.end_date >= period.start_date)
    ]


def compute_payslips(
    session: Session,
    period: PayPeriod,
    *,
    actor: User | None = None,
    rules: PayrollRules | None = None,
) -> list[Payslip]:
    if period.status == PayPeriodStatus.PAID:
        raise ConflictError("Pay period is already paid; payslips are frozen")
    rules = rules or payroll_rules(session)
    existing = {p.employee_id: p for p in period.payslips}
    result: list[Payslip] = []
    total = Decimal("0")
    for employee in employees_in_period(session, period):
        calc = calculate_payslip(session, employee, period, rules)
        slip = existing.get(employee.id)
        if slip is None:
            slip = Payslip(employee_id=employee.id)
            period.payslips.append(slip)
        slip.regular_hours = calc.regular_hours
        slip.overtime_hours = calc.overtime_hours
        slip.premium_hours = calc.premium_hours
        slip.base_amount = calc.base_amount
        slip.overtime_amount = calc.overtime_amount
        slip.premium_amount = calc.premium_amount
        slip.adjustments_amount = calc.adjustments_amount
        slip.gross_amount = calc.gross_amount
        slip.currency = employee.currency or period.currency
        slip.details = calc.details
        slip.generated_at = utcnow()
        result.append(slip)
        total += calc.gross_amount
    session.flush()
    audit.record(
        session, "payroll.computed", "pay_period", period.id, actor=actor, payslips=len(result)
    )
    emit(
        session,
        "payroll.computed",
        period_id=period.id,
        start_date=period.start_date,
        end_date=period.end_date,
        payslips=len(result),
        total_gross=str(money(total)),
    )
    return result


def close_period(
    session: Session, period: PayPeriod, *, actor: User | None = None
) -> list[Payslip]:
    if period.status != PayPeriodStatus.OPEN:
        raise ConflictError("Only open pay periods can be closed")
    slips = compute_payslips(session, period, actor=actor)
    period.status = PayPeriodStatus.CLOSED
    period.closed_at = utcnow()
    audit.record(session, "pay_period.closed", "pay_period", period.id, actor=actor)
    emit(
        session,
        "pay_period.closed",
        period_id=period.id,
        start_date=period.start_date,
        end_date=period.end_date,
        payslips=len(slips),
    )
    return slips


def mark_paid(session: Session, period: PayPeriod, *, actor: User | None = None) -> PayPeriod:
    if period.status != PayPeriodStatus.CLOSED:
        raise ConflictError("Only closed pay periods can be marked as paid")
    period.status = PayPeriodStatus.PAID
    period.paid_at = utcnow()
    audit.record(session, "pay_period.paid", "pay_period", period.id, actor=actor)
    return period


def period_totals(period: PayPeriod) -> dict[str, Decimal]:
    gross = sum((p.gross_amount for p in period.payslips), Decimal("0"))
    overtime = sum((p.overtime_amount for p in period.payslips), Decimal("0"))
    premium = sum((p.premium_amount for p in period.payslips), Decimal("0"))
    ot_hours = sum((p.overtime_hours for p in period.payslips), Decimal("0"))
    premium_hours = sum((p.premium_hours for p in period.payslips), Decimal("0"))
    return {
        "gross": money(gross),
        "overtime": money(overtime),
        "overtime_hours": hours(ot_hours),
        "premium": money(premium),
        "premium_hours": hours(premium_hours),
    }


EXPORT_COLUMNS: tuple[str, ...] = (
    "employee_id",
    "employee",
    "email",
    "department",
    "pay_type",
    "regular_hours",
    "overtime_hours",
    "premium_hours",
    "base_amount",
    "overtime_amount",
    "premium_amount",
    "adjustments_amount",
    "gross_amount",
    "currency",
)


def export_csv(period: PayPeriod) -> str:
    """Bookkeeping-friendly CSV (one row per payslip, semicolon separated for spreadsheets)."""
    import csv
    import io

    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", lineterminator="\n")
    writer.writerow(["period_start", "period_end", *EXPORT_COLUMNS])
    for slip in sorted(
        period.payslips, key=lambda p: (p.employee.last_name, p.employee.first_name)
    ):
        employee = slip.employee
        writer.writerow(
            [
                period.start_date.isoformat(),
                period.end_date.isoformat(),
                employee.id,
                employee.full_name,
                employee.email,
                employee.department.name if employee.department else "",
                employee.pay_type.value,
                slip.regular_hours,
                slip.overtime_hours,
                slip.premium_hours,
                slip.base_amount,
                slip.overtime_amount,
                slip.premium_amount,
                slip.adjustments_amount,
                slip.gross_amount,
                slip.currency,
            ]
        )
    return buffer.getvalue()


def payslips_for_employee(session: Session, employee_id: int, limit: int = 12) -> list[Payslip]:
    stmt = (
        select(Payslip)
        .join(PayPeriod)
        .where(Payslip.employee_id == employee_id)
        .order_by(PayPeriod.start_date.desc())
        .limit(limit)
    )
    return list(session.scalars(stmt))


def reference_datetime(period: PayPeriod) -> datetime:
    return period_window(period.start_date, period.end_date)[0]
