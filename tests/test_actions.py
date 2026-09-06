from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from nexum.automation import AutomationEngine
from nexum.automation.recipes import install_recipes
from nexum.models import (
    AutomationRule,
    Notification,
    PayPeriodStatus,
    RunStatus,
    ShiftStatus,
    TimeOffKind,
    TriggerType,
)
from nexum.services import payroll, people, scheduling, time_tracking
from nexum.services.calendar import week_window
from nexum.services.events import commit_and_dispatch
from tests.conftest import MONDAY, Company, FakeClock


def rule_with(session: Session, *actions: dict, **kw) -> AutomationRule:
    rule = AutomationRule(
        name="t",
        trigger_type=TriggerType.MANUAL,
        actions=list(actions),
        estimated_minutes_saved=kw.get("minutes", 1),
    )
    session.add(rule)
    session.commit()
    return rule


def titles(session: Session, email: str) -> list[str]:
    stmt = (
        select(Notification)
        .join(Notification.user)
        .where(Notification.user.has(email=email))
        .order_by(Notification.id)
    )
    return [n.title for n in session.scalars(stmt)]


def test_weekly_draft_then_cover_then_publish_notifies(
    session: Session, company: Company, engine: AutomationEngine, clock: FakeClock
) -> None:
    install_recipes(session)
    session.commit()
    draft = session.scalar(
        select(AutomationRule).where(AutomationRule.key == "weekly-schedule-draft")
    )
    assert draft is not None
    run = engine.run_rule_id(draft.id, manual=True)
    assert run.status == RunStatus.SUCCESS and run.minutes_saved == 45
    next_monday = MONDAY + timedelta(days=7)
    ws, we = week_window(next_monday)
    shifts = scheduling.list_shifts(session, ws, we)
    assert len(shifts) == 10 and all(s.status == ShiftStatus.DRAFT for s in shifts)
    assert scheduling.open_shifts(session, ws, we) == []  # auto-assigned
    # template-generated open shifts must NOT have spammed managers
    assert "Open shift" not in " ".join(titles(session, "mgr@nexum.test"))
    # publishing sends each employee one message listing their shifts
    scheduling.publish_shifts(session, shifts)
    commit_and_dispatch(session)
    eva_titles = titles(session, "eva@nexum.test")
    assert any(t.startswith("Your schedule is published") for t in eva_titles)


def test_manual_open_shift_alerts_managers_once(
    session: Session, company: Company, engine: AutomationEngine
) -> None:
    install_recipes(session)
    session.commit()
    start = datetime(2026, 9, 10, 8, tzinfo=UTC)
    scheduling.create_shift(
        session,
        department=company.department,
        starts_at=start,
        ends_at=start + timedelta(hours=4),
        role_label="Lead",
    )
    commit_and_dispatch(session)
    mgr = titles(session, "mgr@nexum.test")
    assert len([t for t in mgr if t.startswith("Open shift")]) == 1
    assert "Operations" in mgr[0]


def test_remind_upcoming_shifts_once(
    session: Session, company: Company, engine: AutomationEngine, clock: FakeClock
) -> None:
    start = clock.now + timedelta(hours=5)
    scheduling.create_shift(
        session,
        department=company.department,
        starts_at=start,
        ends_at=start + timedelta(hours=8),
        employee=company.eva,
        status=ShiftStatus.PUBLISHED,
    )
    far = clock.now + timedelta(hours=40)
    scheduling.create_shift(
        session,
        department=company.department,
        starts_at=far,
        ends_at=far + timedelta(hours=8),
        employee=company.eva,
        status=ShiftStatus.PUBLISHED,
    )
    session.commit()
    rule = rule_with(session, {"type": "remind_upcoming_shifts", "params": {"hours": 24}})
    first = engine.run_rule_id(rule.id, manual=True)
    second = engine.run_rule_id(rule.id, manual=True)
    assert first.log[-1].endswith("sent 1 reminder(s)") and first.minutes_saved == 1
    assert second.log[-1].endswith("sent 0 reminder(s)") and second.minutes_saved == 0
    assert titles(session, "eva@nexum.test")[-1].startswith("Upcoming shift")


def test_flag_overtime_once_per_week(
    session: Session, company: Company, engine: AutomationEngine
) -> None:
    for i in range(5):
        start = datetime(2026, 9, 7 + i, 8, tzinfo=UTC)
        scheduling.create_shift(
            session,
            department=company.department,
            starts_at=start,
            ends_at=start + timedelta(hours=10),
            employee=company.eva,
            status=ShiftStatus.PUBLISHED,
        )
    session.commit()
    rule = rule_with(session, {"type": "flag_overtime", "params": {}})
    assert "flagged 1" in engine.run_rule_id(rule.id, manual=True).log[-1]
    assert "already warned" in engine.run_rule_id(rule.id, manual=True).log[-1]
    assert any("above 40.0h" in t for t in titles(session, "mgr@nexum.test"))


def test_missing_time_entries_and_approval_nudges(
    session: Session, company: Company, engine: AutomationEngine, clock: FakeClock
) -> None:
    ended = clock.now - timedelta(hours=20)
    shift = scheduling.create_shift(
        session,
        department=company.department,
        starts_at=ended,
        ends_at=ended + timedelta(hours=8),
        employee=company.eva,
        status=ShiftStatus.PUBLISHED,
    )
    covered = scheduling.create_shift(
        session,
        department=company.department,
        starts_at=ended - timedelta(days=1),
        ends_at=ended - timedelta(days=1) + timedelta(hours=8),
        employee=company.max,
        status=ShiftStatus.PUBLISHED,
    )
    entry = time_tracking.record_entry(
        session, company.max, covered.starts_at, covered.ends_at, shift=covered
    )
    request = people.request_time_off(
        session,
        company.eva,
        kind=TimeOffKind.SICK,
        start_date=date(2026, 9, 20),
        end_date=date(2026, 9, 21),
    )
    entry.created_at = request.created_at = clock.now - timedelta(
        hours=1
    )  # filed an hour ago (fake clock)
    session.commit()
    missing = rule_with(session, {"type": "flag_missing_time_entries", "params": {"days": 3}})
    run = engine.run_rule_id(missing.id, manual=True)
    assert run.log[-1].endswith("1 missing, reminded 1"), run.log
    assert engine.run_rule_id(missing.id, manual=True).log[-1].endswith("1 missing, reminded 0")
    assert titles(session, "eva@nexum.test")[-1].startswith("Missing time entry")
    # approvals: entry + request are younger than 24h -> nothing; move the clock -> nudge once
    nudge = rule_with(session, {"type": "nudge_pending_approvals", "params": {"min_age_hours": 24}})
    assert (
        engine.run_rule_id(nudge.id, manual=True).log[-1].endswith("nothing waiting for approval")
    )
    clock.now += timedelta(days=2)
    assert "nudged managers about 2 item(s)" in engine.run_rule_id(nudge.id, manual=True).log[-1]
    assert "already nudged today" in engine.run_rule_id(nudge.id, manual=True).log[-1]
    assert shift.id


def test_monthly_close_and_payslip_notifications(
    session: Session, company: Company, engine: AutomationEngine, clock: FakeClock
) -> None:
    aug_start = datetime(2026, 8, 3, 8, tzinfo=UTC)
    for i in range(3):
        start = aug_start + timedelta(days=i)
        scheduling.create_shift(
            session,
            department=company.department,
            starts_at=start,
            ends_at=start + timedelta(hours=8),
            employee=company.eva,
            status=ShiftStatus.PUBLISHED,
        )
    session.commit()
    clock.now = datetime(2026, 9, 1, 6, tzinfo=UTC)
    rule = rule_with(
        session,
        {"type": "close_previous_pay_period", "params": {}},
        {"type": "notify_payslips_ready", "params": {}},
        minutes=120,
    )
    run = engine.run_rule_id(rule.id, manual=True)
    assert run.status == RunStatus.SUCCESS and run.minutes_saved == 120
    period = payroll.get_or_create_period(session, date(2026, 8, 1), date(2026, 8, 31))
    session.refresh(period)
    assert period.status == PayPeriodStatus.CLOSED
    eva_slip = next(p for p in period.payslips if p.employee_id == company.eva.id)
    assert eva_slip.gross_amount == Decimal("24.00") * 210
    assert titles(session, "eva@nexum.test")[-1] == f"Payslip ready: {period.label}"
    again = engine.run_rule_id(rule.id, manual=True)
    assert "already closed" in again.log[0] and again.log[-1].endswith(
        "notified 0 employee(s) about 2026-08-01 to 2026-08-31"
    )


def test_compute_current_payroll_and_open_shift_digest(
    session: Session, company: Company, engine: AutomationEngine
) -> None:
    start = datetime(2026, 9, 9, 8, tzinfo=UTC)
    scheduling.create_shift(
        session, department=company.department, starts_at=start, ends_at=start + timedelta(hours=4)
    )
    session.commit()
    rule = rule_with(
        session,
        {"type": "compute_current_payroll", "params": {}},
        {"type": "notify_open_shifts", "params": {"days": 7}},
    )
    run = engine.run_rule_id(rule.id, manual=True)
    assert "refreshed 3 payslip preview(s)" in run.log[0]
    assert "reported 1 open shift(s)" in run.log[1]
    assert "1 open shift(s) need cover" in titles(session, "mgr@nexum.test")


def test_webhook_is_disabled_by_default_and_validates_url(
    session: Session, company: Company, engine: AutomationEngine
) -> None:
    rule = rule_with(
        session, {"type": "webhook", "params": {"url": "https://example.invalid/hook"}}
    )
    run = engine.run_rule_id(rule.id, manual=True)
    assert run.status == RunStatus.SUCCESS and "disabled" in run.log[-1]
    bad = rule_with(session, {"type": "webhook", "params": {"url": "not a url"}})
    assert engine.run_rule_id(bad.id, manual=True).status == RunStatus.FAILED


def test_notify_employee_falls_back_gracefully(
    session: Session, company: Company, engine: AutomationEngine
) -> None:
    rule = rule_with(
        session,
        {"type": "notify_employee", "params": {"employee_id": company.ida.id, "title": "hi"}},
    )
    assert "has no login" in engine.run_rule_id(rule.id, manual=True).log[-1]
    rule2 = rule_with(session, {"type": "notify_employee", "params": {"title": "hi"}})
    assert "no employee referenced" in engine.run_rule_id(rule2.id, manual=True).log[-1]
    rule3 = rule_with(
        session, {"type": "notify_employee", "params": {"employee_id": 999, "title": "hi"}}
    )
    assert "not found" in engine.run_rule_id(rule3.id, manual=True).log[-1]
    assert session.scalar(select(func.count(Notification.id))) == 0
