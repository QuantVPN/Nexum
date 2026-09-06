"""Pay periods and payslips."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, Date, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from nexum.db import Base
from nexum.models.types import Hours, Money, StrEnum, UTCDateTime, str_enum, utcnow

if TYPE_CHECKING:
    from nexum.models.people import Employee


class PayPeriodStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    PAID = "paid"


class PayPeriod(Base):
    __tablename__ = "pay_periods"
    __table_args__ = (UniqueConstraint("start_date", "end_date", name="uq_pay_period_window"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    status: Mapped[PayPeriodStatus] = mapped_column(
        str_enum(PayPeriodStatus, "pay_period_status"), default=PayPeriodStatus.OPEN
    )
    currency: Mapped[str] = mapped_column(String(3), default="SEK")
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    payslips: Mapped[list[Payslip]] = relationship(
        back_populates="pay_period", cascade="all, delete-orphan"
    )

    @property
    def label(self) -> str:
        return f"{self.start_date.isoformat()} to {self.end_date.isoformat()}"


class Payslip(Base):
    __tablename__ = "payslips"
    __table_args__ = (
        UniqueConstraint("pay_period_id", "employee_id", name="uq_payslip_period_emp"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    pay_period_id: Mapped[int] = mapped_column(ForeignKey("pay_periods.id", ondelete="CASCADE"))
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"))
    regular_hours: Mapped[Decimal] = mapped_column(Hours(), default=Decimal("0"))
    overtime_hours: Mapped[Decimal] = mapped_column(Hours(), default=Decimal("0"))
    premium_hours: Mapped[Decimal] = mapped_column(Hours(), default=Decimal("0"))
    base_amount: Mapped[Decimal] = mapped_column(Money(), default=Decimal("0"))
    overtime_amount: Mapped[Decimal] = mapped_column(Money(), default=Decimal("0"))
    premium_amount: Mapped[Decimal] = mapped_column(Money(), default=Decimal("0"))
    adjustments_amount: Mapped[Decimal] = mapped_column(Money(), default=Decimal("0"))
    gross_amount: Mapped[Decimal] = mapped_column(Money(), default=Decimal("0"))
    currency: Mapped[str] = mapped_column(String(3), default="SEK")
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    generated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    pay_period: Mapped[PayPeriod] = relationship(back_populates="payslips")
    employee: Mapped[Employee] = relationship(back_populates="payslips")
