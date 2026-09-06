"""Audit logging helper."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from nexum.models import AuditLog, User


def record(
    session: Session,
    action: str,
    entity_type: str,
    entity_id: int | None = None,
    *,
    actor: User | None = None,
    actor_label: str | None = None,
    **details: Any,
) -> AuditLog:
    entry = AuditLog(
        actor_user_id=actor.id if actor else None,
        actor_label=actor_label or (actor.email if actor else "system"),
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        details=details,
    )
    session.add(entry)
    return entry


def recent(session: Session, limit: int = 20) -> list[AuditLog]:
    stmt = select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)
    return list(session.scalars(stmt))
