# Changelog

## 0.2.0 - 2026-09-07

Databases created by 0.1.0 have no migration history; recreate them (`nexum init-db --drop`)
or stamp them by hand. From now on `nexum db upgrade` applies schema changes.

### Operate safely
- Company settings (name, time zone, currency, payroll rules, e-mail) with an admin page and `/api/v1/company`.
- Everything that means "a day", "a week" or "08:00" now uses the company time zone; storage stays UTC.
- Alembic migrations shipped in the package: `nexum db upgrade | downgrade | current | check | revision`.
- CSRF tokens on every web form, session rotation on login, per-user session invalidation on password change.
- Request ids and an access log; `/healthz` reports database, automation ticker and time zone.
- Security headers and a Content-Security-Policy; inline JavaScript removed.
- PostgreSQL job in CI; the test suite runs against PostgreSQL with `NEXUM_TEST_DATABASE_URL`.

### Scheduling
- Availability rules (unavailable / preferred windows, overnight aware) respected by auto-assignment, claims and hand-overs.
- Skills on employees, templates and shifts; candidate filtering by skill.
- Shift requests: employees claim open shifts, ask to drop a shift or hand it to a colleague; managers decide; conflict-free claims are auto-approved by a recipe.
- Employee edit page, per-employee iCal feed, print stylesheet.

### Payroll
- Paid time from time entries minus explicit or automatic breaks, rounded per company rules.
- Premium windows (evening/night/weekend) and public holidays; highest multiplier wins where they overlap.
- Bi-weekly pay periods, vacation balance, printable payslips, CSV export per period.

### Automation
- E-mail delivery of notifications (per-user opt-out), `send_email` and `chat_webhook` actions, retries with backoff.
- Rule editor with structured trigger fields, duplicate rules, dry runs that roll everything back.
- Effectiveness report per rule (`/automations/report`).
- New recipes: auto-approve claims, drop/hand-over routing, no-show detection, forgotten clock-outs, understaffing forecast, ending contracts (29 built-in recipes in total).

### Security and onboarding
- Forgot-password e-mails, admin-generated reset links, password change page, login rate limiting.
- GDPR: export a person's data as JSON; anonymise a person while keeping payroll history.
- First-run wizard at `/setup`.

## 0.1.0 - 2026-09-06

Foundation: domain model, services, automation engine with 15 recipes, JSON API, web UI, CLI,
tests, CI, Docker and the product plan.
