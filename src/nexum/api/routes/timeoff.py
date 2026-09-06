from __future__ import annotations

from fastapi import APIRouter

from nexum.api.deps import CurrentUser, DbSession, ManagerUser
from nexum.api.schemas import DecisionIn, TimeOffIn, TimeOffOut
from nexum.errors import NotFoundError, PermissionDeniedError
from nexum.models import Role, TimeOffRequest, TimeOffStatus
from nexum.services import people
from nexum.services.events import commit_and_dispatch

router = APIRouter(prefix="/time-off", tags=["time-off"])


@router.get("", response_model=list[TimeOffOut])
def list_requests(
    db: DbSession,
    user: CurrentUser,
    status: TimeOffStatus | None = None,
    employee_id: int | None = None,
) -> list[TimeOffOut]:
    if not user.has_role(Role.MANAGER):
        employee_id = user.employee.id if user.employee else -1
    rows = people.list_time_off(db, employee_id=employee_id, status=status)
    return [TimeOffOut.model_validate(r) for r in rows]


@router.post("", response_model=TimeOffOut, status_code=201)
def create_request(payload: TimeOffIn, db: DbSession, user: CurrentUser) -> TimeOffOut:
    if payload.employee_id is not None and payload.employee_id != (
        user.employee.id if user.employee else None
    ):
        if not user.has_role(Role.MANAGER):
            raise PermissionDeniedError("Only managers can file requests for others")
        employee = people.get_employee(db, payload.employee_id)
    else:
        if user.employee is None:
            raise PermissionDeniedError("Your account is not linked to an employee")
        employee = user.employee
    request = people.request_time_off(
        db,
        employee,
        kind=payload.kind,
        start_date=payload.start_date,
        end_date=payload.end_date,
        reason=payload.reason,
    )
    commit_and_dispatch(db)
    return TimeOffOut.model_validate(request)


@router.post("/{request_id}/decide", response_model=TimeOffOut)
def decide(request_id: int, payload: DecisionIn, db: DbSession, user: ManagerUser) -> TimeOffOut:
    request = db.get(TimeOffRequest, request_id)
    if request is None:
        raise NotFoundError("Request not found")
    people.decide_time_off(db, request, approve=payload.approve, actor=user)
    commit_and_dispatch(db)
    return TimeOffOut.model_validate(request)
