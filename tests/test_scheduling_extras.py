"""Availability, skills, shift requests (claim/drop/transfer), auto-approval and the iCal feed."""

from datetime import UTC, datetime, time, timedelta

import pytest
from sqlalchemy.orm import Session

from nexum.automation import AutomationEngine
from nexum.automation.recipes import install_recipes
from nexum.errors import ConflictError, ValidationError
from nexum.models import AvailabilityKind, ShiftRequestKind, ShiftRequestStatus, ShiftStatus
from nexum.services import ical, people, scheduling
from nexum.services.events import commit_and_dispatch
from tests.conftest import MONDAY, Company, FakeClock, login, post


def at(day_offset: int, hour: int, minutes: int = 0) -> datetime:
    return datetime(2026, 9, 7 + day_offset, hour, minutes, tzinfo=UTC)


def open_shift(session: Session, company: Company, start: datetime, hours: int = 4, **kw):
    kw.setdefault("status", ShiftStatus.PUBLISHED)
    return scheduling.create_shift(
        session,
        department=company.department,
        starts_at=start,
        ends_at=start + timedelta(hours=hours),
        **kw,
    )


def test_unavailable_rules_block_and_preferred_rank(session: Session, company: Company) -> None:
    people.add_availability(session, company.eva, weekday=0, start_time=time(0), end_time=time(12))
    people.add_availability(
        session,
        company.max,
        weekday=0,
        start_time=time(8),
        end_time=time(17),
        kind=AvailabilityKind.PREFERRED,
    )
    morning = open_shift(session, company, at(0, 8))  # Monday 08-12
    assert (
        scheduling.availability_conflict(company.eva, morning.starts_at, morning.ends_at)
        is not None
    )
    assert scheduling.availability_conflict(company.eva, at(0, 13), at(0, 17)) is None
    assert scheduling.preference_score(company.max, morning.starts_at, morning.ends_at) == 1.0
    assert scheduling.preference_score(company.max, at(0, 15), at(0, 19)) == 0.5
    candidates = [e.id for e in scheduling.candidate_employees(session, morning)]
    assert candidates[0] == company.max.id and company.eva.id not in candidates
    with pytest.raises(ConflictError):
        scheduling.assign_shift(session, morning, company.eva)
    scheduling.assign_shift(
        session, morning, company.eva, ignore_availability=True
    )  # manager override
    assert morning.employee_id == company.eva.id
    with pytest.raises(ValidationError):
        people.add_availability(
            session, company.eva, weekday=9, start_time=time(0), end_time=time(1)
        )


def test_overnight_availability_rule(session: Session, company: Company) -> None:
    # unavailable Monday 22:00 -> Tuesday 06:00
    people.add_availability(session, company.eva, weekday=0, start_time=time(22), end_time=time(6))
    assert (
        scheduling.availability_conflict(company.eva, at(1, 2), at(1, 5)) is not None
    )  # Tue 02-05
    assert scheduling.availability_conflict(company.eva, at(1, 7), at(1, 9)) is None


def test_skills_filter_candidates(session: Session, company: Company) -> None:
    people.set_skills(session, company.eva, ["Forklift", " first aid "])
    assert company.eva.skill_names == ["first aid", "forklift"]
    forklift = people.get_or_create_skill(session, "forklift")
    shift = open_shift(session, company, at(2, 8), required_skill=forklift)
    assert shift.required_skill_name == "forklift"
    assert [e.id for e in scheduling.candidate_employees(session, shift)] == [company.eva.id]
    with pytest.raises(ConflictError, match="lacks the required skill"):
        scheduling.assign_shift(session, shift, company.max)
    template = scheduling.create_template(
        session,
        department=company.department,
        name="Fork",
        weekday=3,
        start_time=time(6),
        end_time=time(10),
        required_skill=forklift,
    )
    generated = [
        s
        for s in scheduling.generate_week_from_templates(session, MONDAY)
        if s.template_id == template.id
    ]
    assert generated and all(s.required_skill_id == forklift.id for s in generated)
    with pytest.raises(ValidationError):
        people.get_or_create_skill(session, "  ")


def test_claim_flow_with_auto_approval(
    session: Session, company: Company, engine: AutomationEngine, clock: FakeClock
) -> None:
    install_recipes(session)
    session.commit()
    shift = open_shift(session, company, at(3, 8), 8)
    session.commit()
    assert [
        s.id
        for s in scheduling.claimable_shifts(
            session, company.eva, clock.now, clock.now + timedelta(days=14)
        )
    ] == [shift.id]
    request = scheduling.request_claim(session, company.eva, shift, note="happy to")
    commit_and_dispatch(session)  # -> shift.claim_requested -> auto-approve recipe
    session.refresh(request)
    session.refresh(shift)
    assert request.status == ShiftRequestStatus.APPROVED and shift.employee_id == company.eva.id
    assert request.decision_note == "auto-approved: no conflicts"
    # employee was told, and the approved request emitted its event
    titles = [n.title for n in company.eva.user.notifications]
    assert any("claim request was approved" in t for t in titles)
    # the shift is gone from the claimable list and cannot be claimed twice
    with pytest.raises(ConflictError):
        scheduling.request_claim(session, company.max, shift)


def test_claim_with_conflict_is_blocked_and_pending_shown(
    session: Session, company: Company
) -> None:
    busy = open_shift(session, company, at(4, 8), 8, employee=company.eva)
    clash = open_shift(session, company, at(4, 10), 4)
    with pytest.raises(ConflictError, match="already has a shift"):
        scheduling.request_claim(session, company.eva, clash)
    late = open_shift(session, company, at(4, 20), 2)
    scheduling.request_claim(session, company.eva, late)
    with pytest.raises(ConflictError, match="pending request"):
        scheduling.request_claim(session, company.eva, late)
    assert late.id not in [
        s.id for s in scheduling.claimable_shifts(session, company.eva, at(0, 0), at(6, 0))
    ]
    assert busy.employee_id == company.eva.id


def test_drop_and_transfer_decisions(session: Session, company: Company) -> None:
    mine = open_shift(session, company, at(5, 8), 8, employee=company.eva)
    with pytest.raises(ConflictError):
        scheduling.request_drop(session, company.max, mine)  # not his shift
    drop = scheduling.request_drop(session, company.eva, mine, note="sick kid")
    scheduling.decide_shift_request(
        session, drop, approve=False, actor=company.manager, note="need you"
    )
    assert drop.status == ShiftRequestStatus.REJECTED and mine.employee_id == company.eva.id
    with pytest.raises(ConflictError):
        scheduling.decide_shift_request(session, drop, approve=True)
    drop2 = scheduling.request_drop(session, company.eva, mine)
    scheduling.decide_shift_request(session, drop2, approve=True, actor=company.manager)
    assert mine.employee_id is None and mine.is_open
    # transfer: target must be able to take it
    other = open_shift(session, company, at(6, 8), 8, employee=company.eva)
    with pytest.raises(ValidationError):
        scheduling.request_transfer(session, company.eva, other, company.eva)
    people.add_availability(
        session, company.max, weekday=6, start_time=time(0), end_time=time(23, 59)
    )
    with pytest.raises(ConflictError, match="unavailable"):
        scheduling.request_transfer(session, company.eva, other, company.max)
    transfer = scheduling.request_transfer(session, company.eva, other, company.ida)
    assert (
        transfer.kind == ShiftRequestKind.TRANSFER and transfer.target_employee_id == company.ida.id
    )
    scheduling.decide_shift_request(session, transfer, approve=True, actor=company.manager)
    assert other.employee_id == company.ida.id
    # cancelling
    third = open_shift(session, company, at(2, 8), 4, employee=company.eva)
    req = scheduling.request_drop(session, company.eva, third)
    with pytest.raises(ConflictError):
        scheduling.cancel_shift_request(session, req, company.max)
    scheduling.cancel_shift_request(session, req, company.eva)
    assert req.status == ShiftRequestStatus.CANCELLED
    with pytest.raises(ConflictError, match="already started"):
        scheduling.request_drop(
            session,
            company.eva,
            open_shift(session, company, datetime(2020, 1, 1, tzinfo=UTC), 2, employee=company.eva),
        )


def test_auto_approve_sweep_leaves_conflicts_for_managers(
    session: Session, company: Company, engine: AutomationEngine
) -> None:
    from nexum.models import AutomationRule, TriggerType

    a = open_shift(session, company, at(3, 8), 4)
    b = open_shift(session, company, at(3, 10), 4)
    ra = scheduling.request_claim(session, company.eva, a)
    rb = scheduling.request_claim(session, company.eva, b)  # overlaps a -> only one can win
    session.commit()
    rule = AutomationRule(
        name="sweep",
        trigger_type=TriggerType.MANUAL,
        actions=[{"type": "auto_approve_shift_requests", "params": {"kinds": ["claim"]}}],
    )
    session.add(rule)
    session.commit()
    run = engine.run_rule_id(rule.id, manual=True)
    assert run.status.value == "success" and "auto-approved 1 request(s)" in run.log[-1]
    session.expire_all()
    statuses = {
        ra.id: session.get(type(ra), ra.id).status,
        rb.id: session.get(type(rb), rb.id).status,
    }
    assert sorted(s.value for s in statuses.values()) == ["approved", "pending"]


def test_ical_feed(session: Session, company: Company, client) -> None:
    open_shift(
        session,
        company,
        datetime.now(UTC) + timedelta(days=2),
        8,
        employee=company.eva,
        role_label="Lead",
    )
    open_shift(
        session,
        company,
        datetime.now(UTC) + timedelta(days=3),
        8,
        employee=company.eva,
        status=ShiftStatus.DRAFT,
    )
    session.commit()
    body = ical.feed(session, company.eva)
    assert body.count("BEGIN:VEVENT") == 1 and "SUMMARY:Operations (Lead)" in body
    r = client.get(f"/calendar/{company.eva.calendar_token}.ics")
    assert (
        r.status_code == 200
        and r.headers["content-type"].startswith("text/calendar")
        and "BEGIN:VCALENDAR" in r.text
    )
    assert client.get("/calendar/nope.ics").status_code == 404


def test_shift_request_api(client, session: Session, company: Company) -> None:
    shift = open_shift(session, company, at(3, 8), 8)
    mine = open_shift(session, company, at(4, 8), 8, employee=company.eva)
    session.commit()
    login(client, "eva@nexum.test")
    assert [s["id"] for s in client.get("/api/v1/shifts/claimable").json()] == [shift.id]
    r = client.post("/api/v1/shift-requests", json={"kind": "claim", "shift_id": shift.id})
    assert r.status_code == 201 and r.json()["status"] == "pending"
    claim_id = r.json()["id"]
    r = client.post(
        "/api/v1/shift-requests",
        json={
            "kind": "transfer",
            "shift_id": mine.id,
            "target_employee_id": company.max.id,
            "note": "swap?",
        },
    )
    assert r.status_code == 201
    transfer_id = r.json()["id"]
    assert (
        client.post(
            "/api/v1/shift-requests", json={"kind": "transfer", "shift_id": mine.id}
        ).status_code
        == 403
    )
    assert len(client.get("/api/v1/shift-requests").json()) == 2
    assert (
        client.post(f"/api/v1/shift-requests/{claim_id}/decide", json={"approve": True}).status_code
        == 403
    )
    assert (
        client.post(f"/api/v1/shift-requests/{transfer_id}/cancel").json()["status"] == "cancelled"
    )
    login(client, "mgr@nexum.test")
    r = client.post(f"/api/v1/shift-requests/{claim_id}/decide", json={"approve": True})
    assert r.status_code == 200 and r.json()["status"] == "approved"
    assert (
        client.get(
            "/api/v1/shifts",
            params={"start": "2026-09-10T00:00:00Z", "end": "2026-09-11T00:00:00Z"},
        ).json()[0]["employee_id"]
        == company.eva.id
    )
    # availability + skills + patch via API
    r = client.put(
        f"/api/v1/employees/{company.eva.id}/availability",
        json=[{"weekday": 0, "start_time": "00:00", "end_time": "12:00"}],
    )
    assert r.status_code == 200 and r.json()[0]["kind"] == "unavailable"
    assert client.put(
        f"/api/v1/employees/{company.eva.id}/skills", json={"skills": ["Forklift"]}
    ).json()["skill_names"] == ["forklift"]
    r = client.patch(
        f"/api/v1/employees/{company.eva.id}",
        json={"title": "Senior", "weekly_hours": "32", "skills": ["forklift", "cash desk"]},
    )
    assert (
        r.status_code == 200
        and r.json()["title"] == "Senior"
        and r.json()["skill_names"] == ["cash desk", "forklift"]
    )
    assert (
        client.patch(f"/api/v1/employees/{company.eva.id}", json={"role": "admin"}).status_code
        == 403
    )
    assert "forklift" in client.get("/api/v1/skills").json()
    r = client.post(
        "/api/v1/templates",
        json={
            "department_id": company.department.id,
            "name": "Fork",
            "weekday": 2,
            "start_time": "06:00",
            "end_time": "10:00",
            "required_skill": "forklift",
        },
    )
    assert r.status_code == 201 and r.json()["required_skill_name"] == "forklift"
    login(client, "eva@nexum.test")
    assert client.get(f"/api/v1/employees/{company.eva.id}/availability").status_code == 200
    assert client.get(f"/api/v1/employees/{company.max.id}/availability").status_code == 403
    assert client.get("/api/v1/dashboard/me").json()["calendar_url"].endswith(".ics")


def test_shift_pages(client, session: Session, company: Company, recipes: None) -> None:
    from tests.test_web import web_login

    shift = open_shift(session, company, at(3, 8), 8)
    mine = open_shift(session, company, at(4, 8), 8, employee=company.eva)
    session.commit()
    web_login(client, "eva@nexum.test")
    html = client.get("/me/shifts").text
    assert "Open shifts you can claim" in html and ".ics" in html and "Give away" in html
    assert post(client, f"/me/shifts/{shift.id}/claim").status_code == 303
    html = client.get("/me/shifts").text
    assert "Claim sent" in html
    assert (
        post(
            client,
            f"/me/shifts/{mine.id}/transfer",
            {"target_employee_id": str(company.max.id), "note": "please"},
        ).status_code
        == 303
    )
    assert (
        post(client, f"/me/shifts/{mine.id}/transfer", {"target_employee_id": ""}).status_code
        == 303
    )
    assert "Pick a colleague" in client.get("/me/shifts").text
    assert (
        post(
            client,
            "/me/availability",
            {
                "weekdays": ["5", "6"],
                "start_time": "00:00",
                "end_time": "23:59",
                "kind": "unavailable",
                "note": "weekends off",
            },
        ).status_code
        == 303
    )
    html = client.get("/me/availability").text
    assert "Saturday" in html and "weekends off" in html
    rule_id = company.eva.availability[0].id if company.eva.availability else None
    session.expire_all()
    rule_id = session.get(type(company.eva), company.eva.id).availability[0].id
    assert post(client, f"/me/availability/{rule_id}/delete").status_code == 303
    web_login(client, "mgr@nexum.test")
    html = client.get("/approvals").text
    assert "Shift requests" in html and "transfer" in html
    import re

    req_id = re.search(r"/shift-requests/(\d+)/decide", html).group(1)
    assert (
        post(client, f"/shift-requests/{req_id}/decide", {"decision": "approve"}).status_code == 303
    )
    assert "request approved" in client.get("/approvals").text.lower()
    assert (
        post(
            client,
            f"/employees/{company.eva.id}/edit",
            {
                "first_name": "Eva",
                "last_name": "Lundgren",
                "department_id": str(company.department.id),
                "employment_type": "hourly",
                "pay_type": "hourly",
                "hourly_rate": "220",
                "weekly_hours": "36",
                "skills": "forklift, first aid",
            },
        ).status_code
        == 303
    )
    html = client.get(f"/employees/{company.eva.id}").text
    assert "Eva Lundgren" in html and "forklift" in html
    assert client.get(f"/employees/{company.eva.id}/edit").status_code == 200
