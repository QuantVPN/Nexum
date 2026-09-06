"""Pydantic request/response models for the JSON API."""

from __future__ import annotations

import re
from datetime import date, datetime, time
from decimal import Decimal
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator

from nexum.models import (
    AvailabilityKind,
    EmploymentType,
    NotificationLevel,
    PayPeriodStatus,
    PayType,
    Role,
    RunStatus,
    ShiftRequestKind,
    ShiftRequestStatus,
    ShiftStatus,
    TimeEntryStatus,
    TimeOffKind,
    TimeOffStatus,
    TriggerType,
)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_email(value: str) -> str:
    """Lower-case and sanity-check an address. Deliberately permissive: internal domains
    such as ``ops.local`` or ``corp.test`` are common in company tooling."""
    value = value.strip().lower()
    if not _EMAIL_RE.match(value):
        raise ValueError("must be a valid email address")
    return value


Email = Annotated[str, AfterValidator(normalize_email)]


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- auth ---------------------------------------------------------------------------------


class LoginIn(BaseModel):
    email: Email
    password: str = Field(min_length=1)


class UserOut(ORMModel):
    id: int
    email: str
    full_name: str
    role: Role
    is_active: bool
    employee_id: int | None = None


# --- org & people -------------------------------------------------------------------------


class DepartmentIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    cost_center: str | None = None


class DepartmentOut(ORMModel):
    id: int
    name: str
    cost_center: str | None
    manager_id: int | None


class EmployeeIn(BaseModel):
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    email: Email
    start_date: date
    department_id: int | None = None
    title: str | None = None
    employment_type: EmploymentType = EmploymentType.FULL_TIME
    pay_type: PayType = PayType.MONTHLY
    monthly_salary: Decimal = Decimal("0")
    hourly_rate: Decimal = Decimal("0")
    weekly_hours: Decimal = Decimal("40")
    currency: str = Field(default="SEK", min_length=3, max_length=3)
    login_password: str | None = Field(default=None, min_length=8)
    role: Role = Role.EMPLOYEE
    skills: list[str] = Field(default_factory=list)


class EmployeePatch(BaseModel):
    first_name: str | None = Field(default=None, min_length=1, max_length=100)
    last_name: str | None = Field(default=None, min_length=1, max_length=100)
    title: str | None = None
    department_id: int | None = None
    employment_type: EmploymentType | None = None
    pay_type: PayType | None = None
    monthly_salary: Decimal | None = None
    hourly_rate: Decimal | None = None
    weekly_hours: Decimal | None = None
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    skills: list[str] | None = None
    role: Role | None = None


class AvailabilityIn(BaseModel):
    weekday: int = Field(ge=0, le=6)
    start_time: time
    end_time: time
    kind: AvailabilityKind = AvailabilityKind.UNAVAILABLE
    note: str | None = Field(default=None, max_length=200)


class AvailabilityOut(ORMModel):
    id: int
    weekday: int
    start_time: time
    end_time: time
    kind: AvailabilityKind
    note: str | None


class SkillsIn(BaseModel):
    skills: list[str]


class EmployeeOut(ORMModel):
    id: int
    user_id: int | None
    first_name: str
    last_name: str
    full_name: str
    email: str
    title: str | None
    department_id: int | None
    employment_type: EmploymentType
    pay_type: PayType
    monthly_salary: Decimal
    hourly_rate: Decimal
    weekly_hours: Decimal
    currency: str
    start_date: date
    end_date: date | None
    is_active: bool
    skill_names: list[str] = Field(default_factory=list)


class TimeOffIn(BaseModel):
    kind: TimeOffKind
    start_date: date
    end_date: date
    reason: str | None = None
    employee_id: int | None = Field(default=None, description="Managers may file for others")


class TimeOffOut(ORMModel):
    id: int
    employee_id: int
    kind: TimeOffKind
    start_date: date
    end_date: date
    days: int
    status: TimeOffStatus
    reason: str | None
    decided_at: datetime | None
    created_at: datetime


class DecisionIn(BaseModel):
    approve: bool


# --- scheduling ---------------------------------------------------------------------------


class ShiftIn(BaseModel):
    department_id: int
    starts_at: datetime
    ends_at: datetime
    employee_id: int | None = None
    role_label: str | None = None
    status: ShiftStatus = ShiftStatus.DRAFT
    notes: str | None = None
    required_skill: str | None = None


class ShiftOut(ORMModel):
    id: int
    department_id: int
    employee_id: int | None
    starts_at: datetime
    ends_at: datetime
    duration_hours: float
    role_label: str | None
    status: ShiftStatus
    notes: str | None
    template_id: int | None
    required_skill_name: str | None = None


class ShiftRequestIn(BaseModel):
    kind: ShiftRequestKind
    shift_id: int
    target_employee_id: int | None = None
    note: str | None = None


class ShiftRequestOut(ORMModel):
    id: int
    kind: ShiftRequestKind
    status: ShiftRequestStatus
    shift_id: int
    employee_id: int
    target_employee_id: int | None
    note: str | None
    decision_note: str | None
    decided_at: datetime | None
    created_at: datetime


class AssignIn(BaseModel):
    employee_id: int
    ignore_availability: bool = False


class PublishIn(BaseModel):
    shift_ids: list[int] | None = None
    start: datetime | None = None
    end: datetime | None = None


class GenerateIn(BaseModel):
    week_start: date
    department_id: int | None = None


class WindowIn(BaseModel):
    start: datetime
    end: datetime


class TemplateIn(BaseModel):
    department_id: int
    name: str = Field(min_length=1, max_length=120)
    weekday: int = Field(ge=0, le=6)
    start_time: time
    end_time: time
    headcount: int = Field(default=1, ge=1)
    role_label: str | None = None
    required_skill: str | None = None


class TemplateOut(ORMModel):
    id: int
    department_id: int
    name: str
    weekday: int
    start_time: time
    end_time: time
    headcount: int
    role_label: str | None
    required_skill_name: str | None = None


# --- time tracking ------------------------------------------------------------------------


class ClockIn(BaseModel):
    shift_id: int | None = None
    at: datetime | None = None


class ClockOut(BaseModel):
    at: datetime | None = None
    break_minutes: int = Field(default=0, ge=0)


class TimeEntryIn(BaseModel):
    clock_in: datetime
    clock_out: datetime
    break_minutes: int = Field(default=0, ge=0)
    shift_id: int | None = None
    note: str | None = None
    employee_id: int | None = None


class TimeEntryOut(ORMModel):
    id: int
    employee_id: int
    shift_id: int | None
    clock_in: datetime
    clock_out: datetime | None
    break_minutes: int
    worked_hours: float
    status: TimeEntryStatus
    note: str | None


# --- payroll ------------------------------------------------------------------------------


class PayPeriodIn(BaseModel):
    start_date: date
    end_date: date


class PayPeriodOut(ORMModel):
    id: int
    start_date: date
    end_date: date
    label: str
    status: PayPeriodStatus
    currency: str
    closed_at: datetime | None
    paid_at: datetime | None


class PayslipOut(ORMModel):
    id: int
    pay_period_id: int
    employee_id: int
    regular_hours: Decimal
    overtime_hours: Decimal
    premium_hours: Decimal = Decimal("0")
    base_amount: Decimal
    overtime_amount: Decimal
    premium_amount: Decimal = Decimal("0")
    adjustments_amount: Decimal
    gross_amount: Decimal
    currency: str
    details: dict[str, Any]
    generated_at: datetime


class PayPeriodDetailOut(PayPeriodOut):
    payslips: list[PayslipOut]
    totals: dict[str, Decimal]


# --- automation ---------------------------------------------------------------------------


class RuleIn(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str | None = None
    enabled: bool = True
    trigger_type: TriggerType
    trigger_config: dict[str, Any] = Field(default_factory=dict)
    conditions: list[dict[str, Any]] = Field(default_factory=list)
    actions: list[dict[str, Any]]
    estimated_minutes_saved: int = Field(default=0, ge=0)


class RulePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = None
    enabled: bool | None = None
    trigger_type: TriggerType | None = None
    trigger_config: dict[str, Any] | None = None
    conditions: list[dict[str, Any]] | None = None
    actions: list[dict[str, Any]] | None = None
    estimated_minutes_saved: int | None = Field(default=None, ge=0)


class RuleOut(ORMModel):
    id: int
    key: str | None
    name: str
    description: str | None
    enabled: bool
    trigger_type: TriggerType
    trigger_config: dict[str, Any]
    conditions: list[dict[str, Any]]
    actions: list[dict[str, Any]]
    estimated_minutes_saved: int
    run_count: int
    last_run_at: datetime | None
    next_run_at: datetime | None


class RunOut(ORMModel):
    id: int
    rule_id: int
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int | None
    status: RunStatus
    trigger_summary: str
    log: list[str]
    error: str | None
    minutes_saved: int


class ActionSpecOut(BaseModel):
    name: str
    description: str
    params: dict[str, str]


# --- notifications ------------------------------------------------------------------------


class NotificationOut(ORMModel):
    id: int
    title: str
    body: str | None
    level: NotificationLevel
    link: str | None
    source: str | None
    is_read: bool
    created_at: datetime


class MessageOut(BaseModel):
    detail: str


class CountOut(BaseModel):
    count: int


# --- validators shared by forms ------------------------------------------------------------


class PositiveDecimalMixin(BaseModel):
    @field_validator("monthly_salary", "hourly_rate", "weekly_hours", check_fields=False)
    @classmethod
    def _non_negative(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("must be non-negative")
        return value
