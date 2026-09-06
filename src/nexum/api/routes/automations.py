from __future__ import annotations

from fastapi import APIRouter, Query
from sqlalchemy import select

from nexum.api.deps import AdminUser, DbSession, ManagerUser
from nexum.api.schemas import ActionSpecOut, RuleIn, RuleOut, RulePatch, RunOut
from nexum.automation import get_engine
from nexum.automation.actions import list_actions
from nexum.automation.engine import AutomationEngine
from nexum.errors import NotFoundError
from nexum.models import AutomationRule, AutomationRun
from nexum.services import audit
from nexum.services.dashboard import automation_stats
from nexum.services.events import EVENT_NAMES

router = APIRouter(prefix="/automations", tags=["automations"])


def _get_rule(db: DbSession, rule_id: int) -> AutomationRule:
    rule = db.get(AutomationRule, rule_id)
    if rule is None:
        raise NotFoundError(f"Rule {rule_id} not found")
    return rule


@router.get("/rules", response_model=list[RuleOut])
def list_rules(db: DbSession, _: ManagerUser) -> list[RuleOut]:
    stmt = select(AutomationRule).order_by(AutomationRule.enabled.desc(), AutomationRule.name)
    return [RuleOut.model_validate(r) for r in db.scalars(stmt)]


@router.post("/rules", response_model=RuleOut, status_code=201)
def create_rule(payload: RuleIn, db: DbSession, user: AdminUser) -> RuleOut:
    AutomationEngine.validate_rule(
        payload.trigger_type, payload.trigger_config, payload.conditions, payload.actions
    )
    rule = AutomationRule(**payload.model_dump())
    db.add(rule)
    db.flush()
    audit.record(db, "automation.rule_created", "automation_rule", rule.id, actor=user)
    db.commit()
    return RuleOut.model_validate(rule)


@router.get("/rules/{rule_id}", response_model=RuleOut)
def get_rule(rule_id: int, db: DbSession, _: ManagerUser) -> RuleOut:
    return RuleOut.model_validate(_get_rule(db, rule_id))


@router.patch("/rules/{rule_id}", response_model=RuleOut)
def patch_rule(rule_id: int, payload: RulePatch, db: DbSession, user: AdminUser) -> RuleOut:
    rule = _get_rule(db, rule_id)
    changes = payload.model_dump(exclude_unset=True)
    merged = {
        "trigger_type": changes.get("trigger_type", rule.trigger_type),
        "trigger_config": changes.get("trigger_config", rule.trigger_config),
        "conditions": changes.get("conditions", rule.conditions),
        "actions": changes.get("actions", rule.actions),
    }
    AutomationEngine.validate_rule(
        merged["trigger_type"], merged["trigger_config"], merged["conditions"], merged["actions"]
    )
    for key, value in changes.items():
        setattr(rule, key, value)
    if "trigger_config" in changes or "trigger_type" in changes:
        rule.next_run_at = None
    audit.record(db, "automation.rule_updated", "automation_rule", rule.id, actor=user, **changes)
    db.commit()
    return RuleOut.model_validate(rule)


@router.delete("/rules/{rule_id}", status_code=204)
def delete_rule(rule_id: int, db: DbSession, user: AdminUser) -> None:
    rule = _get_rule(db, rule_id)
    audit.record(
        db, "automation.rule_deleted", "automation_rule", rule.id, actor=user, name=rule.name
    )
    db.delete(rule)
    db.commit()


@router.post("/rules/{rule_id}/toggle", response_model=RuleOut)
def toggle_rule(rule_id: int, db: DbSession, user: AdminUser) -> RuleOut:
    rule = _get_rule(db, rule_id)
    rule.enabled = not rule.enabled
    rule.next_run_at = None
    audit.record(
        db, "automation.rule_toggled", "automation_rule", rule.id, actor=user, enabled=rule.enabled
    )
    db.commit()
    return RuleOut.model_validate(rule)


@router.post("/rules/{rule_id}/run", response_model=RunOut)
def run_rule(rule_id: int, db: DbSession, user: ManagerUser) -> RunOut:
    _get_rule(db, rule_id)
    db.commit()
    run = get_engine().run_rule_id(rule_id, manual=True)
    return RunOut.model_validate(run)


@router.get("/runs", response_model=list[RunOut])
def list_runs(
    db: DbSession,
    _: ManagerUser,
    rule_id: int | None = None,
    limit: int = Query(default=50, ge=1, le=500),
) -> list[RunOut]:
    stmt = select(AutomationRun).order_by(AutomationRun.id.desc()).limit(limit)
    if rule_id is not None:
        stmt = stmt.where(AutomationRun.rule_id == rule_id)
    return [RunOut.model_validate(r) for r in db.scalars(stmt)]


@router.post("/tick", response_model=list[RunOut])
def tick(_: AdminUser) -> list[RunOut]:
    return [RunOut.model_validate(r) for r in get_engine().tick()]


@router.get("/actions", response_model=list[ActionSpecOut])
def actions(_: ManagerUser) -> list[ActionSpecOut]:
    return [
        ActionSpecOut(name=a.name, description=a.description, params=a.params)
        for a in list_actions()
    ]


@router.get("/events", response_model=list[str])
def events(_: ManagerUser) -> list[str]:
    return list(EVENT_NAMES)


@router.get("/stats")
def stats(db: DbSession, _: ManagerUser) -> dict[str, int | float]:
    return automation_stats(db)
