"""Employees, user accounts and time off."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from nexum.errors import ConflictError, NotFoundError, ValidationError
from nexum.models import (
    Department,
    Employee,
    EmploymentType,
    PayType,
    Role,
    TimeOffKind,
    TimeOffRequest,
    TimeOffStatus,
    User,
)
from nexum.models.types import utcnow
from nexum.services import audit
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
    employee.end_date = end_date or date.today()
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
