from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Query

from nexum.api.deps import CurrentUser, DbSession, ManagerUser
from nexum.api.schemas import EmployeeIn, EmployeeOut
from nexum.errors import PermissionDeniedError
from nexum.models import Role
from nexum.services import people
from nexum.services.events import commit_and_dispatch

router = APIRouter(prefix="/employees", tags=["employees"])


@router.get("", response_model=list[EmployeeOut])
def list_employees(
    db: DbSession,
    _: ManagerUser,
    active_only: bool = True,
    department_id: int | None = Query(default=None),
) -> list[EmployeeOut]:
    rows = people.list_employees(db, active_only=active_only, department_id=department_id)
    return [EmployeeOut.model_validate(e) for e in rows]


@router.post("", response_model=EmployeeOut, status_code=201)
def create_employee(payload: EmployeeIn, db: DbSession, user: ManagerUser) -> EmployeeOut:
    if payload.role != Role.EMPLOYEE and not user.has_role(Role.ADMIN):
        raise PermissionDeniedError("Only admins can create manager or admin logins")
    employee = people.create_employee(db, actor=user, **payload.model_dump())
    commit_and_dispatch(db)
    return EmployeeOut.model_validate(employee)


@router.get("/{employee_id}", response_model=EmployeeOut)
def get_employee(employee_id: int, db: DbSession, user: CurrentUser) -> EmployeeOut:
    employee = people.get_employee(db, employee_id)
    if not user.has_role(Role.MANAGER) and (
        user.employee is None or user.employee.id != employee.id
    ):
        raise PermissionDeniedError("You can only view your own record")
    return EmployeeOut.model_validate(employee)


@router.post("/{employee_id}/deactivate", response_model=EmployeeOut)
def deactivate(
    employee_id: int, db: DbSession, user: ManagerUser, end_date: date | None = None
) -> EmployeeOut:
    employee = people.get_employee(db, employee_id)
    people.deactivate_employee(db, employee, end_date, actor=user)
    commit_and_dispatch(db)
    return EmployeeOut.model_validate(employee)
