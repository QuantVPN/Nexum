from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from nexum.errors import ConflictError, ValidationError
from nexum.models import ShiftStatus, TimeOffKind
from nexum.services import people, scheduling
from nexum.services.calendar import week_window
from nexum.services.events import drain_events
from tests.conftest import MONDAY, Company


def dt(day: date, hour: int) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC)


def test_create_shift_validates_window(session: Session, company: Company) -> None:
    with pytest.raises(ValidationError):
        scheduling.create_shift(
            session, department=company.department, starts_at=dt(MONDAY, 10), ends_at=dt(MONDAY, 9)
        )
    with pytest.raises(ValidationError):
        scheduling.create_shift(
            session,
            department=company.department,
            starts_at=dt(MONDAY, 0),
            ends_at=dt(MONDAY, 0) + timedelta(hours=25),
        )


def test_overlap_and_time_off_conflicts(session: Session, company: Company) -> None:
    scheduling.create_shift(
        session,
        department=company.department,
        starts_at=dt(MONDAY, 8),
        ends_at=dt(MONDAY, 16),
        employee=company.eva,
    )
    with pytest.raises(ConflictError):
        scheduling.create_shift(
            session,
            department=company.department,
            starts_at=dt(MONDAY, 15),
            ends_at=dt(MONDAY, 20),
            employee=company.eva,
        )
    # adjacent shift is fine
    scheduling.create_shift(
        session,
        department=company.department,
        starts_at=dt(MONDAY, 16),
        ends_at=dt(MONDAY, 20),
        employee=company.eva,
    )
    req = people.request_time_off(
        session, company.max, kind=TimeOffKind.VACATION, start_date=MONDAY, end_date=MONDAY
    )
    people.decide_time_off(session, req, approve=True, actor=company.admin)
    with pytest.raises(ConflictError):
        scheduling.create_shift(
            session,
            department=company.department,
            starts_at=dt(MONDAY, 8),
            ends_at=dt(MONDAY, 16),
            employee=company.max,
        )


def test_generate_from_templates_is_idempotent(session: Session, company: Company) -> None:
    created = scheduling.generate_week_from_templates(session, MONDAY)
    assert len(created) == 10 and all(s.is_open and s.status == ShiftStatus.DRAFT for s in created)
    assert scheduling.generate_week_from_templates(session, MONDAY) == []
    # cancelling one slot means regeneration fills it again
    scheduling.cancel_shift(session, created[0])
    assert len(scheduling.generate_week_from_templates(session, MONDAY)) == 1
    # passing a non-Monday snaps to the week
    assert scheduling.generate_week_from_templates(session, MONDAY + timedelta(days=3)) == []


def test_auto_assign_respects_weekly_hours_and_fairness(session: Session, company: Company) -> None:
    scheduling.generate_week_from_templates(session, MONDAY)
    ws, we = week_window(MONDAY)
    assigned = scheduling.auto_assign_open_shifts(session, ws, we)
    hours = scheduling.week_hours_by_employee(session, ws, we)
    # 9h shifts: Eva/Max cap at 40h (4 shifts), Ida at 20h (2 shifts) => 10 shifts filled
    assert len(assigned) == 10
    assert hours[company.eva.id] == Decimal("36") and hours[company.max.id] == Decimal("36")
    assert hours[company.ida.id] == Decimal("18")
    assert scheduling.open_shifts(session, ws, we) == []
    # a further 3 h Monday shift: Ida and Max already work Monday (overlap); Eva is free and fits
    extra = scheduling.create_shift(
        session, department=company.department, starts_at=dt(MONDAY, 9), ends_at=dt(MONDAY, 12)
    )
    assert [e.id for e in scheduling.candidate_employees(session, extra)] == [company.eva.id]
    # ...but not once the shift would push her over her weekly hours (36 + 5 > 40)
    long = scheduling.create_shift(
        session, department=company.department, starts_at=dt(MONDAY, 9), ends_at=dt(MONDAY, 14)
    )
    assert scheduling.candidate_employees(session, long) == []


def test_candidates_exclude_other_departments_and_inactive(
    session: Session, company: Company
) -> None:
    other = people.create_department(session, "Support")
    shift = scheduling.create_shift(
        session, department=other, starts_at=dt(MONDAY, 8), ends_at=dt(MONDAY, 12)
    )
    assert scheduling.candidate_employees(session, shift) == []
    people.deactivate_employee(session, company.ida)
    ops_shift = scheduling.create_shift(
        session, department=company.department, starts_at=dt(MONDAY, 8), ends_at=dt(MONDAY, 12)
    )
    names = [e.full_name for e in scheduling.candidate_employees(session, ops_shift)]
    assert "Ida Aro" not in names and len(names) == 2


def test_publish_emits_rich_event(session: Session, company: Company) -> None:
    scheduling.generate_week_from_templates(session, MONDAY)
    ws, we = week_window(MONDAY)
    scheduling.auto_assign_open_shifts(session, ws, we)
    drain_events(session)
    count = scheduling.publish_shifts(session, scheduling.list_shifts(session, ws, we))
    assert count == 10
    events = drain_events(session)
    assert [e.name for e in events] == ["schedule.published"]
    payload = events[0].payload
    assert payload["count"] == 10 and len(payload["shift_ids"]) == 10
    assert sorted(payload["employee_ids"]) == sorted(
        [company.eva.id, company.max.id, company.ida.id]
    )
    assert payload["week_start"] == MONDAY.isoformat()
    # publishing again is a no-op
    assert scheduling.publish_shifts(session, scheduling.list_shifts(session, ws, we)) == 0
    assert drain_events(session) == []


def test_assign_unassign_cancel_flow(session: Session, company: Company) -> None:
    shift = scheduling.create_shift(
        session, department=company.department, starts_at=dt(MONDAY, 8), ends_at=dt(MONDAY, 12)
    )
    scheduling.assign_shift(session, shift, company.eva)
    assert shift.employee_id == company.eva.id
    scheduling.unassign_shift(session, shift)
    assert shift.is_open
    scheduling.cancel_shift(session, shift)
    with pytest.raises(ConflictError):
        scheduling.assign_shift(session, shift, company.eva)
    names = [e.name for e in drain_events(session)]
    assert names == ["shift.created", "shift.assigned", "shift.unassigned", "shift.cancelled"]


def test_template_validation(session: Session, company: Company) -> None:
    with pytest.raises(ValidationError):
        scheduling.create_template(
            session,
            department=company.department,
            name="x",
            weekday=7,
            start_time=dt(MONDAY, 8).time(),
            end_time=dt(MONDAY, 9).time(),
        )
    with pytest.raises(ValidationError):
        scheduling.create_template(
            session,
            department=company.department,
            name="x",
            weekday=0,
            start_time=dt(MONDAY, 8).time(),
            end_time=dt(MONDAY, 9).time(),
            headcount=0,
        )
