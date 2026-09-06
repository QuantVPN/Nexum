"""Company settings (single row) and the payroll rules derived from them."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.orm import Session

from nexum.config import get_settings
from nexum.errors import ValidationError
from nexum.models import User
from nexum.models.company import SINGLETON_ID, CompanySettings, PayPeriodType
from nexum.services import audit
from nexum.services.calendar import load_timezone, set_timezone

EDITABLE_FIELDS: tuple[str, ...] = (
    "name",
    "timezone",
    "currency",
    "default_weekly_hours",
    "weekly_overtime_threshold_hours",
    "overtime_multiplier",
    "premium_rules",
    "holidays",
    "rounding_minutes",
    "auto_break_minutes",
    "auto_break_after_hours",
    "vacation_days_per_year",
    "pay_period_type",
    "biweekly_anchor",
    "email_notifications_enabled",
    "smtp_host",
    "smtp_port",
    "smtp_username",
    "smtp_password",
    "smtp_from",
    "smtp_use_tls",
)


@dataclass(frozen=True)
class PremiumWindow:
    label: str
    weekdays: frozenset[int]
    start: time
    end: time  # end <= start means the window runs past midnight
    multiplier: Decimal


@dataclass(frozen=True)
class Holiday:
    day: date
    label: str
    multiplier: Decimal


@dataclass
class PayrollRules:
    currency: str = "SEK"
    weekly_overtime_threshold_hours: Decimal = Decimal("40")
    overtime_multiplier: Decimal = Decimal("1.5")
    premium_windows: list[PremiumWindow] = field(default_factory=list)
    holidays: dict[date, Holiday] = field(default_factory=dict)
    rounding_minutes: int = 0
    auto_break_minutes: int = 0
    auto_break_after_hours: Decimal = Decimal("6")
    vacation_days_per_year: Decimal = Decimal("25")
    pay_period_type: PayPeriodType = PayPeriodType.MONTHLY
    biweekly_anchor: date | None = None


def get_company(session: Session) -> CompanySettings:
    """The settings row, created from environment defaults on first use."""
    row = session.get(CompanySettings, SINGLETON_ID)
    if row is None:
        env = get_settings()
        row = CompanySettings(
            id=SINGLETON_ID,
            currency=env.default_currency,
            weekly_overtime_threshold_hours=Decimal(str(env.weekly_overtime_threshold_hours)),
            overtime_multiplier=Decimal(str(env.overtime_multiplier)),
            timezone=env.timezone,
            name=env.company_name,
        )
        session.add(row)
        session.flush()
    set_timezone(row.timezone)
    return row


def prime_timezone(session: Session) -> str:
    return get_company(session).timezone


# --- validation -------------------------------------------------------------------------------


def _parse_time(value: Any, field_name: str) -> time:
    try:
        return time.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field_name}: '{value}' is not a valid HH:MM time") from exc


def _decimal(value: Any, field_name: str, minimum: Decimal | None = None) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError(f"{field_name}: '{value}' is not a number") from exc
    if minimum is not None and result < minimum:
        raise ValidationError(f"{field_name}: must be at least {minimum}")
    return result


def validate_premium_rules(rules: Any) -> list[dict[str, Any]]:
    if rules is None:
        return []
    if not isinstance(rules, list):
        raise ValidationError("premium_rules must be a list")
    cleaned: list[dict[str, Any]] = []
    for index, rule in enumerate(rules, start=1):
        if not isinstance(rule, dict):
            raise ValidationError(f"premium rule {index}: must be an object")
        label = str(rule.get("label") or f"Premium {index}")
        weekdays = rule.get("weekdays", [0, 1, 2, 3, 4, 5, 6])
        if not isinstance(weekdays, list) or not all(
            isinstance(d, int) and 0 <= d <= 6 for d in weekdays
        ):
            raise ValidationError(f"premium rule '{label}': weekdays must be a list of 0..6")
        start = _parse_time(rule.get("start", "00:00"), f"premium rule '{label}' start")
        end = _parse_time(rule.get("end", "00:00"), f"premium rule '{label}' end")
        multiplier = _decimal(
            rule.get("multiplier", 1), f"premium rule '{label}' multiplier", Decimal("1")
        )
        cleaned.append(
            {
                "label": label,
                "weekdays": sorted(set(weekdays)),
                "start": start.strftime("%H:%M"),
                "end": end.strftime("%H:%M"),
                "multiplier": str(multiplier),
            }
        )
    return cleaned


def validate_holidays(holidays: Any) -> list[dict[str, Any]]:
    if holidays is None:
        return []
    if not isinstance(holidays, list):
        raise ValidationError("holidays must be a list")
    cleaned: list[dict[str, Any]] = []
    for index, item in enumerate(holidays, start=1):
        if not isinstance(item, dict):
            raise ValidationError(f"holiday {index}: must be an object")
        try:
            day = date.fromisoformat(str(item.get("date")))
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"holiday {index}: 'date' must be YYYY-MM-DD") from exc
        multiplier = _decimal(item.get("multiplier", 1), f"holiday {day} multiplier", Decimal("1"))
        cleaned.append(
            {
                "date": day.isoformat(),
                "label": str(item.get("label") or "Holiday"),
                "multiplier": str(multiplier),
            }
        )
    cleaned.sort(key=lambda h: str(h["date"]))
    return cleaned


def update_company(
    session: Session, *, actor: User | None = None, **changes: Any
) -> CompanySettings:
    row = get_company(session)
    unknown = set(changes) - set(EDITABLE_FIELDS)
    if unknown:
        raise ValidationError(f"Unknown settings: {', '.join(sorted(unknown))}")
    if "timezone" in changes:
        try:
            load_timezone(str(changes["timezone"]))
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
    if "currency" in changes:
        currency = str(changes["currency"]).upper().strip()
        if len(currency) != 3 or not currency.isalpha():
            raise ValidationError("currency must be a 3-letter code such as SEK")
        changes["currency"] = currency
    for key in (
        "default_weekly_hours",
        "weekly_overtime_threshold_hours",
        "auto_break_after_hours",
        "vacation_days_per_year",
    ):
        if key in changes:
            changes[key] = _decimal(changes[key], key, Decimal("0"))
    if "overtime_multiplier" in changes:
        changes["overtime_multiplier"] = _decimal(
            changes["overtime_multiplier"], "overtime_multiplier", Decimal("1")
        )
    for key in ("rounding_minutes", "auto_break_minutes", "smtp_port"):
        if key in changes:
            try:
                changes[key] = int(changes[key])
            except (TypeError, ValueError) as exc:
                raise ValidationError(f"{key} must be a whole number") from exc
            if changes[key] < 0:
                raise ValidationError(f"{key} must not be negative")
    if "premium_rules" in changes:
        changes["premium_rules"] = validate_premium_rules(changes["premium_rules"])
    if "holidays" in changes:
        changes["holidays"] = validate_holidays(changes["holidays"])
    if "pay_period_type" in changes:
        changes["pay_period_type"] = PayPeriodType(str(changes["pay_period_type"]))
        if changes["pay_period_type"] == PayPeriodType.BIWEEKLY and not (
            changes.get("biweekly_anchor") or row.biweekly_anchor
        ):
            raise ValidationError("bi-weekly periods need a biweekly_anchor date")
    if "biweekly_anchor" in changes and isinstance(changes["biweekly_anchor"], str):
        raw = changes["biweekly_anchor"].strip()
        try:
            changes["biweekly_anchor"] = date.fromisoformat(raw) if raw else None
        except ValueError as exc:
            raise ValidationError("biweekly_anchor must be YYYY-MM-DD") from exc
    for key, value in changes.items():
        setattr(row, key, value)
    session.flush()
    set_timezone(row.timezone)
    audit.record(
        session,
        "company.settings_updated",
        "company_settings",
        row.id,
        actor=actor,
        fields=sorted(k for k in changes if not k.startswith("smtp_password")),
    )
    return row


def payroll_rules(session: Session) -> PayrollRules:
    return rules_from(get_company(session))


def rules_from(row: CompanySettings) -> PayrollRules:
    windows = [
        PremiumWindow(
            label=str(r["label"]),
            weekdays=frozenset(int(d) for d in r.get("weekdays", range(7))),
            start=time.fromisoformat(str(r.get("start", "00:00"))),
            end=time.fromisoformat(str(r.get("end", "00:00"))),
            multiplier=Decimal(str(r.get("multiplier", "1"))),
        )
        for r in row.premium_rules or []
    ]
    holidays = {
        date.fromisoformat(str(h["date"])): Holiday(
            day=date.fromisoformat(str(h["date"])),
            label=str(h.get("label", "Holiday")),
            multiplier=Decimal(str(h.get("multiplier", "1"))),
        )
        for h in row.holidays or []
    }
    return PayrollRules(
        currency=row.currency,
        weekly_overtime_threshold_hours=row.weekly_overtime_threshold_hours,
        overtime_multiplier=row.overtime_multiplier,
        premium_windows=windows,
        holidays=holidays,
        rounding_minutes=row.rounding_minutes,
        auto_break_minutes=row.auto_break_minutes,
        auto_break_after_hours=row.auto_break_after_hours,
        vacation_days_per_year=row.vacation_days_per_year,
        pay_period_type=row.pay_period_type,
        biweekly_anchor=row.biweekly_anchor,
    )
