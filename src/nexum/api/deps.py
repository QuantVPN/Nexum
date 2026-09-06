"""FastAPI dependencies: database session, current user, role guards."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from nexum.db import get_db
from nexum.errors import AuthenticationError, PermissionDeniedError
from nexum.models import Employee, Role, User

SESSION_USER_KEY = "user_id"

DbSession = Annotated[Session, Depends(get_db)]


def load_session_user(request: Request, db: Session) -> User | None:
    user_id = request.session.get(SESSION_USER_KEY)
    if not isinstance(user_id, int):
        return None
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        request.session.pop(SESSION_USER_KEY, None)
        return None
    return user


def optional_user(request: Request, db: DbSession) -> User | None:
    return load_session_user(request, db)


def current_user(request: Request, db: DbSession) -> User:
    user = load_session_user(request, db)
    if user is None:
        raise AuthenticationError("Login required")
    return user


CurrentUser = Annotated[User, Depends(current_user)]
OptionalUser = Annotated[User | None, Depends(optional_user)]


def require_role(minimum: Role) -> Callable[[User], User]:
    def guard(user: CurrentUser) -> User:
        if not user.has_role(minimum):
            raise PermissionDeniedError(f"Requires {minimum.value} role")
        return user

    return guard


ManagerUser = Annotated[User, Depends(require_role(Role.MANAGER))]
AdminUser = Annotated[User, Depends(require_role(Role.ADMIN))]


def current_employee(user: CurrentUser, db: DbSession) -> Employee:
    if user.employee is None:
        raise PermissionDeniedError("This account is not linked to an employee record")
    return user.employee


CurrentEmployee = Annotated[Employee, Depends(current_employee)]


def login(request: Request, user: User) -> None:
    request.session[SESSION_USER_KEY] = user.id


def logout(request: Request) -> None:
    request.session.pop(SESSION_USER_KEY, None)
