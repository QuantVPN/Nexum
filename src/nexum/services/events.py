"""Domain events.

Services call :func:`emit` while they work; the events are parked on the session and
only delivered (to the automation engine and any other subscriber) after the transaction
commits via :func:`commit_and_dispatch`. That keeps automations from ever seeing state
that is later rolled back.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from nexum.models.types import utcnow

Dispatcher = Callable[["DomainEvent"], object]

_PENDING_KEY = "nexum.pending_events"
_dispatchers: list[Dispatcher] = []


@dataclass(frozen=True)
class DomainEvent:
    name: str
    payload: dict[str, Any]
    occurred_at: datetime = field(default_factory=utcnow)

    @property
    def summary(self) -> str:
        parts = ", ".join(f"{k}={v}" for k, v in sorted(self.payload.items()))
        return f"{self.name}({parts})"


# Event names emitted by the services (kept here so the automation UI can offer a picklist).
EVENT_NAMES: tuple[str, ...] = (
    "employee.created",
    "employee.deactivated",
    "shift.created",
    "shift.assigned",
    "shift.unassigned",
    "shift.cancelled",
    "schedule.published",
    "timeoff.requested",
    "timeoff.approved",
    "timeoff.rejected",
    "time_entry.opened",
    "time_entry.closed",
    "time_entry.submitted",
    "time_entry.approved",
    "pay_period.closed",
    "payroll.computed",
)


def emit(session: Session, name: str, **payload: Any) -> DomainEvent:
    """Queue an event on the session; delivered after commit."""
    event = DomainEvent(name=name, payload=_jsonable(payload))
    session.info.setdefault(_PENDING_KEY, []).append(event)
    return event


def pending_events(session: Session) -> list[DomainEvent]:
    events: list[DomainEvent] = session.info.get(_PENDING_KEY, [])
    return list(events)


def drain_events(session: Session) -> list[DomainEvent]:
    events = pending_events(session)
    session.info[_PENDING_KEY] = []
    return events


def register_dispatcher(dispatcher: Dispatcher) -> None:
    if dispatcher not in _dispatchers:
        _dispatchers.append(dispatcher)


def unregister_dispatcher(dispatcher: Dispatcher) -> None:
    if dispatcher in _dispatchers:
        _dispatchers.remove(dispatcher)


def clear_dispatchers() -> None:
    _dispatchers.clear()


def dispatch(events: list[DomainEvent]) -> None:
    for event in events:
        for dispatcher in list(_dispatchers):
            dispatcher(event)


def commit_and_dispatch(session: Session) -> list[DomainEvent]:
    """Commit the session, then deliver the events that were queued during the transaction."""
    events = drain_events(session)
    session.commit()
    dispatch(events)
    return events


def _jsonable(payload: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, datetime) or hasattr(value, "isoformat"):
            out[key] = value.isoformat()
        elif hasattr(value, "value") and not isinstance(value, int | float | str | bool):
            out[key] = value.value  # enums
        else:
            out[key] = value
    return out
