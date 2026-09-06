"""Employees, contracts and time off."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Date, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from nexum.db import Base
from nexum.models.types import Hours, Money, StrEnum, UTCDateTime, str_enum, utcnow

if TYPE_CHECKING:
    from nexum.models.identity import User
    from nexum.models.org import Department
    from nexum.models.payroll import Payslip
    from nexum.models.scheduling import Shift
    from nexum.models.time_tracking import TimeEntry


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
