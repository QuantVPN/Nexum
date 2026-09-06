"""The runner: evaluates rules on events and on schedule, records every run."""

from __future__ import annotations

import logging
import threading
import traceback
from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from nexum.automation import actions as action_registry
from nexum.automation import conditions, schedule
from nexum.automation.context import RunContext
from nexum.config import Settings, get_settings
from nexum.errors import NexumError, NotFoundError, ValidationError
from nexum.models import AutomationRule, AutomationRun, RunStatus, TriggerType
from nexum.models.types import utcnow
from nexum.services import events as event_bus
from nexum.services.events import DomainEvent

log = logging.getLogger("nexum.automation")

MAX_EVENT_DEPTH = 3


class AutomationEngine:
    def __init__(
        self,
        session_factory: sessionmaker[Session] | Callable[[], Session],
        settings: Settings | None = None,
        *,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._session_factory = session_factory
        self.settings = settings or get_settings()
        self.clock = clock
        self._local = threading.local()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    # --- wiring -------------------------------------------------------------------------

    def subscribe(self) -> None:
        event_bus.register_dispatcher(self.handle_event)

    def unsubscribe(self) -> None:
        event_bus.unregister_dispatcher(self.handle_event)

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start_background(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="nexum-automation", daemon=True)
        self._thread.start()
        log.info("automation ticker started (every %ss)", self.settings.automation_tick_seconds)

    def stop_background(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.wait(self.settings.automation_tick_seconds):
            try:
                self.tick()
            except Exception:  # pragma: no cover - defensive; never kill the ticker
                log.exception("automation tick failed")

    # --- entry points -------------------------------------------------------------------

    def handle_event(self, event: DomainEvent) -> list[AutomationRun]:
        depth = getattr(self._local, "depth", 0)
        if depth >= MAX_EVENT_DEPTH:
            log.warning("event %s ignored: automation depth limit reached", event.name)
            return []
        self._local.depth = depth + 1
        try:
            runs: list[AutomationRun] = []
            with self._session_factory() as session:
                stmt = select(AutomationRule).where(
                    AutomationRule.enabled.is_(True),
                    AutomationRule.trigger_type == TriggerType.EVENT,
                )
                rules = [r for r in session.scalars(stmt) if r.event_name == event.name]
                rule_ids = [r.id for r in rules]
            for rule_id in rule_ids:
                runs.append(self.run_rule_id(rule_id, event=event, depth=depth + 1))
            return runs
        finally:
            self._local.depth = depth

    def tick(self, now: datetime | None = None) -> list[AutomationRun]:
        """Run every scheduled rule that is due, then advance its ``next_run_at``."""
        now = now or self.clock()
        with self._lock:
            due: list[int] = []
            with self._session_factory() as session:
                from nexum.services.company import prime_timezone

                prime_timezone(session)
                stmt = select(AutomationRule).where(
                    AutomationRule.enabled.is_(True),
                    AutomationRule.trigger_type == TriggerType.SCHEDULE,
                )
                for rule in session.scalars(stmt):
                    if rule.next_run_at is None:
                        rule.next_run_at = schedule.next_run(rule.trigger_config, now)
                        continue
                    if rule.next_run_at <= now:
                        due.append(rule.id)
                session.commit()
            runs: list[AutomationRun] = []
            for rule_id in due:
                runs.append(self.run_rule_id(rule_id, now=now))
                with self._session_factory() as session:
                    stored = session.get(AutomationRule, rule_id)
                    if stored is not None:
                        stored.next_run_at = schedule.next_run(stored.trigger_config, now)
                        session.commit()
            return runs

    def run_rule_id(
        self,
        rule_id: int,
        *,
        event: DomainEvent | None = None,
        now: datetime | None = None,
        manual: bool = False,
        depth: int = 0,
    ) -> AutomationRun:
        with self._session_factory() as session:
            rule = session.get(AutomationRule, rule_id)
            if rule is None:
                raise NotFoundError(f"Automation rule {rule_id} not found")
            run = self.run_rule(session, rule, event=event, now=now, manual=manual, depth=depth)
            if run.id is not None:  # persisted; detach so callers can use it after close
                session.refresh(run)
                session.expunge(run)
            return run

    def run_rule(
        self,
        session: Session,
        rule: AutomationRule,
        *,
        event: DomainEvent | None = None,
        now: datetime | None = None,
        manual: bool = False,
        depth: int = 0,
    ) -> AutomationRun:
        now = now or self.clock()
        ctx = RunContext(
            session=session, settings=self.settings, rule=rule, now=now, event=event, depth=depth
        )
        started = now
        trigger_summary = (
            "manual"
            if manual
            else (event.summary if event else schedule.describe(rule.trigger_config))
        )
        status = RunStatus.SUCCESS
        error: str | None = None
        work_done = False
        pending: list[DomainEvent] = []

        try:
            if not conditions.evaluate(rule.conditions, ctx.as_mapping()):
                status = RunStatus.SKIPPED
                ctx.say("conditions not met")
            else:
                for entry in rule.actions:
                    result = action_registry.run_action(ctx, entry)
                    ctx.say(f"{entry.get('type')}: {result.message}")
                    work_done = work_done or result.work_done
                    if result.data:
                        ctx.data.update(result.data)
            pending = event_bus.drain_events(session)
        except NexumError as exc:
            session.rollback()
            status, error = RunStatus.FAILED, f"{type(exc).__name__}: {exc}"
            ctx.say(error)
        except Exception as exc:
            session.rollback()
            status, error = RunStatus.FAILED, f"{type(exc).__name__}: {exc}"
            ctx.say(traceback.format_exc(limit=5))
            log.exception("automation rule %s failed", rule.name)

        rule = session.merge(rule) if status == RunStatus.FAILED else rule
        finished = self.clock()
        if status == RunStatus.SKIPPED and event is not None and not manual:
            # Event rules see every event of their kind; a non-match is routine, not history.
            log.debug("rule %s skipped event %s (conditions not met)", rule.name, event.name)
            return AutomationRun(
                rule_id=rule.id,
                started_at=started,
                finished_at=finished,
                status=status,
                trigger_summary=trigger_summary[:255],
                log=list(ctx.log),
            )
        run = AutomationRun(
            rule_id=rule.id,
            started_at=started,
            finished_at=finished,
            status=status,
            trigger_summary=trigger_summary[:255],
            log=list(ctx.log),
            error=error,
            minutes_saved=rule.estimated_minutes_saved
            if (status == RunStatus.SUCCESS and work_done)
            else 0,
        )
        session.add(run)
        rule.run_count = (rule.run_count or 0) + 1
        rule.last_run_at = finished
        session.commit()
        if pending and depth < MAX_EVENT_DEPTH:
            event_bus.dispatch(pending)
        return run

    # --- rule management ----------------------------------------------------------------

    @staticmethod
    def validate_rule(
        trigger_type: TriggerType, trigger_config: dict[str, Any], conds: Any, acts: Any
    ) -> None:
        if trigger_type == TriggerType.SCHEDULE:
            schedule.validate(trigger_config)
        elif trigger_type == TriggerType.EVENT and not isinstance(trigger_config.get("event"), str):
            raise ValidationError("event triggers need trigger_config.event")
        conditions.validate(conds)
        action_registry.validate_actions(acts)


_engine: AutomationEngine | None = None


def get_engine() -> AutomationEngine:
    global _engine
    if _engine is None:
        from nexum.db import session_factory

        # Resolve the session factory on every call so a reconfigured database (tests,
        # CLI flags) is picked up without rebuilding the engine.
        _engine = AutomationEngine(lambda: session_factory()())
    return _engine


def set_engine(engine: AutomationEngine | None) -> None:
    global _engine
    _engine = engine
