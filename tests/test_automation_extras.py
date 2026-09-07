"""E-mail delivery, external actions with retries, dry runs, new checks, report and rule editor."""

import json
from datetime import date, timedelta

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from nexum.automation import AutomationEngine
from nexum.models import (
    AutomationRule,
    AutomationRun,
    Notification,
    RunStatus,
    ShiftStatus,
    TriggerType,
)
from nexum.services import company as company_service
from nexum.services import dashboard, mailer, notifications, scheduling, time_tracking
from tests.conftest import Company, FakeClock, login, post


def rule_with(session: Session, *actions: dict, **kw) -> AutomationRule:
    rule = AutomationRule(
        name=kw.get("name", "t"),
        trigger_type=TriggerType.MANUAL,
        actions=list(actions),
        estimated_minutes_saved=kw.get("minutes", 1),
    )
    session.add(rule)
    session.commit()
    return rule


def test_notifications_mirror_to_email_when_opted_in(
    session: Session, company: Company, outbox: list[mailer.Message]
) -> None:
    notifications.notify_user(session, company.manager, "Hello", "body")
    assert (
        len(outbox) == 1
        and outbox[0].to == "mgr@nexum.test"
        and outbox[0].subject == "[My company] Hello"
    )
    company.manager.email_notifications = False
    notifications.notify_user(session, company.manager, "Again", None)
    assert len(outbox) == 1
    company_service.update_company(session, email_notifications_enabled=False)
    notifications.notify_user(session, company.admin, "Silent", None)
    assert len(outbox) == 1


def test_send_email_action(
    session: Session, company: Company, engine: AutomationEngine, outbox: list[mailer.Message]
) -> None:
    rule = rule_with(
        session,
        {
            "type": "send_email",
            "params": {
                "role": "manager",
                "to": "extra@test, extra@test",
                "subject": "Report {now.date}",
                "body": "hi",
            },
        },
    )
    run = engine.run_rule_id(rule.id, manual=True)
    assert run.status == RunStatus.SUCCESS and "sent 3 e-mail(s)" in run.log[-1]
    assert sorted(m.to for m in outbox) == ["admin@nexum.test", "extra@test", "mgr@nexum.test"]
    assert outbox[0].subject == "Report 2026-09-08"
    company_service.update_company(session, smtp_host=None)
    session.commit()
    assert "not configured" in engine.run_rule_id(rule.id, manual=True).log[-1]


def test_chat_webhook_gating_and_retries(
    session: Session, company: Company, engine: AutomationEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    rule = rule_with(
        session,
        {
            "type": "chat_webhook",
            "params": {"url": "https://hooks.example/abc", "text": "Open shifts: {event_name}"},
        },
    )
    assert "disabled" in engine.run_rule_id(rule.id, manual=True).log[-1]
    engine.settings = engine.settings.model_copy(
        update={
            "automation_webhooks_enabled": True,
            "automation_retry_backoff_seconds": 0,
            "automation_retry_attempts": 3,
        }
    )
    calls: list[dict] = []

    def flaky_post(url: str, json: dict, timeout: float) -> httpx.Response:
        calls.append(json)
        if len(calls) < 2:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", flaky_post)
    run = engine.run_rule_id(rule.id, manual=True)
    assert run.status == RunStatus.SUCCESS and any("retrying" in line for line in run.log)
    assert calls[-1] == {"text": "Open shifts: "}

    def always_fails(url: str, json: dict, timeout: float) -> httpx.Response:
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "post", always_fails)
    run = engine.run_rule_id(rule.id, manual=True)
    assert run.status == RunStatus.FAILED and "ConnectError" in (run.error or "")
    bad = rule_with(session, {"type": "chat_webhook", "params": {"url": "nope"}})
    assert engine.run_rule_id(bad.id, manual=True).status == RunStatus.FAILED


def test_dry_run_changes_nothing(
    session: Session, company: Company, engine: AutomationEngine
) -> None:
    rule = rule_with(
        session,
        {"type": "notify_role", "params": {"role": "manager", "title": "would send"}},
        minutes=9,
    )
    run = engine.run_rule_id(rule.id, manual=True, dry_run=True)
    assert run.status == RunStatus.SUCCESS and run.trigger_summary == "dry run"
    assert run.log[0].startswith("notify_role: notified 2") and run.log[-1].startswith("dry run")
    assert session.scalar(select(func.count(Notification.id))) == 0
    assert session.scalar(select(func.count(AutomationRun.id))) == 0
    session.refresh(rule)
    assert rule.run_count == 0 and rule.last_run_at is None
    failing = rule_with(session, {"type": "webhook", "params": {"url": "ftp://x"}})
    assert engine.run_rule_id(failing.id, manual=True, dry_run=True).status == RunStatus.FAILED
    assert session.scalar(select(func.count(AutomationRun.id))) == 0


def test_no_show_and_open_entry_checks(
    session: Session, company: Company, engine: AutomationEngine, clock: FakeClock
) -> None:
    started = clock.now - timedelta(minutes=30)
    late = scheduling.create_shift(
        session,
        department=company.department,
        starts_at=started,
        ends_at=started + timedelta(hours=8),
        employee=company.eva,
        status=ShiftStatus.PUBLISHED,
    )
    fine = scheduling.create_shift(
        session,
        department=company.department,
        starts_at=started,
        ends_at=started + timedelta(hours=8),
        employee=company.max,
        status=ShiftStatus.PUBLISHED,
    )
    time_tracking.clock_in(session, company.max, started + timedelta(minutes=2), shift=fine)
    forgotten = time_tracking.clock_in(session, company.ida, clock.now - timedelta(hours=20))
    session.commit()
    rule = rule_with(session, {"type": "flag_no_shows", "params": {"grace_minutes": 15}})
    assert "flagged 1 possible no-show(s)" in engine.run_rule_id(rule.id, manual=True).log[-1]
    assert "flagged 0" in engine.run_rule_id(rule.id, manual=True).log[-1]
    titles = [n.title for n in session.scalars(select(Notification))]
    assert any("please clock in" in t for t in titles) and any(
        "No-show? Eva Lund" in t for t in titles
    )
    open_rule = rule_with(session, {"type": "flag_long_open_entries", "params": {"max_hours": 14}})
    assert "flagged 1 open" in engine.run_rule_id(open_rule.id, manual=True).log[-1]
    assert "flagged 0" in engine.run_rule_id(open_rule.id, manual=True).log[-1]
    assert late.id and forgotten.id


def test_understaffing_and_contract_end(
    session: Session, company: Company, engine: AutomationEngine, clock: FakeClock
) -> None:
    rule = rule_with(session, {"type": "understaffing_forecast", "params": {"days": 7}})
    run = engine.run_rule_id(rule.id, manual=True)
    # templates need 2 people on each weekday; nothing is scheduled for Wed 9 .. Tue 15 (5 weekdays)
    assert "warned about 5 gap(s)" in run.log[-1]
    assert "already warned today" in engine.run_rule_id(rule.id, manual=True).log[-1]
    note = session.scalar(select(Notification).where(Notification.title.like("Understaffing%")))
    assert note is not None and "Operations has 0/2 shifts staffed" in (note.body or "")
    company.ida.end_date = date(2026, 9, 20)
    session.commit()
    ending = rule_with(session, {"type": "contract_end_reminders", "params": {"days": 30}})
    assert (
        "reminded about 1 ending contract(s)" in engine.run_rule_id(ending.id, manual=True).log[-1]
    )
    assert "reminded about 0" in engine.run_rule_id(ending.id, manual=True).log[-1]


def test_report_service_api_and_page(
    client, session: Session, company: Company, engine: AutomationEngine
) -> None:
    good = rule_with(
        session,
        {"type": "notify_role", "params": {"role": "manager", "title": "x"}},
        name="good",
        minutes=10,
    )
    bad = rule_with(session, {"type": "webhook", "params": {"url": "ftp://x"}}, name="bad")
    engine.run_rule_id(good.id, manual=True)
    engine.run_rule_id(good.id, manual=True)
    engine.run_rule_id(bad.id, manual=True)
    report = dashboard.automation_report(session, days=30)
    assert (
        report["total_runs"] == 3
        and report["total_failed"] == 1
        and report["total_minutes_saved"] == 20
    )
    assert report["rows"][0]["rule"].name == "good" and report["rows"][0]["share"] == 100
    login(client, "mgr@nexum.test")
    api = client.get("/api/v1/automations/report", params={"days": 7}).json()
    assert api["total_runs"] == 3 and api["rules"][1]["last_failure"].startswith("ValidationError")
    from tests.test_web import web_login

    web_login(client, "mgr@nexum.test")
    html = client.get("/automations/report?days=7").text
    assert "Automation report" in html and "good" in html and "ValidationError" in html


def test_rule_editor_dry_run_duplicate_and_preferences(
    client, session: Session, company: Company, recipes: None
) -> None:
    from tests.test_web import web_login

    web_login(client, "admin@nexum.test")
    r = post(
        client,
        "/automations/new",
        {
            "name": "Weekly digest",
            "trigger_type": "schedule",
            "schedule_kind": "weekly",
            "weekday": "4",
            "at": "15:30",
            "conditions": "[]",
            "actions": json.dumps([{"type": "log", "params": {"message": "hi"}}]),
            "estimated_minutes_saved": "3",
            "enabled": "1",
        },
    )
    assert r.status_code == 303, r.text
    rule_id = int(r.headers["location"].rsplit("/", 1)[1])
    rule = session.get(AutomationRule, rule_id)
    assert rule.trigger_config == {"kind": "weekly", "weekday": 4, "at": "15:30"}
    html = client.get(f"/automations/{rule_id}/edit").text
    assert 'value="Weekly digest"' in html and 'value="15:30"' in html
    r = post(
        client,
        f"/automations/{rule_id}/edit",
        {
            "name": "Weekly digest",
            "trigger_type": "event",
            "event": "shift.created",
            "conditions": "[]",
            "actions": json.dumps([{"type": "log"}]),
            "estimated_minutes_saved": "4",
            "enabled": "0",
        },
    )
    assert r.status_code == 303
    session.expire_all()
    rule = session.get(AutomationRule, rule_id)
    assert (
        rule.trigger_config == {"event": "shift.created"}
        and rule.enabled is False
        and rule.estimated_minutes_saved == 4
    )
    r = post(
        client,
        f"/automations/{rule_id}/edit",
        {
            "name": "Weekly digest",
            "trigger_type": "schedule",
            "schedule_kind": "interval",
            "interval_minutes": "abc",
            "conditions": "[]",
            "actions": "[]",
        },
    )
    assert r.status_code == 422 and "whole numbers" in r.text
    r = post(
        client,
        f"/automations/{rule_id}/edit",
        {
            "name": "",
            "trigger_type": "manual",
            "conditions": "[]",
            "actions": json.dumps([{"type": "log"}]),
        },
    )
    assert r.status_code == 422 and "Name is required" in r.text
    assert post(client, f"/automations/{rule_id}/dry-run").status_code == 303
    assert "Dry run" in client.get(f"/automations/{rule_id}").text
    r = post(client, f"/automations/{rule_id}/duplicate")
    assert r.status_code == 303 and r.headers["location"].endswith("/edit")
    copy = session.scalar(
        select(AutomationRule).where(AutomationRule.name == "Weekly digest (copy)")
    )
    assert copy is not None and copy.enabled is False
    assert (
        post(client, "/notifications/preferences", {"email_notifications": "0"}).status_code == 303
    )
    session.expire_all()
    assert session.get(type(company.admin), company.admin.id).email_notifications is False
    login(client, "admin@nexum.test")
    assert (
        client.post(f"/api/v1/automations/rules/{rule_id}/dry-run").json()["trigger_summary"]
        == "dry run"
    )
    assert client.post(f"/api/v1/automations/rules/{rule_id}/duplicate").status_code == 201
    assert (
        client.patch(
            "/api/v1/notifications/preferences", json={"email_notifications": True}
        ).json()["email_notifications"]
        is True
    )
    assert client.get("/api/v1/auth/me").json()["email_notifications"] is True
