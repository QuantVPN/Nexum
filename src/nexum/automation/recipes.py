"""Built-in automation recipes, installed by ``nexum init-db`` / ``nexum seed``.

Each recipe declares an ``estimated_minutes_saved`` per productive run; the dashboard sums
those into the "hours saved" figure so admins can see what the automation is worth.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from nexum.models import AutomationRule, TriggerType

RECIPES: list[dict[str, Any]] = [
    {
        "key": "open-shift-alert",
        "name": "Alert managers when an open shift is created",
        "description": "Whenever a shift is created without an employee, managers get a heads-up.",
        "trigger_type": TriggerType.EVENT,
        "trigger_config": {"event": "shift.created"},
        "conditions": [
            {"field": "event.is_open", "op": "is_true"},
            {"field": "event.source", "op": "ne", "value": "template"},
        ],
        "actions": [
            {
                "type": "notify_role",
                "params": {
                    "role": "manager",
                    "title": "Open shift {shift.starts_at} in {department.name}",
                    "body": "{shift.hours}h shift {shift.role_label} needs somebody.",
                    "link": "/schedule",
                },
            }
        ],
        "estimated_minutes_saved": 2,
    },
    {
        "key": "unassigned-shift-alert",
        "name": "Alert managers when a shift loses its employee",
        "description": "Cancellations and unassignments are surfaced immediately.",
        "trigger_type": TriggerType.EVENT,
        "trigger_config": {"event": "shift.unassigned"},
        "conditions": [],
        "actions": [
            {
                "type": "notify_role",
                "params": {
                    "role": "manager",
                    "title": "Shift {shift.starts_at} is now open",
                    "body": "{employee.full_name} was removed from the shift.",
                    "level": "warning",
                    "link": "/schedule",
                },
            }
        ],
        "estimated_minutes_saved": 2,
    },
    {
        "key": "daily-cover",
        "name": "Fill open shifts every morning",
        "description": "Auto-assign open shifts for the next 7 days, then send managers a digest of anything still open.",
        "trigger_type": TriggerType.SCHEDULE,
        "trigger_config": {"kind": "daily", "at": "06:30"},
        "conditions": [],
        "actions": [
            {"type": "auto_assign_open_shifts", "params": {"days": 7}},
            {"type": "notify_open_shifts", "params": {"days": 7}},
        ],
        "estimated_minutes_saved": 15,
    },
    {
        "key": "weekly-schedule-draft",
        "name": "Draft next week's schedule from templates",
        "description": "Every Monday, generate next week's shifts from the templates and pre-fill them.",
        "trigger_type": TriggerType.SCHEDULE,
        "trigger_config": {"kind": "weekly", "weekday": 0, "at": "06:00"},
        "conditions": [],
        "actions": [
            {"type": "generate_next_week_schedule", "params": {"weeks_ahead": 1}},
            {"type": "auto_assign_open_shifts", "params": {"weeks_ahead": 1}},
        ],
        "estimated_minutes_saved": 45,
    },
    {
        "key": "shift-reminders",
        "name": "Remind employees of tomorrow's shifts",
        "description": "Hourly check; each published shift starting within 24h gets one reminder.",
        "trigger_type": TriggerType.SCHEDULE,
        "trigger_config": {"kind": "hourly", "minute": 0},
        "conditions": [],
        "actions": [{"type": "remind_upcoming_shifts", "params": {"hours": 24}}],
        "estimated_minutes_saved": 5,
    },
    {
        "key": "missing-timesheets",
        "name": "Chase missing time entries",
        "description": "Every evening, employees whose finished shifts have no time entry are reminded.",
        "trigger_type": TriggerType.SCHEDULE,
        "trigger_config": {"kind": "daily", "at": "18:00"},
        "conditions": [],
        "actions": [{"type": "flag_missing_time_entries", "params": {"days": 3}}],
        "estimated_minutes_saved": 10,
    },
    {
        "key": "approval-nudge",
        "name": "Nudge managers about stale approvals",
        "description": "Time-off requests and timesheets waiting more than 24h are bundled into one reminder.",
        "trigger_type": TriggerType.SCHEDULE,
        "trigger_config": {"kind": "daily", "at": "09:00"},
        "conditions": [],
        "actions": [{"type": "nudge_pending_approvals", "params": {"min_age_hours": 24}}],
        "estimated_minutes_saved": 5,
    },
    {
        "key": "overtime-watch",
        "name": "Warn about overtime before it happens",
        "description": "Flags employees scheduled above the weekly threshold while there is still time to re-plan.",
        "trigger_type": TriggerType.SCHEDULE,
        "trigger_config": {"kind": "daily", "at": "12:00"},
        "conditions": [],
        "actions": [{"type": "flag_overtime", "params": {}}],
        "estimated_minutes_saved": 10,
    },
    {
        "key": "timeoff-request-alert",
        "name": "Route time-off requests to managers",
        "description": "New requests reach managers instantly with the dates and kind.",
        "trigger_type": TriggerType.EVENT,
        "trigger_config": {"event": "timeoff.requested"},
        "conditions": [],
        "actions": [
            {
                "type": "notify_role",
                "params": {
                    "role": "manager",
                    "title": "Time-off request from {employee.full_name}",
                    "body": "{request.kind} {request.start_date} to {request.end_date} ({request.days} day(s)).",
                    "link": "/approvals",
                },
            }
        ],
        "estimated_minutes_saved": 2,
    },
    {
        "key": "timeoff-approved-notify",
        "name": "Tell employees when time off is approved",
        "trigger_type": TriggerType.EVENT,
        "trigger_config": {"event": "timeoff.approved"},
        "conditions": [],
        "actions": [
            {
                "type": "notify_employee",
                "params": {
                    "title": "Time off approved",
                    "body": "Your {request.kind} from {request.start_date} to {request.end_date} is approved.",
                    "link": "/me",
                },
            }
        ],
        "estimated_minutes_saved": 2,
    },
    {
        "key": "timeoff-rejected-notify",
        "name": "Tell employees when time off is rejected",
        "trigger_type": TriggerType.EVENT,
        "trigger_config": {"event": "timeoff.rejected"},
        "conditions": [],
        "actions": [
            {
                "type": "notify_employee",
                "params": {
                    "title": "Time off not approved",
                    "body": "Your {request.kind} request for {request.start_date} to {request.end_date} was declined.",
                    "level": "warning",
                    "link": "/me",
                },
            }
        ],
        "estimated_minutes_saved": 2,
    },
    {
        "key": "payroll-preview",
        "name": "Refresh payroll preview nightly",
        "description": "Keeps the projected payroll cost and every employee's salary estimate current.",
        "trigger_type": TriggerType.SCHEDULE,
        "trigger_config": {"kind": "daily", "at": "05:00"},
        "conditions": [],
        "actions": [{"type": "compute_current_payroll", "params": {}}],
        "estimated_minutes_saved": 20,
    },
    {
        "key": "monthly-payroll-close",
        "name": "Close last month's payroll on the 1st",
        "description": "Computes final payslips, locks the period and notifies every employee.",
        "trigger_type": TriggerType.SCHEDULE,
        "trigger_config": {"kind": "monthly", "day": 1, "at": "06:00"},
        "conditions": [],
        "actions": [
            {"type": "close_previous_pay_period", "params": {}},
            {"type": "notify_payslips_ready", "params": {}},
        ],
        "estimated_minutes_saved": 120,
    },
    {
        "key": "auto-approve-claims",
        "name": "Auto-approve conflict-free shift claims",
        "description": "When an employee claims an open shift and nothing conflicts, approve it instantly instead of waiting for a manager.",
        "trigger_type": TriggerType.EVENT,
        "trigger_config": {"event": "shift.claim_requested"},
        "conditions": [],
        "actions": [{"type": "auto_approve_shift_requests", "params": {"kinds": ["claim"]}}],
        "estimated_minutes_saved": 5,
    },
    {
        "key": "drop-request-alert",
        "name": "Route drop requests to managers",
        "trigger_type": TriggerType.EVENT,
        "trigger_config": {"event": "shift.drop_requested"},
        "conditions": [],
        "actions": [
            {
                "type": "notify_role",
                "params": {
                    "role": "manager",
                    "title": "{employee.full_name} wants to drop {shift.starts_at}",
                    "body": "{shift.department} {shift.hours}h. Note: {shift_request.note}",
                    "link": "/approvals",
                },
            }
        ],
        "estimated_minutes_saved": 2,
    },
    {
        "key": "transfer-request-alert",
        "name": "Route hand-over requests to managers",
        "trigger_type": TriggerType.EVENT,
        "trigger_config": {"event": "shift.transfer_requested"},
        "conditions": [],
        "actions": [
            {
                "type": "notify_role",
                "params": {
                    "role": "manager",
                    "title": "{employee.full_name} wants to hand {shift.starts_at} to {target.full_name}",
                    "body": "{shift.department} {shift.hours}h. Note: {shift_request.note}",
                    "link": "/approvals",
                },
            }
        ],
        "estimated_minutes_saved": 2,
    },
    {
        "key": "shift-request-approved-notify",
        "name": "Tell employees when a shift request is approved",
        "trigger_type": TriggerType.EVENT,
        "trigger_config": {"event": "shift.request_approved"},
        "conditions": [],
        "actions": [
            {
                "type": "notify_employee",
                "params": {
                    "title": "Your {shift_request.kind} request was approved",
                    "body": "Shift {shift.starts_at}-{shift.ends_at} ({shift.department}).",
                    "link": "/me/shifts",
                },
            },
            {
                "type": "notify_employee",
                "params": {
                    "employee_id": "{event.target_employee_id}",
                    "title": "A shift was handed to you",
                    "body": "{shift_request.employee_name} handed you {shift.starts_at}-{shift.ends_at} ({shift.department}).",
                    "link": "/me/shifts",
                },
            },
        ],
        "estimated_minutes_saved": 2,
    },
    {
        "key": "shift-request-rejected-notify",
        "name": "Tell employees when a shift request is declined",
        "trigger_type": TriggerType.EVENT,
        "trigger_config": {"event": "shift.request_rejected"},
        "conditions": [],
        "actions": [
            {
                "type": "notify_employee",
                "params": {
                    "title": "Your {shift_request.kind} request was declined",
                    "body": "Shift {shift.starts_at}-{shift.ends_at} ({shift.department}).",
                    "level": "warning",
                    "link": "/me/shifts",
                },
            }
        ],
        "estimated_minutes_saved": 2,
    },
    {
        "key": "schedule-published-notify",
        "name": "Send employees their schedule when it is published",
        "description": "Every affected employee gets one message listing their published shifts.",
        "trigger_type": TriggerType.EVENT,
        "trigger_config": {"event": "schedule.published"},
        "conditions": [],
        "actions": [{"type": "notify_schedule_published", "params": {}}],
        "estimated_minutes_saved": 15,
    },
    {
        "key": "no-show-check",
        "name": "Spot no-shows within 15 minutes",
        "description": "Every 15 minutes, published shifts that started without a clock-in alert the employee and managers.",
        "trigger_type": TriggerType.SCHEDULE,
        "trigger_config": {"kind": "interval", "minutes": 15},
        "conditions": [],
        "actions": [{"type": "flag_no_shows", "params": {"grace_minutes": 15}}],
        "estimated_minutes_saved": 10,
    },
    {
        "key": "open-entry-check",
        "name": "Catch forgotten clock-outs",
        "description": "Hourly; entries open longer than 14 hours are flagged to the employee and managers.",
        "trigger_type": TriggerType.SCHEDULE,
        "trigger_config": {"kind": "hourly", "minute": 30},
        "conditions": [],
        "actions": [{"type": "flag_long_open_entries", "params": {"max_hours": 14}}],
        "estimated_minutes_saved": 5,
    },
    {
        "key": "understaffing-forecast",
        "name": "Forecast understaffing for the coming week",
        "description": "Every morning, compare template headcount with staffed shifts for the next 7 days.",
        "trigger_type": TriggerType.SCHEDULE,
        "trigger_config": {"kind": "daily", "at": "07:00"},
        "conditions": [],
        "actions": [{"type": "understaffing_forecast", "params": {"days": 7}}],
        "estimated_minutes_saved": 15,
    },
    {
        "key": "contract-end-reminders",
        "name": "Remind about ending contracts",
        "description": "Daily reminder to managers 30 days before an employee's end date.",
        "trigger_type": TriggerType.SCHEDULE,
        "trigger_config": {"kind": "daily", "at": "08:00"},
        "conditions": [],
        "actions": [{"type": "contract_end_reminders", "params": {"days": 30}}],
        "estimated_minutes_saved": 10,
    },
    {
        "key": "welcome-new-employee",
        "name": "Welcome new employees and start onboarding",
        "trigger_type": TriggerType.EVENT,
        "trigger_config": {"event": "employee.created"},
        "conditions": [],
        "actions": [
            {
                "type": "notify_employee",
                "params": {
                    "title": "Welcome to the team, {employee.first_name}!",
                    "body": "Your schedule and pay details live here. Check /me for your next shifts.",
                    "link": "/me",
                },
            },
            {
                "type": "notify_role",
                "params": {
                    "role": "manager",
                    "title": "New employee: {employee.full_name}",
                    "body": "Onboarding checklist: equipment, introductions, first-week schedule.",
                    "link": "/employees",
                },
            },
        ],
        "estimated_minutes_saved": 10,
    },
]


def install_recipes(session: Session, *, reset: bool = False) -> list[AutomationRule]:
    """Create missing recipes (or reset every recipe to its definition when ``reset``)."""
    installed: list[AutomationRule] = []
    for recipe in RECIPES:
        rule = session.scalar(select(AutomationRule).where(AutomationRule.key == recipe["key"]))
        if rule is None:
            rule = AutomationRule(key=recipe["key"], enabled=True)
            session.add(rule)
        elif not reset:
            continue
        rule.name = recipe["name"]
        rule.description = recipe.get("description")
        rule.trigger_type = recipe["trigger_type"]
        rule.trigger_config = dict(recipe["trigger_config"])
        rule.conditions = list(recipe.get("conditions", []))
        rule.actions = list(recipe["actions"])
        rule.estimated_minutes_saved = int(recipe.get("estimated_minutes_saved", 0))
        if reset:
            rule.next_run_at = None
        installed.append(rule)
    session.flush()
    return installed
