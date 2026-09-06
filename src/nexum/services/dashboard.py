"""Aggregated numbers for the admin overview and the employee home page."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from nexum.models import (
    AutomationRule,
    AutomationRun,
    Department,
    Employee,
    RunStatus,
    ShiftStatus,
    TimeOffStatus,
)
from nexum.models.types import utcnow
from nexum.services import audit, notifications, payroll, people, scheduling, time_tracking
from nexum.services.calendar import local_date, week_start, week_window


def automation_stats(session: Session, now: datetime | None = None) -> dict[str, Any]:
    now = now or utcnow()
    since = now - timedelta(days=7)
    total_rules = int(session.scalar(select(func.count(AutomationRule.id))) or 0)
    enabled_rules = int(
        session.scalar(
            select(func.count(AutomationRule.id)).where(AutomationRule.enabled.is_(True))
        )
        or 0
    )
    runs_7d = int(
        session.scalar(
            select(func.count(AutomationRun.id)).where(AutomationRun.started_at >= since)
        )
        or 0
    )
    failed_7d = int(
        session.scalar(
            select(func.count(AutomationRun.id)).where(
                AutomationRun.started_at >= since, AutomationRun.status == RunStatus.FAILED
            )
        )
        or 0
    )
    minutes_saved_total = int(session.scalar(select(func.sum(AutomationRun.minutes_saved))) or 0)
    minutes_saved_7d = int(
        session.scalar(
            select(func.sum(AutomationRun.minutes_saved)).where(AutomationRun.started_at >= since)
        )
        or 0
    )
    return {
        "total_rules": total_rules,
        "enabled_rules": enabled_rules,
        "runs_7d": runs_7d,
        "failed_7d": failed_7d,
        "minutes_saved_total": minutes_saved_total,
        "hours_saved_total": round(minutes_saved_total / 60, 1),
        "minutes_saved_7d": minutes_saved_7d,
        "hours_saved_7d": round(minutes_saved_7d / 60, 1),
    }


def admin_overview(session: Session, now: datetime | None = None) -> dict[str, Any]:
    now = now or utcnow()
    week_begin, week_end = week_window(week_start(local_date(now)))
    next7_end = now + timedelta(days=7)

    employees = people.list_employees(session)
    headcount_by_department: dict[str, int] = {}
    for emp in employees:
        name = emp.department.name if emp.department else "Unassigned"
        headcount_by_department[name] = headcount_by_department.get(name, 0) + 1

    week_shifts = scheduling.list_shifts(session, week_begin, week_end)
    week_hours = sum((Decimal(str(round(s.duration_hours, 2))) for s in week_shifts), Decimal("0"))
    open_now = scheduling.open_shifts(session, now, next7_end)
    draft_count = sum(1 for s in week_shifts if s.status == ShiftStatus.DRAFT)

    period = payroll.current_period(session, local_date(now))
    period_totals = payroll.period_totals(period)
    rules = payroll.payroll_rules(session)
    settings_estimate = Decimal("0")
    for emp in employees:
        calc = payroll.calculate_payslip(session, emp, period, rules)
        settings_estimate += calc.gross_amount

    pending_timeoff = people.list_time_off(session, status=TimeOffStatus.PENDING)
    pending_entries = time_tracking.pending_entries(session)
    missing = time_tracking.shifts_missing_entries(session, now - timedelta(days=7), now)

    return {
        "generated_at": now,
        "headcount": len(employees),
        "headcount_by_department": headcount_by_department,
        "departments": len(people.list_departments(session)),
        "week_start": week_begin.date(),
        "week_scheduled_hours": week_hours,
        "week_shift_count": len(week_shifts),
        "week_draft_shifts": draft_count,
        "open_shifts_next_7_days": len(open_now),
        "open_shifts": open_now[:10],
        "pay_period": period,
        "pay_period_totals": period_totals,
        "projected_gross": payroll.money(settings_estimate),
        "pending_time_off": len(pending_timeoff),
        "pending_time_off_items": pending_timeoff[:10],
        "pending_time_entries": len(pending_entries),
        "shifts_missing_time_entries": len(missing),
        "automation": automation_stats(session, now),
        "recent_audit": audit.recent(session, 12),
    }


def employee_overview(
    session: Session, employee: Employee, now: datetime | None = None
) -> dict[str, Any]:
    now = now or utcnow()
    week_begin, week_end = week_window(week_start(local_date(now)))
    upcoming = scheduling.list_shifts(
        session,
        now,
        now + timedelta(days=14),
        employee_id=employee.id,
        statuses=(ShiftStatus.PUBLISHED,),
    )
    week_hours = scheduling.scheduled_hours(session, employee.id, week_begin, week_end)
    slips = payroll.payslips_for_employee(session, employee.id, limit=3)
    requests = people.list_time_off(session, employee_id=employee.id, limit=10)
    period = payroll.current_period(session, local_date(now))
    estimate = payroll.calculate_payslip(session, employee, period)
    unread = notifications.unread_count(session, employee.user) if employee.user else 0
    open_entry = time_tracking.open_entry(session, employee.id)
    claimable = scheduling.claimable_shifts(session, employee, now, now + timedelta(days=14))
    return {
        "employee": employee,
        "claimable_count": len(claimable),
        "upcoming_shifts": upcoming,
        "next_shift": upcoming[0] if upcoming else None,
        "week_hours": week_hours,
        "weekly_hours_target": employee.weekly_hours,
        "payslips": slips,
        "current_period": period,
        "current_estimate": estimate,
        "time_off_requests": requests,
        "unread_notifications": unread,
        "open_time_entry": open_entry,
    }


def department_summary(session: Session, now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or utcnow()
    week_begin, week_end = week_window(week_start(local_date(now)))
    rows: list[dict[str, Any]] = []
    for dept in session.scalars(select(Department).order_by(Department.name)):
        shifts = scheduling.list_shifts(session, week_begin, week_end, department_id=dept.id)
        rows.append(
            {
                "department": dept,
                "headcount": sum(1 for e in dept.employees if e.is_active),
                "week_shifts": len(shifts),
                "open_shifts": sum(1 for s in shifts if s.employee_id is None),
                "week_hours": sum(
                    (Decimal(str(round(s.duration_hours, 2))) for s in shifts), Decimal("0")
                ),
            }
        )
    return rows
