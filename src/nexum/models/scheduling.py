"""Shifts and schedule templates."""

from __future__ import annotations

from datetime import datetime, time
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index, Integer, String, Text, Time
from sqlalchemy.orm import Mapped, mapped_column, relationship

from nexum.db import Base
from nexum.models.types import StrEnum, UTCDateTime, str_enum, utcnow

if TYPE_CHECKING:
    from nexum.models.identity import User
    from nexum.models.org import Department
    from nexum.models.people import Employee, Skill
    from nexum.models.time_tracking import TimeEntry


class ShiftStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    CANCELLED = "cancelled"


class Shift(Base):
    __tablename__ = "shifts"
    __table_args__ = (
        Index("ix_shifts_window", "starts_at", "ends_at"),
        Index("ix_shifts_employee_start", "employee_id", "starts_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    department_id: Mapped[int] = mapped_column(ForeignKey("departments.id", ondelete="CASCADE"))
    employee_id: Mapped[int | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), nullable=True
    )
    starts_at: Mapped[datetime] = mapped_column(UTCDateTime)
    ends_at: Mapped[datetime] = mapped_column(UTCDateTime)
    role_label: Mapped[str | None] = mapped_column(String(80), nullable=True)
    status: Mapped[ShiftStatus] = mapped_column(
        str_enum(ShiftStatus, "shift_status"), default=ShiftStatus.DRAFT
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    template_id: Mapped[int | None] = mapped_column(
        ForeignKey("schedule_templates.id", ondelete="SET NULL"), nullable=True
    )
    required_skill_id: Mapped[int | None] = mapped_column(
        ForeignKey("skills.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    department: Mapped[Department] = relationship(back_populates="shifts")
    required_skill: Mapped[Skill | None] = relationship()
    requests: Mapped[list[ShiftRequest]] = relationship(
        back_populates="shift", cascade="all, delete-orphan"
    )
    employee: Mapped[Employee | None] = relationship(back_populates="shifts")
    template: Mapped[ScheduleTemplate | None] = relationship(back_populates="shifts")
    time_entries: Mapped[list[TimeEntry]] = relationship(back_populates="shift")

    @property
    def is_open(self) -> bool:
        return self.employee_id is None and self.status != ShiftStatus.CANCELLED

    @property
    def required_skill_name(self) -> str | None:
        return self.required_skill.name if self.required_skill else None

    @property
    def duration_hours(self) -> float:
        return (self.ends_at - self.starts_at).total_seconds() / 3600.0


class ScheduleTemplate(Base):
    """A recurring weekly slot: '2 baristas, Monday 08:00-16:00'."""

    __tablename__ = "schedule_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    department_id: Mapped[int] = mapped_column(ForeignKey("departments.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(120))
    weekday: Mapped[int] = mapped_column(Integer)  # 0 = Monday ... 6 = Sunday
    start_time: Mapped[time] = mapped_column(Time)
    end_time: Mapped[time] = mapped_column(Time)
    headcount: Mapped[int] = mapped_column(Integer, default=1)
    role_label: Mapped[str | None] = mapped_column(String(80), nullable=True)
    required_skill_id: Mapped[int | None] = mapped_column(
        ForeignKey("skills.id", ondelete="SET NULL"), nullable=True
    )

    department: Mapped[Department] = relationship(back_populates="templates")
    shifts: Mapped[list[Shift]] = relationship(back_populates="template")
    required_skill: Mapped[Skill | None] = relationship()

    @property
    def required_skill_name(self) -> str | None:
        return self.required_skill.name if self.required_skill else None


class ShiftRequestKind(StrEnum):
    CLAIM = "claim"  # take an open shift
    DROP = "drop"  # give my shift back (becomes open)
    TRANSFER = "transfer"  # hand my shift to a named colleague


class ShiftRequestStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class ShiftRequest(Base):
    __tablename__ = "shift_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[ShiftRequestKind] = mapped_column(str_enum(ShiftRequestKind, "shift_request_kind"))
    status: Mapped[ShiftRequestStatus] = mapped_column(
        str_enum(ShiftRequestStatus, "shift_request_status"), default=ShiftRequestStatus.PENDING
    )
    shift_id: Mapped[int] = mapped_column(ForeignKey("shifts.id", ondelete="CASCADE"))
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"))
    target_employee_id: Mapped[int | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), nullable=True
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    decision_note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    shift: Mapped[Shift] = relationship(back_populates="requests")
    employee: Mapped[Employee] = relationship(
        back_populates="shift_requests", foreign_keys=[employee_id]
    )
    target_employee: Mapped[Employee | None] = relationship(foreign_keys=[target_employee_id])
    decided_by: Mapped[User | None] = relationship()
