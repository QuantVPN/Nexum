from __future__ import annotations

from fastapi import APIRouter

from nexum.api.deps import CurrentUser, DbSession
from nexum.api.schemas import CountOut, NotificationOut
from nexum.services import notifications

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("", response_model=list[NotificationOut])
def list_notifications(db: DbSession, user: CurrentUser) -> list[NotificationOut]:
    return [NotificationOut.model_validate(n) for n in notifications.list_for_user(db, user)]


@router.get("/unread-count", response_model=CountOut)
def unread(db: DbSession, user: CurrentUser) -> CountOut:
    return CountOut(count=notifications.unread_count(db, user))


@router.post("/read-all", response_model=CountOut)
def read_all(db: DbSession, user: CurrentUser) -> CountOut:
    count = notifications.mark_all_read(db, user)
    db.commit()
    return CountOut(count=count)
