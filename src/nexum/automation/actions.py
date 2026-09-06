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
from nexum.errors import NexumError, ValidationError
from nexum.models import (
    Employee,
    Notification,
    NotificationLevel,
    PayPeriod,
    PayPeriodStatus,
    Role,
    Shift,
    TimeOffStatus,
)
from nexum.services import notifications, payroll, people, scheduling, time_tracking
from nexum.services.calendar import local_date, week_start, week_window
from nexum.services.company import payroll_rules

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
    employee_id = params.get("employee_id") or mapping.get("event", {}).get("employee_id")
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
    response = httpx.post(url, json=payload, timeout=float(params.get("timeout", 10)))
    response.raise_for_status()
    return ActionResult(f"POST {url} -> {response.status_code}", count=1, work_done=True)


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
