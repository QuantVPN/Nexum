"""Automation rules and their execution history."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from nexum.db import Base
from nexum.models.types import StrEnum, UTCDateTime, str_enum, utcnow


class TriggerType(StrEnum):
    SCHEDULE = "schedule"
    EVENT = "event"
    MANUAL = "manual"


class RunStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


class AutomationRule(Base):
    """trigger -> conditions -> actions.

    ``trigger_config`` examples::

        {"kind": "interval", "minutes": 15}
        {"kind": "daily", "at": "07:00"}
        {"kind": "weekly", "weekday": 0, "at": "06:00"}
        {"event": "shift.created"}

    ``conditions`` is a list of ``{"field": "shift.employee_id", "op": "is_null"}`` style
    checks (see ``nexum.automation.conditions``). ``actions`` is an ordered list of
    ``{"type": "notify_role", "params": {...}}`` entries executed by the action registry.
    """

    __tablename__ = "automation_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str | None] = mapped_column(String(80), unique=True, nullable=True)
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    trigger_type: Mapped[TriggerType] = mapped_column(str_enum(TriggerType, "trigger_type"))
    trigger_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    conditions: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    actions: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    estimated_minutes_saved: Mapped[int] = mapped_column(Integer, default=0)
    run_count: Mapped[int] = mapped_column(Integer, default=0)
    last_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    runs: Mapped[list[AutomationRun]] = relationship(
        back_populates="rule", cascade="all, delete-orphan", order_by="AutomationRun.id.desc()"
    )

    @property
    def event_name(self) -> str | None:
        if self.trigger_type != TriggerType.EVENT:
            return None
        value = self.trigger_config.get("event")
        return str(value) if value else None


class AutomationRun(Base):
    __tablename__ = "automation_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    rule_id: Mapped[int] = mapped_column(ForeignKey("automation_rules.id", ondelete="CASCADE"))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    status: Mapped[RunStatus] = mapped_column(str_enum(RunStatus, "run_status"))
    trigger_summary: Mapped[str] = mapped_column(String(255), default="")
    log: Mapped[list[str]] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    minutes_saved: Mapped[int] = mapped_column(Integer, default=0)

    rule: Mapped[AutomationRule] = relationship(back_populates="runs")

    @property
    def duration_ms(self) -> int | None:
        if self.finished_at is None:
            return None
        return int((self.finished_at - self.started_at).total_seconds() * 1000)
