"""Shift scheduling: creation, assignment, publishing, templates and auto-assignment."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from nexum.errors import ConflictError, NotFoundError, ValidationError
from nexum.models import (
    Department,
    Employee,
    ScheduleTemplate,
    Shift,
    ShiftStatus,
    User,
)
from nexum.services import audit
from nexum.services.calendar import (
    local_date,
    local_datetime,
    shift_days,
    week_start,
    week_window,
)
from nexum.services.events import emit
from nexum.services.people import approved_time_off_days


class SchedulingConflict(ConflictError):
    pass


def get_shift(session: Session, shift_id: int) -> Shift:
    shift = session.get(Shift, shift_id)
    if shift is None:
        raise NotFoundError(f"Shift {shift_id} not found")
    return shift


def _validate_window(starts_at: datetime, ends_at: datetime) -> None:
    if ends_at <= starts_at:
        raise ValidationError("A shift must end after it starts")
    if ends_at - starts_at > timedelta(hours=24):
        raise ValidationError("A shift cannot be longer than 24 hours")


def overlapping_shift(
    session: Session,
    employee_id: int,
    starts_at: datetime,
    ends_at: datetime,
    exclude_id: int | None = None,
) -> Shift | None:
    stmt = select(Shift).where(
        Shift.employee_id == employee_id,
        Shift.status != ShiftStatus.CANCELLED,
        Shift.starts_at < ends_at,
        Shift.ends_at > starts_at,
    )
    if exclude_id is not None:
        stmt = stmt.where(Shift.id != exclude_id)
    return session.scalar(stmt.limit(1))


def _assert_available(
    session: Session,
    employee: Employee,
    starts_at: datetime,
    ends_at: datetime,
    exclude_id: int | None,
) -> None:
    if not employee.is_active:
        raise SchedulingConflict(f"{employee.full_name} is not an active employee")
    clash = overlapping_shift(session, employee.id, starts_at, ends_at, exclude_id)
    if clash is not None:
        raise SchedulingConflict(
            f"{employee.full_name} already has a shift {clash.starts_at:%Y-%m-%d %H:%M}"
        )
    days = shift_days(starts_at, ends_at)
    off_days = approved_time_off_days(session, employee.id, days[0], days[-1])
    if off_days:
        raise SchedulingConflict(f"{employee.full_name} has approved time off on that day")


def create_shift(
    session: Session,
    *,
    department: Department,
    starts_at: datetime,
    ends_at: datetime,
    employee: Employee | None = None,
    role_label: str | None = None,
    status: ShiftStatus = ShiftStatus.DRAFT,
    notes: str | None = None,
    template: ScheduleTemplate | None = None,
    actor: User | None = None,
) -> Shift:
    _validate_window(starts_at, ends_at)
    if employee is not None:
        _assert_available(session, employee, starts_at, ends_at, None)
    shift = Shift(
        department_id=department.id,
        employee_id=employee.id if employee else None,
        starts_at=starts_at,
        ends_at=ends_at,
        role_label=role_label,
        status=status,
        notes=notes,
        template_id=template.id if template else None,
    )
    session.add(shift)
    session.flush()
    audit.record(session, "shift.created", "shift", shift.id, actor=actor)
    emit(
        session,
        "shift.created",
        shift_id=shift.id,
        department_id=department.id,
        employee_id=shift.employee_id,
        starts_at=starts_at,
        ends_at=ends_at,
        is_open=shift.employee_id is None,
        status=status,
        source="template" if template is not None else "manual",
    )
    return shift


def assign_shift(
    session: Session,
    shift: Shift,
    employee: Employee,
    *,
    actor: User | None = None,
    source: str = "manual",
) -> Shift:
    if shift.status == ShiftStatus.CANCELLED:
        raise ConflictError("Cannot assign a cancelled shift")
    _assert_available(session, employee, shift.starts_at, shift.ends_at, shift.id)
    shift.employee_id = employee.id
    shift.employee = employee
    audit.record(
        session,
        "shift.assigned",
        "shift",
        shift.id,
        actor=actor,
        employee_id=employee.id,
        source=source,
    )
    emit(
        session,
        "shift.assigned",
        shift_id=shift.id,
        department_id=shift.department_id,
        employee_id=employee.id,
        starts_at=shift.starts_at,
        ends_at=shift.ends_at,
        source=source,
    )
    return shift


def unassign_shift(session: Session, shift: Shift, *, actor: User | None = None) -> Shift:
    previous = shift.employee_id
    shift.employee_id = None
    shift.employee = None
    audit.record(session, "shift.unassigned", "shift", shift.id, actor=actor, employee_id=previous)
    emit(
        session,
        "shift.unassigned",
        shift_id=shift.id,
        department_id=shift.department_id,
        employee_id=previous,
        starts_at=shift.starts_at,
        ends_at=shift.ends_at,
    )
    return shift


def cancel_shift(session: Session, shift: Shift, *, actor: User | None = None) -> Shift:
    shift.status = ShiftStatus.CANCELLED
    audit.record(session, "shift.cancelled", "shift", shift.id, actor=actor)
    emit(
        session,
        "shift.cancelled",
        shift_id=shift.id,
        department_id=shift.department_id,
        employee_id=shift.employee_id,
        starts_at=shift.starts_at,
    )
    return shift


def publish_shifts(session: Session, shifts: Iterable[Shift], *, actor: User | None = None) -> int:
    count = 0
    departments: set[int] = set()
    employees: set[int] = set()
    shift_ids: list[int] = []
    earliest: datetime | None = None
    latest: datetime | None = None
    for shift in shifts:
        if shift.status == ShiftStatus.DRAFT:
            shift.status = ShiftStatus.PUBLISHED
            count += 1
            shift_ids.append(shift.id)
            departments.add(shift.department_id)
            if shift.employee_id is not None:
                employees.add(shift.employee_id)
            earliest = shift.starts_at if earliest is None else min(earliest, shift.starts_at)
            latest = shift.ends_at if latest is None else max(latest, shift.ends_at)
    if count:
        audit.record(session, "schedule.published", "shift", None, actor=actor, count=count)
        emit(
            session,
            "schedule.published",
            count=count,
            department_ids=sorted(departments),
            employee_ids=sorted(employees),
            shift_ids=shift_ids,
            week_start=week_start(local_date(earliest)) if earliest else None,
            starts_at=earliest,
            ends_at=latest,
        )
    return count


def list_shifts(
    session: Session,
    start: datetime,
    end: datetime,
    *,
    department_id: int | None = None,
    employee_id: int | None = None,
    include_cancelled: bool = False,
    only_open: bool = False,
    statuses: tuple[ShiftStatus, ...] = (),
) -> list[Shift]:
    stmt = (
        select(Shift)
        .where(Shift.starts_at < end, Shift.ends_at > start)
        .order_by(Shift.starts_at, Shift.id)
    )
    if not include_cancelled:
        stmt = stmt.where(Shift.status != ShiftStatus.CANCELLED)
    if statuses:
        stmt = stmt.where(Shift.status.in_(statuses))
    if department_id is not None:
        stmt = stmt.where(Shift.department_id == department_id)
    if employee_id is not None:
        stmt = stmt.where(Shift.employee_id == employee_id)
    if only_open:
        stmt = stmt.where(Shift.employee_id.is_(None))
    return list(session.scalars(stmt))


def open_shifts(session: Session, start: datetime, end: datetime) -> list[Shift]:
    return list_shifts(session, start, end, only_open=True)


def upcoming_shifts(session: Session, now: datetime, within: timedelta) -> list[Shift]:
    stmt = (
        select(Shift)
        .where(
            Shift.status == ShiftStatus.PUBLISHED,
            Shift.employee_id.is_not(None),
            Shift.starts_at >= now,
            Shift.starts_at < now + within,
        )
        .order_by(Shift.starts_at)
    )
    return list(session.scalars(stmt))


def scheduled_hours(session: Session, employee_id: int, start: datetime, end: datetime) -> Decimal:
    total = Decimal("0")
    for shift in list_shifts(session, start, end, employee_id=employee_id):
        total += Decimal(str(round(shift.duration_hours, 2)))
    return total


def week_hours_by_employee(session: Session, start: datetime, end: datetime) -> dict[int, Decimal]:
    totals: dict[int, Decimal] = {}
    for shift in list_shifts(session, start, end):
        if shift.employee_id is None:
            continue
        totals[shift.employee_id] = totals.get(shift.employee_id, Decimal("0")) + Decimal(
            str(round(shift.duration_hours, 2))
        )
    return totals


def candidate_employees(session: Session, shift: Shift) -> list[Employee]:
    """Active employees who could take ``shift``: same department, no clash, no time off,
    and enough room under their weekly hours. Sorted fairest-first (fewest scheduled hours)."""
    stmt = select(Employee).where(Employee.is_active.is_(True))
    if shift.department_id is not None:
        stmt = stmt.where(Employee.department_id == shift.department_id)
    ws, we = week_window(week_start(local_date(shift.starts_at)))
    days = shift_days(shift.starts_at, shift.ends_at)
    hours_this_week = week_hours_by_employee(session, ws, we)
    shift_hours = Decimal(str(round(shift.duration_hours, 2)))
    ranked: list[tuple[Decimal, str, Employee]] = []
    for employee in session.scalars(stmt):
        if overlapping_shift(session, employee.id, shift.starts_at, shift.ends_at, shift.id):
            continue
        if approved_time_off_days(session, employee.id, days[0], days[-1]):
            continue
        current = hours_this_week.get(employee.id, Decimal("0"))
        if current + shift_hours > employee.weekly_hours:
            continue
        ranked.append((current, employee.last_name, employee))
    ranked.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in ranked]


def auto_assign_open_shifts(
    session: Session, start: datetime, end: datetime, *, actor: User | None = None
) -> list[Shift]:
    assigned: list[Shift] = []
    for shift in open_shifts(session, start, end):
        candidates = candidate_employees(session, shift)
        if not candidates:
            continue
        assign_shift(session, shift, candidates[0], actor=actor, source="auto")
        assigned.append(shift)
    return assigned


# --- templates ----------------------------------------------------------------------------


def create_template(
    session: Session,
    *,
    department: Department,
    name: str,
    weekday: int,
    start_time: object,
    end_time: object,
    headcount: int = 1,
    role_label: str | None = None,
) -> ScheduleTemplate:
    if not 0 <= weekday <= 6:
        raise ValidationError("weekday must be 0 (Monday) .. 6 (Sunday)")
    if headcount < 1:
        raise ValidationError("headcount must be at least 1")
    template = ScheduleTemplate(
        department_id=department.id,
        name=name,
        weekday=weekday,
        start_time=start_time,
        end_time=end_time,
        headcount=headcount,
        role_label=role_label,
    )
    session.add(template)
    session.flush()
    return template


def generate_week_from_templates(
    session: Session,
    week_monday: date,
    *,
    department_id: int | None = None,
    actor: User | None = None,
) -> list[Shift]:
    """Create draft open shifts for one week from the schedule templates. Idempotent."""
    if week_monday.weekday() != 0:
        week_monday = week_start(week_monday)
    stmt = select(ScheduleTemplate).order_by(ScheduleTemplate.weekday, ScheduleTemplate.start_time)
    if department_id is not None:
        stmt = stmt.where(ScheduleTemplate.department_id == department_id)
    created: list[Shift] = []
    for template in session.scalars(stmt):
        day = week_monday + timedelta(days=template.weekday)
        starts_at = local_datetime(day, template.start_time)
        ends_at = local_datetime(day, template.end_time)
        if ends_at <= starts_at:  # overnight shift
            ends_at += timedelta(days=1)
        existing = int(
            session.scalar(
                select(func.count(Shift.id)).where(
                    Shift.template_id == template.id,
                    Shift.starts_at == starts_at,
                    Shift.status != ShiftStatus.CANCELLED,
                )
            )
            or 0
        )
        for _ in range(max(template.headcount - existing, 0)):
            created.append(
                create_shift(
                    session,
                    department=template.department,
                    starts_at=starts_at,
                    ends_at=ends_at,
                    role_label=template.role_label,
                    template=template,
                    actor=actor,
                )
            )
    return created
