"""Company-wide settings: one row that drives time zone, payroll rules and delivery."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, Boolean, Date, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from nexum.db import Base
from nexum.models.types import FixedPoint, Hours, StrEnum, UTCDateTime, str_enum, utcnow

SINGLETON_ID = 1


class PayPeriodType(StrEnum):
    MONTHLY = "monthly"
    BIWEEKLY = "biweekly"


class CompanySettings(Base):
    __tablename__ = "company_settings"

    id: Mapped[int] = mapped_column(primary_key=True, default=SINGLETON_ID)
    name: Mapped[str] = mapped_column(String(160), default="My company")
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    currency: Mapped[str] = mapped_column(String(3), default="SEK")
    default_weekly_hours: Mapped[Decimal] = mapped_column(Hours(), default=Decimal("40"))

    # Payroll rules
    weekly_overtime_threshold_hours: Mapped[Decimal] = mapped_column(Hours(), default=Decimal("40"))
    overtime_multiplier: Mapped[Decimal] = mapped_column(FixedPoint(2), default=Decimal("1.5"))
    premium_rules: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    holidays: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    rounding_minutes: Mapped[int] = mapped_column(Integer, default=0)
    auto_break_minutes: Mapped[int] = mapped_column(Integer, default=0)
    auto_break_after_hours: Mapped[Decimal] = mapped_column(Hours(), default=Decimal("6"))
    vacation_days_per_year: Mapped[Decimal] = mapped_column(Hours(), default=Decimal("25"))
    pay_period_type: Mapped[PayPeriodType] = mapped_column(
        str_enum(PayPeriodType, "pay_period_type"), default=PayPeriodType.MONTHLY
    )
    biweekly_anchor: Mapped[date | None] = mapped_column(Date, nullable=True)

    # E-mail delivery
    email_notifications_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    smtp_host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    smtp_port: Mapped[int] = mapped_column(Integer, default=587)
    smtp_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    smtp_password: Mapped[str | None] = mapped_column(String(255), nullable=True)
    smtp_from: Mapped[str | None] = mapped_column(String(255), nullable=True)
    smtp_use_tls: Mapped[bool] = mapped_column(Boolean, default=True)

    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    @property
    def email_configured(self) -> bool:
        return bool(self.smtp_host and self.smtp_from)
