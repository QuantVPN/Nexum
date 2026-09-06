from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from nexum.automation import AutomationEngine
from nexum.automation.actions import ActionResult, action
from nexum.automation.context import RunContext
from nexum.automation.engine import MAX_EVENT_DEPTH
from nexum.automation.recipes import RECIPES, install_recipes
from nexum.errors import NotFoundError, ValidationError
from nexum.models import AutomationRule, AutomationRun, Notification, RunStatus, Shift, TriggerType
from nexum.services import scheduling
from nexum.services.events import commit_and_dispatch
from tests.conftest import Company, FakeClock


def make_rule(session: Session, **kwargs) -> AutomationRule:
    rule = AutomationRule(
        name=kwargs.pop("name", "rule"),
        trigger_type=kwargs.pop("trigger_type", TriggerType.MANUAL),
        **kwargs,
    )
    session.add(rule)
    session.commit()
    return rule


def test_event_rule_runs_and_records(
    session: Session, company: Company, engine: AutomationEngine
) -> None:
    rule = make_rule(
        session,
        trigger_type=TriggerType.EVENT,
        trigger_config={"event": "shift.created"},
        conditions=[{"field": "event.is_open", "op": "is_true"}],
        actions=[
            {
                "type": "log",
                "params": {"message": "open shift {event.shift_id} in {department.name}"},
            }
        ],
        estimated_minutes_saved=4,
    )
    start = datetime(2026, 9, 7, 8, tzinfo=UTC)
    scheduling.create_shift(
        session, department=company.department, starts_at=start, ends_at=start + timedelta(hours=4)
    )
    scheduling.create_shift(
        session,
        department=company.department,
        starts_at=start,
        ends_at=start + timedelta(hours=4),
        employee=company.eva,
    )
    commit_and_dispatch(session)
    runs = session.scalars(
        select(AutomationRun).where(AutomationRun.rule_id == rule.id).order_by(AutomationRun.id)
    ).all()
    # the assigned shift did not match the condition: no run row is kept for event non-matches
    assert [r.status for r in runs] == [RunStatus.SUCCESS]
    assert runs[0].log[0].startswith("open shift ") and "Operations" in runs[0].log[0]
    assert runs[0].minutes_saved == 0  # log action does no "work"
    assert runs[0].trigger_summary.startswith("shift.created(")
    session.refresh(rule)
    assert rule.run_count == 1 and rule.last_run_at is not None


def test_manual_run_with_unmet_conditions_is_recorded_as_skipped(
    session: Session, company: Company, engine: AutomationEngine
) -> None:
    rule = make_rule(
        session,
        conditions=[{"field": "now.hour", "op": "eq", "value": 23}],
        actions=[{"type": "log"}],
    )
    run = engine.run_rule_id(rule.id, manual=True)
    assert run.status == RunStatus.SKIPPED and run.log == ["conditions not met"]
    assert (
        session.scalar(select(func.count(AutomationRun.id)).where(AutomationRun.rule_id == rule.id))
        == 1
    )


def test_minutes_saved_only_when_work_done(
    session: Session, company: Company, engine: AutomationEngine
) -> None:
    rule = make_rule(
        session,
        actions=[{"type": "notify_role", "params": {"role": "manager", "title": "hi"}}],
        estimated_minutes_saved=7,
    )
    run = engine.run_rule(session, rule, manual=True)
    assert run.status == RunStatus.SUCCESS and run.minutes_saved == 7
    assert run.trigger_summary == "manual"


def test_failed_action_rolls_back_and_is_recorded(
    session: Session, company: Company, engine: AutomationEngine
) -> None:
    rule = make_rule(
        session,
        actions=[
            {
                "type": "notify_role",
                "params": {"role": "manager", "title": "should be rolled back"},
            },
            {"type": "webhook", "params": {"url": "ftp://not-allowed"}},
        ],
    )
    run = engine.run_rule_id(rule.id, manual=True)
    assert run.status == RunStatus.FAILED
    assert "ValidationError" in (run.error or "")
    assert session.scalar(select(func.count(Notification.id))) == 0
    session.expire_all()
    stored = session.get(AutomationRule, rule.id)
    assert stored is not None and stored.run_count == 1


def test_unknown_action_and_bad_params(
    session: Session, company: Company, engine: AutomationEngine
) -> None:
    rule = make_rule(session, actions=[{"type": "does_not_exist"}])
    assert engine.run_rule_id(rule.id, manual=True).status == RunStatus.FAILED
    rule2 = make_rule(session, actions=[{"type": "log", "params": {"bogus": 1}}])
    run = engine.run_rule_id(rule2.id, manual=True)
    assert run.status == RunStatus.SUCCESS  # log accepts **params


def test_tick_runs_due_rules_once_and_advances(
    session: Session, company: Company, engine: AutomationEngine, clock: FakeClock
) -> None:
    rule = make_rule(
        session,
        trigger_type=TriggerType.SCHEDULE,
        trigger_config={"kind": "daily", "at": "06:00"},
        actions=[{"type": "log", "params": {"message": "tick"}}],
    )
    assert engine.tick() == []  # first tick only schedules
    session.refresh(rule)
    assert rule.next_run_at == datetime(2026, 9, 9, 6, tzinfo=UTC)
    assert engine.tick() == []  # not due yet
    clock.now = datetime(2026, 9, 12, 7, tzinfo=UTC)  # several days later: catches up with ONE run
    runs = engine.tick()
    assert len(runs) == 1 and runs[0].status == RunStatus.SUCCESS
    session.refresh(rule)
    assert rule.next_run_at == datetime(2026, 9, 13, 6, tzinfo=UTC)
    assert engine.tick() == []


def test_disabled_rules_do_not_run(
    session: Session, company: Company, engine: AutomationEngine, clock: FakeClock
) -> None:
    rule = make_rule(
        session,
        trigger_type=TriggerType.SCHEDULE,
        trigger_config={"kind": "interval", "minutes": 1},
        actions=[{"type": "log"}],
        enabled=False,
    )
    engine.tick()
    clock.now += timedelta(hours=1)
    assert engine.tick() == []
    ev = make_rule(
        session,
        trigger_type=TriggerType.EVENT,
        trigger_config={"event": "shift.created"},
        actions=[{"type": "log"}],
        enabled=False,
    )
    start = datetime(2026, 9, 7, 8, tzinfo=UTC)
    scheduling.create_shift(
        session, department=company.department, starts_at=start, ends_at=start + timedelta(hours=4)
    )
    commit_and_dispatch(session)
    assert session.scalar(select(func.count(AutomationRun.id))) == 0
    assert rule.id and ev.id


@action("test_create_shift", "test-only: creates an open shift (emits shift.created)")
def _create_shift_action(ctx: RunContext, **params) -> ActionResult:
    from nexum.models import Department

    dept = ctx.session.get(Department, params["department_id"])
    assert dept is not None
    start = datetime(2026, 9, 7, 8, tzinfo=UTC) + timedelta(days=ctx.depth)
    scheduling.create_shift(
        ctx.session, department=dept, starts_at=start, ends_at=start + timedelta(hours=1)
    )
    return ActionResult("created", count=1, work_done=True)


def test_event_recursion_is_depth_limited(
    session: Session, company: Company, engine: AutomationEngine
) -> None:
    make_rule(
        session,
        trigger_type=TriggerType.EVENT,
        trigger_config={"event": "shift.created"},
        actions=[{"type": "test_create_shift", "params": {"department_id": company.department.id}}],
    )
    start = datetime(2026, 9, 1, 8, tzinfo=UTC)
    scheduling.create_shift(
        session, department=company.department, starts_at=start, ends_at=start + timedelta(hours=1)
    )
    commit_and_dispatch(session)
    runs = session.scalar(select(func.count(AutomationRun.id)))
    shifts = session.scalar(select(func.count(Shift.id)))
    assert runs == MAX_EVENT_DEPTH
    assert shifts == 1 + MAX_EVENT_DEPTH


def test_run_rule_id_missing(engine: AutomationEngine) -> None:
    with pytest.raises(NotFoundError):
        engine.run_rule_id(999, manual=True)


def test_validate_rule() -> None:
    AutomationEngine.validate_rule(
        TriggerType.SCHEDULE, {"kind": "daily", "at": "06:00"}, [], [{"type": "log"}]
    )
    AutomationEngine.validate_rule(
        TriggerType.EVENT, {"event": "shift.created"}, None, [{"type": "log"}]
    )
    with pytest.raises(ValidationError):
        AutomationEngine.validate_rule(TriggerType.EVENT, {}, [], [{"type": "log"}])
    with pytest.raises(ValidationError):
        AutomationEngine.validate_rule(TriggerType.MANUAL, {}, [], [])
    with pytest.raises(ValidationError):
        AutomationEngine.validate_rule(TriggerType.MANUAL, {}, [], [{"type": "nope"}])
    with pytest.raises(ValidationError):
        AutomationEngine.validate_rule(
            TriggerType.MANUAL, {}, [{"field": "x", "op": "??"}], [{"type": "log"}]
        )


def test_recipes_install_and_reset(session: Session) -> None:
    installed = install_recipes(session)
    assert len(installed) == len(RECIPES)
    assert install_recipes(session) == []  # idempotent
    rule = session.scalar(select(AutomationRule).where(AutomationRule.key == "daily-cover"))
    assert rule is not None
    rule.enabled = False
    rule.actions = []
    session.commit()
    reset = install_recipes(session, reset=True)
    assert len(reset) == len(RECIPES)
    session.refresh(rule)
    assert (
        rule.actions and rule.enabled is False
    )  # reset restores definition, keeps the operator's on/off choice
    for recipe in RECIPES:  # every recipe must validate against the registry
        AutomationEngine.validate_rule(
            recipe["trigger_type"],
            recipe["trigger_config"],
            recipe.get("conditions"),
            recipe["actions"],
        )


def test_background_ticker_starts_and_stops(engine: AutomationEngine) -> None:
    engine.settings = engine.settings.model_copy(update={"automation_tick_seconds": 1})
    engine.start_background()
    engine.start_background()  # idempotent
    assert engine._thread is not None and engine._thread.is_alive()
    engine.stop_background()
    assert engine._thread is None
