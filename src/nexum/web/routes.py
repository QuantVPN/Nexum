"""Server-rendered pages. Thin handlers: parse the form, call a service, flash, redirect."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from nexum.api import deps
from nexum.api.deps import AdminUser, CurrentUser, DbSession, ManagerUser, OptionalUser
from nexum.automation import get_engine
from nexum.automation import schedule as schedule_rules
from nexum.automation.actions import list_actions
from nexum.automation.engine import AutomationEngine
from nexum.errors import AuthenticationError, NexumError, NotFoundError, ValidationError
from nexum.models import (
    AutomationRule,
    AutomationRun,
    Employee,
    EmploymentType,
    PayType,
    Role,
    ScheduleTemplate,
    ShiftStatus,
    TimeEntry,
    TimeEntryStatus,
    TimeOffKind,
    TimeOffRequest,
    TimeOffStatus,
    TriggerType,
    User,
)
from nexum.models.types import utcnow
from nexum.services import (
    audit,
    dashboard,
    notifications,
    payroll,
    people,
    scheduling,
    time_tracking,
)
from nexum.services.calendar import week_start, week_window
from nexum.services.events import EVENT_NAMES, commit_and_dispatch
from nexum.services.security import verify_password
from nexum.web.templating import flash, render

web_router = APIRouter(include_in_schema=False)

FormStr = Annotated[str, Form()]
FormOptStr = Annotated[str | None, Form()]
FormInt = Annotated[int, Form()]
FormOptInt = Annotated[int | None, Form()]


# --- helpers --------------------------------------------------------------------------------


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url=url, status_code=303)


def render_error(request: Request, exc: NexumError) -> Response:
    return render(request, "error.html", status_code=exc.status_code, error=exc, title="Error")


def parse_date(value: str | None, field: str) -> date:
    try:
        return date.fromisoformat((value or "").strip())
    except ValueError as exc:
        raise ValidationError(f"{field}: enter a date as YYYY-MM-DD") from exc


def parse_datetime(value: str | None, field: str) -> datetime:
    """Accept ``datetime-local`` input (``2026-09-07T08:00``); treated as UTC."""
    try:
        parsed = datetime.fromisoformat((value or "").strip())
    except ValueError as exc:
        raise ValidationError(f"{field}: enter a date and time") from exc
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def parse_time(value: str | None, field: str) -> time:
    try:
        return time.fromisoformat((value or "").strip())
    except ValueError as exc:
        raise ValidationError(f"{field}: enter a time as HH:MM") from exc


def parse_decimal(value: str | None, field: str, default: str = "0") -> Decimal:
    raw = (value or "").strip() or default
    try:
        return Decimal(raw.replace(" ", "").replace(",", "."))
    except InvalidOperation as exc:
        raise ValidationError(f"{field}: enter a number") from exc


def parse_json(value: str | None, field: str, default: Any) -> Any:
    raw = (value or "").strip()
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"{field}: invalid JSON ({exc.msg} at position {exc.pos})") from exc


def week_from_query(request: Request) -> date:
    raw = request.query_params.get("week")
    if raw:
        try:
            return week_start(date.fromisoformat(raw))
        except ValueError:
            pass
    return week_start(utcnow().date())


def int_query(request: Request, name: str) -> int | None:
    raw = request.query_params.get(name)
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def guarded(request: Request, db: Session, back: str, work: Callable[[], str]) -> RedirectResponse:
    """Run ``work`` (which returns a success message), commit + dispatch events, flash, redirect."""
    try:
        message = work()
        commit_and_dispatch(db)
    except NexumError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return redirect(back)
    if message:
        flash(request, message)
    return redirect(back)


def home_for(user: User) -> str:
    return "/dashboard" if user.has_role(Role.MANAGER) else "/me"


# --- auth -----------------------------------------------------------------------------------


@web_router.get("/", response_class=HTMLResponse)
def index(user: OptionalUser) -> Response:
    if user is None:
        return redirect("/login")
    return redirect(home_for(user))


@web_router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, user: OptionalUser) -> Response:
    if user is not None:
        return redirect(home_for(user))
    return render(request, "login.html", title="Sign in", next=request.query_params.get("next", ""))


@web_router.post("/login")
def login_submit(
    request: Request, db: DbSession, email: FormStr, password: FormStr, next: FormOptStr = None
) -> Response:
    user = people.get_user_by_email(db, email)
    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        flash(request, "Invalid email or password", "error")
        return redirect("/login")
    user.last_login_at = utcnow()
    db.commit()
    deps.login(request, user)
    target = next if next and next.startswith("/") and not next.startswith("//") else home_for(user)
    return redirect(target)


@web_router.post("/logout")
def logout(request: Request) -> Response:
    deps.logout(request)
    return redirect("/login")


# --- admin: dashboard -------------------------------------------------------------------------


@web_router.get("/dashboard", response_class=HTMLResponse)
def dashboard_page(request: Request, db: DbSession, user: ManagerUser) -> Response:
    data = dashboard.admin_overview(db)
    departments = dashboard.department_summary(db)
    db.commit()
    return render(
        request,
        "dashboard.html",
        db=db,
        user=user,
        title="Dashboard",
        data=data,
        departments=departments,
    )


# --- admin: employees & departments -------------------------------------------------------------


@web_router.get("/employees", response_class=HTMLResponse)
def employees_page(request: Request, db: DbSession, user: ManagerUser) -> Response:
    show_inactive = request.query_params.get("all") == "1"
    return render(
        request,
        "employees.html",
        db=db,
        user=user,
        title="Employees",
        employees=people.list_employees(db, active_only=not show_inactive),
        departments=people.list_departments(db),
        show_inactive=show_inactive,
        employment_types=EmploymentType.choices(),
        pay_types=PayType.choices(),
        roles=[Role.EMPLOYEE.value, Role.MANAGER.value, Role.ADMIN.value],
    )


@web_router.post("/departments")
def department_create(
    request: Request,
    db: DbSession,
    user: ManagerUser,
    name: FormStr,
    cost_center: FormOptStr = None,
) -> Response:
    def work() -> str:
        people.create_department(db, name.strip(), (cost_center or "").strip() or None, actor=user)
        return f"Department '{name.strip()}' created"

    return guarded(request, db, "/employees", work)


@web_router.post("/employees")
def employee_create(
    request: Request,
    db: DbSession,
    user: ManagerUser,
    first_name: FormStr,
    last_name: FormStr,
    email: FormStr,
    start_date: FormStr,
    department_id: FormOptStr = None,
    title: FormOptStr = None,
    employment_type: FormStr = "full_time",
    pay_type: FormStr = "monthly",
    monthly_salary: FormOptStr = None,
    hourly_rate: FormOptStr = None,
    weekly_hours: FormOptStr = None,
    login_password: FormOptStr = None,
    role: FormStr = "employee",
) -> Response:
    def work() -> str:
        wanted_role = Role(role)
        if wanted_role != Role.EMPLOYEE and not user.has_role(Role.ADMIN):
            raise ValidationError("Only admins can create manager or admin logins")
        employee = people.create_employee(
            db,
            first_name=first_name.strip(),
            last_name=last_name.strip(),
            email=email.strip(),
            start_date=parse_date(start_date, "Start date"),
            department_id=int(department_id) if department_id else None,
            title=(title or "").strip() or None,
            employment_type=EmploymentType(employment_type),
            pay_type=PayType(pay_type),
            monthly_salary=parse_decimal(monthly_salary, "Monthly salary"),
            hourly_rate=parse_decimal(hourly_rate, "Hourly rate"),
            weekly_hours=parse_decimal(weekly_hours, "Weekly hours", "40"),
            login_password=(login_password or "").strip() or None,
            role=wanted_role,
            actor=user,
        )
        return f"{employee.full_name} added"

    return guarded(request, db, "/employees", work)


@web_router.get("/employees/{employee_id}", response_class=HTMLResponse)
def employee_detail(
    request: Request, db: DbSession, user: ManagerUser, employee_id: int
) -> Response:
    employee = people.get_employee(db, employee_id)
    overview = dashboard.employee_overview(db, employee)
    db.commit()
    now = utcnow()
    recent_entries = time_tracking.entries_in_window(
        db, now - timedelta(days=30), now + timedelta(days=1), employee_id=employee.id
    )
    return render(
        request,
        "employee_detail.html",
        db=db,
        user=user,
        title=employee.full_name,
        employee=employee,
        o=overview,
        entries=recent_entries,
    )


@web_router.post("/employees/{employee_id}/deactivate")
def employee_deactivate(
    request: Request, db: DbSession, user: ManagerUser, employee_id: int
) -> Response:
    def work() -> str:
        employee = people.get_employee(db, employee_id)
        people.deactivate_employee(db, employee, actor=user)
        return f"{employee.full_name} deactivated"

    return guarded(request, db, "/employees", work)


# --- admin: schedule ----------------------------------------------------------------------------


def _schedule_url(week: date, department_id: int | None) -> str:
    url = f"/schedule?week={week.isoformat()}"
    return f"{url}&department_id={department_id}" if department_id else url


@web_router.get("/schedule", response_class=HTMLResponse)
def schedule_page(request: Request, db: DbSession, user: ManagerUser) -> Response:
    week = week_from_query(request)
    department_id = int_query(request, "department_id")
    ws, we = week_window(week)
    shifts = scheduling.list_shifts(db, ws, we, department_id=department_id)
    employees = people.list_employees(db, department_id=department_id)
    days = [week + timedelta(days=i) for i in range(7)]
    grid: dict[int | None, dict[date, list[Any]]] = {e.id: {d: [] for d in days} for e in employees}
    grid[None] = {d: [] for d in days}
    for shift in shifts:
        key = shift.employee_id if shift.employee_id in grid else None
        grid[key][shift.starts_at.date()].append(shift)
    open_shifts = [s for s in shifts if s.employee_id is None]
    candidates = {s.id: scheduling.candidate_employees(db, s) for s in open_shifts}
    hours = scheduling.week_hours_by_employee(db, ws, we)
    return render(
        request,
        "schedule.html",
        db=db,
        user=user,
        title="Schedule",
        week=week,
        prev_week=week - timedelta(days=7),
        next_week=week + timedelta(days=7),
        days=days,
        employees=employees,
        grid=grid,
        hours=hours,
        open_shifts=open_shifts,
        candidates=candidates,
        departments=people.list_departments(db),
        department_id=department_id,
        drafts=sum(1 for s in shifts if s.status == ShiftStatus.DRAFT),
        total_shifts=len(shifts),
    )


@web_router.post("/schedule/generate")
def schedule_generate(
    request: Request,
    db: DbSession,
    user: ManagerUser,
    week: FormStr,
    department_id: FormOptStr = None,
) -> Response:
    monday = parse_date(week, "Week")
    dept = int(department_id) if department_id else None

    def work() -> str:
        created = scheduling.generate_week_from_templates(
            db, monday, department_id=dept, actor=user
        )
        return f"Generated {len(created)} draft shift(s) from templates"

    return guarded(request, db, _schedule_url(monday, dept), work)


@web_router.post("/schedule/auto-assign")
def schedule_auto_assign(
    request: Request,
    db: DbSession,
    user: ManagerUser,
    week: FormStr,
    department_id: FormOptStr = None,
) -> Response:
    monday = parse_date(week, "Week")
    dept = int(department_id) if department_id else None

    def work() -> str:
        ws, we = week_window(monday)
        assigned = scheduling.auto_assign_open_shifts(db, ws, we, actor=user)
        return f"Auto-assigned {len(assigned)} shift(s)"

    return guarded(request, db, _schedule_url(monday, dept), work)


@web_router.post("/schedule/publish")
def schedule_publish(
    request: Request,
    db: DbSession,
    user: ManagerUser,
    week: FormStr,
    department_id: FormOptStr = None,
) -> Response:
    monday = parse_date(week, "Week")
    dept = int(department_id) if department_id else None

    def work() -> str:
        ws, we = week_window(monday)
        count = scheduling.publish_shifts(
            db, scheduling.list_shifts(db, ws, we, department_id=dept), actor=user
        )
        return f"Published {count} shift(s); employees have been notified"

    return guarded(request, db, _schedule_url(monday, dept), work)


@web_router.post("/shifts")
def shift_create(
    request: Request,
    db: DbSession,
    user: ManagerUser,
    department_id: FormInt,
    starts_at: FormStr,
    ends_at: FormStr,
    employee_id: FormOptStr = None,
    role_label: FormOptStr = None,
    publish: FormOptStr = None,
) -> Response:
    start = parse_datetime(starts_at, "Start")
    back = _schedule_url(week_start(start.date()), department_id)

    def work() -> str:
        department = people.get_department(db, department_id)
        employee = people.get_employee(db, int(employee_id)) if employee_id else None
        scheduling.create_shift(
            db,
            department=department,
            starts_at=start,
            ends_at=parse_datetime(ends_at, "End"),
            employee=employee,
            role_label=(role_label or "").strip() or None,
            status=ShiftStatus.PUBLISHED if publish else ShiftStatus.DRAFT,
            actor=user,
        )
        return "Shift created"

    return guarded(request, db, back, work)


def _shift_back(db: Session, shift_id: int) -> str:
    shift = scheduling.get_shift(db, shift_id)
    return _schedule_url(week_start(shift.starts_at.date()), None)


@web_router.post("/shifts/{shift_id}/assign")
def shift_assign(
    request: Request, db: DbSession, user: ManagerUser, shift_id: int, employee_id: FormInt
) -> Response:
    back = _shift_back(db, shift_id)

    def work() -> str:
        shift = scheduling.get_shift(db, shift_id)
        employee = people.get_employee(db, employee_id)
        scheduling.assign_shift(db, shift, employee, actor=user)
        return f"Assigned to {employee.full_name}"

    return guarded(request, db, back, work)


@web_router.post("/shifts/{shift_id}/unassign")
def shift_unassign(request: Request, db: DbSession, user: ManagerUser, shift_id: int) -> Response:
    back = _shift_back(db, shift_id)

    def work() -> str:
        scheduling.unassign_shift(db, scheduling.get_shift(db, shift_id), actor=user)
        return "Shift is now open"

    return guarded(request, db, back, work)


@web_router.post("/shifts/{shift_id}/cancel")
def shift_cancel(request: Request, db: DbSession, user: ManagerUser, shift_id: int) -> Response:
    back = _shift_back(db, shift_id)

    def work() -> str:
        scheduling.cancel_shift(db, scheduling.get_shift(db, shift_id), actor=user)
        return "Shift cancelled"

    return guarded(request, db, back, work)


@web_router.get("/templates", response_class=HTMLResponse)
def templates_page(request: Request, db: DbSession, user: ManagerUser) -> Response:
    stmt = select(ScheduleTemplate).order_by(ScheduleTemplate.weekday, ScheduleTemplate.start_time)
    return render(
        request,
        "templates.html",
        db=db,
        user=user,
        title="Schedule templates",
        templates_=list(db.scalars(stmt)),
        departments=people.list_departments(db),
    )


@web_router.post("/templates")
def template_create(
    request: Request,
    db: DbSession,
    user: ManagerUser,
    department_id: FormInt,
    name: FormStr,
    start_time: FormStr,
    end_time: FormStr,
    weekdays: Annotated[list[int], Form()],
    headcount: FormInt = 1,
    role_label: FormOptStr = None,
) -> Response:
    def work() -> str:
        department = people.get_department(db, department_id)
        for weekday in weekdays:
            scheduling.create_template(
                db,
                department=department,
                name=name.strip(),
                weekday=weekday,
                start_time=parse_time(start_time, "Start"),
                end_time=parse_time(end_time, "End"),
                headcount=headcount,
                role_label=(role_label or "").strip() or None,
            )
        return f"Added template for {len(weekdays)} weekday(s)"

    return guarded(request, db, "/templates", work)


@web_router.post("/templates/{template_id}/delete")
def template_delete(
    request: Request, db: DbSession, user: ManagerUser, template_id: int
) -> Response:
    def work() -> str:
        template = db.get(ScheduleTemplate, template_id)
        if template is None:
            raise NotFoundError("Template not found")
        db.delete(template)
        return "Template removed"

    return guarded(request, db, "/templates", work)


# --- admin: approvals ----------------------------------------------------------------------------


@web_router.get("/approvals", response_class=HTMLResponse)
def approvals_page(request: Request, db: DbSession, user: ManagerUser) -> Response:
    return render(
        request,
        "approvals.html",
        db=db,
        user=user,
        title="Approvals",
        time_off=people.list_time_off(db, status=TimeOffStatus.PENDING),
        entries=time_tracking.pending_entries(db),
        missing=time_tracking.shifts_missing_entries(db, utcnow() - timedelta(days=14), utcnow()),
    )


@web_router.post("/time-off/{request_id}/decide")
def time_off_decide(
    request: Request, db: DbSession, user: ManagerUser, request_id: int, decision: FormStr
) -> Response:
    def work() -> str:
        item = db.get(TimeOffRequest, request_id)
        if item is None:
            raise NotFoundError("Request not found")
        people.decide_time_off(db, item, approve=decision == "approve", actor=user)
        return f"Request {item.status.value}"

    return guarded(request, db, "/approvals", work)


@web_router.post("/time/entries/{entry_id}/decide")
def entry_decide(
    request: Request, db: DbSession, user: ManagerUser, entry_id: int, decision: FormStr
) -> Response:
    def work() -> str:
        entry = time_tracking.get_entry(db, entry_id)
        time_tracking.decide_entry(db, entry, approve=decision == "approve", actor=user)
        return f"Time entry {entry.status.value}"

    return guarded(request, db, "/approvals", work)


# --- admin: payroll -----------------------------------------------------------------------------


@web_router.get("/payroll", response_class=HTMLResponse)
def payroll_page(request: Request, db: DbSession, user: ManagerUser) -> Response:
    current = payroll.current_period(db)
    db.commit()
    periods = payroll.list_periods(db)
    return render(
        request,
        "payroll.html",
        db=db,
        user=user,
        title="Payroll",
        periods=[(p, payroll.period_totals(p)) for p in periods],
        current=current,
    )


@web_router.post("/payroll/periods")
def payroll_period_create(
    request: Request, db: DbSession, user: AdminUser, start_date: FormStr, end_date: FormStr
) -> Response:
    def work() -> str:
        period = payroll.get_or_create_period(
            db, parse_date(start_date, "Start"), parse_date(end_date, "End")
        )
        return f"Pay period {period.label} ready"

    return guarded(request, db, "/payroll", work)


@web_router.get("/payroll/{period_id}", response_class=HTMLResponse)
def payroll_detail(request: Request, db: DbSession, user: ManagerUser, period_id: int) -> Response:
    period = payroll.get_period(db, period_id)
    return render(
        request,
        "payroll_detail.html",
        db=db,
        user=user,
        title=f"Payroll {period.label}",
        period=period,
        totals=payroll.period_totals(period),
        payslips=sorted(period.payslips, key=lambda p: p.employee.last_name),
    )


@web_router.post("/payroll/{period_id}/{action}")
def payroll_action(
    request: Request, db: DbSession, user: AdminUser, period_id: int, action: str
) -> Response:
    def work() -> str:
        period = payroll.get_period(db, period_id)
        if action == "compute":
            slips = payroll.compute_payslips(db, period, actor=user)
            return f"Computed {len(slips)} payslip(s)"
        if action == "close":
            slips = payroll.close_period(db, period, actor=user)
            return f"Period closed with {len(slips)} payslip(s)"
        if action == "pay":
            payroll.mark_paid(db, period, actor=user)
            return "Period marked as paid"
        raise NotFoundError("Unknown payroll action")

    return guarded(request, db, f"/payroll/{period_id}", work)


# --- admin: automations -------------------------------------------------------------------------


def _rule_or_404(db: Session, rule_id: int) -> AutomationRule:
    rule = db.get(AutomationRule, rule_id)
    if rule is None:
        raise NotFoundError("Automation rule not found")
    return rule


def describe_trigger(rule: AutomationRule) -> str:
    if rule.trigger_type == TriggerType.SCHEDULE:
        return schedule_rules.describe(rule.trigger_config)
    if rule.trigger_type == TriggerType.EVENT:
        return f"on {rule.event_name}"
    return "manual"


@web_router.get("/automations", response_class=HTMLResponse)
def automations_page(request: Request, db: DbSession, user: ManagerUser) -> Response:
    stmt = select(AutomationRule).order_by(AutomationRule.enabled.desc(), AutomationRule.name)
    rules = list(db.scalars(stmt))
    runs = list(db.scalars(select(AutomationRun).order_by(AutomationRun.id.desc()).limit(15)))
    return render(
        request,
        "automations.html",
        db=db,
        user=user,
        title="Automations",
        rules=rules,
        runs=runs,
        stats=dashboard.automation_stats(db),
        describe_trigger=describe_trigger,
    )


@web_router.get("/automations/new", response_class=HTMLResponse)
def automation_new(request: Request, db: DbSession, user: AdminUser) -> Response:
    return render(
        request,
        "automation_form.html",
        db=db,
        user=user,
        title="New automation",
        actions=list_actions(),
        events=EVENT_NAMES,
        schedule_kinds=schedule_rules.KINDS,
        form={},
    )


@web_router.post("/automations/new")
def automation_create(
    request: Request,
    db: DbSession,
    user: AdminUser,
    name: FormStr,
    trigger_type: FormStr,
    actions: FormStr,
    description: FormOptStr = None,
    trigger_config: FormOptStr = None,
    conditions: FormOptStr = None,
    estimated_minutes_saved: FormOptStr = None,
) -> Response:
    try:
        trig = TriggerType(trigger_type)
        config = parse_json(trigger_config, "Trigger config", {})
        conds = parse_json(conditions, "Conditions", [])
        acts = parse_json(actions, "Actions", [])
        AutomationEngine.validate_rule(trig, config, conds, acts)
        rule = AutomationRule(
            name=name.strip(),
            description=(description or "").strip() or None,
            trigger_type=trig,
            trigger_config=config,
            conditions=conds,
            actions=acts,
            estimated_minutes_saved=int(parse_decimal(estimated_minutes_saved, "Minutes saved")),
        )
        db.add(rule)
        db.flush()
        audit.record(db, "automation.rule_created", "automation_rule", rule.id, actor=user)
        db.commit()
    except (NexumError, ValueError) as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return render(
            request,
            "automation_form.html",
            db=db,
            user=user,
            title="New automation",
            actions=list_actions(),
            events=EVENT_NAMES,
            schedule_kinds=schedule_rules.KINDS,
            form={
                "name": name,
                "description": description,
                "trigger_type": trigger_type,
                "trigger_config": trigger_config,
                "conditions": conditions,
                "actions": actions,
                "estimated_minutes_saved": estimated_minutes_saved,
            },
            status_code=422,
        )
    flash(request, f"Automation '{rule.name}' created")
    return redirect(f"/automations/{rule.id}")


@web_router.get("/automations/{rule_id}", response_class=HTMLResponse)
def automation_detail(request: Request, db: DbSession, user: ManagerUser, rule_id: int) -> Response:
    rule = _rule_or_404(db, rule_id)
    runs = list(
        db.scalars(
            select(AutomationRun)
            .where(AutomationRun.rule_id == rule.id)
            .order_by(AutomationRun.id.desc())
            .limit(50)
        )
    )
    return render(
        request,
        "automation_detail.html",
        db=db,
        user=user,
        title=rule.name,
        rule=rule,
        runs=runs,
        trigger_text=describe_trigger(rule),
    )


@web_router.post("/automations/{rule_id}/toggle")
def automation_toggle(request: Request, db: DbSession, user: AdminUser, rule_id: int) -> Response:
    def work() -> str:
        rule = _rule_or_404(db, rule_id)
        rule.enabled = not rule.enabled
        rule.next_run_at = None
        audit.record(
            db,
            "automation.rule_toggled",
            "automation_rule",
            rule.id,
            actor=user,
            enabled=rule.enabled,
        )
        return f"'{rule.name}' {'enabled' if rule.enabled else 'disabled'}"

    return guarded(request, db, "/automations", work)


@web_router.post("/automations/{rule_id}/run")
def automation_run(request: Request, db: DbSession, user: ManagerUser, rule_id: int) -> Response:
    rule = _rule_or_404(db, rule_id)
    db.commit()
    run = get_engine().run_rule_id(rule.id, manual=True)
    level = "success" if run.status.value == "success" else "error"
    flash(request, f"Run finished: {run.status.value} ({'; '.join(run.log[-3:])})", level)
    return redirect(f"/automations/{rule_id}")


@web_router.post("/automations/{rule_id}/delete")
def automation_delete(request: Request, db: DbSession, user: AdminUser, rule_id: int) -> Response:
    def work() -> str:
        rule = _rule_or_404(db, rule_id)
        audit.record(
            db, "automation.rule_deleted", "automation_rule", rule.id, actor=user, name=rule.name
        )
        db.delete(rule)
        return f"'{rule.name}' deleted"

    return guarded(request, db, "/automations", work)


# --- employee self-service ------------------------------------------------------------------------


def _employee_or_notice(request: Request, db: Session, user: User) -> Employee | Response:
    if user.employee is None:
        return render(request, "no_employee.html", db=db, user=user, title="My page")
    return user.employee


@web_router.get("/me", response_class=HTMLResponse)
def me_page(request: Request, db: DbSession, user: CurrentUser) -> Response:
    employee = _employee_or_notice(request, db, user)
    if isinstance(employee, Response):
        return employee
    overview = dashboard.employee_overview(db, employee)
    db.commit()
    return render(request, "me.html", db=db, user=user, title="My page", o=overview)


@web_router.post("/me/clock-in")
def me_clock_in(request: Request, db: DbSession, user: CurrentUser) -> Response:
    def work() -> str:
        if user.employee is None:
            raise AuthenticationError("No employee record")
        time_tracking.clock_in(db, user.employee, utcnow())
        return "Clocked in"

    return guarded(request, db, "/me", work)


@web_router.post("/me/clock-out")
def me_clock_out(
    request: Request, db: DbSession, user: CurrentUser, break_minutes: FormInt = 0
) -> Response:
    def work() -> str:
        if user.employee is None:
            raise AuthenticationError("No employee record")
        entry = time_tracking.open_entry(db, user.employee.id)
        if entry is None:
            raise ValidationError("You are not clocked in")
        time_tracking.clock_out(db, entry, utcnow(), break_minutes=break_minutes)
        return f"Clocked out: {entry.worked_hours:.2f} h submitted for approval"

    return guarded(request, db, "/me", work)


@web_router.get("/me/pay", response_class=HTMLResponse)
def me_pay(request: Request, db: DbSession, user: CurrentUser) -> Response:
    employee = _employee_or_notice(request, db, user)
    if isinstance(employee, Response):
        return employee
    period = payroll.current_period(db)
    estimate = payroll.calculate_payslip(db, employee, period)
    db.commit()
    return render(
        request,
        "me_pay.html",
        db=db,
        user=user,
        title="My pay",
        employee=employee,
        period=period,
        estimate=estimate,
        payslips=payroll.payslips_for_employee(db, employee.id),
    )


@web_router.get("/me/time", response_class=HTMLResponse)
def me_time(request: Request, db: DbSession, user: CurrentUser) -> Response:
    employee = _employee_or_notice(request, db, user)
    if isinstance(employee, Response):
        return employee
    now = utcnow()
    entries = time_tracking.entries_in_window(
        db, now - timedelta(days=45), now + timedelta(days=1), employee_id=employee.id
    )
    recent_shifts = scheduling.list_shifts(
        db,
        now - timedelta(days=14),
        now,
        employee_id=employee.id,
        statuses=(ShiftStatus.PUBLISHED,),
    )
    return render(
        request,
        "me_time.html",
        db=db,
        user=user,
        title="My time",
        employee=employee,
        entries=list(reversed(entries)),
        recent_shifts=recent_shifts,
        open_entry=time_tracking.open_entry(db, employee.id),
    )


@web_router.post("/me/time/entries")
def me_time_entry(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    clock_in: FormStr,
    clock_out: FormStr,
    break_minutes: FormInt = 0,
    shift_id: FormOptStr = None,
    note: FormOptStr = None,
) -> Response:
    def work() -> str:
        if user.employee is None:
            raise AuthenticationError("No employee record")
        shift = scheduling.get_shift(db, int(shift_id)) if shift_id else None
        entry = time_tracking.record_entry(
            db,
            user.employee,
            parse_datetime(clock_in, "Clock in"),
            parse_datetime(clock_out, "Clock out"),
            break_minutes=break_minutes,
            shift=shift,
            note=(note or "").strip() or None,
        )
        return f"Submitted {entry.worked_hours:.2f} h for approval"

    return guarded(request, db, "/me/time", work)


@web_router.get("/me/requests", response_class=HTMLResponse)
def me_requests(request: Request, db: DbSession, user: CurrentUser) -> Response:
    employee = _employee_or_notice(request, db, user)
    if isinstance(employee, Response):
        return employee
    return render(
        request,
        "me_requests.html",
        db=db,
        user=user,
        title="Time off",
        employee=employee,
        requests=people.list_time_off(db, employee_id=employee.id),
        kinds=TimeOffKind.choices(),
    )


@web_router.post("/me/requests")
def me_request_create(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    kind: FormStr,
    start_date: FormStr,
    end_date: FormStr,
    reason: FormOptStr = None,
) -> Response:
    def work() -> str:
        if user.employee is None:
            raise AuthenticationError("No employee record")
        people.request_time_off(
            db,
            user.employee,
            kind=TimeOffKind(kind),
            start_date=parse_date(start_date, "Start"),
            end_date=parse_date(end_date, "End"),
            reason=(reason or "").strip() or None,
        )
        return "Request sent to your manager"

    return guarded(request, db, "/me/requests", work)


# --- notifications --------------------------------------------------------------------------------


@web_router.get("/notifications", response_class=HTMLResponse)
def notifications_page(request: Request, db: DbSession, user: CurrentUser) -> Response:
    items = notifications.list_for_user(db, user)
    return render(
        request, "notifications.html", db=db, user=user, title="Notifications", items=items
    )


@web_router.post("/notifications/read-all")
def notifications_read_all(request: Request, db: DbSession, user: CurrentUser) -> Response:
    notifications.mark_all_read(db, user)
    db.commit()
    return redirect("/notifications")


__all__ = ["TimeEntry", "TimeEntryStatus", "render_error", "web_router"]
