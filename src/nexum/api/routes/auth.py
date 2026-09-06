from __future__ import annotations

from fastapi import APIRouter, Request

from nexum.api import deps
from nexum.api.deps import CurrentUser, DbSession
from nexum.api.schemas import LoginIn, MessageOut, UserOut
from nexum.errors import AuthenticationError
from nexum.models.types import utcnow
from nexum.services import people
from nexum.services.security import verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


def _user_out(user: CurrentUser) -> UserOut:
    out = UserOut.model_validate(user)
    out.employee_id = user.employee.id if user.employee else None
    return out


@router.post("/login", response_model=UserOut)
def login(request: Request, payload: LoginIn, db: DbSession) -> UserOut:
    user = people.get_user_by_email(db, payload.email)
    if (
        user is None
        or not user.is_active
        or not verify_password(payload.password, user.password_hash)
    ):
        raise AuthenticationError("Invalid email or password")
    user.last_login_at = utcnow()
    db.commit()
    deps.login(request, user)
    return _user_out(user)


@router.post("/logout", response_model=MessageOut)
def logout(request: Request) -> MessageOut:
    deps.logout(request)
    return MessageOut(detail="logged out")


@router.get("/me", response_model=UserOut)
def me(user: CurrentUser) -> UserOut:
    return _user_out(user)
