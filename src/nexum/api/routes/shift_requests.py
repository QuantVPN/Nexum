from __future__ import annotations

from fastapi import APIRouter

from nexum.api.deps import CurrentUser, DbSession, ManagerUser
from nexum.api.schemas import DecisionIn, ShiftRequestIn, ShiftRequestOut
from nexum.errors import PermissionDeniedError
from nexum.models import Role, ShiftRequestKind, ShiftRequestStatus
from nexum.services import people, scheduling
from nexum.services.events import commit_and_dispatch

router = APIRouter(prefix="/shift-requests", tags=["shift-requests"])


@router.get("", response_model=list[ShiftRequestOut])
def list_requests(
    db: DbSession,
    user: CurrentUser,
    status: ShiftRequestStatus | None = None,
    employee_id: int | None = None,
) -> list[ShiftRequestOut]:
    if not user.has_role(Role.MANAGER):
        employee_id = user.employee.id if user.employee else -1
    rows = scheduling.list_shift_requests(db, status=status, employee_id=employee_id)
    return [ShiftRequestOut.model_validate(r) for r in rows]


@router.post("", response_model=ShiftRequestOut, status_code=201)
def create_request(payload: ShiftRequestIn, db: DbSession, user: CurrentUser) -> ShiftRequestOut:
    if user.employee is None:
        raise PermissionDeniedError("Your account is not linked to an employee")
    shift = scheduling.get_shift(db, payload.shift_id)
    if payload.kind == ShiftRequestKind.CLAIM:
        request = scheduling.request_claim(db, user.employee, shift, note=payload.note)
    elif payload.kind == ShiftRequestKind.DROP:
        request = scheduling.request_drop(db, user.employee, shift, note=payload.note)
    else:
        if payload.target_employee_id is None:
            raise PermissionDeniedError("target_employee_id is required for a transfer")
        target = people.get_employee(db, payload.target_employee_id)
        request = scheduling.request_transfer(db, user.employee, shift, target, note=payload.note)
    commit_and_dispatch(db)
    return ShiftRequestOut.model_validate(request)


@router.post("/{request_id}/decide", response_model=ShiftRequestOut)
def decide(
    request_id: int, payload: DecisionIn, db: DbSession, user: ManagerUser
) -> ShiftRequestOut:
    request = scheduling.get_shift_request(db, request_id)
    scheduling.decide_shift_request(db, request, approve=payload.approve, actor=user)
    commit_and_dispatch(db)
    return ShiftRequestOut.model_validate(request)


@router.post("/{request_id}/cancel", response_model=ShiftRequestOut)
def cancel(request_id: int, db: DbSession, user: CurrentUser) -> ShiftRequestOut:
    if user.employee is None:
        raise PermissionDeniedError("Your account is not linked to an employee")
    request = scheduling.get_shift_request(db, request_id)
    scheduling.cancel_shift_request(db, request, user.employee)
    commit_and_dispatch(db)
    return ShiftRequestOut.model_validate(request)
