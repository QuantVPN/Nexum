"""Per-run context handed to actions, plus string templating for messages."""

from __future__ import annotations

import string
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from nexum.config import Settings
from nexum.models import AutomationRule, Department, Employee, PayPeriod, Shift, TimeOffRequest
from nexum.services.events import DomainEvent


@dataclass
class ActionResult:
    message: str
    count: int = 0
    work_done: bool = False
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunContext:
    session: Session
    settings: Settings
    rule: AutomationRule
    now: datetime
    event: DomainEvent | None = None
    log: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    depth: int = 0

    def say(self, message: str) -> None:
        self.log.append(message)

    def as_mapping(self) -> dict[str, Any]:
        """Flattened view used by conditions and templates."""
        mapping: dict[str, Any] = {
            "now": {
                "iso": self.now.isoformat(),
                "date": self.now.date().isoformat(),
                "hour": self.now.hour,
                "minute": self.now.minute,
                "weekday": self.now.weekday(),
            },
            "rule": {"key": self.rule.key, "name": self.rule.name, "id": self.rule.id},
            "event": dict(self.event.payload) if self.event else {},
            "event_name": self.event.name if self.event else None,
        }
        mapping.update(self.data)
        mapping.update(enrich(self.session, mapping["event"]))
        return mapping


def enrich(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    """Load the objects an event refers to so templates can say ``{employee.full_name}``."""
    extra: dict[str, Any] = {}
    employee_id = payload.get("employee_id")
    if isinstance(employee_id, int):
        employee = session.get(Employee, employee_id)
        if employee is not None:
            extra["employee"] = {
                "id": employee.id,
                "full_name": employee.full_name,
                "first_name": employee.first_name,
                "email": employee.email,
                "department": employee.department.name if employee.department else "",
                "user_id": employee.user_id,
            }
    shift_id = payload.get("shift_id")
    if isinstance(shift_id, int):
        shift = session.get(Shift, shift_id)
        if shift is not None:
            extra["shift"] = {
                "id": shift.id,
                "starts_at": shift.starts_at.strftime("%a %Y-%m-%d %H:%M"),
                "ends_at": shift.ends_at.strftime("%H:%M"),
                "department": shift.department.name if shift.department else "",
                "role_label": shift.role_label or "",
                "hours": round(shift.duration_hours, 1),
                "status": shift.status.value,
            }
    department_id = payload.get("department_id")
    if isinstance(department_id, int):
        department = session.get(Department, department_id)
        if department is not None:
            extra["department"] = {"id": department.id, "name": department.name}
    request_id = payload.get("request_id")
    if isinstance(request_id, int):
        request = session.get(TimeOffRequest, request_id)
        if request is not None:
            extra["request"] = {
                "id": request.id,
                "kind": request.kind.value,
                "start_date": request.start_date.isoformat(),
                "end_date": request.end_date.isoformat(),
                "days": request.days,
                "status": request.status.value,
            }
    period_id = payload.get("period_id")
    if isinstance(period_id, int):
        period = session.get(PayPeriod, period_id)
        if period is not None:
            extra["period"] = {
                "id": period.id,
                "label": period.label,
                "status": period.status.value,
                "start_date": period.start_date.isoformat(),
                "end_date": period.end_date.isoformat(),
            }
    return extra


class _SafeFormatter(string.Formatter):
    def __init__(self, mapping: dict[str, Any]) -> None:
        super().__init__()
        self.mapping = mapping

    def get_field(self, field_name: str, args: Any, kwargs: Any) -> tuple[Any, str]:
        from nexum.automation.conditions import resolve_path

        value = resolve_path(self.mapping, field_name)
        return ("" if value is None else value), field_name

    def format_field(self, value: Any, format_spec: str) -> str:
        try:
            return str(super().format_field(value, format_spec))
        except (ValueError, TypeError):
            return str(value)


def render(template: str | None, mapping: dict[str, Any]) -> str:
    if not template:
        return ""
    try:
        return _SafeFormatter(mapping).vformat(template, (), {})
    except (KeyError, IndexError, ValueError):
        return template
