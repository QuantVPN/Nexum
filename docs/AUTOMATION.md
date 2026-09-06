# Automation reference

An automation rule is **trigger → conditions → actions**. Rules live in the
`automation_rules` table and are edited from the UI (`/automations`), the API
(`/api/v1/automations/rules`) or shipped as built-in recipes.

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
| `{"kind": "daily", "at": "06:30"}` | every day at 06:30 UTC |
| `{"kind": "weekly", "weekday": 0, "at": "06:00"}` | Mondays (0) to Sundays (6) at 06:00 UTC |
| `{"kind": "monthly", "day": 1, "at": "06:00"}` | the 1st of each month (day clamped to month length) |

A scheduled rule that missed several slots (server down) runs **once** when the ticker
returns, then resumes its normal cadence.

### Event (`trigger_type: "event"`)

`trigger_config` is `{"event": "<name>"}`. Events and their payload keys:

| Event | Payload keys |
|---|---|
| `employee.created` | `employee_id`, `department_id`, `email`, `employment_type` |
| `employee.deactivated` | `employee_id` |
| `shift.created` | `shift_id`, `department_id`, `employee_id`, `starts_at`, `ends_at`, `is_open`, `status`, `source` (`template` or `manual`) |
| `shift.assigned` | `shift_id`, `department_id`, `employee_id`, `starts_at`, `ends_at`, `source` (`manual` or `auto`) |
| `shift.unassigned` | `shift_id`, `department_id`, `employee_id` (previous), `starts_at`, `ends_at` |
| `shift.cancelled` | `shift_id`, `department_id`, `employee_id`, `starts_at` |
| `schedule.published` | `count`, `department_ids`, `employee_ids`, `shift_ids`, `week_start`, `starts_at`, `ends_at` |
| `timeoff.requested` / `timeoff.approved` / `timeoff.rejected` | `request_id`, `employee_id`, `kind`, `start_date`, `end_date`, `days` (+ `department_id` on requested) |
| `time_entry.opened` | `entry_id`, `employee_id`, `at` |
| `time_entry.closed` / `time_entry.submitted` / `time_entry.approved` | `entry_id`, `employee_id`, `worked_hours` |
| `payroll.computed` | `period_id`, `start_date`, `end_date`, `payslips`, `total_gross` |
| `pay_period.closed` | `period_id`, `start_date`, `end_date`, `payslips` |

### Manual (`trigger_type: "manual"`)

Runs only from "Run now" in the UI, `POST /api/v1/automations/rules/{id}/run`, or
`nexum automations fire <id-or-key>`. Any rule can also be run manually regardless of its
trigger type.

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
| `now.iso`, `now.date`, `now.hour`, `now.minute`, `now.weekday` | the engine clock (UTC) |
| `rule.id`, `rule.key`, `rule.name` | the rule itself |
| `employee.*` | loaded when the payload has `employee_id`: `id full_name first_name email department user_id` |
| `shift.*` | from `shift_id`: `id starts_at ends_at department role_label hours status` |
| `department.*` | from `department_id`: `id name` |
| `request.*` | from `request_id`: `id kind start_date end_date days status` |
| `period.*` | from `period_id`: `id label status start_date end_date` |

The same paths work inside message templates: `"Open shift {shift.starts_at} in {department.name}"`.
Unknown placeholders render as empty strings; a template never crashes a run.

## Actions

Actions run in order inside one transaction. If any action raises, the whole run is
rolled back and recorded as `failed`. `estimated_minutes_saved` is credited only when at
least one action reports that it did real work (sent something, assigned something, ...).

| Action | Params | What it does |
|---|---|---|
| `notify_role` | `role`, `title`, `body`, `level`, `link` | in-app notification to every active user with at least that role |
| `notify_employee` | `employee_id` (default `event.employee_id`), `title`, `body`, `level`, `link` | notification to one employee's login |
| `notify_open_shifts` | `days` (7), `role` (manager) | digest of open shifts in the window; silent when none |
| `notify_schedule_published` | — | after `schedule.published`, each affected employee gets their shift list |
| `remind_upcoming_shifts` | `hours` (24) | one reminder per published shift starting within the window |
| `generate_next_week_schedule` | `weeks_ahead` (1), `department_id` | draft open shifts from templates for that week (idempotent) |
| `auto_assign_open_shifts` | `weeks_ahead` **or** `start_days` + `days` | fill open shifts respecting clashes, time off and weekly hours; fairest-first |
| `publish_drafts` | `days` (14) | publish draft shifts starting within the window |
| `flag_overtime` | `threshold_hours` (settings) | warn managers once per week about employees scheduled above the threshold |
| `flag_missing_time_entries` | `days` (3) | remind employees (once per shift) and managers about ended shifts without a time entry |
| `nudge_pending_approvals` | `min_age_hours` (24) | one reminder per day to managers about stale time-off requests and timesheets |
| `compute_current_payroll` | — | refresh payslip previews for the open current period |
| `close_previous_pay_period` | — | compute final payslips for last month and lock the period |
| `notify_payslips_ready` | `period_id` (default: from event or last closed) | tell each employee their payslip is ready (once per payslip) |
| `webhook` | `url`, `timeout` | POST rule/event/context JSON; requires `NEXUM_AUTOMATION_WEBHOOKS_ENABLED=true` |
| `log` | `message` | append a rendered message to the run log (handy for testing) |

`GET /api/v1/automations/actions` returns this list with parameter hints; the "New
automation" page shows it in the sidebar.

## Built-in recipes

Installed by `nexum init-db` / `nexum seed` / `nexum automations install-recipes`. They are
upserted by `key`, so an upgrade can add or fix recipes while keeping your enable/disable
choices. `--reset` restores every recipe's definition.

| Key | Trigger | Actions | Saves/run |
|---|---|---|---|
| `open-shift-alert` | `shift.created` (open, not template-generated) | notify managers | 2 min |
| `unassigned-shift-alert` | `shift.unassigned` | notify managers | 2 min |
| `daily-cover` | daily 06:30 | auto-assign next 7 days, open-shift digest | 15 min |
| `weekly-schedule-draft` | Monday 06:00 | draft next week from templates, auto-assign it | 45 min |
| `shift-reminders` | hourly | remind shifts starting within 24 h | 5 min |
| `missing-timesheets` | daily 18:00 | chase missing time entries (3 days) | 10 min |
| `approval-nudge` | daily 09:00 | nudge stale approvals (> 24 h) | 5 min |
| `overtime-watch` | daily 12:00 | flag employees above the weekly threshold | 10 min |
| `timeoff-request-alert` | `timeoff.requested` | notify managers | 2 min |
| `timeoff-approved-notify` / `timeoff-rejected-notify` | `timeoff.approved` / `.rejected` | notify the employee | 2 min |
| `schedule-published-notify` | `schedule.published` | send each employee their shifts | 15 min |
| `payroll-preview` | daily 05:00 | recompute current-period payslip previews | 20 min |
| `monthly-payroll-close` | 1st 06:00 | close last month, notify payslips ready | 120 min |
| `welcome-new-employee` | `employee.created` | welcome the employee, onboarding note to managers | 10 min |

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
- The dashboard's "hours saved" is the sum of `minutes_saved` over runs; only productive
  successful runs count.
- Run history keeps every scheduled, manual and successful/failed event run. Event runs
  whose conditions did not match are not stored.
