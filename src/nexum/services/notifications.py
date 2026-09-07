"""In-app notifications for users, employees and whole roles."""

from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from nexum.models import Employee, Notification, NotificationLevel, Role, User
from nexum.services import mailer
from nexum.services.company import get_company


def notify_user(
    session: Session,
    user: User,
    title: str,
    body: str | None = None,
    *,
    level: NotificationLevel = NotificationLevel.INFO,
    link: str | None = None,
    source: str | None = None,
) -> Notification:
    note = Notification(
        user_id=user.id, title=title, body=body, level=level, link=link, source=source
    )
    session.add(note)
    deliver_email(session, user, title, body)
    return note


def deliver_email(session: Session, user: User, title: str, body: str | None) -> bool:
    """Mirror a notification to e-mail when the company and the user opted in."""
    settings = get_company(session)
    if not (settings.email_notifications_enabled and settings.email_configured):
        return False
    if not user.email_notifications or not user.is_active:
        return False
    return mailer.try_send(settings, user.email, f"[{settings.name}] {title}", body or title)


def notify_employee(
    session: Session,
    employee: Employee,
    title: str,
    body: str | None = None,
    *,
    level: NotificationLevel = NotificationLevel.INFO,
    link: str | None = None,
    source: str | None = None,
) -> Notification | None:
    if employee.user is None or not employee.user.is_active:
        return None
    return notify_user(session, employee.user, title, body, level=level, link=link, source=source)


def notify_role(
    session: Session,
    role: Role,
    title: str,
    body: str | None = None,
    *,
    level: NotificationLevel = NotificationLevel.INFO,
    link: str | None = None,
    source: str | None = None,
    minimum: bool = True,
) -> int:
    """Notify every active user with ``role`` (or at least that role when ``minimum``)."""
    users = session.scalars(select(User).where(User.is_active.is_(True))).all()
    count = 0
    for user in users:
        if (minimum and user.has_role(role)) or (not minimum and user.role == role):
            notify_user(session, user, title, body, level=level, link=link, source=source)
            count += 1
    return count


def unread_count(session: Session, user: User) -> int:
    stmt = select(func.count(Notification.id)).where(
        Notification.user_id == user.id, Notification.is_read.is_(False)
    )
    return int(session.scalar(stmt) or 0)


def list_for_user(session: Session, user: User, limit: int = 50) -> list[Notification]:
    stmt = (
        select(Notification)
        .where(Notification.user_id == user.id)
        .order_by(Notification.id.desc())
        .limit(limit)
    )
    return list(session.scalars(stmt))


def mark_all_read(session: Session, user: User) -> int:
    result = session.execute(
        update(Notification)
        .where(Notification.user_id == user.id, Notification.is_read.is_(False))
        .values(is_read=True)
    )
    return int(getattr(result, "rowcount", 0) or 0)
