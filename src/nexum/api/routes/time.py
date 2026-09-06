from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter

from nexum.api.deps import CurrentEmployee, CurrentUser, DbSession, ManagerUser
from nexum.api.schemas import ClockIn, ClockOut, DecisionIn, TimeEntryIn, TimeEntryOut
from nexum.errors import ConflictError, PermissionDeniedError
from nexum.models import Role, TimeEntryStatus
from nexum.models.types import utcnow
from nexum.services import people, scheduling, time_tracking
from nexum.services.calendar import to_utc
from nexum.services.events import commit_and_dispatch

router = APIRouter(prefix="/time", tags=["time"])


@router.post("/clock-in", response_model=TimeEntryOut, status_code=201)
def clock_in(payload: ClockIn, db: DbSession, employee: CurrentEmployee) -> TimeEntryOut:
    shift = scheduling.get_shift(db, payload.shift_id) if payload.shift_id else None
    entry = time_tracking.clock_in(
        db, employee, to_utc(payload.at) if payload.at else utcnow(), shift=shift
    )
    commit_and_dispatch(db)
    return TimeEntryOut.model_validate(entry)


@router.post("/clock-out", response_model=TimeEntryOut)
def clock_out(payload: ClockOut, db: DbSession, employee: CurrentEmployee) -> TimeEntryOut:
    entry = time_tracking.open_entry(db, employee.id)
    if entry is None:
        raise ConflictError("You are not clocked in")
    time_tracking.clock_out(
        db,
        entry,
        to_utc(payload.at) if payload.at else utcnow(),
        break_minutes=payload.break_minutes,
    )
    commit_and_dispatch(db)
    return TimeEntryOut.model_validate(entry)


@router.get("/entries", response_model=list[TimeEntryOut])
def list_entries(
    db: DbSession,
    user: CurrentUser,
    start: datetime | None = None,
    end: datetime | None = None,
    employee_id: int | None = None,
    status: TimeEntryStatus | None = None,
) -> list[TimeEntryOut]:
    finish = to_utc(end) if end else utcnow() + timedelta(days=1)
    begin = to_utc(start) if start else finish - timedelta(days=31)
    if not user.has_role(Role.MANAGER):
        employee_id = user.employee.id if user.employee else -1
    rows = time_tracking.entries_in_window(
        db, begin, finish, employee_id=employee_id, statuses=(status,) if status else ()
    )
    return [TimeEntryOut.model_validate(e) for e in rows]


@router.post("/entries", response_model=TimeEntryOut, status_code=201)
def create_entry(payload: TimeEntryIn, db: DbSession, user: CurrentUser) -> TimeEntryOut:
    if payload.employee_id is not None and (
        user.employee is None or payload.employee_id != user.employee.id
    ):
        if not user.has_role(Role.MANAGER):
            raise PermissionDeniedError("Only managers can record time for others")
        employee = people.get_employee(db, payload.employee_id)
        status = TimeEntryStatus.APPROVED
    else:
        if user.employee is None:
            raise PermissionDeniedError("Your account is not linked to an employee")
        employee = user.employee
        status = TimeEntryStatus.SUBMITTED
    shift = scheduling.get_shift(db, payload.shift_id) if payload.shift_id else None
    entry = time_tracking.record_entry(
        db,
        employee,
        to_utc(payload.clock_in),
        to_utc(payload.clock_out),
        break_minutes=payload.break_minutes,
        shift=shift,
        note=payload.note,
        status=status,
    )
    commit_and_dispatch(db)
    return TimeEntryOut.model_validate(entry)


@router.post("/entries/{entry_id}/decide", response_model=TimeEntryOut)
def decide(entry_id: int, payload: DecisionIn, db: DbSession, user: ManagerUser) -> TimeEntryOut:
    entry = time_tracking.get_entry(db, entry_id)
    time_tracking.decide_entry(db, entry, approve=payload.approve, actor=user)
    commit_and_dispatch(db)
    return TimeEntryOut.model_validate(entry)
