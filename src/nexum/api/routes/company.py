from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from nexum.api.deps import AdminUser, DbSession, ManagerUser
from nexum.models import PayPeriodType
from nexum.services import company
from nexum.services.calendar import COMMON_TIMEZONES

router = APIRouter(prefix="/company", tags=["company"])


class CompanyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    timezone: str
    currency: str
    default_weekly_hours: Decimal
    weekly_overtime_threshold_hours: Decimal
    overtime_multiplier: Decimal
    premium_rules: list[dict[str, Any]]
    holidays: list[dict[str, Any]]
    rounding_minutes: int
    auto_break_minutes: int
    auto_break_after_hours: Decimal
    vacation_days_per_year: Decimal
    pay_period_type: PayPeriodType
    biweekly_anchor: date | None
    email_notifications_enabled: bool
    email_configured: bool
    smtp_host: str | None
    smtp_port: int
    smtp_username: str | None
    smtp_from: str | None
    smtp_use_tls: bool
    updated_at: datetime


class CompanyPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    timezone: str | None = None
    currency: str | None = None
    default_weekly_hours: Decimal | None = None
    weekly_overtime_threshold_hours: Decimal | None = None
    overtime_multiplier: Decimal | None = None
    premium_rules: list[dict[str, Any]] | None = None
    holidays: list[dict[str, Any]] | None = None
    rounding_minutes: int | None = None
    auto_break_minutes: int | None = None
    auto_break_after_hours: Decimal | None = None
    vacation_days_per_year: Decimal | None = None
    pay_period_type: PayPeriodType | None = None
    biweekly_anchor: date | None = None
    email_notifications_enabled: bool | None = None
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    smtp_use_tls: bool | None = None


@router.get("", response_model=CompanyOut)
def get_company(db: DbSession, _: ManagerUser) -> CompanyOut:
    row = company.get_company(db)
    db.commit()
    return CompanyOut.model_validate(row)


@router.patch("", response_model=CompanyOut)
def patch_company(payload: CompanyPatch, db: DbSession, user: AdminUser) -> CompanyOut:
    changes = payload.model_dump(exclude_unset=True)
    try:
        row = company.update_company(db, actor=user, **changes)
    except ValueError as exc:  # unknown time zone
        from nexum.errors import ValidationError

        raise ValidationError(str(exc)) from exc
    db.commit()
    return CompanyOut.model_validate(row)


@router.get("/timezones", response_model=list[str])
def timezones(_: ManagerUser) -> list[str]:
    return list(COMMON_TIMEZONES)
