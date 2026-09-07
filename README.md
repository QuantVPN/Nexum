# Nexum

All-in-one company overview platform: administrators get the right numbers, employees get
their schedules and pay, and an automation engine does the coordination work in between.

- **Overview** — headcount, scheduled hours, open shifts, projected payroll, approvals backlog, and how many hours the automations saved.
- **Scheduling** — weekly templates, draft → publish flow, open shifts, auto-assignment that respects clashes, time off, availability, skills and weekly hours; employees claim open shifts, drop or hand over their own.
- **Time & attendance** — clock in/out, manual entries, manager approval, automatic chasing of missing entries, no-show and forgotten clock-out detection.
- **Payroll** — monthly or bi-weekly periods, weekly overtime, evening/night/weekend/holiday premiums, rounding and break rules, unpaid-leave deductions, vacation balance, printable payslips, CSV export.
- **Employee self-service** — my shifts (with an iCal feed), my pay, my time, my requests, my availability, notifications by e-mail if wanted.
- **Automation** — rules made of *trigger → conditions → actions*, 29 built-in recipes, a rule editor with dry runs, e-mail and Slack/Teams delivery, run history and an effectiveness report.
- **Operations** — company settings (time zone, currency, payroll rules, SMTP), migrations, CSRF and security headers, login rate limiting, password reset, GDPR export and anonymisation, first-run setup wizard.

Everything is served by one Python process (FastAPI) with a server-rendered UI, a JSON API
(`/api/docs`), and a CLI.

| Admin dashboard | Schedule grid |
|---|---|
| ![Dashboard](docs/screenshots/dashboard.png) | ![Schedule](docs/screenshots/schedule.png) |

| Automations | Employee page |
|---|---|
| ![Automations](docs/screenshots/automations.png) | ![My page](docs/screenshots/me.png) |

| Automation report | My shifts (claims and hand-overs) |
|---|---|
| ![Automation report](docs/screenshots/automation-report.png) | ![My shifts](docs/screenshots/me-shifts.png) |

More in [`docs/screenshots`](docs/screenshots): settings, payroll period, approvals, pay page, availability.

## Quick start

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --all-extras --dev      # install
uv run nexum seed               # demo company "Nexum Demo AB" + built-in automations
uv run nexum serve              # http://127.0.0.1:8000
```

Demo logins (password `demo1234`):

| Role | Email | Lands on |
|---|---|---|
| admin | `admin@nexum.local` | `/dashboard` |
| manager | `manager@nexum.local` | `/dashboard` |
| employee | `eva.lund@nexum.local` (and the other employees) | `/me` |

Start from scratch instead of the demo:

```bash
uv run nexum db upgrade                    # schema (Alembic migrations)
uv run nexum serve                         # open http://127.0.0.1:8000 -> the setup wizard
```

`nexum init-db` + `nexum create-admin` do the same from the command line.

## CLI

```
nexum init-db [--drop]                   create the schema and install recipes
nexum seed [--reset]                     load the demo company
nexum serve [--host] [--port] [--reload]  run the app with uvicorn
nexum create-admin                       create an administrator login
nexum automations list                   rules, triggers, run counts, next run
nexum automations run-due                run every scheduled rule that is due (cron-friendly)
nexum automations fire <id|key>          run one rule now
nexum automations install-recipes [--reset]
nexum db upgrade [REV]                   apply migrations (run on every deploy)
nexum db current | check | downgrade REV | revision -m "msg"
```

## Configuration

Environment variables (prefix `NEXUM_`) or a `.env` file; see [`.env.example`](.env.example).

| Variable | Default | Meaning |
|---|---|---|
| `NEXUM_DATABASE_URL` | `sqlite:///./nexum.db` | SQLAlchemy URL; `postgresql+psycopg://...` for PostgreSQL (`uv sync --extra postgres`) |
| `NEXUM_SECRET_KEY` | change-me | signs session cookies; **set this in production** |
| `NEXUM_ENVIRONMENT` | `development` | `production` enables secure cookies |
| `NEXUM_AUTOMATION_ENABLED` | `true` | background ticker for scheduled rules |
| `NEXUM_AUTOMATION_TICK_SECONDS` | `30` | ticker interval |
| `NEXUM_AUTOMATION_WEBHOOKS_ENABLED` | `false` | allow the `webhook` / `chat_webhook` actions to call out |
| `NEXUM_AUTOMATION_RETRY_ATTEMPTS` | `3` | attempts for webhook/chat/e-mail actions |
| `NEXUM_AUTOMATION_RETRY_BACKOFF_SECONDS` | `0.5` | linear backoff between attempts |
| `NEXUM_COMPANY_NAME`, `NEXUM_TIMEZONE`, `NEXUM_DEFAULT_CURRENCY`, `NEXUM_WEEKLY_OVERTIME_THRESHOLD_HOURS`, `NEXUM_OVERTIME_MULTIPLIER` | | defaults for the company settings row on first start; edit them on `/settings` afterwards |

## Docker

```bash
docker compose up --build        # app + PostgreSQL on http://localhost:8000
docker compose exec app nexum seed
```

## Development

```bash
uv run ruff check src tests && uv run ruff format --check src tests
uv run mypy
uv run pytest --cov=nexum
NEXUM_TEST_DATABASE_URL=postgresql+psycopg://nexum:nexum@localhost:5432/nexum_test uv run pytest  # optional
```

Changed a model? Generate a migration and commit it: `uv run nexum db revision -m "add x"`.

Project layout and design notes: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).
Automation rule language, actions and recipes: [`docs/AUTOMATION.md`](docs/AUTOMATION.md).
Product plan, roadmap and backlog to v1.0: [`docs/PLAN.md`](docs/PLAN.md).

## Status

Version 0.2.0 covers milestones M0 to M6 of the plan except a few items listed there
(rule builder without JSON, Slack/Teams beyond incoming webhooks, OIDC, onboarding docs
site). It runs on SQLite or PostgreSQL with migrations, and the test suite (about 140
tests, ~95% line coverage) runs against both. See [`CHANGELOG.md`](CHANGELOG.md).

## License

MIT
