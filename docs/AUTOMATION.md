# Automation reference

An automation rule is **trigger → conditions → actions**. Rules live in the
`automation_rules` table and are edited from the UI (`/automations`, with a structured
trigger form and JSON for conditions and actions), the API (`/api/v1/automations/rules`)
or shipped as built-in recipes. *Test run* on a rule page executes it and rolls every
change back, so you can see what it would do.

```json
{
  "name": "Route time-off requests to managers",
  "trigger_type": "event",
  "trigger_config": {"event": "timeoff.requested"},
  "conditions": [{"field": "request.days", "op": "gte", "value": 3}],
  "actions": [
    {"type": "notify_role", "params": {
      "role": "manager",
      "title": "Time-off request from {employee.full_name}",
      "body": "{request.kind} {request.start_date} to {request.end_date} ({request.days} day(s)).",
      "link": "/approvals"}}
  ],
  "estimated_minutes_saved": 2
}
```

## Triggers

### Schedule (`trigger_type: "schedule"`)

| `trigger_config` | Fires |
|---|---|
| `{"kind": "interval", "minutes": 15}` | every 15 minutes after the previous run |
| `{"kind": "hourly", "minute": 0}` | every hour at :00 |
| `{"kind": "daily", "at": "06:30"}` | every day at 06:30 company time |
| `{"kind": "weekly", "weekday": 0, "at": "06:00"}` | Mondays (0) to Sundays (6) at 06:00 company time |
| `{"kind": "monthly", "day": 1, "at": "06:00"}` | the 1st of each month (day clamped to month length) |

Wall-clock times use the company time zone from the settings page. A scheduled rule that
missed several slots (server down) runs **once** when the ticker returns, then resumes its
normal cadence.

### Event (`trigger_type: "event"`)

`trigger_config` is `{"event": "<name>"}`. Events and their payload keys:

| Event | Payload keys |
|---|---|
| `employee.created` | `employee_id`, `department_id`, `email`, `employment_type` |
| `employee.deactivated` | `employee_id` |
| `shift.created` | `shift_id`, `department_id`, `employee_id`, `starts_at`, `ends_at`, `is_open`, `status`, `source` (`template`/`manual`) |
| `shift.assigned` | `shift_id`, `department_id`, `employee_id`, `starts_at`, `ends_at`, `source` (`manual`/`auto`/`claim`/`transfer`) |
| `shift.unassigned` | `shift_id`, `department_id`, `employee_id` (previous), `starts_at`, `ends_at` |
| `shift.cancelled` | `shift_id`, `department_id`, `employee_id`, `starts_at` |
| `shift.claim_requested` | `shift_request_id`, `kind`, `shift_id`, `department_id`, `employee_id`, `starts_at` |
| `shift.drop_requested` | as above |
| `shift.transfer_requested` | as above plus `target_employee_id` |
| `shift.request_approved` | `shift_request_id`, `kind`, `shift_id`, `department_id`, `employee_id`, `target_employee_id`, `starts_at` |
| `shift.request_rejected` | as above |
| `schedule.published` | `count`, `department_ids`, `employee_ids`, `shift_ids`, `week_start`, `starts_at`, `ends_at` |
| `timeoff.requested` | `request_id`, `employee_id`, `department_id`, `kind`, `start_date`, `end_date`, `days` |
| `timeoff.approved` | `request_id`, `employee_id`, `kind`, `start_date`, `end_date`, `days` |
| `timeoff.rejected` | as above |
| `time_entry.opened` | `entry_id`, `employee_id`, `at` |
| `time_entry.closed` | `entry_id`, `employee_id`, `worked_hours` |
| `time_entry.submitted` | as above |
| `time_entry.approved` | as above |
| `pay_period.closed` | `period_id`, `start_date`, `end_date`, `payslips` |
| `payroll.computed` | `period_id`, `start_date`, `end_date`, `payslips`, `total_gross` |

### Manual (`trigger_type: "manual"`)

Runs only from "Run now" in the UI, `POST /api/v1/automations/rules/{id}/run`, or
`nexum automations fire <id-or-key>`. Any rule can also be run manually regardless of its
trigger type, and any rule can be dry-run (`.../dry-run`).

## Conditions

`conditions` is a list; **all** entries must hold. Each entry is one of:

- `{"field": "<path>", "op": "<op>", "value": <any>}`
- `{"any": [ ... ]}` — at least one nested condition holds
- `{"all": [ ... ]}` — every nested condition holds
- `{"not": { ... }}`

Operators: `eq ne gt gte lt lte in not_in contains startswith is_null not_null is_true is_false`.
Comparisons coerce ISO date strings and numeric strings; a type mismatch evaluates to
false rather than raising.

Field paths resolve against the run context:

| Path prefix | Contents |
|---|---|
| `event.*` | the event payload (see above); empty for scheduled/manual runs |
| `event_name` | the event name |
| `now.iso`, `now.date`, `now.hour`, `now.minute`, `now.weekday` | the engine clock (company time) |
| `rule.id`, `rule.key`, `rule.name` | the rule itself |
| `employee.*` | loaded when the payload has `employee_id`: `id full_name first_name email department user_id` |
| `target.*` | from `target_employee_id`: `id full_name email` |
| `shift.*` | from `shift_id`: `id starts_at ends_at department role_label hours status` |
| `shift_request.*` | from `shift_request_id`: `id kind status employee_name target_name note` |
| `department.*` | from `department_id`: `id name` |
| `request.*` | from `request_id` (time off): `id kind start_date end_date days status` |
| `period.*` | from `period_id`: `id label status start_date end_date` |

The same paths work inside message templates: `"Open shift {shift.starts_at} in {department.name}"`.
Unknown placeholders render as empty strings; a template never crashes a run. Actions that
take an `employee_id` also accept a template such as `"{event.target_employee_id}"`.

## Actions

Actions run in order inside one transaction. If any action raises, the whole run is
rolled back and recorded as `failed`. Outbound actions (`webhook`, `chat_webhook`,
`send_email`) retry with backoff (`NEXUM_AUTOMATION_RETRY_*`). `estimated_minutes_saved`
is credited only when at least one action reports that it did real work.

| Action | Params | What it does |
|---|---|---|
| `auto_approve_shift_requests` | `kinds`: list of claim|drop|transfer (default ["claim"]) | Approve pending shift requests that cause no conflict (the event's request, or all pending). |
| `auto_assign_open_shifts` | `weeks_ahead`: int: cover that whole ISO week (0 = this week); overrides the day window, `days`: window length in days from now (default 7), `start_days`: offset from now in days (default 0) | Fill open shifts with available employees (no clashes, no time off, under weekly hours). |
| `chat_webhook` | `url`: https://hooks.slack.com/..., `text`: template | Post a message to a Slack/Teams incoming webhook (requires webhooks to be enabled). |
| `close_previous_pay_period` | — | Close the previous pay period (last month or last bi-weekly period): final payslips, locked. |
| `compute_current_payroll` | — | Recompute payslip previews for the current (open) pay period. |
| `contract_end_reminders` | `days`: int (default 30) | Remind managers about employees whose contract ends within the coming days. |
| `flag_long_open_entries` | `max_hours`: int (default 14) | Remind employees (and managers) about time entries that were never clocked out. |
| `flag_missing_time_entries` | `days`: look-back window (default 3) | Nudge employees (and managers) about ended shifts that have no time entry. |
| `flag_no_shows` | `grace_minutes`: int (default 15), `lookback_hours`: int (default 3) | Alert the employee and managers when a published shift started without a clock-in. |
| `flag_overtime` | `threshold_hours`: float (default: settings threshold) | Warn managers about employees scheduled above the weekly overtime threshold this week. |
| `generate_next_week_schedule` | `weeks_ahead`: int (default 1), `department_id`: int (optional) | Create draft open shifts from the schedule templates for an upcoming week. |
| `log` | `message`: template | Append a message to the run log (useful for testing rules). |
| `notify_employee` | `employee_id`: int (optional, default event.employee_id), `title`: template, `body`: template | Notify the employee referenced by the event (event.employee_id) or an explicit employee_id. |
| `notify_open_shifts` | `days`: int (default 7), `role`: role to notify (default manager) | Send managers a digest of open shifts in the coming days (silent when there are none). |
| `notify_payslips_ready` | `period_id`: int (optional) | Tell each employee with a payslip in the period (event.period_id or last closed) it is ready. |
| `notify_role` | `role`: admin|manager|employee, `title`: template, `body`: template, `level`: info|warning|critical | Send an in-app notification to every user with at least the given role. |
| `notify_schedule_published` | — | After a schedule is published, send each affected employee their shifts for that window. |
| `nudge_pending_approvals` | `min_age_hours`: int (default 24) | Remind managers about time-off requests and timesheets waiting longer than a threshold. |
| `publish_drafts` | `days`: window length in days (default 14) | Publish draft shifts that start within the window so employees can see them. |
| `remind_upcoming_shifts` | `hours`: look-ahead window (default 24) | Remind employees of published shifts starting soon (each shift reminded once). |
| `send_email` | `to`: comma separated addresses, `role`: admin|manager|employee, `employee_id`: int or template, `subject`: template, `body`: template | Send an e-mail (needs SMTP in company settings) to addresses, a role and/or an employee. |
| `understaffing_forecast` | `days`: int (default 7) | Compare template headcount with assigned shifts per day and warn managers about gaps. |
| `webhook` | `url`: https://..., `timeout`: seconds | POST the event/context as JSON to a URL (requires NEXUM_AUTOMATION_WEBHOOKS_ENABLED=true). |

`GET /api/v1/automations/actions` returns this list with parameter hints; the rule editor
shows it in the sidebar.

### Delivery

In-app notifications are the primary channel. When SMTP is configured on `/settings` and
"send notifications by e-mail" is on, every notification is also e-mailed to users who
have not opted out on their notifications page. `chat_webhook` posts to Slack/Teams
incoming webhooks and `webhook` posts the full context as JSON; both need
`NEXUM_AUTOMATION_WEBHOOKS_ENABLED=true`.

## Built-in recipes

Installed by `nexum init-db` / `nexum seed` / the setup wizard / `nexum automations
install-recipes`. They are upserted by `key`, so an upgrade can add or fix recipes while
keeping your enable/disable choices. `--reset` restores every recipe's definition.

| Key | Trigger | Actions | Saves/run |
|---|---|---|---|
| `open-shift-alert` | `shift.created` | `notify_role` | 2 min |
| `unassigned-shift-alert` | `shift.unassigned` | `notify_role` | 2 min |
| `daily-cover` | daily at 06:30 (company time) | `auto_assign_open_shifts`, `notify_open_shifts` | 15 min |
| `weekly-schedule-draft` | weekly on Mon at 06:00 (company time) | `generate_next_week_schedule`, `auto_assign_open_shifts` | 45 min |
| `shift-reminders` | hourly at :00 | `remind_upcoming_shifts` | 5 min |
| `missing-timesheets` | daily at 18:00 (company time) | `flag_missing_time_entries` | 10 min |
| `approval-nudge` | daily at 09:00 (company time) | `nudge_pending_approvals` | 5 min |
| `overtime-watch` | daily at 12:00 (company time) | `flag_overtime` | 10 min |
| `timeoff-request-alert` | `timeoff.requested` | `notify_role` | 2 min |
| `timeoff-approved-notify` | `timeoff.approved` | `notify_employee` | 2 min |
| `timeoff-rejected-notify` | `timeoff.rejected` | `notify_employee` | 2 min |
| `payroll-preview` | daily at 05:00 (company time) | `compute_current_payroll` | 20 min |
| `monthly-payroll-close` | monthly on day 1 at 06:00 (company time) | `close_previous_pay_period`, `notify_payslips_ready` | 120 min |
| `auto-approve-claims` | `shift.claim_requested` | `auto_approve_shift_requests` | 5 min |
| `drop-request-alert` | `shift.drop_requested` | `notify_role` | 2 min |
| `transfer-request-alert` | `shift.transfer_requested` | `notify_role` | 2 min |
| `shift-request-approved-notify` | `shift.request_approved` | `notify_employee`, `notify_employee` | 2 min |
| `shift-request-rejected-notify` | `shift.request_rejected` | `notify_employee` | 2 min |
| `schedule-published-notify` | `schedule.published` | `notify_schedule_published` | 15 min |
| `no-show-check` | every 15 min | `flag_no_shows` | 10 min |
| `open-entry-check` | hourly at :30 | `flag_long_open_entries` | 5 min |
| `understaffing-forecast` | daily at 07:00 (company time) | `understaffing_forecast` | 15 min |
| `contract-end-reminders` | daily at 08:00 (company time) | `contract_end_reminders` | 10 min |
| `welcome-new-employee` | `employee.created` | `notify_employee`, `notify_role` | 10 min |

## Adding an action

```python
from nexum.automation.actions import action
from nexum.automation.context import ActionResult, RunContext

@action("celebrate", "Send a party message to a department.", {"department_id": "int"})
def celebrate(ctx: RunContext, **params) -> ActionResult:
    # use ctx.session, ctx.now, ctx.event, ctx.settings; call services; ctx.say("...") to log
    ...
    return ActionResult("sent 3 messages", count=3, work_done=True)
```

Import the module somewhere at startup (for example from `nexum/automation/__init__.py`)
so the registration runs. Add a test that runs the action through `AutomationEngine.run_rule`.

## Operations

- The ticker runs in the web process every `NEXUM_AUTOMATION_TICK_SECONDS` (30). Run
  `nexum automations run-due` from cron instead if you prefer an external scheduler
  (set `NEXUM_AUTOMATION_ENABLED=false` in the web process then).
- `nexum automations list` shows enabled state, trigger, run count and next run.
- `nexum automations fire <key>` runs one rule now; exit code 1 on failure.
- `/automations/report` (and `/api/v1/automations/report`) shows runs, failures, average
  duration and minutes saved per rule for the last 7/30/90 days.
- The dashboard's "hours saved" is the sum of `minutes_saved` over runs; only productive
  successful runs count.
- Run history keeps every scheduled, manual and successful/failed event run. Event runs
  whose conditions did not match, and dry runs, are not stored.
