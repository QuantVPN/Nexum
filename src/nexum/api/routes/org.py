from __future__ import annotations

from fastapi import APIRouter

from nexum.api.deps import CurrentUser, DbSession, ManagerUser
from nexum.api.schemas import DepartmentIn, DepartmentOut
from nexum.services import people
from nexum.services.events import commit_and_dispatch

router = APIRouter(prefix="/departments", tags=["org"])


@router.get("", response_model=list[DepartmentOut])
def list_departments(db: DbSession, _: CurrentUser) -> list[DepartmentOut]:
    return [DepartmentOut.model_validate(d) for d in people.list_departments(db)]


@router.post("", response_model=DepartmentOut, status_code=201)
def create_department(payload: DepartmentIn, db: DbSession, user: ManagerUser) -> DepartmentOut:
    department = people.create_department(db, payload.name, payload.cost_center, actor=user)
    commit_and_dispatch(db)
    return DepartmentOut.model_validate(department)
