from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Query

from nexum.api.deps import AdminUser, CurrentUser, DbSession, ManagerUser
from nexum.api.schemas import (
    AvailabilityIn,
    AvailabilityOut,
    EmployeeIn,
    EmployeeOut,
    EmployeePatch,
    SkillsIn,
)
from nexum.errors import PermissionDeniedError
from nexum.models import Employee, Role, User
from nexum.services import auth, people
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
    data = payload.model_dump()
    skills = data.pop("skills")
    employee = people.create_employee(db, actor=user, **data)
    if skills:
        people.set_skills(db, employee, skills)
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


def _self_or_manager(user: User, employee: Employee) -> None:
    if not user.has_role(Role.MANAGER) and (
        user.employee is None or user.employee.id != employee.id
    ):
        raise PermissionDeniedError("You can only view your own record")


@router.patch("/{employee_id}", response_model=EmployeeOut)
def patch_employee(
    employee_id: int, payload: EmployeePatch, db: DbSession, user: ManagerUser
) -> EmployeeOut:
    employee = people.get_employee(db, employee_id)
    changes = payload.model_dump(exclude_unset=True)
    skills = changes.pop("skills", None)
    role = changes.pop("role", None)
    if role is not None and not user.has_role(Role.ADMIN):
        raise PermissionDeniedError("Only admins can change roles")
    people.update_employee(db, employee, actor=user, skills=skills, role=role, **changes)
    commit_and_dispatch(db)
    return EmployeeOut.model_validate(employee)


@router.put("/{employee_id}/skills", response_model=EmployeeOut)
def put_skills(
    employee_id: int, payload: SkillsIn, db: DbSession, user: ManagerUser
) -> EmployeeOut:
    employee = people.get_employee(db, employee_id)
    people.set_skills(db, employee, payload.skills)
    commit_and_dispatch(db)
    return EmployeeOut.model_validate(employee)


@router.get("/{employee_id}/availability", response_model=list[AvailabilityOut])
def get_availability(employee_id: int, db: DbSession, user: CurrentUser) -> list[AvailabilityOut]:
    employee = people.get_employee(db, employee_id)
    _self_or_manager(user, employee)
    return [AvailabilityOut.model_validate(r) for r in employee.availability]


@router.put("/{employee_id}/availability", response_model=list[AvailabilityOut])
def put_availability(
    employee_id: int, payload: list[AvailabilityIn], db: DbSession, user: CurrentUser
) -> list[AvailabilityOut]:
    employee = people.get_employee(db, employee_id)
    _self_or_manager(user, employee)
    rules = people.replace_availability(db, employee, [r.model_dump() for r in payload])
    commit_and_dispatch(db)
    return [AvailabilityOut.model_validate(r) for r in rules]


@router.post("/{employee_id}/reset-link")
def reset_link(employee_id: int, db: DbSession, user: AdminUser) -> dict[str, Any]:
    employee = people.get_employee(db, employee_id)
    if employee.user is None or not employee.user.is_active:
        raise PermissionDeniedError("This employee has no active login")
    raw = auth.create_reset_token(db, employee.user, created_by=user)
    db.commit()
    return {
        "url": f"/reset/{raw}",
        "expires_in_hours": int(auth.RESET_TOKEN_TTL.total_seconds() // 3600),
    }


@router.get("/{employee_id}/export")
def export_employee(employee_id: int, db: DbSession, _: AdminUser) -> dict[str, Any]:
    return people.export_employee(db, people.get_employee(db, employee_id))


@router.post("/{employee_id}/anonymize", response_model=EmployeeOut)
def anonymize(employee_id: int, db: DbSession, user: AdminUser) -> EmployeeOut:
    employee = people.get_employee(db, employee_id)
    people.anonymize_employee(db, employee, actor=user)
    commit_and_dispatch(db)
    return EmployeeOut.model_validate(employee)


@router.post("/{employee_id}/deactivate", response_model=EmployeeOut)
def deactivate(
    employee_id: int, db: DbSession, user: ManagerUser, end_date: date | None = None
) -> EmployeeOut:
    employee = people.get_employee(db, employee_id)
    people.deactivate_employee(db, employee, end_date, actor=user)
    commit_and_dispatch(db)
    return EmployeeOut.model_validate(employee)
