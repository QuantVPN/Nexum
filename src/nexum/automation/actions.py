"""Built-in actions. Register more with :func:`action`.

Every action receives the :class:`RunContext` plus its ``params`` from the rule and returns an
:class:`ActionResult`. Actions must be idempotent enough to survive re-runs (they check for
existing notifications, use idempotent generators, and so on).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import select

from nexum.automation.context import ActionResult, RunContext, render
from nexum.automation.retry import with_retries
from nexum.errors import NexumError, ValidationError
from nexum.models import (
    Department,
    Employee,
    Notification,
    NotificationLevel,
    PayPeriod,
    PayPeriodStatus,
    Role,
    Shift,
    ShiftRequestKind,
    ShiftRequestStatus,
    ShiftStatus,
    TimeEntry,
    TimeOffStatus,
    User,
)
from nexum.services import mailer, notifications, payroll, people, scheduling, time_tracking
from nexum.services.calendar import (
    day_window,
    iter_days,
    local_date,
    to_local,
    week_start,
    week_window,
)
from nexum.services.company import get_company, payroll_rules

ActionFn = Callable[..., ActionResult]


@dataclass(frozen=True)
class ActionSpec:
    name: str
    fn: ActionFn
    description: str
    params: dict[str, str]


_REGISTRY: dict[str, ActionSpec] = {}


def action(
    name: str, description: str, params: dict[str, str] | None = None
) -> Callable[[ActionFn], ActionFn]:
    def decorator(fn: ActionFn) -> ActionFn:
        _REGISTRY[name] = ActionSpec(name=name, fn=fn, description=description, params=params or {})
        return fn

    return decorator


def get_action(name: str) -> ActionSpec:
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        raise ValidationError(f"Unknown action '{name}'") from exc


def list_actions() -> list[ActionSpec]:
    return sorted(_REGISTRY.values(), key=lambda a: a.name)


def validate_actions(actions: Any) -> list[dict[str, Any]]:
    if not isinstance(actions, list) or not actions:
        raise ValidationError("actions must be a non-empty list")
    for entry in actions:
        if not isinstance(entry, dict) or not isinstance(entry.get("type"), str):
            raise ValidationError("each action needs a string 'type'")
        get_action(entry["type"])
        params = entry.get("params", {})
        if params is not None and not isinstance(params, dict):
            raise ValidationError("action 'params' must be an object")
    return actions


def _level(value: str | None) -> NotificationLevel:
    try:
        return NotificationLevel(value or "info")
    except ValueError as exc:
        raise ValidationError(f"Unknown notification level {value!r}") from exc


def _role(value: str | None) -> Role:
    try:
        return Role(value or "manager")
    except ValueError as exc:
        raise ValidationError(f"Unknown role {value!r}") from exc


def _already_notified(ctx: RunContext, source: str) -> bool:
    stmt = select(Notification.id).where(Notification.source == source).limit(1)
    return ctx.session.scalar(stmt) is not None


# --- messaging ---------------------------------------------------------------------------


@action(
    "notify_role",
    "Send an in-app notification to every user with at least the given role.",
    {
        "role": "admin|manager|employee",
        "title": "template",
        "body": "template",
        "level": "info|warning|critical",
    },
)
def notify_role(ctx: RunContext, **params: Any) -> ActionResult:
    mapping = ctx.as_mapping()
    title = render(params.get("title"), mapping) or ctx.rule.name
    body = render(params.get("body"), mapping) or None
    count = notifications.notify_role(
        ctx.session,
        _role(params.get("role")),
        title,
        body,
        level=_level(params.get("level")),
        link=params.get("link"),
        source=f"rule:{ctx.rule.id}",
    )
    return ActionResult(f"notified {count} user(s): {title}", count=count, work_done=count > 0)


@action(
    "notify_employee",
    "Notify the employee referenced by the event (event.employee_id) or an explicit employee_id.",
    {
        "employee_id": "int (optional, default event.employee_id)",
        "title": "template",
        "body": "template",
    },
)
def notify_employee(ctx: RunContext, **params: Any) -> ActionResult:
    mapping = ctx.as_mapping()
    employee_id: Any = params.get("employee_id") or mapping.get("event", {}).get("employee_id")
    if isinstance(employee_id, str):  # e.g. "{event.target_employee_id}"
        rendered = render(employee_id, mapping).strip()
        employee_id = int(rendered) if rendered.isdigit() else None
    if not isinstance(employee_id, int):
        return ActionResult("no employee referenced; nothing sent")
    employee = ctx.session.get(Employee, employee_id)
    if employee is None:
        return ActionResult(f"employee {employee_id} not found")
    note = notifications.notify_employee(
        ctx.session,
        employee,
        render(params.get("title"), mapping) or ctx.rule.name,
        render(params.get("body"), mapping) or None,
        level=_level(params.get("level")),
        link=params.get("link"),
        source=f"rule:{ctx.rule.id}",
    )
    if note is None:
        return ActionResult(f"{employee.full_name} has no login; nothing sent")
    return ActionResult(f"notified {employee.full_name}", count=1, work_done=True)


@action(
    "log", "Append a message to the run log (useful for testing rules).", {"message": "template"}
)
def log_message(ctx: RunContext, **params: Any) -> ActionResult:
    text = render(params.get("message", ""), ctx.as_mapping())
    ctx.say(text)
    return ActionResult(text or "logged")


@action(
    "webhook",
    "POST the event/context as JSON to a URL (requires NEXUM_AUTOMATION_WEBHOOKS_ENABLED=true).",
    {"url": "https://...", "timeout": "seconds"},
)
def webhook(ctx: RunContext, **params: Any) -> ActionResult:
    url = params.get("url")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise ValidationError("webhook needs an http(s) url")
    if not ctx.settings.automation_webhooks_enabled:
        return ActionResult("webhooks disabled by configuration; skipped")
    payload = {
        "rule": ctx.rule.key or ctx.rule.name,
        "event": ctx.event.name if ctx.event else None,
        "context": json.loads(json.dumps(ctx.as_mapping(), default=str)),
    }

    def call() -> httpx.Response:
        response = httpx.post(url, json=payload, timeout=float(params.get("timeout", 10)))
        response.raise_for_status()
        return response

    response = with_retries(
        call,
        attempts=ctx.settings.automation_retry_attempts,
        backoff_seconds=ctx.settings.automation_retry_backoff_seconds,
        log=ctx.say,
    )
    return ActionResult(f"POST {url} -> {response.status_code}", count=1, work_done=True)


@action(
    "chat_webhook",
    "Post a message to a Slack/Teams incoming webhook (requires webhooks to be enabled).",
    {"url": "https://hooks.slack.com/...", "text": "template"},
)
def chat_webhook(ctx: RunContext, **params: Any) -> ActionResult:
    url = params.get("url")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise ValidationError("chat_webhook needs an http(s) url")
    if not ctx.settings.automation_webhooks_enabled:
        return ActionResult("webhooks disabled by configuration; skipped")
    text = render(params.get("text"), ctx.as_mapping()) or ctx.rule.name

    def call() -> httpx.Response:
        response = httpx.post(url, json={"text": text}, timeout=float(params.get("timeout", 10)))
        response.raise_for_status()
        return response

    with_retries(
        call,
        attempts=ctx.settings.automation_retry_attempts,
        backoff_seconds=ctx.settings.automation_retry_backoff_seconds,
        log=ctx.say,
    )
    return ActionResult(f"posted to chat: {text[:60]}", count=1, work_done=True)


def _recipients(ctx: RunContext, params: dict[str, Any], mapping: dict[str, Any]) -> list[str]:
    addresses: list[str] = []
    if params.get("to"):
        addresses += [a.strip() for a in str(params["to"]).split(",") if a.strip()]
    if params.get("role"):
        role = _role(str(params["role"]))
        users = ctx.session.scalars(select(User).where(User.is_active.is_(True))).all()
        addresses += [u.email for u in users if u.has_role(role)]
    employee_ref: Any = params.get("employee_id")
    if employee_ref is None and "employee_id" in mapping.get("event", {}):
        employee_ref = mapping["event"]["employee_id"]
    if isinstance(employee_ref, str):
        rendered = render(employee_ref, mapping).strip()
        employee_ref = int(rendered) if rendered.isdigit() else None
    if isinstance(employee_ref, int):
        employee = ctx.session.get(Employee, employee_ref)
        if employee is not None:
            addresses.append(employee.email)
    unique: list[str] = []
    for address in addresses:
        if address not in unique:
            unique.append(address)
    return unique


@action(
    "send_email",
    "Send an e-mail (needs SMTP in company settings) to addresses, a role and/or an employee.",
    {
        "to": "comma separated addresses",
        "role": "admin|manager|employee",
        "employee_id": "int or template",
        "subject": "template",
        "body": "template",
    },
)
def send_email(ctx: RunContext, **params: Any) -> ActionResult:
    settings = get_company(ctx.session)
    if not settings.email_configured:
        return ActionResult("e-mail is not configured; skipped")
    mapping = ctx.as_mapping()
    recipients = _recipients(ctx, params, mapping)
    if not recipients:
        return ActionResult("no recipients; nothing sent")
    subject = render(params.get("subject"), mapping) or ctx.rule.name
    body = render(params.get("body"), mapping) or subject
    sent = 0
    for address in recipients:

        def deliver(to: str = address) -> bool:
            return mailer.send(settings, to, subject, body)

        with_retries(
            deliver,
            attempts=ctx.settings.automation_retry_attempts,
            backoff_seconds=ctx.settings.automation_retry_backoff_seconds,
            log=ctx.say,
        )
        sent += 1
    return ActionResult(f"sent {sent} e-mail(s): {subject}", count=sent, work_done=sent > 0)


# --- scheduling ----------------------------------------------------------------------------


@action(
    "generate_next_week_schedule",
    "Create draft open shifts from the schedule templates for an upcoming week.",
    {"weeks_ahead": "int (default 1)", "department_id": "int (optional)"},
)
def generate_next_week_schedule(ctx: RunContext, **params: Any) -> ActionResult:
    weeks_ahead = int(params.get("weeks_ahead", 1))
    monday = week_start(local_date(ctx.now)) + timedelta(days=7 * weeks_ahead)
    created = scheduling.generate_week_from_templates(
        ctx.session, monday, department_id=params.get("department_id")
    )
    return ActionResult(
        f"generated {len(created)} draft shift(s) for week of {monday}",
        count=len(created),
        work_done=bool(created),
        data={"week_start": monday.isoformat()},
    )


@action(
    "auto_assign_open_shifts",
    "Fill open shifts with available employees (no clashes, no time off, under weekly hours).",
    {
        "weeks_ahead": "int: cover that whole ISO week (0 = this week); overrides the day window",
        "days": "window length in days from now (default 7)",
        "start_days": "offset from now in days (default 0)",
    },
)
def auto_assign_open_shifts(ctx: RunContext, **params: Any) -> ActionResult:
    if params.get("weeks_ahead") is not None:
        monday = week_start(local_date(ctx.now)) + timedelta(days=7 * int(params["weeks_ahead"]))
        start, end = week_window(monday)
    else:
        start = ctx.now + timedelta(days=int(params.get("start_days", 0)))
        end = start + timedelta(days=int(params.get("days", 7)))
    assigned = scheduling.auto_assign_open_shifts(ctx.session, start, end)
    for shift in assigned:
        ctx.say(
            f"assigned shift #{shift.id} ({shift.starts_at:%a %d %b %H:%M}) "
            f"to {shift.employee.full_name if shift.employee else '?'}"
        )
    return ActionResult(
        f"auto-assigned {len(assigned)} shift(s)", count=len(assigned), work_done=bool(assigned)
    )


@action(
    "publish_drafts",
    "Publish draft shifts that start within the window so employees can see them.",
    {"days": "window length in days (default 14)"},
)
def publish_drafts(ctx: RunContext, **params: Any) -> ActionResult:
    end = ctx.now + timedelta(days=int(params.get("days", 14)))
    drafts = scheduling.list_shifts(ctx.session, ctx.now, end)
    count = scheduling.publish_shifts(ctx.session, drafts)
    return ActionResult(f"published {count} shift(s)", count=count, work_done=count > 0)


@action(
    "notify_open_shifts",
    "Send managers a digest of open shifts in the coming days (silent when there are none).",
    {"days": "int (default 7)", "role": "role to notify (default manager)"},
)
def notify_open_shifts(ctx: RunContext, **params: Any) -> ActionResult:
    end = ctx.now + timedelta(days=int(params.get("days", 7)))
    open_shifts = scheduling.open_shifts(ctx.session, ctx.now, end)
    if not open_shifts:
        return ActionResult("no open shifts; nothing to report")
    lines = [
        f"{s.starts_at:%a %d %b %H:%M}-{s.ends_at:%H:%M} {s.department.name}"
        + (f" ({s.role_label})" if s.role_label else "")
        for s in open_shifts[:15]
    ]
    more = f"\n...and {len(open_shifts) - 15} more" if len(open_shifts) > 15 else ""
    count = notifications.notify_role(
        ctx.session,
        _role(params.get("role")),
        f"{len(open_shifts)} open shift(s) need cover",
        "\n".join(lines) + more,
        level=NotificationLevel.WARNING,
        link="/schedule",
        source=f"rule:{ctx.rule.id}",
    )
    return ActionResult(
        f"reported {len(open_shifts)} open shift(s) to {count} user(s)",
        count=len(open_shifts),
        work_done=True,
    )


@action(
    "remind_upcoming_shifts",
    "Remind employees of published shifts starting soon (each shift reminded once).",
    {"hours": "look-ahead window (default 24)"},
)
def remind_upcoming_shifts(ctx: RunContext, **params: Any) -> ActionResult:
    hours = float(params.get("hours", 24))
    sent = 0
    for shift in scheduling.upcoming_shifts(ctx.session, ctx.now, timedelta(hours=hours)):
        source = f"shift-reminder:{shift.id}"
        if shift.employee is None or _already_notified(ctx, source):
            continue
        note = notifications.notify_employee(
            ctx.session,
            shift.employee,
            f"Upcoming shift {shift.starts_at:%a %d %b %H:%M}",
            f"{shift.department.name} {shift.starts_at:%H:%M}-{shift.ends_at:%H:%M}"
            + (f" as {shift.role_label}" if shift.role_label else ""),
            link="/me",
            source=source,
        )
        if note is not None:
            sent += 1
    return ActionResult(f"sent {sent} reminder(s)", count=sent, work_done=sent > 0)


@action(
    "flag_overtime",
    "Warn managers about employees scheduled above the weekly overtime threshold this week.",
    {"threshold_hours": "float (default: settings threshold)"},
)
def flag_overtime(ctx: RunContext, **params: Any) -> ActionResult:
    default_threshold = payroll_rules(ctx.session).weekly_overtime_threshold_hours
    threshold = Decimal(str(params.get("threshold_hours", default_threshold)))
    ws, we = week_window(week_start(local_date(ctx.now)))
    totals = scheduling.week_hours_by_employee(ctx.session, ws, we)
    flagged: list[str] = []
    for employee_id, total in totals.items():
        if total > threshold:
            employee = ctx.session.get(Employee, employee_id)
            if employee is not None:
                flagged.append(f"{employee.full_name}: {total}h")
    if not flagged:
        return ActionResult("nobody above the overtime threshold")
    source = f"overtime:{ws.date().isoformat()}:{ctx.rule.id}"
    if _already_notified(ctx, source):
        return ActionResult(
            f"{len(flagged)} above threshold; managers already warned this week", count=len(flagged)
        )
    notifications.notify_role(
        ctx.session,
        Role.MANAGER,
        f"{len(flagged)} employee(s) scheduled above {threshold}h this week",
        "\n".join(flagged),
        level=NotificationLevel.WARNING,
        link="/schedule",
        source=source,
    )
    return ActionResult(f"flagged {len(flagged)} employee(s)", count=len(flagged), work_done=True)


@action(
    "auto_approve_shift_requests",
    "Approve pending shift requests that cause no conflict (the event's request, or all pending).",
    {"kinds": 'list of claim|drop|transfer (default ["claim"])'},
)
def auto_approve_shift_requests(ctx: RunContext, **params: Any) -> ActionResult:
    kinds = {ShiftRequestKind(k) for k in params.get("kinds", ["claim"])}
    if ctx.event is not None and isinstance(ctx.event.payload.get("shift_request_id"), int):
        candidates = [
            scheduling.get_shift_request(ctx.session, ctx.event.payload["shift_request_id"])
        ]
    else:
        candidates = scheduling.list_shift_requests(ctx.session, status=ShiftRequestStatus.PENDING)
    approved = 0
    for request in candidates:
        if request.kind not in kinds or request.status != ShiftRequestStatus.PENDING:
            continue
        try:
            with ctx.session.begin_nested():
                scheduling.decide_shift_request(
                    ctx.session, request, approve=True, note="auto-approved: no conflicts"
                )
        except NexumError as exc:
            ctx.say(f"left request #{request.id} for a manager: {exc}")
            continue
        approved += 1
        ctx.say(f"approved {request.kind.value} #{request.id} by {request.employee.full_name}")
    return ActionResult(
        f"auto-approved {approved} request(s)", count=approved, work_done=approved > 0
    )


# --- time & attendance ----------------------------------------------------------------------


@action(
    "flag_missing_time_entries",
    "Nudge employees (and managers) about ended shifts that have no time entry.",
    {"days": "look-back window (default 3)"},
)
def flag_missing_time_entries(ctx: RunContext, **params: Any) -> ActionResult:
    start = ctx.now - timedelta(days=int(params.get("days", 3)))
    missing = time_tracking.shifts_missing_entries(ctx.session, start, ctx.now)
    sent = 0
    for shift in missing:
        source = f"missing-entry:{shift.id}"
        if shift.employee is None or _already_notified(ctx, source):
            continue
        if notifications.notify_employee(
            ctx.session,
            shift.employee,
            f"Missing time entry for {shift.starts_at:%a %d %b}",
            "Please report your worked hours so payroll is correct.",
            level=NotificationLevel.WARNING,
            link="/me/time",
            source=source,
        ):
            sent += 1
    if sent:
        notifications.notify_role(
            ctx.session,
            Role.MANAGER,
            f"{sent} shift(s) are missing time entries",
            "Employees have been reminded.",
            link="/time",
            source=f"rule:{ctx.rule.id}",
        )
    return ActionResult(f"{len(missing)} missing, reminded {sent}", count=sent, work_done=sent > 0)


@action(
    "nudge_pending_approvals",
    "Remind managers about time-off requests and timesheets waiting longer than a threshold.",
    {"min_age_hours": "int (default 24)"},
)
def nudge_pending_approvals(ctx: RunContext, **params: Any) -> ActionResult:
    cutoff = ctx.now - timedelta(hours=int(params.get("min_age_hours", 24)))
    stale_requests = [
        r
        for r in people.list_time_off(ctx.session, status=TimeOffStatus.PENDING)
        if r.created_at <= cutoff
    ]
    stale_entries = [
        e for e in time_tracking.pending_entries(ctx.session) if e.created_at <= cutoff
    ]
    total = len(stale_requests) + len(stale_entries)
    if not total:
        return ActionResult("nothing waiting for approval")
    source = f"approvals:{local_date(ctx.now).isoformat()}:{ctx.rule.id}"
    if _already_notified(ctx, source):
        return ActionResult(f"{total} waiting; already nudged today", count=total)
    notifications.notify_role(
        ctx.session,
        Role.MANAGER,
        f"{total} item(s) waiting for approval",
        f"{len(stale_requests)} time-off request(s), {len(stale_entries)} timesheet line(s).",
        level=NotificationLevel.WARNING,
        link="/approvals",
        source=source,
    )
    return ActionResult(f"nudged managers about {total} item(s)", count=total, work_done=True)


@action(
    "flag_no_shows",
    "Alert the employee and managers when a published shift started without a clock-in.",
    {"grace_minutes": "int (default 15)", "lookback_hours": "int (default 3)"},
)
def flag_no_shows(ctx: RunContext, **params: Any) -> ActionResult:
    grace = timedelta(minutes=int(params.get("grace_minutes", 15)))
    lookback = timedelta(hours=int(params.get("lookback_hours", 3)))
    stmt = (
        select(Shift)
        .where(
            Shift.status == ShiftStatus.PUBLISHED,
            Shift.employee_id.is_not(None),
            Shift.starts_at <= ctx.now - grace,
            Shift.starts_at >= ctx.now - lookback,
        )
        .order_by(Shift.starts_at)
    )
    flagged = 0
    for shift in ctx.session.scalars(stmt):
        if shift.employee is None or shift.time_entries:
            continue
        clocked = ctx.session.scalar(
            select(TimeEntry.id)
            .where(
                TimeEntry.employee_id == shift.employee_id,
                TimeEntry.clock_in >= shift.starts_at - timedelta(hours=1),
                TimeEntry.clock_in <= shift.ends_at,
            )
            .limit(1)
        )
        if clocked is not None:
            continue
        source = f"no-show:{shift.id}"
        if _already_notified(ctx, source) or _already_notified(ctx, f"{source}:managers"):
            continue
        when = to_local(shift.starts_at).strftime("%a %d %b %H:%M")
        notifications.notify_employee(
            ctx.session,
            shift.employee,
            f"Your shift started at {when} - please clock in",
            "No clock-in has been recorded for this shift.",
            level=NotificationLevel.WARNING,
            link="/me/time",
            source=source,
        )
        notifications.notify_role(
            ctx.session,
            Role.MANAGER,
            f"No-show? {shift.employee.full_name} has not clocked in ({when})",
            f"{shift.department.name} shift started {when}; no time entry yet.",
            level=NotificationLevel.WARNING,
            link="/schedule",
            source=f"{source}:managers",
        )
        flagged += 1
    return ActionResult(
        f"flagged {flagged} possible no-show(s)", count=flagged, work_done=flagged > 0
    )


@action(
    "flag_long_open_entries",
    "Remind employees (and managers) about time entries that were never clocked out.",
    {"max_hours": "int (default 14)"},
)
def flag_long_open_entries(ctx: RunContext, **params: Any) -> ActionResult:
    cutoff = ctx.now - timedelta(hours=int(params.get("max_hours", 14)))
    stmt = select(TimeEntry).where(TimeEntry.clock_out.is_(None), TimeEntry.clock_in <= cutoff)
    flagged = 0
    for entry in ctx.session.scalars(stmt):
        source = f"open-entry:{entry.id}"
        if _already_notified(ctx, source) or _already_notified(ctx, f"{source}:managers"):
            continue
        since = to_local(entry.clock_in).strftime("%a %d %b %H:%M")
        notifications.notify_employee(
            ctx.session,
            entry.employee,
            "You are still clocked in",
            f"Clocked in since {since}. Clock out or report the correct hours.",
            level=NotificationLevel.WARNING,
            link="/me/time",
            source=source,
        )
        notifications.notify_role(
            ctx.session,
            Role.MANAGER,
            f"{entry.employee.full_name} has an open time entry since {since}",
            "It will not count for payroll until it is closed and approved.",
            link="/approvals",
            source=f"{source}:managers",
        )
        flagged += 1
    return ActionResult(f"flagged {flagged} open entr(y/ies)", count=flagged, work_done=flagged > 0)


@action(
    "understaffing_forecast",
    "Compare template headcount with assigned shifts per day and warn managers about gaps.",
    {"days": "int (default 7)"},
)
def understaffing_forecast(ctx: RunContext, **params: Any) -> ActionResult:
    from nexum.models import ScheduleTemplate

    start_day = local_date(ctx.now) + timedelta(days=1)
    end_day = start_day + timedelta(days=int(params.get("days", 7)) - 1)
    templates = list(ctx.session.scalars(select(ScheduleTemplate)))
    gaps: list[str] = []
    for day in iter_days(start_day, end_day):
        required: dict[int, int] = {}
        for template in templates:
            if template.weekday == day.weekday():
                required[template.department_id] = (
                    required.get(template.department_id, 0) + template.headcount
                )
        if not required:
            continue
        lo, hi = day_window(day)
        assigned: dict[int, int] = {}
        for shift in scheduling.list_shifts(ctx.session, lo, hi):
            if shift.employee_id is not None:
                assigned[shift.department_id] = assigned.get(shift.department_id, 0) + 1
        for department_id, need in required.items():
            have = assigned.get(department_id, 0)
            if have < need:
                department = ctx.session.get(Department, department_id)
                name = department.name if department else f"department {department_id}"
                gaps.append(f"{day:%a %d %b}: {name} has {have}/{need} shifts staffed")
    if not gaps:
        return ActionResult("staffing matches the templates for the coming days")
    source = f"understaffing:{local_date(ctx.now).isoformat()}:{ctx.rule.id}"
    if _already_notified(ctx, source):
        return ActionResult(f"{len(gaps)} gap(s); managers already warned today", count=len(gaps))
    notifications.notify_role(
        ctx.session,
        Role.MANAGER,
        f"Understaffing ahead: {len(gaps)} gap(s) in the next {params.get('days', 7)} days",
        "\n".join(gaps[:20]),
        level=NotificationLevel.WARNING,
        link="/schedule",
        source=source,
    )
    return ActionResult(f"warned about {len(gaps)} gap(s)", count=len(gaps), work_done=True)


@action(
    "contract_end_reminders",
    "Remind managers about employees whose contract ends within the coming days.",
    {"days": "int (default 30)"},
)
def contract_end_reminders(ctx: RunContext, **params: Any) -> ActionResult:
    horizon = local_date(ctx.now) + timedelta(days=int(params.get("days", 30)))
    reminded = 0
    for employee in people.list_employees(ctx.session):
        if employee.end_date is None or employee.end_date > horizon:
            continue
        if employee.end_date < local_date(ctx.now):
            continue
        source = f"contract-end:{employee.id}:{employee.end_date.isoformat()}"
        if _already_notified(ctx, source):
            continue
        kind = employee.employment_type.value.replace("_", " ")
        notifications.notify_role(
            ctx.session,
            Role.MANAGER,
            f"{employee.full_name}'s {kind} contract ends {employee.end_date:%d %b}",
            "Decide on renewal, hand-over and access removal.",
            link=f"/employees/{employee.id}",
            source=source,
        )
        reminded += 1
    return ActionResult(
        f"reminded about {reminded} ending contract(s)", count=reminded, work_done=reminded > 0
    )


# --- payroll ------------------------------------------------------------------------------


@action(
    "compute_current_payroll",
    "Recompute payslip previews for the current (open) pay period.",
    {},
)
def compute_current_payroll(ctx: RunContext, **params: Any) -> ActionResult:
    period = payroll.current_period(ctx.session, local_date(ctx.now))
    if period.status != PayPeriodStatus.OPEN:
        return ActionResult(f"period {period.label} is {period.status.value}; nothing to do")
    slips = payroll.compute_payslips(ctx.session, period)
    totals = payroll.period_totals(period)
    return ActionResult(
        f"refreshed {len(slips)} payslip preview(s); "
        f"projected gross {totals['gross']} {period.currency}",
        count=len(slips),
        work_done=bool(slips),
    )


@action(
    "close_previous_pay_period",
    "Close the previous pay period (last month or last bi-weekly period): final payslips, locked.",
    {},
)
def close_previous_pay_period(ctx: RunContext, **params: Any) -> ActionResult:
    period = payroll.previous_period(ctx.session, local_date(ctx.now))
    if period.status != PayPeriodStatus.OPEN:
        return ActionResult(f"period {period.label} already {period.status.value}")
    slips = payroll.close_period(ctx.session, period)
    ctx.data["period_id"] = period.id
    totals = payroll.period_totals(period)
    return ActionResult(
        f"closed {period.label}: {len(slips)} payslip(s), "
        f"gross {totals['gross']} {period.currency}",
        count=len(slips),
        work_done=True,
        data={"period_id": period.id},
    )


@action(
    "notify_payslips_ready",
    "Tell each employee with a payslip in the period (event.period_id or last closed) it is ready.",
    {"period_id": "int (optional)"},
)
def notify_payslips_ready(ctx: RunContext, **params: Any) -> ActionResult:
    period_id = params.get("period_id") or ctx.data.get("period_id")
    if period_id is None and ctx.event is not None:
        period_id = ctx.event.payload.get("period_id")
    period: PayPeriod | None = None
    if isinstance(period_id, int):
        period = ctx.session.get(PayPeriod, period_id)
    if period is None:
        stmt = (
            select(PayPeriod)
            .where(PayPeriod.status != PayPeriodStatus.OPEN)
            .order_by(PayPeriod.end_date.desc())
            .limit(1)
        )
        period = ctx.session.scalar(stmt)
    if period is None:
        return ActionResult("no closed pay period found")
    sent = 0
    for slip in period.payslips:
        source = f"payslip-ready:{slip.id}"
        if _already_notified(ctx, source):
            continue
        if notifications.notify_employee(
            ctx.session,
            slip.employee,
            f"Payslip ready: {period.label}",
            f"Gross {slip.gross_amount} {slip.currency} "
            f"({slip.regular_hours}h + {slip.overtime_hours}h overtime).",
            link="/me/pay",
            source=source,
        ):
            sent += 1
    return ActionResult(
        f"notified {sent} employee(s) about {period.label}", count=sent, work_done=sent > 0
    )


@action(
    "notify_schedule_published",
    "After a schedule is published, send each affected employee their shifts for that window.",
    {},
)
def notify_schedule_published(ctx: RunContext, **params: Any) -> ActionResult:
    if ctx.event is None:
        return ActionResult("no publish event in context")
    payload = ctx.event.payload
    shift_ids = payload.get("shift_ids") or []
    if not shift_ids:
        return ActionResult("no shifts in event")
    shifts = list(
        ctx.session.scalars(select(Shift).where(Shift.id.in_(shift_ids)).order_by(Shift.starts_at))
    )
    by_employee: dict[int, list[Shift]] = {}
    for shift in shifts:
        if shift.employee_id is not None:
            by_employee.setdefault(shift.employee_id, []).append(shift)
    sent = 0
    for employee_id, mine in by_employee.items():
        employee = ctx.session.get(Employee, employee_id)
        if employee is None:
            continue
        lines = [
            f"{s.starts_at:%a %d %b %H:%M}-{s.ends_at:%H:%M} {s.department.name}"
            + (f" ({s.role_label})" if s.role_label else "")
            for s in mine
        ]
        if notifications.notify_employee(
            ctx.session,
            employee,
            f"Your schedule is published ({len(mine)} shift(s))",
            "\n".join(lines),
            link="/me",
            source=f"rule:{ctx.rule.id}",
        ):
            sent += 1
    return ActionResult(f"sent schedules to {sent} employee(s)", count=sent, work_done=sent > 0)


def run_action(ctx: RunContext, entry: dict[str, Any]) -> ActionResult:
    spec = get_action(str(entry.get("type")))
    params = entry.get("params") or {}
    try:
        return spec.fn(ctx, **params)
    except TypeError as exc:  # bad params
        raise ValidationError(f"{spec.name}: {exc}") from exc
    except NexumError:
        raise


__all__ = [
    "ActionSpec",
    "Shift",
    "action",
    "get_action",
    "list_actions",
    "run_action",
    "validate_actions",
]
