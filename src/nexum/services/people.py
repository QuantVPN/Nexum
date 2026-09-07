"""Employees, user accounts and time off."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, time
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from nexum.errors import ConflictError, NotFoundError, ValidationError
from nexum.models import (
    AvailabilityKind,
    AvailabilityRule,
    Department,
    Employee,
    EmploymentType,
    PayType,
    Role,
    Skill,
    TimeOffKind,
    TimeOffRequest,
    TimeOffStatus,
    User,
)
from nexum.models.types import utcnow
from nexum.services import audit
from nexum.services.calendar import today
from nexum.services.events import emit
from nexum.services.security import hash_password


def get_user_by_email(session: Session, email: str) -> User | None:
    return session.scalar(select(User).where(User.email == email.lower().strip()))


def create_user(
    session: Session,
    *,
    email: str,
    full_name: str,
    password: str,
    role: Role = Role.EMPLOYEE,
) -> User:
    email = email.lower().strip()
    if get_user_by_email(session, email) is not None:
        raise ConflictError(f"A user with email {email} already exists")
    if len(password) < 8:
        raise ValidationError("Password must be at least 8 characters")
    user = User(email=email, full_name=full_name, password_hash=hash_password(password), role=role)
    session.add(user)
    session.flush()
    return user


def get_department(session: Session, department_id: int) -> Department:
    department = session.get(Department, department_id)
    if department is None:
        raise NotFoundError(f"Department {department_id} not found")
    return department


def create_department(
    session: Session, name: str, cost_center: str | None = None, *, actor: User | None = None
) -> Department:
    existing = session.scalar(select(Department).where(Department.name == name))
    if existing is not None:
        raise ConflictError(f"Department '{name}' already exists")
    department = Department(name=name, cost_center=cost_center)
    session.add(department)
    session.flush()
    audit.record(session, "department.created", "department", department.id, actor=actor, name=name)
    return department


def list_departments(session: Session) -> list[Department]:
    return list(session.scalars(select(Department).order_by(Department.name)))


def get_employee(session: Session, employee_id: int) -> Employee:
    employee = session.get(Employee, employee_id)
    if employee is None:
        raise NotFoundError(f"Employee {employee_id} not found")
    return employee


def list_employees(
    session: Session, *, active_only: bool = True, department_id: int | None = None
) -> list[Employee]:
    stmt = select(Employee).order_by(Employee.last_name, Employee.first_name)
    if active_only:
        stmt = stmt.where(Employee.is_active.is_(True))
    if department_id is not None:
        stmt = stmt.where(Employee.department_id == department_id)
    return list(session.scalars(stmt))


def create_employee(
    session: Session,
    *,
    first_name: str,
    last_name: str,
    email: str,
    start_date: date,
    department_id: int | None = None,
    title: str | None = None,
    employment_type: EmploymentType = EmploymentType.FULL_TIME,
    pay_type: PayType = PayType.MONTHLY,
    monthly_salary: Decimal = Decimal("0"),
    hourly_rate: Decimal = Decimal("0"),
    weekly_hours: Decimal = Decimal("40"),
    currency: str = "SEK",
    login_password: str | None = None,
    role: Role = Role.EMPLOYEE,
    actor: User | None = None,
) -> Employee:
    email = email.lower().strip()
    if session.scalar(select(Employee).where(Employee.email == email)) is not None:
        raise ConflictError(f"An employee with email {email} already exists")
    if department_id is not None:
        get_department(session, department_id)
    if pay_type == PayType.HOURLY and hourly_rate <= 0:
        raise ValidationError("Hourly employees need an hourly rate above zero")
    if pay_type == PayType.MONTHLY and monthly_salary <= 0:
        raise ValidationError("Monthly employees need a monthly salary above zero")

    user: User | None = None
    if login_password is not None:
        user = create_user(
            session,
            email=email,
            full_name=f"{first_name} {last_name}",
            password=login_password,
            role=role,
        )

    employee = Employee(
        first_name=first_name,
        last_name=last_name,
        email=email,
        title=title,
        department_id=department_id,
        employment_type=employment_type,
        pay_type=pay_type,
        monthly_salary=monthly_salary,
        hourly_rate=hourly_rate,
        weekly_hours=weekly_hours,
        currency=currency,
        start_date=start_date,
        user=user,
    )
    session.add(employee)
    session.flush()
    audit.record(
        session,
        "employee.created",
        "employee",
        employee.id,
        actor=actor,
        email=email,
        department_id=department_id,
    )
    emit(
        session,
        "employee.created",
        employee_id=employee.id,
        department_id=department_id,
        email=email,
        employment_type=employment_type,
    )
    return employee


def deactivate_employee(
    session: Session, employee: Employee, end_date: date | None = None, *, actor: User | None = None
) -> Employee:
    employee.is_active = False
    employee.end_date = end_date or today()
    if employee.user is not None:
        employee.user.is_active = False
    audit.record(session, "employee.deactivated", "employee", employee.id, actor=actor)
    emit(session, "employee.deactivated", employee_id=employee.id)
    return employee


# --- time off -------------------------------------------------------------------------


def request_time_off(
    session: Session,
    employee: Employee,
    *,
    kind: TimeOffKind,
    start_date: date,
    end_date: date,
    reason: str | None = None,
) -> TimeOffRequest:
    if end_date < start_date:
        raise ValidationError("End date must be on or after the start date")
    overlap = session.scalar(
        select(TimeOffRequest).where(
            TimeOffRequest.employee_id == employee.id,
            TimeOffRequest.status.in_([TimeOffStatus.PENDING, TimeOffStatus.APPROVED]),
            TimeOffRequest.start_date <= end_date,
            TimeOffRequest.end_date >= start_date,
        )
    )
    if overlap is not None:
        raise ConflictError("An overlapping time-off request already exists")
    request = TimeOffRequest(
        employee_id=employee.id,
        kind=kind,
        start_date=start_date,
        end_date=end_date,
        reason=reason,
    )
    session.add(request)
    session.flush()
    emit(
        session,
        "timeoff.requested",
        request_id=request.id,
        employee_id=employee.id,
        department_id=employee.department_id,
        kind=kind,
        start_date=start_date,
        end_date=end_date,
        days=request.days,
    )
    return request


def decide_time_off(
    session: Session, request: TimeOffRequest, *, approve: bool, actor: User | None = None
) -> TimeOffRequest:
    if request.status != TimeOffStatus.PENDING:
        raise ConflictError("Only pending requests can be decided")
    request.status = TimeOffStatus.APPROVED if approve else TimeOffStatus.REJECTED
    request.decided_by_user_id = actor.id if actor else None
    request.decided_at = utcnow()
    audit.record(
        session,
        "timeoff.approved" if approve else "timeoff.rejected",
        "time_off_request",
        request.id,
        actor=actor,
    )
    emit(
        session,
        "timeoff.approved" if approve else "timeoff.rejected",
        request_id=request.id,
        employee_id=request.employee_id,
        kind=request.kind,
        start_date=request.start_date,
        end_date=request.end_date,
        days=request.days,
    )
    return request


def list_time_off(
    session: Session,
    *,
    employee_id: int | None = None,
    status: TimeOffStatus | None = None,
    limit: int = 200,
) -> list[TimeOffRequest]:
    stmt = select(TimeOffRequest).order_by(TimeOffRequest.start_date.desc()).limit(limit)
    if employee_id is not None:
        stmt = stmt.where(TimeOffRequest.employee_id == employee_id)
    if status is not None:
        stmt = stmt.where(TimeOffRequest.status == status)
    return list(session.scalars(stmt))


def approved_time_off_days(
    session: Session, employee_id: int, start: date, end: date, kinds: tuple[TimeOffKind, ...] = ()
) -> set[date]:
    """Calendar days inside [start, end] covered by approved time off."""
    stmt = select(TimeOffRequest).where(
        TimeOffRequest.employee_id == employee_id,
        TimeOffRequest.status == TimeOffStatus.APPROVED,
        TimeOffRequest.start_date <= end,
        TimeOffRequest.end_date >= start,
    )
    if kinds:
        stmt = stmt.where(TimeOffRequest.kind.in_(kinds))
    days: set[date] = set()
    for req in session.scalars(stmt):
        day = max(req.start_date, start)
        last = min(req.end_date, end)
        while day <= last:
            days.add(day)
            day = date.fromordinal(day.toordinal() + 1)
    return days


def is_on_time_off(session: Session, employee_id: int, day: date) -> bool:
    return day in approved_time_off_days(session, employee_id, day, day)


# --- skills ------------------------------------------------------------------------------------


def normalize_skill(name: str) -> str:
    return " ".join(name.strip().split()).lower()


def get_or_create_skill(session: Session, name: str) -> Skill:
    key = normalize_skill(name)
    if not key:
        raise ValidationError("Skill name cannot be empty")
    skill = session.scalar(select(Skill).where(Skill.name == key))
    if skill is None:
        skill = Skill(name=key)
        session.add(skill)
        session.flush()
    return skill


def list_skills(session: Session) -> list[Skill]:
    return list(session.scalars(select(Skill).order_by(Skill.name)))


def parse_skill_names(text: str | None) -> list[str]:
    return [part for part in (normalize_skill(p) for p in (text or "").split(",")) if part]


def set_skills(session: Session, employee: Employee, names: Iterable[str]) -> list[Skill]:
    wanted = {normalize_skill(n) for n in names if normalize_skill(n)}
    employee.skills = [get_or_create_skill(session, n) for n in sorted(wanted)]
    session.flush()
    return list(employee.skills)


# --- availability ------------------------------------------------------------------------------


def add_availability(
    session: Session,
    employee: Employee,
    *,
    weekday: int,
    start_time: time,
    end_time: time,
    kind: AvailabilityKind = AvailabilityKind.UNAVAILABLE,
    note: str | None = None,
) -> AvailabilityRule:
    if not 0 <= weekday <= 6:
        raise ValidationError("weekday must be 0 (Monday) .. 6 (Sunday)")
    rule = AvailabilityRule(
        employee_id=employee.id,
        weekday=weekday,
        start_time=start_time,
        end_time=end_time,
        kind=kind,
        note=(note or "").strip() or None,
    )
    employee.availability.append(rule)
    session.flush()
    return rule


def remove_availability(session: Session, rule: AvailabilityRule) -> None:
    session.delete(rule)
    session.flush()


def replace_availability(
    session: Session, employee: Employee, rules: Iterable[dict[str, Any]]
) -> list[AvailabilityRule]:
    for existing in list(employee.availability):
        session.delete(existing)
    employee.availability = []
    session.flush()
    created: list[AvailabilityRule] = []
    for spec in rules:
        created.append(
            add_availability(
                session,
                employee,
                weekday=int(spec["weekday"]),
                start_time=time.fromisoformat(str(spec.get("start_time", "00:00"))),
                end_time=time.fromisoformat(str(spec.get("end_time", "00:00"))),
                kind=AvailabilityKind(str(spec.get("kind", "unavailable"))),
                note=spec.get("note"),
            )
        )
    return created


# --- editing ---------------------------------------------------------------------------------

EMPLOYEE_EDITABLE: tuple[str, ...] = (
    "first_name",
    "last_name",
    "title",
    "department_id",
    "employment_type",
    "pay_type",
    "monthly_salary",
    "hourly_rate",
    "weekly_hours",
    "currency",
)


def update_employee(
    session: Session,
    employee: Employee,
    *,
    actor: User | None = None,
    skills: Iterable[str] | None = None,
    role: Role | None = None,
    **fields: Any,
) -> Employee:
    unknown = set(fields) - set(EMPLOYEE_EDITABLE)
    if unknown:
        raise ValidationError(f"Cannot edit: {', '.join(sorted(unknown))}")
    if "department_id" in fields and fields["department_id"] is not None:
        get_department(session, int(fields["department_id"]))
    for key, value in fields.items():
        setattr(employee, key, value)
    if employee.pay_type == PayType.HOURLY and employee.hourly_rate <= 0:
        raise ValidationError("Hourly employees need an hourly rate above zero")
    if employee.pay_type == PayType.MONTHLY and employee.monthly_salary <= 0:
        raise ValidationError("Monthly employees need a monthly salary above zero")
    if skills is not None:
        set_skills(session, employee, skills)
    if role is not None and employee.user is not None:
        employee.user.role = role
    if employee.user is not None:
        employee.user.full_name = employee.full_name
    session.flush()
    audit.record(
        session,
        "employee.updated",
        "employee",
        employee.id,
        actor=actor,
        fields=sorted(fields),
    )
    return employee


# --- vacation ----------------------------------------------------------------------------------


def vacation_balance(
    session: Session, employee: Employee, days_per_year: Decimal, as_of: date
) -> dict[str, Any]:
    """Simple even accrual: ``days_per_year`` earned evenly over the calendar year for the
    part of the year the employee is employed; approved vacation weekdays are taken."""
    year_start, year_end = date(as_of.year, 1, 1), date(as_of.year, 12, 31)
    year_days = (year_end - year_start).days + 1
    employed_from = max(employee.start_date, year_start)
    employed_to = min(employee.end_date or year_end, year_end)
    if employed_to < employed_from:
        entitlement = accrued = Decimal("0")
    else:
        employed_days = (employed_to - employed_from).days + 1
        entitlement = days_per_year * Decimal(employed_days) / Decimal(year_days)
        accrued_until = min(as_of, employed_to)
        accrued_days = max((accrued_until - employed_from).days + 1, 0)
        accrued = days_per_year * Decimal(accrued_days) / Decimal(year_days)
    taken_days = approved_time_off_days(
        session, employee.id, year_start, year_end, kinds=(TimeOffKind.VACATION,)
    )
    taken = Decimal(sum(1 for d in taken_days if d.weekday() < 5))
    quant = Decimal("0.1")
    return {
        "year": as_of.year,
        "entitlement": entitlement.quantize(quant),
        "accrued": accrued.quantize(quant),
        "taken": taken,
        "remaining": (entitlement - taken).quantize(quant),
    }


# --- data protection ----------------------------------------------------------------------------


def export_employee(session: Session, employee: Employee) -> dict[str, Any]:
    """Everything stored about one employee, JSON-friendly (GDPR access request)."""
    from nexum.models import Notification, ShiftRequest

    def iso(value: Any) -> Any:
        return value.isoformat() if hasattr(value, "isoformat") else value

    user = employee.user
    notifications = (
        list(session.scalars(select(Notification).where(Notification.user_id == user.id)))
        if user
        else []
    )
    requests = list(
        session.scalars(select(ShiftRequest).where(ShiftRequest.employee_id == employee.id))
    )
    return {
        "exported_at": iso(utcnow()),
        "employee": {
            "id": employee.id,
            "first_name": employee.first_name,
            "last_name": employee.last_name,
            "email": employee.email,
            "title": employee.title,
            "department": employee.department.name if employee.department else None,
            "employment_type": employee.employment_type.value,
            "pay_type": employee.pay_type.value,
            "monthly_salary": str(employee.monthly_salary),
            "hourly_rate": str(employee.hourly_rate),
            "weekly_hours": str(employee.weekly_hours),
            "currency": employee.currency,
            "start_date": iso(employee.start_date),
            "end_date": iso(employee.end_date),
            "is_active": employee.is_active,
            "skills": employee.skill_names,
        },
        "login": (
            {
                "email": user.email,
                "role": user.role.value,
                "is_active": user.is_active,
                "last_login_at": iso(user.last_login_at),
                "email_notifications": user.email_notifications,
            }
            if user
            else None
        ),
        "availability": [
            {
                "weekday": r.weekday,
                "start_time": iso(r.start_time),
                "end_time": iso(r.end_time),
                "kind": r.kind.value,
                "note": r.note,
            }
            for r in employee.availability
        ],
        "shifts": [
            {
                "id": s.id,
                "starts_at": iso(s.starts_at),
                "ends_at": iso(s.ends_at),
                "department": s.department.name,
                "status": s.status.value,
                "role_label": s.role_label,
            }
            for s in employee.shifts
        ],
        "time_entries": [
            {
                "id": e.id,
                "clock_in": iso(e.clock_in),
                "clock_out": iso(e.clock_out),
                "break_minutes": e.break_minutes,
                "status": e.status.value,
                "note": e.note,
            }
            for e in employee.time_entries
        ],
        "time_off": [
            {
                "id": r.id,
                "kind": r.kind.value,
                "start_date": iso(r.start_date),
                "end_date": iso(r.end_date),
                "status": r.status.value,
                "reason": r.reason,
            }
            for r in employee.time_off_requests
        ],
        "shift_requests": [
            {
                "id": r.id,
                "kind": r.kind.value,
                "status": r.status.value,
                "shift_id": r.shift_id,
                "note": r.note,
                "created_at": iso(r.created_at),
            }
            for r in requests
        ],
        "payslips": [
            {
                "period": p.pay_period.label,
                "regular_hours": str(p.regular_hours),
                "overtime_hours": str(p.overtime_hours),
                "premium_hours": str(p.premium_hours),
                "gross_amount": str(p.gross_amount),
                "currency": p.currency,
            }
            for p in employee.payslips
        ],
        "notifications": [
            {"title": n.title, "created_at": iso(n.created_at), "is_read": n.is_read}
            for n in notifications
        ],
    }


def anonymize_employee(
    session: Session, employee: Employee, *, actor: User | None = None
) -> Employee:
    """Erase personal data while keeping the aggregate records payroll and history need."""
    from nexum.models import Notification, ShiftRequest
    from nexum.models.identity import new_session_salt
    from nexum.models.people import new_calendar_token
    from nexum.services.security import hash_password

    if employee.is_active:
        deactivate_employee(session, employee, actor=actor)
    marker = f"anonymized-{employee.id}"
    employee.first_name = "Former"
    employee.last_name = f"Employee {employee.id}"
    employee.email = f"{marker}@example.invalid"
    employee.title = None
    employee.calendar_token = new_calendar_token()
    employee.skills = []
    for rule in list(employee.availability):
        session.delete(rule)
    for entry in employee.time_entries:
        entry.note = None
    for request in employee.time_off_requests:
        request.reason = None
    for shift_request in session.scalars(
        select(ShiftRequest).where(ShiftRequest.employee_id == employee.id)
    ):
        shift_request.note = None
    user = employee.user
    if user is not None:
        user.email = f"{marker}@example.invalid"
        user.full_name = f"Former employee {employee.id}"
        user.is_active = False
        user.password_hash = hash_password(new_session_salt() + new_session_salt())
        user.session_salt = new_session_salt()
        user.email_notifications = False
        for note in session.scalars(select(Notification).where(Notification.user_id == user.id)):
            session.delete(note)
    session.flush()
    audit.record(session, "employee.anonymized", "employee", employee.id, actor=actor)
    return employee
