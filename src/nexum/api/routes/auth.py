from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from nexum.api import deps
from nexum.api.deps import CurrentUser, DbSession
from nexum.api.schemas import LoginIn, MessageOut, UserOut
from nexum.services import auth
from nexum.services.events import commit_and_dispatch

router = APIRouter(prefix="/auth", tags=["auth"])


def client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


def _user_out(user: CurrentUser) -> UserOut:
    out = UserOut.model_validate(user)
    out.employee_id = user.employee.id if user.employee else None
    return out


class PasswordChangeIn(BaseModel):
    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=1)


class ForgotIn(BaseModel):
    email: str = Field(min_length=3, max_length=255)


class ResetIn(BaseModel):
    token: str = Field(min_length=10)
    new_password: str = Field(min_length=1)


@router.post("/login", response_model=UserOut)
def login(request: Request, payload: LoginIn, db: DbSession) -> UserOut:
    user = auth.authenticate(db, payload.email, payload.password, client_ip=client_ip(request))
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


@router.post("/password", response_model=MessageOut)
def change_password(
    request: Request, payload: PasswordChangeIn, db: DbSession, user: CurrentUser
) -> MessageOut:
    auth.change_password(db, user, payload.current_password, payload.new_password)
    db.commit()
    deps.login(request, user)  # keep this session, drop every other one
    return MessageOut(detail="password changed")


@router.post("/forgot", response_model=MessageOut)
def forgot(request: Request, payload: ForgotIn, db: DbSession) -> MessageOut:
    auth.request_reset(db, payload.email, reset_url_base=str(request.base_url))
    commit_and_dispatch(db)
    return MessageOut(detail="If that address has a login, a reset link has been sent.")


@router.post("/reset", response_model=MessageOut)
def reset(payload: ResetIn, db: DbSession) -> MessageOut:
    auth.reset_password(db, payload.token, payload.new_password)
    db.commit()
    return MessageOut(detail="password reset; you can sign in now")
