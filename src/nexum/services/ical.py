"""iCalendar feed of an employee's published shifts."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from nexum.models import Employee, ShiftStatus
from nexum.models.types import utcnow
from nexum.services import scheduling


def _ics_dt(value: datetime) -> str:
    return value.astimezone(utcnow().tzinfo).strftime("%Y%m%dT%H%M%SZ")


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\;").replace(",", "\\,").replace("\n", "\\n")


def feed(session: Session, employee: Employee, *, days_back: int = 30, days_ahead: int = 90) -> str:
    now = utcnow()
    shifts = scheduling.list_shifts(
        session,
        now - timedelta(days=days_back),
        now + timedelta(days=days_ahead),
        employee_id=employee.id,
        statuses=(ShiftStatus.PUBLISHED,),
    )
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Nexum//Schedule//EN",
        "CALSCALE:GREGORIAN",
        f"X-WR-CALNAME:{_escape(employee.full_name)} shifts",
    ]
    for shift in shifts:
        summary = shift.department.name + (f" ({shift.role_label})" if shift.role_label else "")
        lines += [
            "BEGIN:VEVENT",
            f"UID:shift-{shift.id}@nexum",
            f"DTSTAMP:{_ics_dt(now)}",
            f"DTSTART:{_ics_dt(shift.starts_at)}",
            f"DTEND:{_ics_dt(shift.ends_at)}",
            f"SUMMARY:{_escape(summary)}",
            f"DESCRIPTION:{_escape(shift.notes or '')}",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"
