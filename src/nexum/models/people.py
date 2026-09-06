"""Employees, contracts and time off."""

from __future__ import annotations

import secrets
from datetime import date, datetime, time
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Column, Date, ForeignKey, Integer, String, Table, Text, Time
from sqlalchemy.orm import Mapped, mapped_column, relationship

from nexum.db import Base
from nexum.models.types import Hours, Money, StrEnum, UTCDateTime, str_enum, utcnow

if TYPE_CHECKING:
    from nexum.models.identity import User
    from nexum.models.org import Department
    from nexum.models.payroll import Payslip
    from nexum.models.scheduling import Shift, ShiftRequest
    from nexum.models.time_tracking import TimeEntry


employee_skills = Table(
    "employee_skills",
    Base.metadata,
    Column("employee_id", ForeignKey("employees.id", ondelete="CASCADE"), primary_key=True),
    Column("skill_id", ForeignKey("skills.id", ondelete="CASCADE"), primary_key=True),
)


class Skill(Base):
    __tablename__ = "skills"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)

    employees: Mapped[list[Employee]] = relationship(
        secondary=employee_skills, back_populates="skills"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Skill {self.name}>"


class AvailabilityKind(StrEnum):
    UNAVAILABLE = "unavailable"
    PREFERRED = "preferred"


def new_calendar_token() -> str:
    return secrets.token_urlsafe(24)


class EmploymentType(StrEnum):
    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    HOURLY = "hourly"
    CONTRACTOR = "contractor"


class PayType(StrEnum):
    MONTHLY = "monthly"
    HOURLY = "hourly"


class TimeOffKind(StrEnum):
    VACATION = "vacation"
    SICK = "sick"
    PARENTAL = "parental"
    UNPAID = "unpaid"
    OTHER = "other"


class TimeOffStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class Employee(Base):
    __tablename__ = "employees"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), unique=True, nullable=True
    )
    first_name: Mapped[str] = mapped_column(String(100))
    last_name: Mapped[str] = mapped_column(String(100))
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    title: Mapped[str | None] = mapped_column(String(120), nullable=True)
    department_id: Mapped[int | None] = mapped_column(
        ForeignKey("departments.id", ondelete="SET NULL"), nullable=True
    )
    employment_type: Mapped[EmploymentType] = mapped_column(
        str_enum(EmploymentType, "employment_type"), default=EmploymentType.FULL_TIME
    )
    pay_type: Mapped[PayType] = mapped_column(
        str_enum(PayType, "pay_type"), default=PayType.MONTHLY
    )
    monthly_salary: Mapped[Decimal] = mapped_column(Money(), default=Decimal("0"))
    hourly_rate: Mapped[Decimal] = mapped_column(Money(), default=Decimal("0"))
    currency: Mapped[str] = mapped_column(String(3), default="SEK")
    weekly_hours: Mapped[Decimal] = mapped_column(Hours(), default=Decimal("40"))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    calendar_token: Mapped[str] = mapped_column(String(48), unique=True, default=new_calendar_token)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    user: Mapped[User | None] = relationship(back_populates="employee")
    department: Mapped[Department | None] = relationship(
        back_populates="employees", foreign_keys=[department_id]
    )
    shifts: Mapped[list[Shift]] = relationship(back_populates="employee")
    time_entries: Mapped[list[TimeEntry]] = relationship(
        back_populates="employee", cascade="all, delete-orphan"
    )
    time_off_requests: Mapped[list[TimeOffRequest]] = relationship(
        back_populates="employee", cascade="all, delete-orphan"
    )
    payslips: Mapped[list[Payslip]] = relationship(
        back_populates="employee", cascade="all, delete-orphan"
    )
    skills: Mapped[list[Skill]] = relationship(
        secondary=employee_skills, back_populates="employees"
    )
    availability: Mapped[list[AvailabilityRule]] = relationship(
        back_populates="employee",
        cascade="all, delete-orphan",
        order_by="AvailabilityRule.weekday, AvailabilityRule.start_time",
    )
    shift_requests: Mapped[list[ShiftRequest]] = relationship(
        back_populates="employee",
        foreign_keys="ShiftRequest.employee_id",
        cascade="all, delete-orphan",
    )

    @property
    def skill_names(self) -> list[str]:
        return sorted(s.name for s in self.skills)

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Employee {self.full_name}>"


class TimeOffRequest(Base):
    __tablename__ = "time_off_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"))
    kind: Mapped[TimeOffKind] = mapped_column(str_enum(TimeOffKind, "time_off_kind"))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    status: Mapped[TimeOffStatus] = mapped_column(
        str_enum(TimeOffStatus, "time_off_status"), default=TimeOffStatus.PENDING
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    employee: Mapped[Employee] = relationship(back_populates="time_off_requests")

    @property
    def days(self) -> int:
        return (self.end_date - self.start_date).days + 1


class AvailabilityRule(Base):
    """Recurring weekly availability: 'unavailable Mondays 00:00-12:00', 'prefers evenings'."""

    __tablename__ = "availability_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"))
    weekday: Mapped[int] = mapped_column(Integer)  # 0 = Monday
    start_time: Mapped[time] = mapped_column(Time)
    end_time: Mapped[time] = mapped_column(Time)  # end <= start means "until the next day"
    kind: Mapped[AvailabilityKind] = mapped_column(
        str_enum(AvailabilityKind, "availability_kind"), default=AvailabilityKind.UNAVAILABLE
    )
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)

    employee: Mapped[Employee] = relationship(back_populates="availability")
