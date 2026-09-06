from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter

from nexum.api.deps import CurrentUser, DbSession, ManagerUser
from nexum.api.schemas import (
    AssignIn,
    CountOut,
    GenerateIn,
    PublishIn,
    ShiftIn,
    ShiftOut,
    TemplateIn,
    TemplateOut,
    WindowIn,
)
from nexum.errors import ValidationError
from nexum.models import Role, ScheduleTemplate
from nexum.models.types import utcnow
from nexum.services import people, scheduling
from nexum.services.calendar import to_utc
from nexum.services.events import commit_and_dispatch

router = APIRouter(tags=["schedule"])


def _window(start: datetime | None, end: datetime | None) -> tuple[datetime, datetime]:
    begin = to_utc(start) if start else utcnow()
    finish = to_utc(end) if end else begin + timedelta(days=7)
    if finish <= begin:
        raise ValidationError("end must be after start")
    return begin, finish


@router.get("/shifts", response_model=list[ShiftOut])
def list_shifts(
    db: DbSession,
    user: CurrentUser,
    start: datetime | None = None,
    end: datetime | None = None,
    department_id: int | None = None,
    employee_id: int | None = None,
    only_open: bool = False,
) -> list[ShiftOut]:
    begin, finish = _window(start, end)
    if not user.has_role(Role.MANAGER):
        employee_id = user.employee.id if user.employee else -1
    rows = scheduling.list_shifts(
        db, begin, finish, department_id=department_id, employee_id=employee_id, only_open=only_open
    )
    return [ShiftOut.model_validate(s) for s in rows]


@router.post("/shifts", response_model=ShiftOut, status_code=201)
def create_shift(payload: ShiftIn, db: DbSession, user: ManagerUser) -> ShiftOut:
    department = people.get_department(db, payload.department_id)
    employee = people.get_employee(db, payload.employee_id) if payload.employee_id else None
    shift = scheduling.create_shift(
        db,
        department=department,
        starts_at=to_utc(payload.starts_at),
        ends_at=to_utc(payload.ends_at),
        employee=employee,
        role_label=payload.role_label,
        status=payload.status,
        notes=payload.notes,
        actor=user,
    )
    commit_and_dispatch(db)
    return ShiftOut.model_validate(shift)


@router.post("/shifts/{shift_id}/assign", response_model=ShiftOut)
def assign(shift_id: int, payload: AssignIn, db: DbSession, user: ManagerUser) -> ShiftOut:
    shift = scheduling.get_shift(db, shift_id)
    employee = people.get_employee(db, payload.employee_id)
    scheduling.assign_shift(db, shift, employee, actor=user)
    commit_and_dispatch(db)
    return ShiftOut.model_validate(shift)


@router.post("/shifts/{shift_id}/unassign", response_model=ShiftOut)
def unassign(shift_id: int, db: DbSession, user: ManagerUser) -> ShiftOut:
    shift = scheduling.get_shift(db, shift_id)
    scheduling.unassign_shift(db, shift, actor=user)
    commit_and_dispatch(db)
    return ShiftOut.model_validate(shift)


@router.post("/shifts/{shift_id}/cancel", response_model=ShiftOut)
def cancel(shift_id: int, db: DbSession, user: ManagerUser) -> ShiftOut:
    shift = scheduling.get_shift(db, shift_id)
    scheduling.cancel_shift(db, shift, actor=user)
    commit_and_dispatch(db)
    return ShiftOut.model_validate(shift)


@router.post("/shifts/publish", response_model=CountOut)
def publish(payload: PublishIn, db: DbSession, user: ManagerUser) -> CountOut:
    if payload.shift_ids:
        shifts = [scheduling.get_shift(db, sid) for sid in payload.shift_ids]
    else:
        begin, finish = _window(payload.start, payload.end)
        shifts = scheduling.list_shifts(db, begin, finish)
    count = scheduling.publish_shifts(db, shifts, actor=user)
    commit_and_dispatch(db)
    return CountOut(count=count)


@router.post("/schedule/generate", response_model=list[ShiftOut])
def generate(payload: GenerateIn, db: DbSession, user: ManagerUser) -> list[ShiftOut]:
    created = scheduling.generate_week_from_templates(
        db, payload.week_start, department_id=payload.department_id, actor=user
    )
    commit_and_dispatch(db)
    return [ShiftOut.model_validate(s) for s in created]


@router.post("/schedule/auto-assign", response_model=list[ShiftOut])
def auto_assign(payload: WindowIn, db: DbSession, user: ManagerUser) -> list[ShiftOut]:
    begin, finish = _window(payload.start, payload.end)
    assigned = scheduling.auto_assign_open_shifts(db, begin, finish, actor=user)
    commit_and_dispatch(db)
    return [ShiftOut.model_validate(s) for s in assigned]


@router.get("/shifts/{shift_id}/candidates", response_model=list[int])
def candidates(shift_id: int, db: DbSession, _: ManagerUser) -> list[int]:
    shift = scheduling.get_shift(db, shift_id)
    return [e.id for e in scheduling.candidate_employees(db, shift)]


@router.get("/templates", response_model=list[TemplateOut])
def list_templates(db: DbSession, _: ManagerUser) -> list[TemplateOut]:
    from sqlalchemy import select

    stmt = select(ScheduleTemplate).order_by(ScheduleTemplate.weekday, ScheduleTemplate.start_time)
    return [TemplateOut.model_validate(t) for t in db.scalars(stmt)]


@router.post("/templates", response_model=TemplateOut, status_code=201)
def create_template(payload: TemplateIn, db: DbSession, _: ManagerUser) -> TemplateOut:
    department = people.get_department(db, payload.department_id)
    template = scheduling.create_template(
        db,
        department=department,
        name=payload.name,
        weekday=payload.weekday,
        start_time=payload.start_time,
        end_time=payload.end_time,
        headcount=payload.headcount,
        role_label=payload.role_label,
    )
    commit_and_dispatch(db)
    return TemplateOut.model_validate(template)
