"""``nexum`` command line: database setup, demo data, server and automation tools."""

from __future__ import annotations

import random
from datetime import date, time, timedelta
from decimal import Decimal
from typing import Annotated

import typer
from sqlalchemy import select

from nexum import __version__
from nexum.config import get_settings

app = typer.Typer(help="Nexum: company overview + automation.", no_args_is_help=True)
automations_app = typer.Typer(help="Inspect and run automation rules.", no_args_is_help=True)
app.add_typer(automations_app, name="automations")


def _version(value: bool) -> None:
    if value:
        typer.echo(f"nexum {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool, typer.Option("--version", callback=_version, is_eager=True, help="Show version")
    ] = False,
) -> None:
    """Nexum command line."""


@app.command("init-db")
def init_db_cmd(
    drop: Annotated[bool, typer.Option(help="Drop all tables first (destructive)")] = False,
    recipes: Annotated[bool, typer.Option(help="Install built-in automation recipes")] = True,
) -> None:
    """Create the database schema (and the built-in automation recipes)."""
    from nexum.automation.recipes import install_recipes
    from nexum.db import init_db, session_scope

    if drop and not typer.confirm("This deletes ALL data. Continue?", default=False):
        raise typer.Abort()
    init_db(drop=drop)
    typer.echo(f"Schema ready at {get_settings().database_url}")
    if recipes:
        with session_scope() as session:
            installed = install_recipes(session)
        typer.echo(f"Installed {len(installed)} automation recipe(s)")


@app.command("create-admin")
def create_admin(
    email: Annotated[str, typer.Option(prompt=True)],
    name: Annotated[str, typer.Option(prompt=True)],
    password: Annotated[str, typer.Option(prompt=True, hide_input=True, confirmation_prompt=True)],
) -> None:
    """Create an administrator login."""
    from nexum.db import init_db, session_scope
    from nexum.models import Role
    from nexum.services import people

    init_db()
    with session_scope() as session:
        user = people.create_user(
            session, email=email, full_name=name, password=password, role=Role.ADMIN
        )
        typer.echo(f"Admin {user.email} created")


@app.command()
def serve(
    host: Annotated[str, typer.Option()] = "127.0.0.1",
    port: Annotated[int, typer.Option()] = 8000,
    reload: Annotated[bool, typer.Option(help="Auto-reload on code changes")] = False,
) -> None:
    """Run the web application (UI + JSON API) with uvicorn."""
    import uvicorn

    uvicorn.run("nexum.main:app", host=host, port=port, reload=reload)


@app.command()
def seed(
    reset: Annotated[bool, typer.Option(help="Drop and recreate the database first")] = False,
) -> None:
    """Load a demo company (Nexum Demo AB) with logins, schedules, time and payroll data.

    Logins (all passwords ``demo1234``): admin@nexum.local, manager@nexum.local,
    and every employee, e.g. eva.lund@nexum.local.
    """
    from nexum.automation import get_engine
    from nexum.automation.recipes import install_recipes
    from nexum.db import init_db, session_scope
    from nexum.models import (
        EmploymentType,
        PayType,
        Role,
        ShiftStatus,
        TimeEntryStatus,
        TimeOffKind,
        User,
    )
    from nexum.models.types import utcnow
    from nexum.services import payroll, people, scheduling, time_tracking
    from nexum.services.calendar import week_start, week_window
    from nexum.services.events import commit_and_dispatch

    init_db(drop=reset)
    rng = random.Random(42)
    password = "demo1234"
    now = utcnow()
    this_monday = week_start(now.date())

    with session_scope() as session:
        if session.scalar(select(User).where(User.email == "admin@nexum.local")) is not None:
            typer.echo(
                "Demo data already present (admin@nexum.local exists). Use --reset to reload."
            )
            raise typer.Exit(code=1)
        install_recipes(session)
        engine = get_engine()
        engine.subscribe()

        admin = people.create_user(
            session,
            email="admin@nexum.local",
            full_name="Alex Admin",
            password=password,
            role=Role.ADMIN,
        )
        ops = people.create_department(session, "Operations", "OPS-100", actor=admin)
        support = people.create_department(session, "Support", "SUP-200", actor=admin)
        warehouse = people.create_department(session, "Warehouse", "WH-300", actor=admin)

        manager = people.create_employee(
            session,
            first_name="Maria",
            last_name="Nilsson",
            email="manager@nexum.local",
            start_date=date(2023, 3, 1),
            department_id=ops.id,
            title="Operations manager",
            monthly_salary=Decimal("52000"),
            login_password=password,
            role=Role.MANAGER,
            actor=admin,
        )
        ops.manager_id = manager.id

        hourly, monthly = PayType.HOURLY, PayType.MONTHLY
        full, part, casual = (
            EmploymentType.FULL_TIME,
            EmploymentType.PART_TIME,
            EmploymentType.HOURLY,
        )
        roster = [
            ("Eva", "Lund", ops, hourly, "215", "0", casual, "40"),
            ("Max", "Berg", ops, monthly, "0", "36500", full, "40"),
            ("Sara", "Ek", ops, monthly, "0", "34000", full, "40"),
            ("Omar", "Haddad", support, hourly, "190", "0", part, "30"),
            ("Lina", "Svensson", support, monthly, "0", "33000", full, "40"),
            ("Jonas", "Pettersson", support, hourly, "205", "0", casual, "40"),
            ("Amira", "Karim", warehouse, hourly, "180", "0", casual, "40"),
            ("Erik", "Holm", warehouse, monthly, "0", "31000", full, "40"),
            ("Noor", "Ali", warehouse, hourly, "175", "0", part, "24"),
        ]
        employees = []
        for first, last, dept, actual_pay_type, rate, salary, emp_type, weekly in roster:
            employees.append(
                people.create_employee(
                    session,
                    first_name=first,
                    last_name=last,
                    email=f"{first}.{last}@nexum.local".lower(),
                    start_date=date(2024, 1, 15) + timedelta(days=rng.randint(0, 400)),
                    department_id=dept.id,
                    employment_type=emp_type,
                    pay_type=actual_pay_type,
                    hourly_rate=Decimal(rate),
                    monthly_salary=Decimal(salary),
                    weekly_hours=Decimal(weekly),
                    login_password=password,
                    actor=admin,
                )
            )

        for dept, slots in (
            (
                ops,
                [("Day", time(8), time(17), 2, None), ("Evening", time(14), time(22), 1, "Lead")],
            ),
            (
                support,
                [
                    ("Morning", time(7), time(15), 1, "Phones"),
                    ("Day", time(9), time(17), 1, "Chat"),
                ],
            ),
            (
                warehouse,
                [
                    ("Early", time(6), time(14), 2, "Picking"),
                    ("Late", time(13), time(21), 1, "Dispatch"),
                ],
            ),
        ):
            for name, start, end, headcount, role_label in slots:
                for weekday in range(5):
                    scheduling.create_template(
                        session,
                        department=dept,
                        name=name,
                        weekday=weekday,
                        start_time=start,
                        end_time=end,
                        headcount=headcount,
                        role_label=role_label,
                    )

        # Last week (published, worked), this + next week (published), week after (draft)
        for offset in (-7, 0, 7, 14):
            monday = this_monday + timedelta(days=offset)
            scheduling.generate_week_from_templates(session, monday, actor=admin)
            ws, we = week_window(monday)
            scheduling.auto_assign_open_shifts(session, ws, we, actor=admin)
            if offset <= 7:
                scheduling.publish_shifts(
                    session, scheduling.list_shifts(session, ws, we), actor=admin
                )

        # Time entries for shifts that have already ended
        for shift in scheduling.list_shifts(
            session, now - timedelta(days=10), now, statuses=(ShiftStatus.PUBLISHED,)
        ):
            if shift.employee is None or shift.ends_at > now:
                continue
            if rng.random() < 0.15:
                continue  # leave a few missing so the "chase timesheets" recipe has work
            jitter = timedelta(minutes=rng.choice([-10, -5, 0, 5, 15, 40]))
            status = TimeEntryStatus.APPROVED if rng.random() < 0.8 else TimeEntryStatus.SUBMITTED
            time_tracking.record_entry(
                session,
                shift.employee,
                shift.starts_at + timedelta(minutes=rng.choice([-5, 0, 3])),
                shift.ends_at + jitter,
                break_minutes=30,
                shift=shift,
                status=status,
            )

        people.request_time_off(
            session,
            employees[0],
            kind=TimeOffKind.VACATION,
            start_date=this_monday + timedelta(days=21),
            end_date=this_monday + timedelta(days=25),
            reason="Family trip",
        )
        approved = people.request_time_off(
            session,
            employees[4],
            kind=TimeOffKind.UNPAID,
            start_date=this_monday + timedelta(days=9),
            end_date=this_monday + timedelta(days=9),
        )
        people.decide_time_off(session, approved, approve=True, actor=admin)

        period = payroll.current_period(session, now.date())
        payroll.compute_payslips(session, period, actor=admin)
        commit_and_dispatch(session)
        engine.tick(now)

    typer.echo("Demo company loaded.")
    typer.echo("  admin:    admin@nexum.local / demo1234")
    typer.echo("  manager:  manager@nexum.local / demo1234")
    typer.echo("  employee: eva.lund@nexum.local / demo1234 (and the other employees)")
    typer.echo("Run `nexum serve` and open http://127.0.0.1:8000")


@automations_app.command("list")
def automations_list() -> None:
    """List automation rules and their state."""
    from nexum.automation import schedule as schedule_rules
    from nexum.db import init_db, session_scope
    from nexum.models import AutomationRule, TriggerType

    init_db()
    with session_scope() as session:
        rules = session.scalars(select(AutomationRule).order_by(AutomationRule.id)).all()
        if not rules:
            typer.echo("No rules. Run `nexum init-db` to install the built-in recipes.")
            return
        for rule in rules:
            state = "on " if rule.enabled else "off"
            if rule.trigger_type == TriggerType.SCHEDULE:
                trigger = schedule_rules.describe(rule.trigger_config)
            elif rule.trigger_type == TriggerType.EVENT:
                trigger = f"on {rule.event_name}"
            else:
                trigger = "manual"
            nxt = rule.next_run_at.strftime("%Y-%m-%d %H:%M") if rule.next_run_at else "-"
            typer.echo(
                f"[{state}] #{rule.id:<3} {rule.name:<50} {trigger:<32} "
                f"runs={rule.run_count:<4} next={nxt}"
            )


@automations_app.command("run-due")
def automations_run_due() -> None:
    """Run every scheduled rule that is due right now (what the background ticker does)."""
    from nexum.automation import get_engine
    from nexum.db import init_db

    init_db()
    engine = get_engine()
    engine.subscribe()
    runs = engine.tick()
    if not runs:
        typer.echo("Nothing due.")
    for run in runs:
        typer.echo(f"#{run.rule_id} {run.status.value}: {'; '.join(run.log)}")


@automations_app.command("fire")
def automations_fire(rule: Annotated[str, typer.Argument(help="Rule id or key")]) -> None:
    """Run one rule immediately, ignoring its schedule and conditions' trigger."""
    from nexum.automation import get_engine
    from nexum.db import init_db, session_scope
    from nexum.models import AutomationRule

    init_db()
    with session_scope() as session:
        stmt = (
            select(AutomationRule).where(AutomationRule.id == int(rule))
            if rule.isdigit()
            else select(AutomationRule).where(AutomationRule.key == rule)
        )
        found = session.scalar(stmt)
        if found is None:
            typer.echo(f"No rule '{rule}'", err=True)
            raise typer.Exit(code=1)
        rule_id = found.id
    engine = get_engine()
    engine.subscribe()
    run = engine.run_rule_id(rule_id, manual=True)
    typer.echo(f"{run.status.value}: {'; '.join(run.log)}")
    if run.error:
        typer.echo(run.error, err=True)
        raise typer.Exit(code=1)


@automations_app.command("install-recipes")
def automations_install(
    reset: Annotated[bool, typer.Option(help="Reset built-in rules to their defaults")] = False,
) -> None:
    """Install (or reset) the built-in automation recipes."""
    from nexum.automation.recipes import install_recipes
    from nexum.db import init_db, session_scope

    init_db()
    with session_scope() as session:
        installed = install_recipes(session, reset=reset)
    typer.echo(f"{'Reset' if reset else 'Installed'} {len(installed)} recipe(s)")


if __name__ == "__main__":  # pragma: no cover
    app()
