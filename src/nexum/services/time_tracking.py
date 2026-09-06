"""Clock in / out and timesheet approval."""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from nexum.errors import ConflictError, NotFoundError, ValidationError
from nexum.models import Employee, Shift, ShiftStatus, TimeEntry, TimeEntryStatus, User
from nexum.services import audit
from nexum.services.company import PayrollRules
from nexum.services.events import emit


def get_entry(session: Session, entry_id: int) -> TimeEntry:
    entry = session.get(TimeEntry, entry_id)
    if entry is None:
        raise NotFoundError(f"Time entry {entry_id} not found")
    return entry


def open_entry(session: Session, employee_id: int) -> TimeEntry | None:
    stmt = select(TimeEntry).where(
        TimeEntry.employee_id == employee_id, TimeEntry.clock_out.is_(None)
    )
    return session.scalar(stmt)


def clock_in(
    session: Session, employee: Employee, at: datetime, *, shift: Shift | None = None
) -> TimeEntry:
    if open_entry(session, employee.id) is not None:
        raise ConflictError("Employee is already clocked in")
    entry = TimeEntry(employee_id=employee.id, shift_id=shift.id if shift else None, clock_in=at)
    session.add(entry)
    session.flush()
    emit(session, "time_entry.opened", entry_id=entry.id, employee_id=employee.id, at=at)
    return entry


def clock_out(
    session: Session, entry: TimeEntry, at: datetime, *, break_minutes: int = 0
) -> TimeEntry:
    if entry.clock_out is not None:
        raise ConflictError("Entry is already closed")
    if at <= entry.clock_in:
        raise ValidationError("Clock-out must be after clock-in")
    entry.clock_out = at
    entry.break_minutes = max(break_minutes, 0)
    entry.status = TimeEntryStatus.SUBMITTED
    emit(
        session,
        "time_entry.closed",
        entry_id=entry.id,
        employee_id=entry.employee_id,
        worked_hours=round(entry.worked_hours, 2),
    )
    return entry


def record_entry(
    session: Session,
    employee: Employee,
    clock_in_at: datetime,
    clock_out_at: datetime,
    *,
    break_minutes: int = 0,
    shift: Shift | None = None,
    note: str | None = None,
    status: TimeEntryStatus = TimeEntryStatus.SUBMITTED,
) -> TimeEntry:
    """Create a complete entry in one go (manual timesheet line or import)."""
    if clock_out_at <= clock_in_at:
        raise ValidationError("Clock-out must be after clock-in")
    entry = TimeEntry(
        employee_id=employee.id,
        shift_id=shift.id if shift else None,
        clock_in=clock_in_at,
        clock_out=clock_out_at,
        break_minutes=max(break_minutes, 0),
        status=status,
        note=note,
    )
    session.add(entry)
    session.flush()
    emit(
        session,
        "time_entry.submitted",
        entry_id=entry.id,
        employee_id=employee.id,
        worked_hours=round(entry.worked_hours, 2),
    )
    return entry


def decide_entry(
    session: Session, entry: TimeEntry, *, approve: bool, actor: User | None = None
) -> TimeEntry:
    if entry.clock_out is None:
        raise ConflictError("Cannot approve an open entry")
    entry.status = TimeEntryStatus.APPROVED if approve else TimeEntryStatus.REJECTED
    audit.record(
        session,
        "time_entry.approved" if approve else "time_entry.rejected",
        "time_entry",
        entry.id,
        actor=actor,
    )
    if approve:
        emit(
            session,
            "time_entry.approved",
            entry_id=entry.id,
            employee_id=entry.employee_id,
            worked_hours=round(entry.worked_hours, 2),
        )
    return entry


def entries_in_window(
    session: Session,
    start: datetime,
    end: datetime,
    *,
    employee_id: int | None = None,
    statuses: tuple[TimeEntryStatus, ...] = (),
) -> list[TimeEntry]:
    stmt = (
        select(TimeEntry)
        .where(TimeEntry.clock_in >= start, TimeEntry.clock_in < end)
        .order_by(TimeEntry.clock_in)
    )
    if employee_id is not None:
        stmt = stmt.where(TimeEntry.employee_id == employee_id)
    if statuses:
        stmt = stmt.where(TimeEntry.status.in_(statuses))
    return list(session.scalars(stmt))


def pending_entries(session: Session, limit: int = 200) -> list[TimeEntry]:
    stmt = (
        select(TimeEntry)
        .where(TimeEntry.status == TimeEntryStatus.SUBMITTED)
        .order_by(TimeEntry.clock_in)
        .limit(limit)
    )
    return list(session.scalars(stmt))


def shifts_missing_entries(session: Session, start: datetime, end: datetime) -> list[Shift]:
    """Published, assigned shifts that have ended without any time entry."""
    stmt = (
        select(Shift)
        .where(
            Shift.status == ShiftStatus.PUBLISHED,
            Shift.employee_id.is_not(None),
            Shift.ends_at >= start,
            Shift.ends_at <= end,
            ~Shift.time_entries.any(),
        )
        .order_by(Shift.ends_at)
    )
    return list(session.scalars(stmt))


# --- paid time under the company rules ---------------------------------------------------------


def paid_minutes(
    start: datetime,
    end: datetime,
    *,
    break_minutes: int,
    rules: PayrollRules,
    apply_rounding: bool = True,
) -> int:
    """Minutes that count for pay: span minus the explicit break (or the automatic unpaid
    break once the span exceeds ``auto_break_after_hours``), rounded to ``rounding_minutes``."""
    span = max(int((end - start).total_seconds() // 60), 0)
    deduction = break_minutes
    if deduction <= 0 and rules.auto_break_minutes > 0:
        threshold_minutes = int(rules.auto_break_after_hours * 60)
        if span > threshold_minutes:
            deduction = rules.auto_break_minutes
    paid = max(span - deduction, 0)
    if apply_rounding and rules.rounding_minutes > 0:
        step = rules.rounding_minutes
        paid = int((Decimal(paid) / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP)) * step
    return paid


def entry_paid_hours(entry: TimeEntry, rules: PayrollRules) -> Decimal:
    if entry.clock_out is None:
        return Decimal("0")
    minutes = paid_minutes(
        entry.clock_in, entry.clock_out, break_minutes=entry.break_minutes, rules=rules
    )
    return (Decimal(minutes) / Decimal(60)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def shift_paid_hours(shift: Shift, rules: PayrollRules) -> Decimal:
    """Scheduled shifts fall back to the automatic break rule but are never rounded."""
    minutes = paid_minutes(
        shift.starts_at, shift.ends_at, break_minutes=0, rules=rules, apply_rounding=False
    )
    return (Decimal(minutes) / Decimal(60)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
